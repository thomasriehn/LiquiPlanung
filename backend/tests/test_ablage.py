"""Belegablage: Pfadsicherheit, lokale Ablage, SharePoint (gemockt), Zwischenablage,
Upload-Prüfung und Datei-Header."""

import io
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import httpx
import pytest

from app import models
from app.config import get_settings
from app.services import ablage, geheimnisse, sharepoint, systemeinstellungen
from app.services.ablage import AblageFehler
from app.services.zugaenge import DateiRef

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


@pytest.fixture(autouse=True)
def stammordner(tmp_path, monkeypatch):
    s = get_settings()
    basis = tmp_path / "ablage"
    basis.mkdir()
    monkeypatch.setattr(s, "ablage_verzeichnis", str(basis))
    monkeypatch.setattr(s, "graph_client_secret", None)
    monkeypatch.setattr(s, "geheimnis_schluessel", None)
    geheimnisse.cache_leeren()
    sharepoint.token_cache_leeren()
    yield basis.resolve()
    sharepoint.token_cache_leeren()


def _mandant(db, kurzname="nordlicht", **kw):
    m = models.Mandant(name=f"{kurzname} GmbH", kurzname=kurzname, **kw)
    db.add(m)
    db.flush()
    return m


def _fehler(status, func, *args, **kw):
    with pytest.raises(AblageFehler) as e:
        func(*args, **kw)
    assert e.value.status == status, str(e.value)
    return e.value


# --- Pfadsicherheit -----------------------------------------------------------------


@pytest.mark.parametrize("boese", [
    "../x", "..", "a/../../x", "a/../b", "a\\..\\..\\x", "/etc", "/", "//server/share", "C:/Windows",
    "c:\\x", "~/x", "a/\x00b", ".zwischenablage", "a/.git", ".versteckt/x",
])
def test_sicherer_pfad_verbietet(boese):
    _fehler(422, ablage.sicherer_pfad, boese)


def test_sicherer_pfad_erlaubt(stammordner):
    assert ablage.sicherer_pfad("") == stammordner
    assert ablage.sicherer_pfad("a/b") == stammordner / "a" / "b"
    assert ablage.sicherer_pfad("a/./b/") == stammordner / "a" / "b"
    assert ablage.sicherer_pfad("a\\b") == stammordner / "a" / "b"
    assert stammordner.is_dir()  # stamm() legt an


def test_sicherer_pfad_symlink_ausbruch(stammordner, tmp_path):
    draussen = tmp_path / "draussen"
    draussen.mkdir()
    (draussen / "geheim.txt").write_text("x")
    os.symlink(draussen, stammordner / "link")
    _fehler(422, ablage.sicherer_pfad, "link")
    _fehler(422, ablage.sicherer_pfad, "link/geheim.txt")
    # Symlink innerhalb der Ablage auf versteckten Ordner
    (stammordner / ".zwischenablage").mkdir()
    os.symlink(stammordner / ".zwischenablage", stammordner / "zw")
    _fehler(422, ablage.sicherer_pfad, "zw")
    # Symlink innerhalb der Ablage ist erlaubt
    (stammordner / "echt").mkdir()
    os.symlink(stammordner / "echt", stammordner / "kurz")
    assert ablage.sicherer_pfad("kurz") == stammordner / "echt"


def test_sicherer_dateiname():
    assert ablage.sicherer_dateiname("Bestellung Müller.PDF") == "Bestellung Müller.pdf"
    assert ablage.sicherer_dateiname('a<b>c:d"e|f?g*h#i%j.pdf') == "a_b_c_d_e_f_g_h_i_j.pdf"
    assert ablage.sicherer_dateiname("C:\\fakepath\\..\\angebot.pdf") == "angebot.pdf"
    assert ablage.sicherer_dateiname("../../etc/passwd") == "passwd"
    assert ablage.sicherer_dateiname("x.exe", ".pdf") == "x.pdf"
    assert ablage.sicherer_dateiname("Auftrag 4711.2026", ".pdf") == "Auftrag 4711.2026.pdf"
    assert ablage.sicherer_dateiname("...", ".pdf") == "Datei.pdf"
    assert ablage.sicherer_dateiname("") == "Datei"
    assert ablage.sicherer_dateiname("CON.txt") == "_CON.txt"
    assert ablage.sicherer_dateiname(".htaccess") == "htaccess"
    assert ablage.sicherer_dateiname("a\x00b\nc.pdf") == "a_b_c.pdf"
    lang = ablage.sicherer_dateiname("x" * 500 + ".pdf")
    assert lang == "x" * 120 + ".pdf"
    assert ablage.sicherer_dateiname("Rechnung", "PDF") == "Rechnung.pdf"


