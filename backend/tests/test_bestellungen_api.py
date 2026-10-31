"""API-Tests Bestellungen & Bestellfreigabe: Rechte, Upload → Auslesen → Anlegen →
Datei-Proxy, Entscheidung mit Journal-Kennzahlen, Ablösung und Löschhaken,
Systemeinstellungen (ohne Klartext-Geheimnisse), Belegablage, Seiten.

Kein Netzzugriff: Graph und KI laufen über httpx.MockTransport bzw. monkeypatch;
ungeplante Anfragen schlagen fehl. Die Ablage liegt unter tmp_path."""

import io
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app import models
from app.config import get_settings
from app.services import ki, sharepoint
from test_api import _login

HEUTE = date.today()
ZAHLTAG = HEUTE + timedelta(days=21)
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
VORLAGEN = Path(__file__).resolve().parent.parent / "app" / "templates"
GEHEIM_GRAPH = "Graph-Secret~Nicht-Zeigen-42"
GEHEIM_KI = "sk-GEHEIM-KI-SCHLUESSEL-999"
SITE = "contoso.sharepoint.com,1111,2222"


@pytest.fixture(autouse=True)
def _umgebung(tmp_path, monkeypatch):
    """Ablage unter tmp_path, keine Geheimnisse aus der Umgebung, kein Netz."""
    s = get_settings()
    monkeypatch.setattr(s, "ablage_verzeichnis", str(tmp_path / "ablage"))
    monkeypatch.setattr(s, "ablage_max_mb", 25)
    monkeypatch.setattr(s, "graph_client_secret", None)
    monkeypatch.setattr(s, "llm_api_key", None)
    monkeypatch.setattr(s, "llm_url", None)

    def kein_netz(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unerwarteter Netzzugriff: {req.url}")

    monkeypatch.setattr(sharepoint, "http_client_fabrik",
                        lambda: httpx.Client(transport=httpx.MockTransport(kein_netz)))
    monkeypatch.setattr(ki, "http_client_fabrik",
                        lambda: httpx.Client(transport=httpx.MockTransport(kein_netz)))
    sharepoint.token_cache_leeren()
    yield
    sharepoint.token_cache_leeren()


# ---------------------------------------------------------------- Hilfen

def _ablage_stamm() -> Path:
    return Path(get_settings().ablage_verzeichnis)


def _mandant(client, kurz: str = "nordlicht", name: str = "Nordlicht GmbH") -> tuple[int, dict]:
    r = client.post("/api/mandanten", json={"name": name, "kurzname": kurz})
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    konten = client.get(f"/api/mandanten/{mid}/konten").json()["konten"]
    return mid, {k["nummer"]: k["id"] for k in konten}


def _benutzer(client, email: str, rolle: str, mids: list[int], darf_freigeben: bool = False) -> int:
    r = client.post("/api/benutzer", json={
        "email": email, "name": email.split("@")[0].title(), "passwort": "geheim123",
        "rolle": rolle, "mandanten_ids": mids, "darf_freigeben": darf_freigeben})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _anmelden(client, email: str | None = None) -> None:
    client.get("/logout")
    if email is None:
        _login(client)
        return
    r = client.post("/login", data={"email": email, "passwort": "geheim123"},
                    follow_redirects=False)
    assert r.status_code == 303


def _neu(client, mid: int, konto_id: int, **extra) -> dict:
    daten = {
        "kreditor": "Müller Bürobedarf GmbH", "bestellnummer": "4711",
        "bestelldatum": HEUTE.isoformat(), "zahldatum": ZAHLTAG.isoformat(),
        "betrag_netto": 1000, "konto_id": konto_id, **extra,
    }
    r = client.post(f"/api/mandanten/{mid}/bestellungen", json=daten)
    assert r.status_code == 201, r.text
    return r.json()


def _sitzung():
    import app.database as db_modul

    return db_modul.SessionLocal()


def _audit(aktion: str) -> list[str]:
    with _sitzung() as s:
        return [a.detail or "" for a in s.scalars(
            select(models.AuditLog).where(models.AuditLog.aktion == aktion))]


def _ki_fake(monkeypatch, fehler: Exception | None = None) -> list:
    """Ersetzt das Auslesen (Modul B) durch einen festen Vorschlag."""
    aufrufe = []

    def fake(db, mandant, daten, dateiname, mime, *, heute=None):
        aufrufe.append({"dateiname": dateiname, "mime": mime, "groesse": len(daten)})
        if fehler is not None:
            raise fehler
        return {
            "vorschlag": {
                "kreditor": "Müller Bürobedarf GmbH", "bestellnummer": "4711",
                "beschreibung": "Toner", "bestelldatum": HEUTE.isoformat(), "lieferdatum": None,
                "zahldatum": ZAHLTAG.isoformat(), "zahlungsziel_tage": 21,
                "betrag_netto": 1000.0, "ust_satz": 19.0, "betrag_brutto": 1190.0,
                "konto_id": None, "konto_quelle": None, "iban": None,
            },
            "ki": {"aktiv": True, "modell": "test-modell", "quelle": "TEXT",
                   "felder": ["kreditor", "betrag_netto"], "hinweise": [], "fehler": None},
            "text_quelle": "TEXT",
            "text_auszug": "Bestellung Nr. 4711",
        }

    monkeypatch.setattr(ki, "bestellung_auslesen", fake)
    return aufrufe


def _graph_einrichten(client) -> None:
    r = client.put("/api/system/einstellungen", json={
        "graph_tenant_id": "contoso.onmicrosoft.com", "graph_client_id": "app-1234",
        "graph_client_secret": GEHEIM_GRAPH})
    assert r.status_code == 200, r.text


def _graph_mock(monkeypatch, routen: dict, protokoll: list | None = None) -> None:
    """Graph-Attrappe: (Methode, Pfad) → JSON-Antwort, httpx.Response oder Funktion."""

    def handler(req: httpx.Request) -> httpx.Response:
        if protokoll is not None:
            protokoll.append(req)
        if req.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600})
        antwort = routen.get((req.method, req.url.path))
        if antwort is None:
            return httpx.Response(404, json={"error": {"code": "itemNotFound", "message": "fehlt"}})
        if callable(antwort):
            return antwort(req)
        if isinstance(antwort, httpx.Response):
            return antwort
        return httpx.Response(200, json=antwort)

    monkeypatch.setattr(sharepoint, "http_client_fabrik",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def _text_pdf(zeilen: list[str]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    puffer = io.BytesIO()
    c = canvas.Canvas(puffer, pagesize=A4)
    y = 800
    for z in zeilen:
        c.drawString(60, y, z)
        y -= 18
    c.save()
    return puffer.getvalue()


# ---------------------------------------------------------------- Seiten

def test_seiten_smoke_und_rechte(client):
    assert client.get("/system", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/mandanten/1/bestellungen", follow_redirects=False).status_code == 303

    _login(client)
    mid, _ = _mandant(client)
    _benutzer(client, "bea@example.com", "BEARBEITER", [mid])
    _benutzer(client, "lea@example.com", "LESER", [mid], darf_freigeben=True)

    if not (VORLAGEN / "system.html").exists() or not (VORLAGEN / "bestellungen.html").exists():
        pytest.skip("Vorlagen der Oberfläche fehlen noch")

    r = client.get("/system")
    assert r.status_code == 200 and "Systemeinstellungen" in r.text
    r = client.get(f"/mandanten/{mid}/bestellungen")
    assert r.status_code == 200
    assert "const DARF_FREIGEBEN = true" in r.text and "const KI_AKTIV = false" in r.text

    _anmelden(client, "bea@example.com")
    r = client.get("/system", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    r = client.get(f"/mandanten/{mid}/bestellungen")
    assert r.status_code == 200 and "const DARF_FREIGEBEN = false" in r.text

    # Leser mit Kennzeichen: trotzdem kein Freigaberecht
    _anmelden(client, "lea@example.com")
    r = client.get(f"/mandanten/{mid}/bestellungen")
    assert "const DARF_FREIGEBEN = false" in r.text and "const LESER = true" in r.text


# ---------------------------------------------------------------- Systemeinstellungen

def test_systemeinstellungen_ohne_klartext(client):
    _login(client)
    assert client.get("/api/system/einstellungen").json()["graph"]["konfiguriert"] is False

    r = client.put("/api/system/einstellungen", json={
        "graph_tenant_id": "contoso.onmicrosoft.com", "graph_client_id": "app-1234",
        "graph_client_secret": GEHEIM_GRAPH, "llm_url": "https://llm.example/v1",
        "llm_modell": "modell-1", "llm_api_key": GEHEIM_KI, "llm_vision": True})
    assert r.status_code == 200, r.text
    assert GEHEIM_GRAPH not in r.text and GEHEIM_KI not in r.text
    d = r.json()
    assert d["graph"]["konfiguriert"] and d["llm"]["konfiguriert"] and d["llm"]["vision"]
    assert d["graph"]["client_secret"] == {"gesetzt": True, "lesbar": True, "aus_umgebung": False}
    assert GEHEIM_GRAPH not in client.get("/api/system/einstellungen").text

    with _sitzung() as s:
        zeile = s.get(models.SystemEinstellung, "graph_client_secret")
        assert zeile.verschluesselt and GEHEIM_GRAPH not in zeile.wert
        assert GEHEIM_KI not in s.get(models.SystemEinstellung, "llm_api_key").wert
    details = _audit("SYSTEMEINSTELLUNGEN_GEAENDERT")
    assert details and "graph_client_secret" in details[-1]
    assert all(GEHEIM_GRAPH not in x and GEHEIM_KI not in x for x in details)

    # Geheimnis nicht gesendet = unverändert; "" = löschen
    r = client.put("/api/system/einstellungen", json={"llm_modell": "modell-2"})
    assert r.json()["llm"]["api_key"]["gesetzt"] is True
    r = client.put("/api/system/einstellungen", json={"llm_api_key": ""})
    assert r.json()["llm"]["api_key"]["gesetzt"] is False

    # Prüfungen
    assert client.put("/api/system/einstellungen",
                      json={"graph_tenant_id": "contoso/../evil"}).status_code == 422
    assert client.put("/api/system/einstellungen", json={"llm_url": "ftp://x"}).status_code == 422
    assert client.put("/api/system/einstellungen", json={"unbekannt": "x"}).status_code == 422

    # nur Administratoren
    mid, _ = _mandant(client)
    _benutzer(client, "bea@example.com", "BEARBEITER", [mid])
    _anmelden(client, "bea@example.com")
    assert client.get("/api/system/einstellungen").status_code == 403
    assert client.put("/api/system/einstellungen", json={"llm_modell": "x"}).status_code == 403
    assert client.post("/api/system/graph/test").status_code == 403
    assert client.post("/api/system/ki/test").status_code == 403


def test_verbindungstests(client, monkeypatch):
    _login(client)
    assert client.post("/api/system/graph/test").status_code == 409
    assert client.post("/api/system/ki/test").status_code == 409

    _graph_einrichten(client)
    protokoll: list = []
    _graph_mock(monkeypatch, {
        ("GET", "/v1.0/sites"): {"value": [{"id": SITE, "displayName": "Insolvenzakte"}]},
    }, protokoll)
    r = client.post("/api/system/graph/test")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "sites_lesbar": True, "hinweis": None}
    graph_anfrage = next(x for x in protokoll if x.url.host == "graph.microsoft.com")
    assert graph_anfrage.headers["authorization"] == "Bearer tok-123"

    # Anmeldefehler von Entra ID: nie 401 an den Browser (würde zur Anmeldung umleiten)
    sharepoint.token_cache_leeren()

    def abgelehnt(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client",
                                         "error_description": "AADSTS7000215: Invalid client secret"})

    monkeypatch.setattr(sharepoint, "http_client_fabrik",
                        lambda: httpx.Client(transport=httpx.MockTransport(abgelehnt)))
    r = client.post("/api/system/graph/test")
    assert r.status_code == 502 and "AADSTS7000215" in r.json()["detail"]

    # KI
    client.put("/api/system/einstellungen", json={
        "llm_url": "https://llm.example/v1", "llm_modell": "modell-1", "llm_api_key": GEHEIM_KI})
    gesendet: list = []

    def llm(req: httpx.Request) -> httpx.Response:
        gesendet.append(req)
        return httpx.Response(200, json={"model": "modell-1", "choices": [
            {"message": {"content": '{"antwort": "ok"}'}}]})

    monkeypatch.setattr(ki, "http_client_fabrik",
                        lambda: httpx.Client(transport=httpx.MockTransport(llm)))
    r = client.post("/api/system/ki/test")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "modell": "modell-1", "antwort": "ok"}
    assert str(gesendet[0].url) == "https://llm.example/v1/chat/completions"
    assert gesendet[0].headers["authorization"] == f"Bearer {GEHEIM_KI}"

    monkeypatch.setattr(ki, "http_client_fabrik", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(401, json={"error": {"message": "bad key"}}))))
    r = client.post("/api/system/ki/test")
    assert r.status_code == 502 and GEHEIM_KI not in r.text