def test_standard_lokal_pfad_und_mandant_ablage(db):
    m = _mandant(db)
    assert ablage.standard_lokal_pfad(m) == f"nordlicht-{m.id}"
    ab = ablage.mandant_ablage(m)
    assert ab["typ"] == "LOKAL" and ab["lokal_pfad"] == f"nordlicht-{m.id}" and ab["konfiguriert"] is False
    assert ab["beschreibung"] == f"lokal: nordlicht-{m.id}"
    m2 = _mandant(db, "../böse:x")
    assert ablage.standard_lokal_pfad(m2) == f"böse_x-{m2.id}"
    m3 = _mandant(db, "..")
    assert ablage.standard_lokal_pfad(m3) == f"mandant-{m3.id}"

    m.ablage_typ, m.ablage_pfad = "LOKAL", "Kanzlei/Nordlicht"
    ab = ablage.mandant_ablage(m)
    assert ab["lokal_pfad"] == "Kanzlei/Nordlicht" and ab["konfiguriert"] is True

    m.ablage_typ, m.ablage_pfad = "SHAREPOINT", "Akte › Dokumente › /Belege"
    m.ablage_site_id, m.ablage_drive_id, m.ablage_ordner_id = "h,1,2", "b!d", "O1"
    ab = ablage.mandant_ablage(m)
    assert ab["typ"] == "SHAREPOINT" and ab["lokal_pfad"] is None
    assert ab["drive_id"] == "b!d" and ab["ordner_id"] == "O1" and ab["konfiguriert"] is True
    assert ab["beschreibung"] == "SharePoint: Akte › Dokumente › /Belege"


# --- lokaler Ordnerdialog ----------------------------------------------------------------


def test_lokal_ordner(stammordner, tmp_path):
    (stammordner / "nordlicht" / "2026").mkdir(parents=True)
    (stammordner / "nordlicht" / "a.pdf").write_bytes(PDF)
    (stammordner / "Bravo").mkdir()
    (stammordner / ".zwischenablage").mkdir()
    (stammordner / ".versteckt.txt").write_text("x")
    (tmp_path / "draussen").mkdir()
    os.symlink(tmp_path / "draussen", stammordner / "link")
    erg = ablage.lokal_ordner("")
    assert erg["pfad"] == ""
    assert [o["name"] for o in erg["unterordner"]] == ["Bravo", "nordlicht"]
    assert erg["unterordner"][1] == {"name": "nordlicht", "pfad": "nordlicht", "elemente": 2}
    assert erg["dateien"] == []
    erg = ablage.lokal_ordner("nordlicht")
    assert erg["unterordner"] == [{"name": "2026", "pfad": "nordlicht/2026", "elemente": 0}]
    assert erg["dateien"][0]["name"] == "a.pdf" and erg["dateien"][0]["groesse"] == len(PDF)
    assert erg["dateien"][0]["pfad"] == "nordlicht/a.pdf"
    _fehler(404, ablage.lokal_ordner, "gibtsnicht")
    _fehler(422, ablage.lokal_ordner, "../")
    _fehler(422, ablage.lokal_ordner, "link")


def test_lokal_ordner_anlegen(stammordner):
    assert ablage.lokal_ordner_anlegen("", "Kanzlei") == "Kanzlei"
    assert ablage.lokal_ordner_anlegen("Kanzlei", "Nord/Süd") == "Kanzlei/Nord_Süd"
    assert (stammordner / "Kanzlei" / "Nord_Süd").is_dir()
    assert ablage.lokal_ordner_anlegen("Kanzlei", "../../ausbruch") == "Kanzlei/ausbruch"
    assert not (stammordner.parent / "ausbruch").exists()
    _fehler(409, ablage.lokal_ordner_anlegen, "", "Kanzlei")
    _fehler(422, ablage.lokal_ordner_anlegen, "", " .. ")
    _fehler(404, ablage.lokal_ordner_anlegen, "fehlt", "x")
    _fehler(422, ablage.lokal_ordner_anlegen, "/tmp", "x")


# --- lokal speichern / lesen ----------------------------------------------------------------


def test_speichern_lokal_ohne_ueberschreiben(db, stammordner):
    m = _mandant(db)
    w = ablage.standard_lokal_pfad(m)
    r1 = ablage.speichern(db, m, "2026-10-30_Müller_Angebot.pdf", PDF, "application/pdf")
    assert r1 == DateiRef(ablage="LOKAL", name="2026-10-30_Müller_Angebot.pdf", mime="application/pdf",
                          groesse=len(PDF), pfad=f"{w}/2026-10-30_Müller_Angebot.pdf")
    r2 = ablage.speichern(db, m, "2026-10-30_Müller_Angebot.pdf", b"%PDF-zwei", "application/pdf")
    r3 = ablage.speichern(db, m, "2026-10-30_Müller_Angebot.pdf", b"%PDF-drei", "application/pdf")
    assert r2.name == "2026-10-30_Müller_Angebot (1).pdf"
    assert r3.pfad == f"{w}/2026-10-30_Müller_Angebot (2).pdf"
    assert ablage.lesen(db, r1) == PDF
    assert ablage.lesen(db, r2) == b"%PDF-zwei"
    assert ablage.lokal_dateipfad(r3) == stammordner / w / "2026-10-30_Müller_Angebot (2).pdf"
    # unsicherer Name wird bereinigt und landet im Mandantenordner
    r4 = ablage.speichern(db, m, "../../../etc/passwd", b"x", "text/plain")
    assert r4.pfad == f"{w}/passwd"


def test_speichern_lokal_gewaehlter_ordner(db, stammordner):
    m = _mandant(db, ablage_typ="LOKAL", ablage_pfad="Kanzlei/Nord")
    ref = ablage.speichern(db, m, "a.pdf", PDF, "application/pdf")
    assert ref.pfad == "Kanzlei/Nord/a.pdf"
    assert (stammordner / "Kanzlei" / "Nord" / "a.pdf").read_bytes() == PDF