# ---------------------------------------------------------------- SharePoint-Auswahl

def test_sharepoint_auswahl(client, monkeypatch):
    _login(client)
    assert client.get("/api/sharepoint/websites").status_code == 409   # nicht eingerichtet
    _graph_einrichten(client)
    _graph_mock(monkeypatch, {
        ("GET", "/v1.0/sites"): {"value": [{"id": SITE, "displayName": "Insolvenzakte",
                                             "webUrl": "https://contoso.sharepoint.com/sites/akte"}]},
        ("GET", f"/v1.0/sites/{SITE}/drives"): {"value": [
            {"id": "b!d1", "name": "Dokumente", "webUrl": "https://contoso.sharepoint.com/sites/akte/Doks"}]},
        ("GET", "/v1.0/drives/b!d1/root"): {"id": "wurzel", "name": "root", "root": {},
                                            "folder": {"childCount": 2}},
        ("GET", "/v1.0/drives/b!d1/root/children"): {"value": [
            {"id": "f1", "name": "Akten", "folder": {"childCount": 3}},
            {"id": "d1", "name": "liste.pdf", "file": {"mimeType": "application/pdf"}, "size": 10}]},
        ("GET", "/v1.0/drives/b!d1/items/geheim"): httpx.Response(
            403, json={"error": {"code": "accessDenied", "message": "nein"}}),
    })
    r = client.get("/api/sharepoint/websites?suche=")
    assert r.status_code == 200, r.text
    assert r.json()[0]["name"] == "Insolvenzakte"
    r = client.get(f"/api/sharepoint/bibliotheken?site_id={SITE}")
    assert r.status_code == 200 and r.json()[0]["id"] == "b!d1"
    assert client.get("/api/sharepoint/bibliotheken").status_code == 422
    r = client.get("/api/sharepoint/ordner?drive_id=b!d1&item_id=")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ordner"]["ist_wurzel"] is True
    assert [o["name"] for o in d["unterordner"]] == ["Akten"] and d["unterordner"][0]["elemente"] == 3
    assert [x["name"] for x in d["dateien"]] == ["liste.pdf"]
    assert client.get("/api/sharepoint/ordner").status_code == 422
    # Berechtigungsfehler von Graph werden als 403 durchgereicht
    assert client.get("/api/sharepoint/ordner?drive_id=b!d1&item_id=geheim").status_code == 403

    mid, _ = _mandant(client)
    _benutzer(client, "bea@example.com", "BEARBEITER", [mid])
    _anmelden(client, "bea@example.com")
    assert client.get("/api/sharepoint/websites").status_code == 403
    assert client.get("/api/ablage/lokal").status_code == 403


# ---------------------------------------------------------------- Belegablage je Mandant

def test_ablage_lokal_und_pfadausbruch(client, tmp_path):
    _login(client)
    mid, _ = _mandant(client)
    r = client.get(f"/api/mandanten/{mid}/ablage")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["typ"] == "LOKAL" and d["lokal_pfad"] == f"nordlicht-{mid}" and d["konfiguriert"] is False
    assert d["bestellungen_offen_einplanen"] is True and d["graph_konfiguriert"] is False

    # lokaler Ordnerdialog
    r = client.post("/api/ablage/lokal", json={"eltern": "", "name": "Akten"})
    assert r.status_code == 201 and r.json() == {"pfad": "Akten"}
    assert (_ablage_stamm() / "Akten").is_dir()
    r = client.get("/api/ablage/lokal?pfad=")
    assert [o["name"] for o in r.json()["unterordner"]] == ["Akten"]
    assert client.get("/api/ablage/lokal?pfad=../..").status_code == 422

    # Pfadausbrüche
    draussen = tmp_path / "draussen"
    draussen.mkdir()
    (_ablage_stamm() / "link").symlink_to(draussen, target_is_directory=True)
    for boese in ("../ausbruch", "/etc", "Akten/../../x", ".zwischenablage", "link/x", "a\x00b"):
        r = client.put(f"/api/mandanten/{mid}/ablage", json={"typ": "LOKAL", "lokal_pfad": boese})
        assert r.status_code == 422, (boese, r.text)
    assert not (tmp_path / "ausbruch").exists() and not (draussen / "x").exists()
    assert client.put(f"/api/mandanten/{mid}/ablage", json={"typ": "FTP"}).status_code == 422

    r = client.put(f"/api/mandanten/{mid}/ablage", json={"typ": "LOKAL", "lokal_pfad": "Akten/Nordlicht"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["typ"] == "LOKAL" and d["lokal_pfad"] == "Akten/Nordlicht" and d["konfiguriert"] is True
    assert d["beschreibung"] == "lokal: Akten/Nordlicht"
    assert (_ablage_stamm() / "Akten" / "Nordlicht").is_dir()
    assert any("Akten/Nordlicht" in x for x in _audit("ABLAGE_GEAENDERT"))

    # Ordner zweier Mandanten dürfen sich nicht überschneiden (gleich, darunter, darüber)
    mid_b, _ = _mandant(client, "suedwind", "Südwind AG")
    for konflikt in ("Akten/Nordlicht", "Akten/Nordlicht/unter", "Akten"):
        r = client.put(f"/api/mandanten/{mid_b}/ablage", json={"typ": "LOKAL", "lokal_pfad": konflikt})
        assert r.status_code == 409, (konflikt, r.text)
    assert not (_ablage_stamm() / "Akten" / "Nordlicht" / "unter").exists()
    # auch der Standardordner eines anderen Mandanten ist reserviert
    r = client.put(f"/api/mandanten/{mid}/ablage", json={"typ": "LOKAL", "lokal_pfad": f"suedwind-{mid_b}"})
    assert r.status_code == 409 and "Südwind AG" in r.json()["detail"]
    assert client.put(f"/api/mandanten/{mid_b}/ablage",
                      json={"typ": "LOKAL", "lokal_pfad": "Akten/Suedwind"}).status_code == 200

    # Durchsuchen nur innerhalb des Mandantenordners
    (_ablage_stamm() / "Akten" / "Nordlicht" / "angebot.pdf").write_bytes(PDF)
    (_ablage_stamm() / "Akten" / "fremd.pdf").write_bytes(PDF)
    r = client.get(f"/api/mandanten/{mid}/ablage/durchsuchen?element=")
    assert r.status_code == 200, r.text
    assert [x["name"] for x in r.json()["dateien"]] == ["angebot.pdf"]
    assert client.get(f"/api/mandanten/{mid}/ablage/durchsuchen?element=..").status_code == 422

    # leerer Pfad = Standardordner (Kurzname-ID)
    r = client.put(f"/api/mandanten/{mid}/ablage", json={"typ": "LOKAL", "lokal_pfad": ""})
    assert r.json()["lokal_pfad"] == f"nordlicht-{mid}"

    # Bearbeiter: lesen ja, ändern nein
    _benutzer(client, "bea@example.com", "BEARBEITER", [mid])
    _anmelden(client, "bea@example.com")
    assert client.get(f"/api/mandanten/{mid}/ablage").status_code == 200
    assert client.put(f"/api/mandanten/{mid}/ablage",
                      json={"typ": "LOKAL", "lokal_pfad": "x"}).status_code == 403


def test_ablage_sharepoint_pfad_serverseitig(client, monkeypatch):
    _login(client)
    mid, _ = _mandant(client)
    daten = {"typ": "SHAREPOINT", "drive_id": "b!d1", "ordner_id": "f2", "site_id": SITE,
             "pfad": "vom Client", "web_url": "https://boese.example/"}
    assert client.put(f"/api/mandanten/{mid}/ablage", json=daten).status_code == 409   # ohne Graph

    _graph_einrichten(client)
    _graph_mock(monkeypatch, {
        ("GET", "/v1.0/drives/b!d1/items/f2"): {
            "id": "f2", "name": "Nordlicht", "folder": {"childCount": 0},
            "webUrl": "https://contoso.sharepoint.com/sites/akte/Doks/Akten/Nordlicht",
            "parentReference": {"driveId": "b!d1", "id": "f1", "path": "/drives/b!d1/root:/Akten"}},
        ("GET", "/v1.0/drives/b!d1/items/datei1"): {
            "id": "datei1", "name": "x.pdf", "file": {"mimeType": "application/pdf"},
            "parentReference": {"driveId": "b!d1", "id": "f1", "path": "/drives/b!d1/root:/Akten"}},
        ("GET", "/v1.0/drives/b!d1"): {"id": "b!d1", "name": "Dokumente",
                                      "webUrl": "https://contoso.sharepoint.com/sites/akte/Doks"},
        ("GET", f"/v1.0/sites/{SITE}"): {"id": SITE, "displayName": "Insolvenzakte"},
    })
    r = client.put(f"/api/mandanten/{mid}/ablage", json=daten)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["typ"] == "SHAREPOINT" and d["konfiguriert"] is True and d["graph_konfiguriert"] is True
    assert d["pfad"] == "Insolvenzakte › Dokumente › /Akten/Nordlicht"
    assert d["beschreibung"] == "SharePoint: Insolvenzakte › Dokumente › /Akten/Nordlicht"
    assert d["web_url"] == "https://contoso.sharepoint.com/sites/akte/Doks/Akten/Nordlicht"
    assert (d["drive_id"], d["ordner_id"], d["site_id"]) == ("b!d1", "f2", SITE)

    # Datei statt Ordner, fehlende Bibliothek, unbekannter Ordner
    assert client.put(f"/api/mandanten/{mid}/ablage",
                      json={**daten, "ordner_id": "datei1"}).status_code == 422
    assert client.put(f"/api/mandanten/{mid}/ablage",
                      json={**daten, "drive_id": ""}).status_code == 422
    assert client.put(f"/api/mandanten/{mid}/ablage",
                      json={**daten, "ordner_id": "gibtsnicht"}).status_code == 404
    assert client.get(f"/api/mandanten/{mid}/ablage").json()["ordner_id"] == "f2"


# ---------------------------------------------------------------- Erfassung und Datei-Proxy

def test_upload_auslesen_anlegen_und_proxy(client, monkeypatch):
    aufrufe = _ki_fake(monkeypatch)
    _login(client)
    mid, konten = _mandant(client)

    # Client-Content-Type wird ignoriert (Inhalt zählt)
    r = client.post(f"/api/mandanten/{mid}/bestellungen/auslesen",
                    files={"datei": ("Bestellung Müller.pdf", PDF, "text/html")})
    assert r.status_code == 200, r.text
    erg = r.json()
    assert re.fullmatch(r"[0-9a-f]{32}", erg["token"])
    assert erg["datei"] == {"name": "Bestellung Müller.pdf", "mime": "application/pdf",
                            "groesse": len(PDF)}
    assert erg["vorschlag"]["kreditor"] == "Müller Bürobedarf GmbH"
    assert erg["ki"]["aktiv"] is True and erg["text_quelle"] == "TEXT"
    assert erg["duplikat"] is None
    assert aufrufe == [{"dateiname": "Bestellung Müller.pdf", "mime": "application/pdf",
                        "groesse": len(PDF)}]
    assert any("Bestellung Müller.pdf" in x for x in _audit("BESTELLUNG_AUSGELESEN"))

    # Vorschau aus der Zwischenablage
    r = client.get(f"/api/mandanten/{mid}/bestellungen/zwischenablage/{erg['token']}")
    assert r.status_code == 200 and r.content == PDF
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "filename*=UTF-8''Bestellung%20M%C3%BCller.pdf" in r.headers["content-disposition"]

    # Anlegen mit Token: Datei landet im Mandantenordner, Token ist danach weg
    b = _neu(client, mid, konten["3400"], token=erg["token"],
             ki_daten={"vorschlag": erg["vorschlag"], "ki": erg["ki"], "datei": erg["datei"]})
    assert b["status"] == "OFFEN" and b["ust_satz"] == 19.0 and b["betrag_brutto"] == 1190.0
    assert b["ki"] is True
    datei = b["datei"]
    assert datei["name"] == f"{HEUTE.isoformat()}_Müller Bürobedarf GmbH_Bestellung Müller.pdf"
    assert datei["ablage"] == "LOKAL" and datei["pfad"] == f"nordlicht-{mid}/{datei['name']}"
    assert datei["url"] == f"/mandanten/{mid}/bestellungen/{b['id']}/datei"
    assert (_ablage_stamm() / f"nordlicht-{mid}" / datei["name"]).read_bytes() == PDF
    assert client.get(f"/api/mandanten/{mid}/bestellungen/zwischenablage/{erg['token']}").status_code == 404
    assert any(f"#{b['id']}" in x for x in _audit("BESTELLUNG_ERFASST"))

    # Datei-Proxy: eigene Header (inline, UTF-8-Name, nosniff, kein Caching)
    r = client.get(datei["url"])
    assert r.status_code == 200, r.text
    assert r.content == PDF
    assert r.headers["content-type"] == "application/pdf"
    cd = r.headers["content-disposition"]
    assert cd.startswith("inline; ")
    assert 'filename="' in cd and "Muller Burobedarf" in cd
    assert "filename*=UTF-8''" in cd and "M%C3%BCller%20B%C3%BCrobedarf" in cd
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, no-store"
    assert "content-security-policy" not in r.headers   # PDF-Viewer verträgt keine Sandbox

    # erneuter Upload derselben Datei → Duplikat-Hinweis
    r = client.post(f"/api/mandanten/{mid}/bestellungen/auslesen",
                    files={"datei": ("Bestellung Müller.pdf", PDF, "application/pdf")})
    assert r.json()["duplikat"]["id"] == b["id"]

    # Detail mit Journal, Liste mit Kontext
    d = client.get(f"/api/bestellungen/{b['id']}").json()
    assert [j["aktion"] for j in d["journal"]] == ["ERFASST"]
    liste = client.get(f"/api/mandanten/{mid}/bestellungen?status=OFFEN").json()
    assert [x["id"] for x in liste["bestellungen"]] == [b["id"]]
    assert "Müller Bürobedarf GmbH" in liste["kreditoren"]
    assert liste["darf_freigeben"] is True and liste["ki_aktiv"] is False
    assert liste["ablage"]["typ"] == "LOKAL"
    assert client.get(f"/api/mandanten/{mid}/bestellungen?status=UNSINN").status_code == 422

    # Datei ersetzen: neue Datei, alte bleibt in der Ablage
    r = client.post(f"/api/bestellungen/{b['id']}/datei",
                    files={"datei": ("korrigiert.pdf", PDF + b"%neu", "application/pdf")})
    assert r.status_code == 200, r.text
    neu = r.json()["datei"]
    assert neu["name"].endswith("_korrigiert.pdf") and neu["groesse"] == len(PDF) + 4
    assert (_ablage_stamm() / f"nordlicht-{mid}" / datei["name"]).exists()
    assert client.get(neu["url"]).content == PDF + b"%neu"
    j = client.get(f"/api/mandanten/{mid}/bestellungen/journal?bestellung_id={b['id']}").json()
    assert j[0]["aktion"] == "DATEI"


def test_upload_pruefungen(client, monkeypatch):
    _ki_fake(monkeypatch)
    _login(client)
    mid, konten = _mandant(client)
    url = f"/api/mandanten/{mid}/bestellungen/auslesen"

    # getarntes HTML, leere Datei, zu groß
    r = client.post(url, files={"datei": ("rechnung.pdf", b"<html><script>alert(1)</script>", "application/pdf")})
    assert r.status_code == 422, r.text
    assert client.post(url, files={"datei": ("leer.pdf", b"", "application/pdf")}).status_code == 422
    monkeypatch.setattr(get_settings(), "ablage_max_mb", 1)
    gross = PDF + b"0" * (1024 * 1024)
    assert client.post(url, files={"datei": ("gross.pdf", gross, "application/pdf")}).status_code == 413
    monkeypatch.setattr(get_settings(), "ablage_max_mb", 25)

    # Text: Auslieferung nur als Download, mit Sandbox
    r = client.post(url, files={"datei": ("notiz.txt", "Bestellung über 100 €".encode(), "text/html")})
    assert r.status_code == 200, r.text
    assert r.json()["datei"]["mime"] == "text/plain"
    b = _neu(client, mid, konten["3400"], token=r.json()["token"])
    r = client.get(b["datei"]["url"])
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["content-disposition"].startswith("attachment; ")
    assert r.headers["content-security-policy"] == "sandbox"

    # Auslesen scheitert unerwartet → Erfassung bleibt möglich
    _ki_fake(monkeypatch, fehler=RuntimeError("kaputt"))
    r = client.post(url, files={"datei": ("b.pdf", PDF, "application/pdf")})
    assert r.status_code == 200, r.text
    assert r.json()["ki"]["aktiv"] is False and "kaputt" in r.json()["ki"]["fehler"]
    assert r.json()["token"]

    # Token und Ablage-Element zugleich, ungültige KI-Daten, Token fremd/unbekannt
    basis = {"kreditor": "X", "bestelldatum": HEUTE.isoformat(), "zahldatum": ZAHLTAG.isoformat(),
             "betrag_netto": 10, "konto_id": konten["3400"]}
    pfad = f"/api/mandanten/{mid}/bestellungen"
    assert client.post(pfad, json={**basis, "token": r.json()["token"], "ablage_element": "a.pdf"}).status_code == 422
    assert client.post(pfad, json={**basis, "ki_daten": "text"}).status_code == 422
    assert client.post(pfad, json={**basis, "token": "0" * 32}).status_code == 404

    # Validierungsfehler vor dem Speichern: keine verwaiste Datei in der Ablage
    vorher = sorted(p.name for p in (_ablage_stamm() / f"nordlicht-{mid}").iterdir())
    r2 = client.post(pfad, json={**basis, "token": r.json()["token"], "betrag_netto": -5})
    assert r2.status_code == 422
    assert sorted(p.name for p in (_ablage_stamm() / f"nordlicht-{mid}").iterdir()) == vorher

    # Leser dürfen nicht hochladen oder anlegen
    _benutzer(client, "lea@example.com", "LESER", [mid])
    _anmelden(client, "lea@example.com")
    assert client.post(url, files={"datei": ("b.pdf", PDF, "application/pdf")}).status_code == 403
    assert client.post(pfad, json=basis).status_code == 403


def test_auslesen_mit_echtem_ki_client(client, monkeypatch):
    """Durchstich Modul B: Text-PDF → Belegtext → KI (MockTransport) → Vorschlag."""
    _login(client)
    mid, konten = _mandant(client)
    client.put("/api/system/einstellungen", json={
        "llm_url": "https://llm.example/v1", "llm_modell": "modell-1", "llm_api_key": GEHEIM_KI})
    gesendet: list = []
    antwort = {
        "dokumentart": "Bestellung", "kreditor": "Papier Peters KG", "bestellnummer": "B-77",
        "beschreibung": "Kopierpapier", "bestelldatum": HEUTE.isoformat(), "lieferdatum": None,
        "zahldatum": None, "zahlungsziel_tage": 14, "betrag_netto": "2.000,00", "ust_satz": 19,
        "betrag_brutto": "2.380,00", "konto_nummer": "3400", "iban": None, "hinweise": [],
    }

    def llm(req: httpx.Request) -> httpx.Response:
        import json as _json

        gesendet.append(_json.loads(req.content))
        return httpx.Response(200, json={"model": "modell-1", "choices": [
            {"message": {"content": _json.dumps(antwort)}}]})

    monkeypatch.setattr(ki, "http_client_fabrik",
                        lambda: httpx.Client(transport=httpx.MockTransport(llm)))
    pdf = _text_pdf(["Bestellung B-77", "Papier Peters KG", "Kopierpapier 2.000,00 EUR netto"])
    r = client.post(f"/api/mandanten/{mid}/bestellungen/auslesen",
                    files={"datei": ("peters.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    erg = r.json()
    assert erg["ki"]["aktiv"] is True and erg["ki"]["fehler"] is None, erg["ki"]
    assert erg["text_quelle"] == "TEXT"
    v = erg["vorschlag"]
    assert v["kreditor"] == "Papier Peters KG" and v["betrag_netto"] == 2000.0
    assert v["konto_id"] == konten["3400"]
    assert gesendet and gesendet[0]["model"] == "modell-1"
    assert "Kopierpapier" in str(gesendet[0]["messages"])


def test_auslesen_und_anlegen_aus_ablage(client, monkeypatch):
    _ki_fake(monkeypatch)
    _login(client)
    mid, konten = _mandant(client)
    ordner = _ablage_stamm() / f"nordlicht-{mid}" / "Eingang"
    ordner.mkdir(parents=True)
    (ordner / "angebot.pdf").write_bytes(PDF)
    (_ablage_stamm() / "fremd.pdf").write_bytes(PDF)

    url = f"/api/mandanten/{mid}/bestellungen/auslesen-ablage"
    r = client.post(url, json={"element": "Eingang/angebot.pdf"})
    assert r.status_code == 200, r.text
    erg = r.json()
    assert erg["ablage_element"] == "Eingang/angebot.pdf" and "token" not in erg
    assert erg["datei"]["name"] == "angebot.pdf" and erg["duplikat"] is None
    for boese in ("../fremd.pdf", "/etc/passwd"):
        assert client.post(url, json={"element": boese}).status_code in (403, 422)
    assert client.post(url, json={"element": ""}).status_code == 422
    assert client.post(url, json={"element": "Eingang/fehlt.pdf"}).status_code == 404

    # Verknüpfen ohne Kopie
    b = _neu(client, mid, konten["3400"], ablage_element="Eingang/angebot.pdf")
    assert b["datei"]["pfad"] == f"nordlicht-{mid}/Eingang/angebot.pdf"
    assert sorted(p.name for p in ordner.iterdir()) == ["angebot.pdf"]
    assert client.get(b["datei"]["url"]).content == PDF
    assert client.post(url, json={"element": "Eingang/angebot.pdf"}).json()["duplikat"]["id"] == b["id"]


def test_sharepoint_ablage_speichern_und_proxy(client, monkeypatch):
    """Ablage in SharePoint: Speichern über Graph, Auslieferung über den Server –
    der Browser sieht die vorab signierte Download-URL nie."""
    _ki_fake(monkeypatch)
    _login(client)
    mid, konten = _mandant(client)
    _graph_einrichten(client)
    protokoll: list = []
    ordner = {"id": "f2", "name": "Nordlicht", "folder": {"childCount": 0},
              "webUrl": "https://contoso.sharepoint.com/Doks/Akten/Nordlicht",
              "parentReference": {"driveId": "b!d1", "id": "f1", "path": "/drives/b!d1/root:/Akten"}}

    def handler(req: httpx.Request) -> httpx.Response:
        protokoll.append(req)
        pfad = req.url.path
        if req.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600})
        if req.url.host == "download.example":
            return httpx.Response(200, content=PDF)
        if req.method == "GET" and pfad == "/v1.0/drives/b!d1/items/f2":
            return httpx.Response(200, json=ordner)
        if req.method == "GET" and pfad == "/v1.0/drives/b!d1":
            return httpx.Response(200, json={"id": "b!d1", "name": "Dokumente"})
        if req.method == "PUT" and pfad.startswith("/v1.0/drives/b!d1/items/f2:/") \
                and pfad.endswith(":/content"):
            name = pfad[len("/v1.0/drives/b!d1/items/f2:/"):-len(":/content")]
            return httpx.Response(201, json={
                "id": "neu1", "name": name, "size": len(req.content),
                "webUrl": f"https://contoso.sharepoint.com/Doks/Akten/Nordlicht/{name}",
                "parentReference": {"driveId": "b!d1", "id": "f2",
                                    "path": "/drives/b!d1/root:/Akten/Nordlicht"}})
        if req.method == "GET" and pfad == "/v1.0/drives/b!d1/items/neu1/content":
            return httpx.Response(302, headers={"Location": "https://download.example/signiert?t=geheim"})
        return httpx.Response(404, json={"error": {"code": "itemNotFound", "message": "fehlt"}})

    monkeypatch.setattr(sharepoint, "http_client_fabrik",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    r = client.put(f"/api/mandanten/{mid}/ablage",
                   json={"typ": "SHAREPOINT", "drive_id": "b!d1", "ordner_id": "f2"})
    assert r.status_code == 200, r.text
    assert r.json()["pfad"] == "Dokumente › /Akten/Nordlicht"

    token = client.post(f"/api/mandanten/{mid}/bestellungen/auslesen",
                        files={"datei": ("auftrag.pdf", PDF, "application/pdf")}).json()["token"]
    b = _neu(client, mid, konten["3400"], token=token)
    datei = b["datei"]
    assert datei["ablage"] == "SHAREPOINT"
    assert datei["name"] == f"{HEUTE.isoformat()}_Müller Bürobedarf GmbH_auftrag.pdf"
    assert datei["pfad"] == f"/Akten/Nordlicht/{datei['name']}"
    hochgeladen = next(x for x in protokoll if x.method == "PUT")
    assert hochgeladen.content == PDF and hochgeladen.headers["content-type"] == "application/pdf"
    assert not any((_ablage_stamm() / f"nordlicht-{mid}").glob("*.pdf"))    # nichts lokal abgelegt

    protokoll.clear()
    r = client.get(datei["url"], follow_redirects=False)
    assert r.status_code == 200, r.text
    assert r.content == PDF and "location" not in r.headers
    assert "signiert" not in r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"].startswith("inline; ")
    download = next(x for x in protokoll if x.url.host == "download.example")
    assert "authorization" not in download.headers          # Token nur an Graph

    # SharePoint nicht erreichbar → 502, nicht 500
    monkeypatch.setattr(sharepoint, "http_client_fabrik", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("weg")))))
    sharepoint.token_cache_leeren()
    assert client.get(datei["url"]).status_code == 502


def test_datei_proxy_rechte(client, monkeypatch):
    _ki_fake(monkeypatch)
    _login(client)
    mid_a, konten_a = _mandant(client, "nordlicht", "Nordlicht GmbH")
    mid_b, konten_b = _mandant(client, "suedwind", "Südwind AG")
    token = client.post(f"/api/mandanten/{mid_a}/bestellungen/auslesen",
                        files={"datei": ("a.pdf", PDF, "application/pdf")}).json()["token"]
    b = _neu(client, mid_a, konten_a["3400"], token=token)
    ohne = _neu(client, mid_a, konten_a["3400"], kreditor="Ohne Datei")
    url = b["datei"]["url"]

    # Bestellung gehört nicht zum Mandanten im Pfad; Bestellung ohne Datei
    assert client.get(f"/mandanten/{mid_b}/bestellungen/{b['id']}/datei").status_code == 404
    assert client.get(f"/mandanten/{mid_a}/bestellungen/{ohne['id']}/datei").status_code == 404
    # Zwischenablage: Token eines anderen Mandanten
    t2 = client.post(f"/api/mandanten/{mid_a}/bestellungen/auslesen",
                     files={"datei": ("c.pdf", PDF, "application/pdf")}).json()["token"]
    assert client.get(f"/api/mandanten/{mid_b}/bestellungen/zwischenablage/{t2}").status_code == 404

    _benutzer(client, "sued@example.com", "BEARBEITER", [mid_b])
    _benutzer(client, "lea@example.com", "LESER", [mid_a])
    _benutzer(client, "kollege@example.com", "BEARBEITER", [mid_a])

    _anmelden(client, "sued@example.com")
    assert client.get(url).status_code == 403
    assert client.get(f"/api/bestellungen/{b['id']}").status_code == 403
    assert client.get(f"/api/bestellungen/{b['id']}/freigabe").status_code == 403
    assert client.patch(f"/api/bestellungen/{b['id']}", json={"kreditor": "X"}).status_code == 403
    assert client.get(f"/api/mandanten/{mid_a}/bestellungen").status_code == 403

    # fremder Upload desselben Mandanten bleibt privat
    _anmelden(client, "kollege@example.com")
    assert client.get(f"/api/mandanten/{mid_a}/bestellungen/zwischenablage/{t2}").status_code == 404

    _anmelden(client, "lea@example.com")
    assert client.get(url).status_code == 200    # Leser dürfen ansehen

    client.get("/logout")
    r = client.get(url, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"

    # Datei in der Ablage gelöscht → 404 statt Fehler
    _anmelden(client)
    (_ablage_stamm() / b["datei"]["pfad"]).unlink()
    assert client.get(url).status_code == 404


# ---------------------------------------------------------------- Freigabe

def test_entscheidung_rechte_und_journal_kennzahlen(client):
    _login(client)
    mid, konten = _mandant(client)
    b = _neu(client, mid, konten["3400"])
    iso = ZAHLTAG.isocalendar()

    r = client.get(f"/api/bestellungen/{b['id']}/freigabe")
    assert r.status_code == 200, r.text
    kz = r.json()
    assert (kz["kw"]["jahr"], kz["kw"]["kw"]) == (iso.year, iso.week)
    assert kz["betrag_brutto"] == 1190.0 and kz["massgeblich"] in ("konto", "gruppe")
    assert [e["ebene"] for e in kz["ebenen"]][-1] == "gesamt"
    assert kz["ampel"] in ("gruen", "rot")
    assert client.get(f"/api/bestellungen/{b['id']}/freigabe?jahr=2020&kw=1").status_code == 422
    assert client.get(f"/api/bestellungen/{b['id']}/freigabe?jahr=2026").status_code == 422
    assert client.get(f"/api/bestellungen/{b['id']}/freigabe?basis=quatsch").status_code == 422
    assert client.get("/api/bestellungen/999999/freigabe").status_code == 404

    _benutzer(client, "bea@example.com", "BEARBEITER", [mid])
    _benutzer(client, "lea@example.com", "LESER", [mid], darf_freigeben=True)
    _benutzer(client, "sachwalter@example.com", "BEARBEITER", [mid], darf_freigeben=True)
    entscheidung = {"entscheidung": "FREIGEBEN", "jahr": iso.year, "kw": iso.week,
                    "kennzahlen": {"ebenen": "vom Client gefälscht"}}

    _anmelden(client, "bea@example.com")
    r = client.post(f"/api/bestellungen/{b['id']}/entscheidung", json=entscheidung)
    assert r.status_code == 403 and r.json()["detail"] == "Keine Berechtigung zur Bestellfreigabe"
    assert client.get("/api/profil").json()["darf_bestellfreigabe"] is False
    _anmelden(client, "lea@example.com")
    assert client.post(f"/api/bestellungen/{b['id']}/entscheidung", json=entscheidung).status_code == 403
    assert client.get("/api/profil").json()["darf_bestellfreigabe"] is False

    _anmelden(client, "sachwalter@example.com")
    assert client.get("/api/profil").json()["darf_bestellfreigabe"] is True
    r = client.post(f"/api/bestellungen/{b['id']}/entscheidung",
                    json={**entscheidung, "entscheidung": "ABLEHNEN"})
    assert r.status_code == 422          # Ablehnung ohne Begründung
    assert client.post(f"/api/bestellungen/{b['id']}/entscheidung",
                       json={**entscheidung, "entscheidung": "VIELLEICHT"}).status_code == 422
    r = client.post(f"/api/bestellungen/{b['id']}/entscheidung", json=entscheidung)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "FREIGEGEBEN" and r.json()["entschieden_von"] == "Sachwalter"

    # Journal: serverseitig berechnete Kennzahlen, nicht die des Clients
    journal = client.get(f"/api/mandanten/{mid}/bestellungen/journal").json()
    assert journal[0]["aktion"] == "FREIGEGEBEN" and journal[0]["benutzer"] == "Sachwalter"
    gespeichert = journal[0]["kennzahlen"]
    assert isinstance(gespeichert["ebenen"], list)
    assert gespeichert["kw"]["label"] == f"KW {iso.week}/{iso.year}"
    assert gespeichert["betrag_brutto"] == 1190.0
    assert gespeichert["ebenen"] == kz["ebenen"]
    assert any(f"#{b['id']}" in x and "verfügbar" in x for x in _audit("BESTELLUNG_FREIGEGEBEN"))

    # nochmal entscheiden: Zustandsfehler; zurücknehmen nur mit Begründung
    assert client.post(f"/api/bestellungen/{b['id']}/entscheidung", json=entscheidung).status_code == 409
    assert client.post(f"/api/bestellungen/{b['id']}/zuruecksetzen", json={}).status_code == 422
    r = client.post(f"/api/bestellungen/{b['id']}/zuruecksetzen", json={"begruendung": "Irrtum"})
    assert r.status_code == 200 and r.json()["status"] == "OFFEN"
    assert client.post(f"/api/bestellungen/{b['id']}/zuruecksetzen",
                       json={"begruendung": "x"}).status_code == 409

    # Ablehnen mit Begründung (ohne jahr/kw: KW des Zahldatums)
    r = client.post(f"/api/bestellungen/{b['id']}/entscheidung",
                    json={"entscheidung": "ABLEHNEN", "begruendung": "Kein Bedarf"})
    assert r.status_code == 200 and r.json()["status"] == "ABGELEHNT"
    j = client.get(f"/api/mandanten/{mid}/bestellungen/journal?bestellung_id={b['id']}&limit=1").json()
    assert len(j) == 1 and j[0]["aktion"] == "ABGELEHNT" and j[0]["begruendung"] == "Kein Bedarf"
    assert j[0]["kennzahlen"]["kw"]["kw"] == iso.week

    _anmelden(client, "bea@example.com")
    assert client.post(f"/api/bestellungen/{b['id']}/zuruecksetzen",
                       json={"begruendung": "x"}).status_code == 403


def test_uebersicht_plan_und_abgleich(client):
    _login(client)
    mid, konten = _mandant(client)
    b = _neu(client, mid, konten["3400"])
    iso = ZAHLTAG.isocalendar()

    r = client.get(f"/api/mandanten/{mid}/bestellungen/freigabe-uebersicht?jahr={iso.year}&kw={iso.week}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [x["id"] for x in d["offen"]] == [b["id"]] and d["summe_offen"] == 1190.0
    assert d["gesamt"]["ebene"] == "gesamt"
    r = client.get(f"/api/mandanten/{mid}/bestellungen/freigabe-uebersicht")   # laufende KW
    assert r.status_code == 200
    heute_iso = HEUTE.isocalendar()
    assert (r.json()["kw"]["jahr"], r.json()["kw"]["kw"]) == (heute_iso.year, heute_iso.week)

    von, bis = ZAHLTAG - timedelta(days=3), ZAHLTAG + timedelta(days=3)
    r = client.get(f"/api/mandanten/{mid}/plan/bestellungen?konto_id={konten['3400']}"
                   f"&von={von.isoformat()}&bis={bis.isoformat()}")
    assert r.status_code == 200, r.text
    assert [x["id"] for x in r.json()] == [b["id"]] and r.json()[0]["plan_betrag"] == 1190.0
    r = client.get(f"/api/mandanten/{mid}/plan/bestellungen?konto_id=TERMIN:BESTELLUNG"
                   f"&von={von.isoformat()}&bis={bis.isoformat()}")
    assert r.status_code == 200 and r.json() == []     # nur Bestellungen ohne Konto
    assert client.get(f"/api/mandanten/{mid}/plan/bestellungen?konto_id=abc"
                      f"&von={von.isoformat()}&bis={bis.isoformat()}").status_code == 422
    assert client.get(f"/api/mandanten/{mid}/plan/bestellungen?von=gestern&bis=x").status_code == 422
    assert client.get(f"/api/mandanten/{mid}/plan/bestellungen?von={bis}&bis={von}").status_code == 422

    assert client.get(f"/api/mandanten/{mid}/bestellungen/abgleich").json() == []


# ---------------------------------------------------------------- Ändern, Stornieren, Löschen

def test_aendern_stornieren_loeschen(client):
    _login(client)
    mid, konten = _mandant(client)
    pfad = f"/api/mandanten/{mid}/bestellungen"
    basis = {"kreditor": "X", "bestelldatum": HEUTE.isoformat(), "zahldatum": ZAHLTAG.isoformat(),
             "betrag_netto": 100, "konto_id": konten["3400"]}
    assert client.post(pfad, json={**basis, "kreditor": ""}).status_code == 422
    assert client.post(pfad, json={**basis, "konto_id": konten["1200"]}).status_code == 422
    assert client.post(pfad, json={**basis, "zahldatum": "31.12.2026"}).status_code == 422
    ohne_ust = client.post(pfad, json={**basis, "ust_satz": None}).json()
    assert ohne_ust["betrag_brutto"] == 100.0 and ohne_ust["ust_satz"] is None

    b = _neu(client, mid, konten["3400"])
    r = client.patch(f"/api/bestellungen/{b['id']}", json={"betrag_netto": 2000})
    assert r.status_code == 200 and r.json()["betrag_netto"] == 2000.0
    assert client.patch(f"/api/bestellungen/{b['id']}", json={"foo": 1}).status_code == 422
    assert client.patch(f"/api/bestellungen/{b['id']}", json={"betrag_netto": -5}).status_code == 422

    # freigegeben + Betragsänderung → erneute Freigabe nötig
    assert client.post(f"/api/bestellungen/{b['id']}/entscheidung",
                       json={"entscheidung": "FREIGEBEN"}).status_code == 200
    r = client.patch(f"/api/bestellungen/{b['id']}", json={"betrag_netto": 2100})
    assert r.json()["status"] == "OFFEN"
    assert client.delete(f"/api/bestellungen/{b['id']}").status_code == 409   # schon entschieden

    r = client.post(f"/api/bestellungen/{b['id']}/stornieren", json={"begruendung": "entfällt"})
    assert r.status_code == 200 and r.json()["status"] == "STORNIERT"
    assert client.patch(f"/api/bestellungen/{b['id']}", json={"kreditor": "Y"}).status_code == 409
    assert client.post(f"/api/bestellungen/{b['id']}/stornieren").status_code == 409
    assert client.post(f"/api/bestellungen/{b['id']}/entscheidung",
                       json={"entscheidung": "FREIGEBEN"}).status_code == 409

    frisch = _neu(client, mid, konten["3400"], kreditor="Kurzlebig AG")
    assert client.delete(f"/api/bestellungen/{frisch['id']}").status_code == 200
    assert client.get(f"/api/bestellungen/{frisch['id']}").status_code == 404
    journal = client.get(f"/api/mandanten/{mid}/bestellungen/journal").json()
    geloescht = next(j for j in journal if j["aktion"] == "GELOESCHT")
    assert geloescht["bestellung_id"] is None and "Kurzlebig AG" in geloescht["bestellung_text"]

    _benutzer(client, "lea@example.com", "LESER", [mid])
    _anmelden(client, "lea@example.com")
    assert client.get(f"/api/bestellungen/{b['id']}").status_code == 200
    assert client.patch(f"/api/bestellungen/{b['id']}", json={"kreditor": "Z"}).status_code == 403
    assert client.post(f"/api/bestellungen/{b['id']}/stornieren").status_code == 403


# ---------------------------------------------------------------- Ablösung und Löschhaken

def _buchung(mid: int, betrag: str, text: str, batch_format: str | None = None) -> tuple[int, int | None]:
    """Auszahlungsbuchung 3400 an Bank 1200 (optional in einem Import)."""
    with _sitzung() as s:
        batch_id = None
        if batch_format:
            batch = models.ImportBatch(mandant_id=mid, dateiname="test", format=batch_format, anzahl=1)
            s.add(batch)
            s.flush()
            batch_id = batch.id
        bu = models.Buchung(mandant_id=mid, batch_id=batch_id, datum=HEUTE, konto_nr="3400",
                            gegenkonto_nr="1200", betrag=Decimal(betrag), sh="S", text=text)
        s.add(bu)
        s.commit()
        return bu.id, batch_id


def _status(client, bid: int) -> dict:
    return client.get(f"/api/bestellungen/{bid}").json()


def test_abloesung_und_loeschhaken(client):
    _login(client)
    mid, konten = _mandant(client)
    b = _neu(client, mid, konten["3400"])
    bid = b["id"]

    # Zahlung (Buchung) ablösen → Buchung löschen → Bestellung wieder offen
    buchung_id, _ = _buchung(mid, "1190.00", "Müller Bürobedarf Bestellung 4711")
    kandidaten = client.get(f"/api/bestellungen/{bid}/abloesung/kandidaten").json()
    assert kandidaten[0]["typ"] == "BUCHUNG" and kandidaten[0]["id"] == buchung_id
    vorschlaege = client.get(f"/api/mandanten/{mid}/bestellungen/abgleich").json()
    assert vorschlaege[0]["bestellung"]["id"] == bid and vorschlaege[0]["konfidenz"] == "SICHER"
    r = client.post(f"/api/bestellungen/{bid}/abloesung", json={"art": "ZAHLUNG", "buchung_id": buchung_id})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ABGELOEST" and r.json()["abweichung"] == 0.0
    assert any(f"#{bid}" in x for x in _audit("BESTELLUNG_ABGELOEST"))
    assert client.post(f"/api/bestellungen/{bid}/abloesung",
                       json={"art": "ZAHLUNG", "buchung_id": buchung_id}).status_code == 409
    assert client.delete(f"/api/buchungen/{buchung_id}").status_code == 200
    d = _status(client, bid)
    assert d["status"] == "OFFEN" and d["abloesungen"] == []
    assert d["journal"][0]["aktion"] == "ABLOESUNG_AUFGEHOBEN"

    # Rechnung (offener Posten) → Posten löschen
    posten = client.post(f"/api/mandanten/{mid}/posten", json={
        "art": "KREDITOR", "partner": "Müller Bürobedarf GmbH", "belegnr": "RE-4711",
        "faellig_am": ZAHLTAG.isoformat(), "betrag_brutto": 1150, "konto_id": konten["3400"]})
    assert posten.status_code == 201, posten.text
    posten_id = posten.json()["id"]
    r = client.post(f"/api/bestellungen/{bid}/abloesung", json={"art": "RECHNUNG", "posten_id": posten_id})
    assert r.status_code == 200 and r.json()["abgeloest_durch"] == "RECHNUNG"
    assert r.json()["abweichung"] == -40.0
    assert client.delete(f"/api/posten/{posten_id}").status_code == 200
    assert _status(client, bid)["status"] == "OFFEN"

    # Teilzahlung manuell, dann über die Ablösung selbst aufheben
    r = client.post(f"/api/bestellungen/{bid}/abloesung", json={
        "art": "MANUELL", "betrag": 500, "datum": HEUTE.isoformat(), "abschliessend": False})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "OFFEN" and r.json()["rest_offen"] == 690.0
    aid = r.json()["abloesungen"][0]["id"]
    assert client.post(f"/api/bestellungen/{bid}/abloesung",
                       json={"art": "MANUELL", "betrag": 10}).status_code == 422   # Datum fehlt
    r = client.delete(f"/api/bestell-abloesungen/{aid}")
    assert r.status_code == 200 and r.json()["abloesungen"] == [] and r.json()["rest_offen"] == 1190.0
    assert client.delete(f"/api/bestell-abloesungen/{aid}").status_code == 404

    # Import löschen: Buchungen (Core-Delete) und Bankumsätze
    buchung_id, batch_id = _buchung(mid, "1190.00", "Müller 4711", batch_format="DATEV")
    client.post(f"/api/bestellungen/{bid}/abloesung", json={"art": "ZAHLUNG", "buchung_id": buchung_id})
    assert _status(client, bid)["status"] == "ABGELOEST"
    assert client.delete(f"/api/importe/{batch_id}").status_code == 200
    assert _status(client, bid)["status"] == "OFFEN"

    with _sitzung() as s:
        batch = models.ImportBatch(mandant_id=mid, dateiname="auszug.sta", format="MT940", anzahl=1)
        s.add(batch)
        s.flush()
        t = models.BankTransaktion(mandant_id=mid, batch_id=batch.id, konto_id=konten["1200"],
                                   buchungstag=HEUTE, betrag=Decimal("-1190.00"),
                                   partner="Müller Bürobedarf GmbH", verwendungszweck="Best. 4711")
        s.add(t)
        s.commit()
        t_id, batch_id = t.id, batch.id
    r = client.post(f"/api/bestellungen/{bid}/abloesung", json={"art": "ZAHLUNG", "transaktion_id": t_id})
    assert r.status_code == 200 and r.json()["status"] == "ABGELOEST"
    assert client.delete(f"/api/importe/{batch_id}").status_code == 200
    assert _status(client, bid)["status"] == "OFFEN"

    # PDF-Verbuchung aufheben
    buchung_id, _ = _buchung(mid, "1190.00", "Müller 4711", batch_format="PDF")
    client.post(f"/api/bestellungen/{bid}/abloesung", json={"art": "ZAHLUNG", "buchung_id": buchung_id})
    assert _status(client, bid)["status"] == "ABGELOEST"
    r = client.post(f"/api/mandanten/{mid}/pdf-umsaetze/aufheben", json={"buchung_id": buchung_id})
    assert r.status_code == 200, r.text
    d = _status(client, bid)
    assert d["status"] == "OFFEN"
    with _sitzung() as s:
        assert s.scalar(select(func.count()).select_from(models.BestellAbloesung)) == 0

    # Eingaben
    assert client.post(f"/api/bestellungen/{bid}/abloesung",
                       json={"art": "ZAHLUNG", "buchung_id": "abc"}).status_code == 422
    assert client.post(f"/api/bestellungen/{bid}/abloesung", json={"art": "TAUSCH"}).status_code == 422
    _benutzer(client, "lea@example.com", "LESER", [mid])
    _anmelden(client, "lea@example.com")
    assert client.get(f"/api/bestellungen/{bid}/abloesung/kandidaten").status_code == 200
    assert client.post(f"/api/bestellungen/{bid}/abloesung",
                       json={"art": "MANUELL", "betrag": 1, "datum": HEUTE.isoformat()}).status_code == 403


# ---------------------------------------------------------------- Mandant, Benutzer

def test_mandantenschalter_und_benutzerkennzeichen(client):
    _login(client)
    mid, _ = _mandant(client)
    assert client.patch(f"/api/mandanten/{mid}", json={"bestellungen_offen_einplanen": False}).status_code == 200
    assert client.get(f"/api/mandanten/{mid}/ablage").json()["bestellungen_offen_einplanen"] is False
    assert client.patch(f"/api/mandanten/{mid}",
                        json={"bestellungen_offen_einplanen": "nein"}).status_code == 422

    uid = _benutzer(client, "sw@example.com", "BEARBEITER", [mid], darf_freigeben=True)
    zeile = next(b for b in client.get("/api/benutzer").json() if b["id"] == uid)
    assert zeile["darf_freigeben"] is True
    assert any("darf Bestellungen freigeben" in x for x in _audit("BENUTZER_ANGELEGT"))
    assert client.patch(f"/api/benutzer/{uid}", json={"darf_freigeben": False}).status_code == 200
    zeile = next(b for b in client.get("/api/benutzer").json() if b["id"] == uid)
    assert zeile["darf_freigeben"] is False
    assert any("Bestellfreigabe entzogen" in x for x in _audit("BENUTZER_GEAENDERT"))

    profil = client.get("/api/profil").json()
    assert profil["darf_bestellfreigabe"] is True and profil["darf_freigeben"] is False   # Admin


# ---------------------------------------------------------------- Fehlerabbildung

@pytest.mark.parametrize("text, code", [
    ("Eine Bestellung im Status „storniert“ kann nicht geändert werden", 409),
    ("Nur Bestellungen „zur Freigabe“ können entschieden werden (Status: freigegeben)", 409),
    ("Nur freigegebene oder abgelehnte Bestellungen können zurückgesetzt werden", 409),
    ("Die Bestellung hat Ablösungen und kann nicht gelöscht werden", 409),
    ("Über die Bestellung wurde bereits entschieden – bitte stornieren statt löschen", 409),
    ("Die Buchung ist bereits einer Bestellung zugeordnet", 409),
    ("Der Bankumsatz ist bereits mit offenen Posten abgeglichen", 409),
    ("Der Posten ist storniert", 409),
    ("Kreditor fehlt", 422),
    ("Unbekannter Status: X", 422),
    ("Die KW liegt in der Vergangenheit", 422),
    ("Nur Eingangsrechnungen (Kreditoren-Posten) können eine Bestellung ablösen", 422),
    ("Bitte die Ablehnung begründen", 422),
    ("Bank- und Kassenkonten sind für Bestellungen nicht zulässig", 422),
])
def test_wertfehler_zustand_oder_eingabe(text, code):
    from fastapi import HTTPException

    from app.routers.bestellungen import _fehler

    class Sitzung:
        def rollback(self):
            self.zurueck = True

    s = Sitzung()
    with pytest.raises(HTTPException) as info, _fehler(s):
        raise ValueError(text)
    assert info.value.status_code == code and info.value.detail == text and s.zurueck


# ---------------------------------------------------------------- Mandantentrennung der Ablage

def _mit_datei(client, mid: int, konto_id: int, name: str = "geheim.pdf", inhalt: bytes | None = None,
               kreditor: str = "Lieferant A") -> dict:
    r = client.post(f"/api/mandanten/{mid}/bestellungen/auslesen",
                    files={"datei": (name, inhalt or PDF, "application/pdf")})
    assert r.status_code == 200, r.text
    return _neu(client, mid, konto_id, kreditor=kreditor, token=r.json()["token"])


def test_standardordner_enthaelt_keinen_fremden_ordner(client, monkeypatch):
    """Konzern: Tochter liegt unter „holding/tochter“, später wird die Mutter
    „holding“ angelegt – ihr Standardordner ist „holding-<id>“, nicht „holding“."""
    _ki_fake(monkeypatch)
    _login(client)
    mid_a, kn_a = _mandant(client, "tochter", "Tochter GmbH")
    assert client.put(f"/api/mandanten/{mid_a}/ablage",
                      json={"typ": "LOKAL", "lokal_pfad": "holding/tochter"}).status_code == 200
    _mit_datei(client, mid_a, kn_a["3400"], inhalt=PDF + b"% Mandant A vertraulich\n")
    mid_b, _ = _mandant(client, "holding", "Holding AG")
    assert client.get(f"/api/mandanten/{mid_b}/ablage").json()["lokal_pfad"] == f"holding-{mid_b}"
    _benutzer(client, "bob@example.com", "BEARBEITER", [mid_b])
    _anmelden(client, "bob@example.com")
    r = client.get(f"/api/mandanten/{mid_b}/ablage/durchsuchen")
    assert r.status_code == 200 and r.json()["unterordner"] == [] and r.json()["dateien"] == []
    assert client.get(f"/api/mandanten/{mid_b}/ablage/durchsuchen?element=tochter").status_code == 404


def test_aehnliche_kurznamen_getrennte_ordner(client, monkeypatch):
    _ki_fake(monkeypatch)
    _login(client)
    mid_a, kn_a = _mandant(client, "nord", "Nord GmbH")
    _mit_datei(client, mid_a, kn_a["3400"])
    mid_b, _ = _mandant(client, "nord.", "Nord Punkt GmbH")
    assert client.get(f"/api/mandanten/{mid_b}/ablage").json()["lokal_pfad"] == f"nord-{mid_b}"
    _benutzer(client, "lea@example.com", "LESER", [mid_b])
    _anmelden(client, "lea@example.com")
    r = client.get(f"/api/mandanten/{mid_b}/ablage/durchsuchen")
    assert r.status_code == 200 and r.json()["dateien"] == []


def test_ueberschneidung_vor_jedem_zugriff(client, monkeypatch):
    """Ältere bzw. von außen gesetzte Überschneidung: Durchsuchen, Auslesen,
    Verknüpfen und Speichern werden mit 409 abgelehnt – ohne fremde Namen."""
    _ki_fake(monkeypatch)
    _login(client)
    mid_a, kn_a = _mandant(client, "tochter", "Tochter GmbH")
    assert client.put(f"/api/mandanten/{mid_a}/ablage",
                      json={"typ": "LOKAL", "lokal_pfad": "holding/tochter"}).status_code == 200
    a = _mit_datei(client, mid_a, kn_a["3400"])
    mid_b, kn_b = _mandant(client, "holding", "Holding AG")
    with _sitzung() as s:
        m = s.get(models.Mandant, mid_b)
        m.ablage_typ, m.ablage_pfad = "LOKAL", "holding"   # Elternordner von A
        s.commit()
    element = "tochter/" + a["datei"]["name"]
    r = client.get(f"/api/mandanten/{mid_b}/ablage/durchsuchen?element=tochter")
    assert r.status_code == 409 and "Tochter GmbH" not in r.json()["detail"]
    assert client.post(f"/api/mandanten/{mid_b}/bestellungen/auslesen-ablage",
                       json={"element": element}).status_code == 409
    r = client.post(f"/api/mandanten/{mid_b}/bestellungen", json={
        "kreditor": "X", "bestelldatum": HEUTE.isoformat(), "zahldatum": ZAHLTAG.isoformat(),
        "betrag_netto": 1, "konto_id": kn_b["3400"], "ablage_element": element})
    assert r.status_code == 409
    token = client.post(f"/api/mandanten/{mid_b}/bestellungen/auslesen",
                        files={"datei": ("b.pdf", PDF, "application/pdf")}).json()["token"]
    r = client.post(f"/api/mandanten/{mid_b}/bestellungen", json={
        "kreditor": "X", "bestelldatum": HEUTE.isoformat(), "zahldatum": ZAHLTAG.isoformat(),
        "betrag_netto": 1, "konto_id": kn_b["3400"], "token": token})
    assert r.status_code == 409
    assert client.get(f"/api/mandanten/{mid_b}/bestellungen").json()["bestellungen"] == []
    assert not (_ablage_stamm() / "holding" / "b.pdf").exists()
    # auch der andere Mandant kommt nicht mehr an seine Ablage, bis der Admin es löst
    assert client.get(f"/api/mandanten/{mid_a}/ablage/durchsuchen").status_code == 409
    assert client.put(f"/api/mandanten/{mid_b}/ablage",
                      json={"typ": "LOKAL", "lokal_pfad": ""}).status_code == 200
    assert client.get(f"/api/mandanten/{mid_a}/ablage/durchsuchen?element=").status_code == 200


def test_ordner_mit_fremden_belegen_nicht_waehlbar(client, monkeypatch):
    _ki_fake(monkeypatch)
    _login(client)
    mid_a, kn_a = _mandant(client, "alpha", "Alpha GmbH")
    assert client.put(f"/api/mandanten/{mid_a}/ablage",
                      json={"typ": "LOKAL", "lokal_pfad": "Akten/A"}).status_code == 200
    _mit_datei(client, mid_a, kn_a["3400"])
    assert client.put(f"/api/mandanten/{mid_a}/ablage",
                      json={"typ": "LOKAL", "lokal_pfad": "Akten/A2"}).status_code == 200
    mid_b, _ = _mandant(client, "beta", "Beta GmbH")
    r = client.put(f"/api/mandanten/{mid_b}/ablage", json={"typ": "LOKAL", "lokal_pfad": "akten/a"})
    assert r.status_code == 409 and "Belege" in r.json()["detail"] and "Alpha GmbH" in r.json()["detail"]
    assert client.put(f"/api/mandanten/{mid_a}/ablage",
                      json={"typ": "LOKAL", "lokal_pfad": "Akten/A"}).status_code == 200


def test_mandant_loeschen_verschiebt_ablage(client, monkeypatch):
    """Ein neuer Mandant mit gleichem Kurznamen (SQLite: auch gleiche ID) erbt
    keine Belege; der Ordner des gelöschten Mandanten wird verschoben, nie gelöscht."""
    _ki_fake(monkeypatch)
    _login(client)
    mid_a, kn_a = _mandant(client, "alt", "Alt GmbH")
    a = _mit_datei(client, mid_a, kn_a["3400"], inhalt=PDF + b"% alt\n")
    r = client.delete(f"/api/mandanten/{mid_a}", params={"bestaetigung": "alt"})
    assert r.status_code == 200, r.text
    archiv = list((_ablage_stamm() / ".geloescht").iterdir())
    assert len(archiv) == 1 and re.fullmatch(rf"alt-{mid_a}-\d{{8}}T\d{{6}}Z", archiv[0].name)
    assert (archiv[0] / a["datei"]["name"]).read_bytes() == PDF + b"% alt\n"
    assert not (_ablage_stamm() / f"alt-{mid_a}").exists()
    assert any(".geloescht/alt-" in x for x in _audit("MANDANT_GELOESCHT"))

    mid_b, _ = _mandant(client, "alt", "Ganz andere GmbH")
    _benutzer(client, "lea@example.com", "LESER", [mid_b])
    _anmelden(client, "lea@example.com")
    r = client.get(f"/api/mandanten/{mid_b}/ablage/durchsuchen")
    assert r.status_code == 200 and r.json()["dateien"] == []
    # versteckter Archivordner ist nirgends erreichbar
    _anmelden(client)
    assert client.get("/api/ablage/lokal?pfad=.geloescht").status_code == 422
    assert ".geloescht" not in [o["name"] for o in client.get("/api/ablage/lokal").json()["unterordner"]]


def test_mandant_loeschen_ohne_ablageordner(client):
    _login(client)
    mid, _ = _mandant(client, "leer", "Leer GmbH")
    assert client.delete(f"/api/mandanten/{mid}", params={"bestaetigung": "leer"}).status_code == 200
    assert not (_ablage_stamm() / ".geloescht").exists()


def test_mandant_anlegen_standardordner_belegt(client):
    _login(client)
    mid, _ = _mandant(client, "erster", "Erster GmbH")
    belegt = _ablage_stamm() / f"neu-{mid + 1}"
    belegt.mkdir(parents=True)
    (belegt / "alt.pdf").write_bytes(PDF)
    r = client.post("/api/mandanten", json={"name": "Neu GmbH", "kurzname": "neu"})
    assert r.status_code == 409 and f"neu-{mid + 1}" in r.json()["detail"]
    assert [m["kurzname"] for m in client.get("/api/mandanten").json()] == ["erster"]
    (belegt / "alt.pdf").unlink()
    assert client.post("/api/mandanten", json={"name": "Neu GmbH", "kurzname": "neu"}).status_code == 201


SP_ORDNER = {
    "fA": {"id": "fA", "name": "Akten", "folder": {"childCount": 1},
           "parentReference": {"driveId": "b!d1", "id": "wurzel1", "path": "/drives/b!d1/root:"}},
    "fB": {"id": "fB", "name": "Beta", "folder": {"childCount": 1},
           "parentReference": {"driveId": "b!d1", "id": "fA", "path": "/drives/b!d1/root:/Akten"}},
    "fC": {"id": "fC", "name": "Akten-Gamma", "folder": {"childCount": 0},
           "parentReference": {"driveId": "b!d1", "id": "wurzel1", "path": "/drives/b!d1/root:"}},
}


def test_sharepoint_ordner_ueberschneidung(client, monkeypatch):
    _login(client)
    mid_a, _ = _mandant(client, "alpha", "Alpha GmbH")
    mid_b, _ = _mandant(client, "beta", "Beta GmbH")
    _graph_einrichten(client)
    routen = {("GET", f"/v1.0/drives/b!d1/items/{k}"): v for k, v in SP_ORDNER.items()}
    routen.update({
        ("GET", "/v1.0/drives/b!d1/root"): {"id": "wurzel1", "name": "root", "root": {}, "folder": {}},
        ("GET", "/v1.0/drives/b!d1"): {"id": "b!d1", "name": "Dokumente"},
        ("GET", "/v1.0/drives/b!d2/root"): {"id": "wurzel2", "name": "root", "root": {}, "folder": {}},
        ("GET", "/v1.0/drives/b!d2"): {"id": "b!d2", "name": "Rechnungen"},
    })
    _graph_mock(monkeypatch, routen)

    def setzen(mid, drive, ordner=None):
        return client.put(f"/api/mandanten/{mid}/ablage",
                          json={"typ": "SHAREPOINT", "drive_id": drive, "ordner_id": ordner or ""})

    assert setzen(mid_a, "b!d1", "fA").status_code == 200
    # darunter, gleich, Bibliothekswurzel derselben Bibliothek → 409
    for ordner in ("fB", "fA", None):
        r = setzen(mid_b, "b!d1", ordner)
        assert r.status_code == 409 and "Alpha GmbH" in r.json()["detail"], (ordner, r.text)
    assert client.get(f"/api/mandanten/{mid_b}/ablage").json()["typ"] == "LOKAL"
    # Nachbarordner mit gleichem Namensanfang und andere Bibliothek sind frei
    assert setzen(mid_b, "b!d1", "fC").status_code == 200
    # Alpha darf nun weder die Wurzel noch darüber wählen
    assert setzen(mid_a, "b!d1", None).status_code == 409
    assert setzen(mid_a, "b!d2", None).status_code == 200
    assert setzen(mid_b, "b!d1", None).status_code == 200


def test_ablage_name_kreditor_mit_schraegstrich():
    from app.routers.bestellungen import _ablage_name

    def name(kreditor, original, mime="application/pdf"):
        return _ablage_name(models.Bestellung(bestelldatum=date(2026, 10, 1), kreditor=kreditor),
                            original, mime)

    assert name("Hansen A/S", "Bestellung Nr. 5+6 (Kopie).pdf") == \
        "2026-10-01_Hansen A_S_Bestellung Nr. 5+6 (Kopie).pdf"
    assert name("Müller GmbH / Co. KG", "angebot.pdf") == "2026-10-01_Müller GmbH _ Co. KG_angebot.pdf"
    assert name("A\\B Logistik", "auftrag.pdf") == "2026-10-01_A_B Logistik_auftrag.pdf"
    assert name("Normal GmbH", "C:\\fakepath\\x.exe") == "2026-10-01_Normal GmbH_x.pdf"
    assert name("../..", "x.pdf") == "2026-10-01_Kreditor_x.pdf"


def test_ki_url_wechsel_sendet_key_nicht_weiter(client, monkeypatch):
    _login(client)
    r = client.put("/api/system/einstellungen", json={
        "llm_url": "https://api.openai.com/v1", "llm_modell": "gpt-test", "llm_api_key": GEHEIM_KI})
    assert r.status_code == 200 and r.json()["llm"]["api_key"]["gesetzt"] is True
    gesehen: list = []

    def llm(req: httpx.Request) -> httpx.Response:
        gesehen.append(req)
        return httpx.Response(200, json={"model": "x", "choices": [{"message": {"content": '{"antwort":"ok"}'}}]})

    monkeypatch.setattr(ki, "http_client_fabrik", lambda: httpx.Client(transport=httpx.MockTransport(llm)))
    r = client.put("/api/system/einstellungen", json={"llm_url": "http://fremder-host.example:8080/v1"})
    assert r.status_code == 200, r.text
    assert r.json()["llm"]["api_key"]["gesetzt"] is False
    assert any("llm_api_key (verworfen" in x for x in _audit("SYSTEMEINSTELLUNGEN_GEAENDERT"))
    assert client.post("/api/system/ki/test").status_code == 200
    assert str(gesehen[-1].url).startswith("http://fremder-host.example:8080/")
    assert "authorization" not in gesehen[-1].headers
    # Key über http:// nach außen wird abgelehnt (422), lokal erlaubt
    r = client.put("/api/system/einstellungen", json={"llm_api_key": "sk-neu"})
    assert r.status_code == 422 and "https://" in r.json()["detail"]
    assert client.put("/api/system/einstellungen", json={
        "llm_url": "http://localhost:11434/v1", "llm_api_key": "sk-lokal"}).status_code == 200
    client.post("/api/system/ki/test")
    assert gesehen[-1].headers["authorization"] == "Bearer sk-lokal"


def _ki_mitschnitt(monkeypatch) -> list:
    """KI-Server-Attrappe: protokolliert (Ziel, Authorization) je Anfrage."""
    gesehen: list = []

    def llm(req: httpx.Request) -> httpx.Response:
        gesehen.append((f"{req.url.scheme}://{req.url.host}", req.headers.get("authorization")))
        return httpx.Response(200, json={"model": "x", "choices": [{"message": {"content": '{"antwort":"ok"}'}}]})

    monkeypatch.setattr(ki, "http_client_fabrik", lambda: httpx.Client(transport=httpx.MockTransport(llm)))
    return gesehen


def test_ki_env_key_nur_an_url_aus_umgebung(client, monkeypatch):
    """N5 a/b: LLM_API_KEY geht nicht an die erste in der Oberfläche gesetzte URL;
    mit LLM_URL nur dorthin, auch nach einem Schlüsseltausch."""
    s = get_settings()
    monkeypatch.setattr(s, "llm_api_key", "sk-UMGEBUNG-999")
    gesehen = _ki_mitschnitt(monkeypatch)
    _login(client)
    assert client.post("/api/system/ki/test").status_code == 409
    assert "LLM_URL ebenfalls in der Umgebung setzen" in client.post("/api/system/ki/test").json()["detail"]
    r = client.put("/api/system/einstellungen", json={"llm_url": "https://boese.example/v1", "llm_modell": "m"})
    assert r.status_code == 200, r.text
    assert "LLM_URL ebenfalls in der Umgebung setzen" in r.json()["llm"]["api_key_hinweis"]
    assert client.post("/api/system/ki/test").status_code == 200
    assert gesehen[-1] == ("https://boese.example", None)

    monkeypatch.setattr(s, "llm_url", "https://ki.example/v1")
    d = client.get("/api/system/einstellungen").json()["llm"]
    assert d["url"] == "https://ki.example/v1" and d["url_aus_umgebung"] is True
    assert d["api_key"]["aus_umgebung"] is True and d["api_key_hinweis"] is None
    client.post("/api/system/ki/test")
    assert gesehen[-1] == ("https://ki.example", "Bearer sk-UMGEBUNG-999")
    # URL aus der Umgebung lässt sich nicht umstellen
    r = client.put("/api/system/einstellungen", json={"llm_url": "https://boese.example/v1"})
    assert r.status_code == 422 and "LLM_URL" in r.json()["detail"]
    monkeypatch.setattr(s, "llm_api_key", "sk-UMGEBUNG-NEU")
    client.post("/api/system/ki/test")
    assert gesehen[-1] == ("https://ki.example", "Bearer sk-UMGEBUNG-NEU")
    assert all("UMGEBUNG" not in (auth or "") for ziel, auth in gesehen if ziel != "https://ki.example")


def test_ki_key_ohne_url_geht_nicht_an_erste_url(client, monkeypatch):
    """N5 c: Key bei leerer URL gespeichert (Formular sendet llm_url ""), danach
    fremde URL ohne Key → Key verworfen, nicht gesendet."""
    gesehen = _ki_mitschnitt(monkeypatch)
    _login(client)
    r = client.put("/api/system/einstellungen", json={"llm_url": "", "llm_modell": "m", "llm_api_key": GEHEIM_KI})
    assert r.status_code == 200 and r.json()["llm"]["api_key"]["gesetzt"] is True
    r = client.put("/api/system/einstellungen", json={"llm_url": "https://boese.example/v1"})
    assert r.status_code == 200 and r.json()["llm"]["api_key"]["gesetzt"] is False
    assert any("llm_api_key (verworfen" in x for x in _audit("SYSTEMEINSTELLUNGEN_GEAENDERT"))
    assert client.post("/api/system/ki/test").status_code == 200
    assert gesehen[-1] == ("https://boese.example", None)


def test_abgeloeste_bestellung_markiert_erledigende_zahlung(client):
    """N6: zwei Zahlungen ohne „vollständig abgelöst“ decken das Brutto – die zweite
    ist die erledigende (erledigt), die erste eine Teilzahlung."""
    _login(client)
    mid, konten = _mandant(client)
    bid = _neu(client, mid, konten["3400"], betrag_netto=1000, ust_satz=0)["id"]
    for betrag in (400, 600):
        r = client.post(f"/api/bestellungen/{bid}/abloesung", json={
            "art": "MANUELL", "betrag": betrag, "datum": HEUTE.isoformat(), "abschliessend": False})
        assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "ABGELOEST"
    assert [(a["betrag"], a["abschliessend"], a["erledigt"]) for a in d["abloesungen"]] == [
        (400.0, False, False), (600.0, False, True)]
    # Teilzahlung wieder aufheben → offen, keine Ablösung „erledigt“
    r = client.delete(f"/api/bestell-abloesungen/{d['abloesungen'][1]['id']}")
    assert r.status_code == 200 and r.json()["status"] == "OFFEN"
    assert [a["erledigt"] for a in r.json()["abloesungen"]] == [False]