def test_speichern_lokal_symlink_als_ziel_wird_nicht_ueberschrieben(db, stammordner, tmp_path):
    m = _mandant(db)
    (stammordner / ablage.standard_lokal_pfad(m)).mkdir()
    opfer = tmp_path / "opfer.txt"
    opfer.write_text("unverändert")
    os.symlink(opfer, stammordner / ablage.standard_lokal_pfad(m) / "a.pdf")
    ref = ablage.speichern(db, m, "a.pdf", PDF, "application/pdf")
    assert ref.name == "a (1).pdf"
    assert opfer.read_text() == "unverändert"


def test_lesen_fehlerfaelle(db, stammordner):
    _fehler(404, ablage.lesen, db, DateiRef(ablage="LOKAL", name="x", mime="x", groesse=0, pfad="nordlicht/fehlt.pdf"))
    _fehler(422, ablage.lesen, db, DateiRef(ablage="LOKAL", name="x", mime="x", groesse=0, pfad="../../etc/passwd"))
    _fehler(422, ablage.lesen, db, DateiRef(ablage="LOKAL", name="x", mime="x", groesse=0, pfad="/etc/passwd"))
    _fehler(404, ablage.lesen, db, DateiRef(ablage="LOKAL", name="x", mime="x", groesse=0, pfad=None))
    _fehler(404, ablage.lesen, db, DateiRef(ablage="", name="x", mime="x", groesse=0))


def test_ref_aus_bestellung():
    b = models.Bestellung(datei_ablage="LOKAL", datei_name="a.pdf", datei_mime="application/pdf",
                          datei_groesse=3, datei_pfad="n/a.pdf")
    assert ablage.ref_aus_bestellung(b) == DateiRef("LOKAL", "a.pdf", "application/pdf", 3, "n/a.pdf")
    assert ablage.ref_aus_bestellung(models.Bestellung()) is None


# --- Mandantenordner lokal durchsuchen ------------------------------------------------------


def test_mandant_durchsuchen_lokal(db, stammordner, tmp_path):
    m = _mandant(db)
    andere = _mandant(db, "andere")
    w, wa = ablage.standard_lokal_pfad(m), ablage.standard_lokal_pfad(andere)
    (stammordner / wa).mkdir()
    (stammordner / wa / "fremd.pdf").write_bytes(PDF)
    erg = ablage.mandant_durchsuchen(db, m)  # legt den Standardordner an
    assert erg == {"typ": "LOKAL", "wurzel_name": w, "element": "",
                   "pfad_teile": [{"element": "", "name": w}], "unterordner": [], "dateien": []}
    (stammordner / w / "2026" / "Q4").mkdir(parents=True)
    (stammordner / w / "2026" / "Q4" / "b.pdf").write_bytes(PDF)
    os.symlink(stammordner / wa, stammordner / w / "zu_andere")
    os.symlink(stammordner / wa / "fremd.pdf", stammordner / w / "fremd.pdf")

    erg = ablage.mandant_durchsuchen(db, m, "")
    assert [o["element"] for o in erg["unterordner"]] == ["2026"]  # Symlinks nicht gelistet
    assert erg["dateien"] == []
    erg = ablage.mandant_durchsuchen(db, m, "2026/Q4")
    assert erg["element"] == "2026/Q4"
    assert erg["pfad_teile"] == [{"element": "", "name": w}, {"element": "2026", "name": "2026"},
                                 {"element": "2026/Q4", "name": "Q4"}]
    assert erg["dateien"][0]["element"] == "2026/Q4/b.pdf"
    assert erg["dateien"][0]["mime"] == "application/pdf"

    _fehler(422, ablage.mandant_durchsuchen, db, m, "../andere")
    _fehler(422, ablage.mandant_durchsuchen, db, m, "/etc")
    _fehler(403, ablage.mandant_durchsuchen, db, m, "zu_andere")
    _fehler(404, ablage.mandant_durchsuchen, db, m, "2026/Q4/b.pdf")  # Datei ist kein Ordner

    ref, daten = ablage.mandant_datei(db, m, "2026/Q4/b.pdf")
    assert daten == PDF
    assert ref == DateiRef("LOKAL", "b.pdf", "application/pdf", len(PDF), f"{w}/2026/Q4/b.pdf")
    _fehler(403, ablage.mandant_datei, db, m, "fremd.pdf")  # Symlink in fremden Mandantenordner
    _fehler(422, ablage.mandant_datei, db, m, "../andere/fremd.pdf")
    _fehler(404, ablage.mandant_datei, db, m, "2026")
    _fehler(422, ablage.mandant_datei, db, m, "")
    assert ablage.mandant_datei(db, andere, "fremd.pdf")[1] == PDF


def test_mandant_datei_lokal_typ_und_groesse(db, stammordner, monkeypatch):
    m = _mandant(db)
    ordner = stammordner / ablage.standard_lokal_pfad(m)
    ordner.mkdir()
    (ordner / "seite.html").write_bytes(b"<html><script>alert(1)</script></html>")
    _fehler(422, ablage.mandant_datei, db, m, "seite.html")
    monkeypatch.setattr(get_settings(), "ablage_max_mb", 1)
    (ordner / "gross.pdf").write_bytes(b"%PDF-" + b"0" * (1024 * 1024))
    _fehler(413, ablage.mandant_datei, db, m, "gross.pdf")


# --- Mandantentrennung lokal ----------------------------------------------------------------


def test_lokal_ueberschneidung(db, stammordner):
    m = _mandant(db)
    anderer = _mandant(db, "andere", ablage_typ="LOKAL", ablage_pfad="Kanzlei/Nord")
    for rel in ("Kanzlei/Nord", "kanzlei", "KANZLEI/nord/2026", ablage.standard_lokal_pfad(anderer)):
        e = _fehler(409, ablage.lokal_ueberschneidung, db, m, rel)
        assert "andere GmbH" in e.nachricht
    ablage.lokal_ueberschneidung(db, m, "Kanzlei/Nordlicht")   # nur gleicher Namensanfang
    ablage.lokal_ueberschneidung(db, m, ablage.standard_lokal_pfad(m))
    # ohne Details (Benutzer): kein fremder Name
    e = _fehler(409, ablage.lokal_ueberschneidung, db, m, "Kanzlei", details=False)
    assert "andere GmbH" not in e.nachricht and "Kanzlei/Nord" not in e.nachricht
    # Symlink auf den fremden Ordner zählt wie der Ordner selbst
    (stammordner / "Kanzlei" / "Nord").mkdir(parents=True)
    os.symlink(stammordner / "Kanzlei" / "Nord", stammordner / "kurz")
    _fehler(409, ablage.lokal_ueberschneidung, db, m, "kurz")


def test_lokal_wurzel_ueberschneidung_vor_zugriff(db, stammordner):
    _mandant(db, "tochter", ablage_typ="LOKAL", ablage_pfad="holding/tochter")
    m = _mandant(db, "holding", ablage_typ="LOKAL", ablage_pfad="holding")
    (stammordner / "holding" / "tochter").mkdir(parents=True)
    (stammordner / "holding" / "tochter" / "a.pdf").write_bytes(PDF)
    _fehler(409, ablage.mandant_durchsuchen, db, m, "tochter")
    _fehler(409, ablage.mandant_datei, db, m, "tochter/a.pdf")
    _fehler(409, ablage.speichern, db, m, "b.pdf", PDF, "application/pdf")
    assert not (stammordner / "holding" / "b.pdf").exists()


def test_mandant_ordner_archivieren(db, stammordner, monkeypatch):
    monkeypatch.setattr(ablage, "_jetzt", lambda: datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc))
    m = _mandant(db, ablage_typ="LOKAL", ablage_pfad="Kanzlei/Nord")
    _mandant(db, "andere", ablage_typ="LOKAL", ablage_pfad="Kanzlei/Andere")
    standard = stammordner / ablage.standard_lokal_pfad(m)
    standard.mkdir()
    (standard / "alt.pdf").write_bytes(PDF)
    (stammordner / "Kanzlei" / "Nord").mkdir(parents=True)
    (stammordner / "Kanzlei" / "Nord" / "a.pdf").write_bytes(PDF)
    (stammordner / "Kanzlei" / "Andere").mkdir()

    plan = ablage.mandant_ordner_plan(db, m)
    assert plan == (f"nordlicht-{m.id}", [standard, stammordner / "Kanzlei" / "Nord"])
    verschoben = ablage.mandant_ordner_archivieren(plan)
    archiv = stammordner / ".geloescht"
    assert [neu for _, neu in verschoben] == [archiv / f"nordlicht-{m.id}-20261031T120000Z",
                                             archiv / f"nordlicht-{m.id}-20261031T120000Z-2"]
    assert (archiv / f"nordlicht-{m.id}-20261031T120000Z" / "alt.pdf").read_bytes() == PDF
    assert not standard.exists() and not (stammordner / "Kanzlei" / "Nord").exists()
    assert (stammordner / "Kanzlei" / "Andere").is_dir()        # fremder Ordner bleibt
    # Löschen fehlgeschlagen → zurück
    ablage.archivierung_zuruecknehmen(verschoben)
    assert (standard / "alt.pdf").exists() and (stammordner / "Kanzlei" / "Nord" / "a.pdf").exists()


def test_mandant_ordner_plan_ueberspringt_fremde_ordner(db, stammordner):
    # Altbestand: Ordner überschneidet sich mit einem anderen Mandanten → bleibt stehen
    _mandant(db, "tochter", ablage_typ="LOKAL", ablage_pfad="holding/tochter")
    m = _mandant(db, "holding", ablage_typ="LOKAL", ablage_pfad="holding")
    (stammordner / "holding" / "tochter").mkdir(parents=True)
    assert ablage.mandant_ordner_plan(db, m) == (f"holding-{m.id}", [])
    assert ablage.mandant_ordner_archivieren(ablage.mandant_ordner_plan(db, m)) == []


def test_standardordner_pruefen(db, stammordner):
    m = _mandant(db)
    ablage.standardordner_pruefen(db, m)
    (stammordner / ablage.standard_lokal_pfad(m)).mkdir()
    ablage.standardordner_pruefen(db, m)                      # leer ist in Ordnung
    (stammordner / ablage.standard_lokal_pfad(m) / "x.pdf").write_bytes(PDF)
    _fehler(409, ablage.standardordner_pruefen, db, m)


# --- SharePoint (gemockt) -----------------------------------------------------------------


GRAPH = {"graph_tenant_id": "contoso.onmicrosoft.com", "graph_client_id": "c1", "graph_client_secret": "s1"}
SIGNIERT = "https://contoso.sharepoint.com/dl/"


def _ordner(id_, name, eltern_id, eltern_pfad):
    return {"id": id_, "name": name, "folder": {"childCount": 1},
            "parentReference": {"driveId": "b!d", "id": eltern_id, "path": f"/drives/b!d/root:{eltern_pfad}"}}


def _datei(id_, name, eltern_id, eltern_pfad, groesse=10):
    return {"id": id_, "name": name, "file": {"mimeType": "application/pdf"}, "size": groesse,
            "lastModifiedDateTime": "2026-10-01T08:00:00Z",
            "parentReference": {"driveId": "b!d", "id": eltern_id, "path": f"/drives/b!d/root:{eltern_pfad}"}}


class SpBaum:
    """Bibliothek b!d:  / → Mandanten → {Nordlicht → 2026 → b.pdf, Nordlicht Alt, Andere → fremd.pdf}"""

    def __init__(self):
        self.anfragen: list[httpx.Request] = []
        self.inhalt = {"F_B": PDF, "F_FREMD": b"%PDF-fremd"}
        self.hochgeladen: list[tuple[str, bytes]] = []
        self.items = {
            "ROOT": {"id": "ROOT", "name": "root", "root": {}, "folder": {"childCount": 1},
                     "parentReference": {"driveId": "b!d"}},
            "O_MAND": _ordner("O_MAND", "Mandanten", "ROOT", ""),
            "O_M": _ordner("O_M", "Nordlicht", "O_MAND", "/Mandanten"),
            "O_26": _ordner("O_26", "2026", "O_M", "/Mandanten/Nordlicht"),
            "F_B": _datei("F_B", "b.pdf", "O_26", "/Mandanten/Nordlicht/2026", len(PDF)),
            "O_ALT": _ordner("O_ALT", "Nordlicht Alt", "O_MAND", "/Mandanten"),
            "O_X": _ordner("O_X", "Andere", "O_MAND", "/Mandanten"),
            "F_FREMD": _datei("F_FREMD", "fremd.pdf", "O_X", "/Mandanten/Andere"),
            # behauptet, unter Nordlicht zu liegen, hängt aber direkt an der Wurzel
            "O_LUEGE": _ordner("O_LUEGE", "x", "ROOT", "/Mandanten/Nordlicht"),
            "F_GROSS": _datei("F_GROSS", "gross.pdf", "O_M", "/Mandanten/Nordlicht", 50 * 1024 * 1024),
        }

    def kinder(self, eltern):
        return [i for i in self.items.values() if (i.get("parentReference") or {}).get("id") == eltern]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.anfragen.append(req)
        if req.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if str(req.url).startswith(SIGNIERT):
            assert "authorization" not in req.headers
            return httpx.Response(200, content=self.inhalt[req.url.path.rsplit("/", 1)[-1]])
        assert req.headers["authorization"] == "Bearer tok"
        teile = unquote(req.url.path).removeprefix("/v1.0/drives/b!d/").split("/")
        if teile == ["root"]:
            return httpx.Response(200, json=self.items["ROOT"])
        if teile[0] == "root" and teile[1:] == ["children"]:
            return httpx.Response(200, json={"value": self.kinder("ROOT")})
        if req.url.path == "/v1.0/drives/b!d":
            return httpx.Response(200, json={"id": "b!d", "name": "Dokumente"})
        if teile[0] == "items" and len(teile) >= 2:
            item_id, rest = teile[1], teile[2:]
            if ":" in item_id and req.method == "PUT":  # O_M:/name:/content
                ordner_id = item_id.split(":")[0]
                name = teile[2].rstrip(":")
                self.hochgeladen.append((name, req.content))
                return httpx.Response(201, json={
                    "id": "NEU", "name": name, "size": len(req.content), "webUrl": f"https://h/{name}",
                    "parentReference": self.items[ordner_id]["parentReference"] | {
                        "path": f"/drives/b!d/root:{ablage_pfad(self, ordner_id)}"}})
            item = self.items.get(item_id)
            if item is None:
                return httpx.Response(404, json={"error": {"code": "itemNotFound"}})
            if rest == []:
                return httpx.Response(200, json=item)
            if rest == ["children"]:
                return httpx.Response(200, json={"value": self.kinder(item_id)})
            if rest == ["content"]:
                return httpx.Response(302, headers={"location": SIGNIERT + item_id})
        return httpx.Response(404, json={"error": {"code": "itemNotFound"}})


def ablage_pfad(baum, item_id):
    item = baum.items[item_id]
    return sharepoint._pfad(item)


@pytest.fixture()
def sp(db, monkeypatch):
    baum = SpBaum()
    monkeypatch.setattr(sharepoint, "http_client_fabrik", lambda: httpx.Client(transport=httpx.MockTransport(baum)))
    systemeinstellungen.setze(db, None, GRAPH)
    return baum


def _sp_mandant(db, ordner_id="O_M"):
    return _mandant(db, ablage_typ="SHAREPOINT", ablage_pfad="Akte › Dokumente › /Mandanten/Nordlicht",
                    ablage_site_id="h,1,2", ablage_drive_id="b!d", ablage_ordner_id=ordner_id)


def test_sharepoint_ueberschneidung(db, sp):
    _sp_mandant(db, "O_M")                                   # /Mandanten/Nordlicht
    neu = _mandant(db, "neu")
    gc = sharepoint.GraphClient(systemeinstellungen.graph_zugang(db))

    def pruefen(item_id, drive="b!d"):
        e = gc.element(drive, item_id)
        ablage.sharepoint_ueberschneidung(db, gc, neu, drive, e)

    for item_id in ("O_M", "O_26", "O_MAND", None):          # gleich, darunter, darüber, Wurzel
        with pytest.raises(AblageFehler) as e:
            pruefen(item_id)
        assert e.value.status == 409 and "nordlicht GmbH" in e.value.nachricht
    pruefen("O_ALT")                                          # „Nordlicht Alt“ liegt daneben
    pruefen("O_X")
    # Pfad nicht ermittelbar → vorsichtshalber ablehnen
    sp.items["O_OHNE"] = {"id": "O_OHNE", "name": "ohne", "folder": {},
                          "parentReference": {"driveId": "b!d", "id": "ROOT"}}
    with pytest.raises(AblageFehler):
        pruefen("O_OHNE")
    # Ordner des anderen gibt es nicht mehr → kein Konflikt
    _mandant(db, "weg", ablage_typ="SHAREPOINT", ablage_drive_id="b!d", ablage_ordner_id="WEG")
    pruefen("O_X")
    # anderer Mandant nutzt die Bibliothekswurzel → alles in der Bibliothek überschneidet sich
    _mandant(db, "wurzel", ablage_typ="SHAREPOINT", ablage_drive_id="b!d", ablage_ordner_id=None)
    with pytest.raises(AblageFehler):
        pruefen("O_X")


def test_speichern_sharepoint_ohne_graph(db):
    m = _sp_mandant(db)
    e = _fehler(409, ablage.speichern, db, m, "a.pdf", PDF, "application/pdf")
    assert "Systemeinstellungen" in str(e)
    _fehler(409, ablage.mandant_durchsuchen, db, m)
    _fehler(409, ablage.lesen, db, DateiRef("SHAREPOINT", "a.pdf", "application/pdf", 1, drive_id="b!d", item_id="F_B"))


def test_speichern_und_lesen_sharepoint(db, sp):
    m = _sp_mandant(db)
    ref = ablage.speichern(db, m, "2026-10-30_Müller.pdf", PDF, "application/pdf")
    assert ref == DateiRef(ablage="SHAREPOINT", name="2026-10-30_Müller.pdf", mime="application/pdf",
                           groesse=len(PDF), pfad="/Mandanten/Nordlicht/2026-10-30_Müller.pdf", drive_id="b!d",
                           item_id="NEU", web_url="https://h/2026-10-30_Müller.pdf")
    assert sp.hochgeladen == [("2026-10-30_Müller.pdf", PDF)]
    ref_b = DateiRef("SHAREPOINT", "b.pdf", "application/pdf", len(PDF), drive_id="b!d", item_id="F_B")
    assert ablage.lesen(db, ref_b) == PDF
    _fehler(404, ablage.lesen, db, DateiRef("SHAREPOINT", "x", "x", 0, drive_id="b!d", item_id="WEG"))
    _fehler(404, ablage.lesen, db, DateiRef("SHAREPOINT", "x", "x", 0, drive_id="b!d", item_id=None))


def test_speichern_sharepoint_ohne_bibliothek(db, sp):
    m = _mandant(db, ablage_typ="SHAREPOINT")
    _fehler(409, ablage.speichern, db, m, "a.pdf", PDF, "application/pdf")


def test_mandant_durchsuchen_sharepoint(db, sp):
    m = _sp_mandant(db)
    erg = ablage.mandant_durchsuchen(db, m)
    assert erg["typ"] == "SHAREPOINT" and erg["wurzel_name"] == "Nordlicht" and erg["element"] == ""
    assert erg["pfad_teile"] == [{"element": "", "name": "Nordlicht"}]
    assert {o["element"] for o in erg["unterordner"]} == {"O_26"}
    assert {d["element"] for d in erg["dateien"]} == {"F_GROSS"}

    erg = ablage.mandant_durchsuchen(db, m, "O_26")
    assert erg["element"] == "O_26"
    assert erg["pfad_teile"] == [{"element": "", "name": "Nordlicht"}, {"element": "O_26", "name": "2026"}]
    assert erg["dateien"] == [{"element": "F_B", "name": "b.pdf", "groesse": len(PDF), "mime": "application/pdf",
                               "geaendert": "2026-10-01T08:00:00Z"}]
    # Wurzel-ID explizit = Wurzel
    assert ablage.mandant_durchsuchen(db, m, "O_M")["element"] == ""

    for fremd in ("O_X", "O_ALT", "O_MAND", "ROOT", "O_LUEGE"):
        _fehler(403, ablage.mandant_durchsuchen, db, m, fremd)
    _fehler(404, ablage.mandant_durchsuchen, db, m, "GIBTSNICHT")
    _fehler(404, ablage.mandant_durchsuchen, db, m, "../../me")
    _fehler(422, ablage.mandant_durchsuchen, db, m, "F_B")  # Datei statt Ordner
    # kein Inhalt fremder Ordner abgefragt
    assert not any(r.url.path.endswith("/O_X/children") for r in sp.anfragen)


def test_mandant_durchsuchen_tiefe_brotkrumen(db, sp):
    sp.items["O_Q4"] = _ordner("O_Q4", "Q4", "O_26", "/Mandanten/Nordlicht/2026")
    m = _sp_mandant(db)
    erg = ablage.mandant_durchsuchen(db, m, "O_Q4")
    assert erg["pfad_teile"] == [{"element": "", "name": "Nordlicht"}, {"element": "O_26", "name": "2026"},
                                 {"element": "O_Q4", "name": "Q4"}]


def test_mandant_durchsuchen_bibliothekswurzel(db, sp):
    m = _sp_mandant(db, ordner_id=None)
    erg = ablage.mandant_durchsuchen(db, m)
    assert erg["wurzel_name"] == "Dokumente"
    assert {o["element"] for o in erg["unterordner"]} == {"O_MAND", "O_LUEGE"}
    erg = ablage.mandant_durchsuchen(db, m, "O_26")
    assert [t["name"] for t in erg["pfad_teile"]] == ["Dokumente", "Mandanten", "Nordlicht", "2026"]
    assert ablage.mandant_datei(db, m, "F_FREMD")[1] == b"%PDF-fremd"


def test_mandant_datei_sharepoint(db, sp, monkeypatch):
    m = _sp_mandant(db)
    ref, daten = ablage.mandant_datei(db, m, "F_B")
    assert daten == PDF
    assert ref == DateiRef(ablage="SHAREPOINT", name="b.pdf", mime="application/pdf", groesse=len(PDF),
                           pfad="/Mandanten/Nordlicht/2026/b.pdf", drive_id="b!d", item_id="F_B", web_url=None)
    _fehler(403, ablage.mandant_datei, db, m, "F_FREMD")
    assert not any(str(r.url).endswith("F_FREMD") and r.url.host != "graph.microsoft.com" for r in sp.anfragen)
    _fehler(422, ablage.mandant_datei, db, m, "O_26")
    _fehler(413, ablage.mandant_datei, db, m, "F_GROSS")
    # Graph meldet keine Größe, Inhalt zu groß → Abbruch beim Herunterladen
    monkeypatch.setattr(get_settings(), "ablage_max_mb", 1)
    sp.items["F_B"]["size"] = None
    sp.inhalt["F_B"] = b"%PDF-" + b"0" * (1024 * 1024)
    _fehler(413, ablage.mandant_datei, db, m, "F_B")


def test_sharepoint_berechtigungsfehler_wird_502(db, sp, monkeypatch):
    m = _sp_mandant(db)

    def verboten(req):
        if req.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(403, json={"error": {"code": "accessDenied"}})

    monkeypatch.setattr(sharepoint, "http_client_fabrik", lambda: httpx.Client(transport=httpx.MockTransport(verboten)))
    e = _fehler(502, ablage.speichern, db, m, "a.pdf", PDF, "application/pdf")
    assert "Berechtigung" in str(e)


# --- Zwischenablage -----------------------------------------------------------------------


def test_zwischenablage_rundreise(stammordner):
    token = ablage.zwischen_speichern(7, 3, "C:\\fakepath\\Angebot Müller.pdf", PDF, "application/pdf")
    assert len(token) == 32 and int(token, 16) >= 0
    meta, daten = ablage.zwischen_lesen(token, 7)
    assert daten == PDF
    assert meta["dateiname"] == "Angebot Müller.pdf" and meta["mime"] == "application/pdf"
    assert meta["groesse"] == len(PDF) and meta["mandant_id"] == 7 and meta["benutzer_id"] == 3
    assert datetime.fromisoformat(meta["zeit"]).tzinfo is not None
    assert (stammordner / ".zwischenablage" / f"{token}.bin").is_file()
    # nicht im lokalen Ordnerdialog sichtbar
    assert ablage.lokal_ordner("")["unterordner"] == []
    ablage.zwischen_entfernen(token)
    _fehler(404, ablage.zwischen_lesen, token, 7)
    ablage.zwischen_entfernen(token)  # idempotent


def test_zwischenablage_fremder_mandant_und_ungueltige_token(stammordner):
    token = ablage.zwischen_speichern(7, None, "a.pdf", PDF, "application/pdf")
    _fehler(404, ablage.zwischen_lesen, token, 8)
    _fehler(404, ablage.zwischen_lesen, token, "7")
    assert ablage.zwischen_lesen(token.upper(), 7)[1] == PDF
    for boese in ("../../etc/passwd", "../" + token, token + ".json", token[:-1], "", None, 123, token + "\x00",
                  "g" * 32):
        _fehler(404, ablage.zwischen_lesen, boese, 7)
        ablage.zwischen_entfernen(boese)  # wirft nicht
    assert ablage.zwischen_lesen(token, 7)[1] == PDF


def test_zwischenablage_ablauf_und_aufraeumen(stammordner, monkeypatch):
    jetzt = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(ablage, "_jetzt", lambda: jetzt)
    alt = ablage.zwischen_speichern(1, None, "alt.pdf", PDF, "application/pdf")
    jetzt = jetzt + timedelta(hours=20)
    neu = ablage.zwischen_speichern(1, None, "neu.pdf", PDF, "application/pdf")
    ordner = stammordner / ".zwischenablage"
    verwaist = "f" * 32
    (ordner / f"{verwaist}.bin").write_bytes(b"x")
    os.utime(ordner / f"{verwaist}.bin", (0, 0))
    (ordner / "fremd.txt").write_text("bleibt")
    jetzt = jetzt + timedelta(hours=5)  # alt: 25 h, neu: 5 h
    assert ablage.zwischen_aufraeumen() == 2  # alt + verwaist
    assert not (ordner / f"{alt}.json").exists() and not (ordner / f"{verwaist}.bin").exists()
    assert (ordner / "fremd.txt").exists()
    assert ablage.zwischen_lesen(neu, 1)[1] == PDF
    jetzt = jetzt + timedelta(hours=20)
    _fehler(404, ablage.zwischen_lesen, neu, 1)  # abgelaufen → entfernt
    assert not (ordner / f"{neu}.bin").exists()
    assert ablage.zwischen_aufraeumen(1) == 0


# --- Upload-Prüfung -------------------------------------------------------------------------


def _zip(namen):
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as z:
        for n in namen:
            z.writestr(n, "<x/>")
    return puffer.getvalue()


@pytest.mark.parametrize("daten,name,mime", [
    (PDF, "x.bin", "application/pdf"),
    (b"\xef\xbb\xbf" + b" " * 100 + b"%PDF-1.7", "a.pdf", "application/pdf"),
    (PNG, "a.png", "image/png"),
    (b"\xff\xd8\xff\xe0\x00\x10JFIF", "a.jpg", "image/jpeg"),
    (b"II*\x00\x08\x00", "a.tif", "image/tiff"),
    (b"MM\x00*\x00\x08", "a.tif", "image/tiff"),
    (b"RIFF\x10\x00\x00\x00WEBPVP8 ", "a.webp", "image/webp"),
    (_zip(["[Content_Types].xml", "word/document.xml"]), "a.docx",
     "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    (_zip(["[Content_Types].xml", "xl/workbook.xml"]), "a.xlsx",
     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("Bestellung über 100 €".encode("utf-8"), "a.txt", "text/plain"),
    ("Grüße".encode("cp1252"), "A.TXT", "text/plain"),
    (b"Subject: Bestellung\r\n\r\nText", "mail.eml", "message/rfc822"),
])
def test_dateityp_erkennen(daten, name, mime):
    assert ablage.dateityp_erkennen(daten, name) == mime
    assert ablage.pruefe_upload(daten, name) == mime


@pytest.mark.parametrize("daten,name", [
    (b"<html><script>alert(1)</script></html>", "a.pdf"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "a.svg"),
    (b"Subject: x", "a.html"),
    (b"Text", "ohne_endung"),
    (b"bin\x00\x01\x02", "a.txt"),
    (_zip(["a.txt"]), "a.docx"),
    (_zip(["[Content_Types].xml", "ppt/x.xml"]), "a.pptx"),
    (b"PK\x03\x04kaputt", "a.docx"),
    (b"\x00\x00\x00\x18ftypheic", "foto.heic"),
])
def test_dateityp_unbekannt(daten, name):
    assert ablage.dateityp_erkennen(daten, name) is None
    _fehler(422, ablage.pruefe_upload, daten, name)


def test_pruefe_upload_leer_und_zu_gross(monkeypatch):
    e = _fehler(422, ablage.pruefe_upload, b"", "a.pdf")
    assert "leer" in str(e)
    monkeypatch.setattr(get_settings(), "ablage_max_mb", 1)
    assert ablage.pruefe_upload(b"%PDF-" + b"0" * (1024 * 1024 - 5), "a.pdf") == "application/pdf"
    e = _fehler(413, ablage.pruefe_upload, b"%PDF-" + b"0" * (1024 * 1024), "a.pdf")
    assert "1 MB" in str(e)


# --- Header ------------------------------------------------------------------------------------


def test_content_disposition():
    cd = ablage.content_disposition("Bestellung Müller.pdf")
    assert cd == "inline; filename=\"Bestellung Muller.pdf\"; filename*=UTF-8''Bestellung%20M%C3%BCller.pdf"
    cd = ablage.content_disposition('bö"se\r\nX-Evil: 1/../a.pdf', "attachment")
    assert "\r" not in cd and "\n" not in cd
    assert cd.startswith('attachment; filename="bo_se__X-Evil: 1_.._a.pdf"')
    assert ablage.content_disposition("見積書.pdf", "x").startswith('attachment; filename="datei.pdf"')
    assert ablage.content_disposition("", "inline") == "inline; filename=\"datei\"; filename*=UTF-8''datei"
    assert 'filename="50_ Rabatt_ ok.pdf"' in ablage.content_disposition("50% Rabatt; ok.pdf")


def test_datei_header():
    h = ablage.datei_header("a.pdf", "application/pdf")
    assert h["Content-Disposition"].startswith("inline;")
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Cache-Control"] == "private, no-store"
    assert h["Content-Type"] == "application/pdf"
    assert "Content-Security-Policy" not in h  # Chromes PDF-Viewer
    h = ablage.datei_header("a.png", "image/png")
    assert h["Content-Disposition"].startswith("inline;") and h["Content-Security-Policy"] == "sandbox"
    h = ablage.datei_header("a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert h["Content-Disposition"].startswith("attachment;") and h["Content-Security-Policy"] == "sandbox"
    h = ablage.datei_header("a.html", "text/html")
    assert h["Content-Type"] == "application/octet-stream"
    assert h["Content-Disposition"].startswith("attachment;") and h["Content-Security-Policy"] == "sandbox"
    assert ablage.inline_erlaubt("image/jpeg") and ablage.inline_erlaubt("image/tiff")
    assert not ablage.inline_erlaubt("image/svg+xml") and not ablage.inline_erlaubt("text/plain")
    assert json.dumps(h)  # nur Strings
