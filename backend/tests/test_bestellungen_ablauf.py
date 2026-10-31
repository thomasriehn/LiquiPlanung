"""Bestellungen – Ablauf und Absicherung: Status nach Aufheben von Teilzahlungen,
Exklusivität einer Zahlung (OP-Ausgleich, eindeutige Indizes), Rechnungsablösung
nur durch zahlungsrelevante Posten, gleichzeitige Statuswechsel, Sperre geänderter
Zahlungsbuchungen und erneute Freigabe bei wesentlichen Änderungen.

Alle Daten synthetisch."""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app import models
from app.config import get_settings
from app.services import ausgleich
from app.services import bestellungen as svc
from app.services.liquiditaet import plan_fluesse
from app.services.zugaenge import DateiRef

from test_api import _login
from test_bestellungen import HEUTE, START, _benutzer, _bestellung, _zahlung
from test_liquiditaet import _konto, _mandant

ZAHLTAG = date(2026, 8, 20)   # Donnerstag, KW 34
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _freigegeben(db, m, u, netto="1000", **extra) -> models.Bestellung:
    """Freigegebene Bestellung ohne USt (brutto = netto), Zahldatum 20.08."""
    extra.setdefault("zahldatum", ZAHLTAG)
    return _bestellung(db, m, u, status="FREIGEGEBEN", netto=netto, ust_satz=None, **extra)


def _projektion(db, m, b) -> dict:
    db.commit()
    fl = plan_fluesse(db, m, START, START + timedelta(days=34), heute=HEUTE, nur_offen=True)
    return {d: v for d, v in fl.get(b.konto_id, {}).items() if v}


def _posten(db, m, *, partner="Telko GmbH", betrag="1000", faellig=date(2026, 8, 25),
            rechnungsdatum=date(2026, 8, 1), **extra) -> models.OffenerPosten:
    p = models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner=partner, belegnr="R-2",
                             rechnungsdatum=rechnungsdatum, faellig_am=faellig,
                             betrag_brutto=Decimal(betrag), konto_id=_konto(db, m, "4920").id,
                             status="OFFEN", **extra)
    db.add(p)
    db.flush()
    return p


def _letztes_journal(db, b) -> models.BestellJournal:
    return db.scalars(select(models.BestellJournal).where(
        models.BestellJournal.bestellung_id == b.id).order_by(models.BestellJournal.id.desc())).first()


# ---------------------------------------------------------------------------
# S1: Status nach Aufheben einer Teilzahlung neu bewerten
# ---------------------------------------------------------------------------

def test_aufheben_frueherer_teilzahlung_bewertet_status_neu(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _freigegeben(db, m, u)
    z1 = _zahlung(db, m, date(2026, 8, 4), "400")
    z2 = _zahlung(db, m, date(2026, 8, 5), "600")
    svc.abloesen(db, b, u, art="ZAHLUNG", buchung_id=z1.id, abschliessend=False)
    a2 = svc.abloesen(db, b, u, art="ZAHLUNG", buchung_id=z2.id, abschliessend=False)
    # die Summe deckt das Brutto → abgelöst; gespeichert bleibt „nicht abschließend“
    assert b.status == "ABGELOEST" and a2.abschliessend is False
    assert _projektion(db, m, b) == {}

    # erste Teilzahlung war falsch zugeordnet (Import gelöscht) → 400 € wieder offen
    assert svc.buchungen_entfernt(db, [z1.id], u) == 1
    assert b.status == "FREIGEGEBEN" and b.abgeloest_am is None and b.abgeloest_durch is None
    d = svc.als_dict(db, b)
    assert d["rest_geplant"] == 400.0 and d["abweichung"] is None
    j = _letztes_journal(db, b)
    assert (j.aktion, j.status_vorher, j.status_nachher) == (
        "ABLOESUNG_AUFGEHOBEN", "ABGELOEST", "FREIGEGEBEN")
    assert _projektion(db, m, b) == {ZAHLTAG: Decimal("-400")}

    # die richtige Zahlung lässt sich wieder zuordnen
    z3 = _zahlung(db, m, date(2026, 8, 6), "400")
    svc.abloesen(db, b, u, art="ZAHLUNG", buchung_id=z3.id, abschliessend=False)
    assert b.status == "ABGELOEST" and b.abgeloest_am == date(2026, 8, 6)


def test_ausdruecklich_abschliessende_abloesung_bleibt_nach_aufheben(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _freigegeben(db, m, u)
    a1 = svc.abloesen(db, b, u, art="MANUELL", betrag=Decimal("400"), datum=date(2026, 8, 3),
                      abschliessend=False)
    a2 = svc.abloesen(db, b, u, art="MANUELL", betrag=Decimal("300"), datum=date(2026, 8, 4),
                      abschliessend=True)   # Minderlieferung: ausdrücklich erledigt
    assert b.status == "ABGELOEST" and a2.abschliessend is True
    svc.abloesung_aufheben(db, a1, u)
    assert b.status == "ABGELOEST" and b.abgeloest_am == date(2026, 8, 4)
    assert svc.als_dict(db, b)["abweichung"] == -700.0
    svc.abloesung_aufheben(db, a2, u)
    assert b.status == "FREIGEGEBEN" and svc.als_dict(db, b)["rest_geplant"] == 1000.0


def test_aufheben_der_deckenden_teilzahlung(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _bestellung(db, m, u, netto="1000", ust_satz=None, zahldatum=ZAHLTAG)   # OFFEN
    svc.abloesen(db, b, u, art="MANUELL", betrag=Decimal("400"), datum=date(2026, 8, 3),
                 abschliessend=False)
    a2 = svc.abloesen(db, b, u, art="MANUELL", betrag=Decimal("600"), datum=date(2026, 8, 4),
                      abschliessend=False)
    assert b.status == "ABGELOEST"
    svc.abloesung_aufheben(db, a2, u)
    assert b.status == "OFFEN" and svc.als_dict(db, b)["rest_offen"] == 600.0


# ---------------------------------------------------------------------------
# S2: Bankumsatz einer Bestellung nicht zusätzlich im OP-Ausgleich
# ---------------------------------------------------------------------------

def test_op_ausgleich_ignoriert_umsatz_einer_bestellung(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _freigegeben(db, m, u)
    t = models.BankTransaktion(mandant_id=m.id, konto_id=_konto(db, m, "1200").id,
                               buchungstag=date(2026, 8, 4), betrag=Decimal("-1000"),
                               partner="Telko GmbH", verwendungszweck="Rechnung R-2")
    db.add(t)
    p = _posten(db, m)
    # ohne Ablösung wäre (t, R-2) ein Vorschlag
    assert any(v["transaktion"]["id"] == t.id for v in ausgleich.vorschlaege(db, m))
    svc.abloesen(db, b, u, art="ZAHLUNG", transaktion_id=t.id)
    assert not any(v["transaktion"]["id"] == t.id for v in ausgleich.vorschlaege(db, m))
    with pytest.raises(ValueError, match="einer Bestellung zugeordnet"):
        ausgleich.gleiche_aus(db, m, [(t.id, [p.id])])
    assert p.status == "OFFEN" and not t.zuordnungen


# ---------------------------------------------------------------------------
# S3: Rechnungsablösung nur durch zahlungsrelevante Kreditoren-Posten
# ---------------------------------------------------------------------------

def test_insolvenzforderung_loest_nicht_ab_und_ist_kein_kandidat(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _freigegeben(db, m, u)
    p = _posten(db, m, forderungsklasse="INSOLVENZFORDERUNG", faellig=ZAHLTAG)
    assert all(k["id"] != p.id for k in svc.abloese_kandidaten(db, m, b) if k["typ"] == "POSTEN")
    assert all(v["kandidat"]["id"] != p.id for v in svc.abloese_vorschlaege(db, m)
               if v["kandidat"]["typ"] == "POSTEN")
    with pytest.raises(ValueError, match="Insolvenzforderung"):
        svc.abloesen(db, b, u, art="RECHNUNG", posten_id=p.id)
    assert b.status == "FREIGEGEBEN" and _projektion(db, m, b) == {ZAHLTAG: Decimal("-1000")}


def test_alte_rechnung_ist_kein_ablosekandidat(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _freigegeben(db, m, u)            # bestellt 27.07.
    alt = _posten(db, m, rechnungsdatum=date(2026, 5, 1), faellig=date(2026, 5, 15),
                  forderungsklasse="MASSE")
    neu = _posten(db, m, rechnungsdatum=date(2026, 8, 1), faellig=ZAHLTAG)
    ids = {k["id"] for k in svc.abloese_kandidaten(db, m, b) if k["typ"] == "POSTEN"}
    assert neu.id in ids and alt.id not in ids
    ohne_datum = _posten(db, m, rechnungsdatum=None, faellig=date(2026, 8, 28))
    assert ohne_datum.id in {k["id"] for k in svc.abloese_kandidaten(db, m, b) if k["typ"] == "POSTEN"}


@pytest.mark.parametrize("feld, wert, grund", [
    ("status", "STORNIERT", "Rechnung storniert"),
    ("art", "DEBITOR", "keine Eingangsrechnung"),
    ("forderungsklasse", "INSOLVENZFORDERUNG", "Insolvenzforderung"),
])
def test_posten_aenderung_hebt_rechnungsabloesung_auf(db, feld, wert, grund):
    m = _mandant(db)
    u = _benutzer(db)
    b = _freigegeben(db, m, u)
    p = _posten(db, m, faellig=ZAHLTAG)
    svc.abloesen(db, b, u, art="RECHNUNG", posten_id=p.id)
    assert b.status == "ABGELOEST"
    p.notiz = "geprüft"
    assert svc.posten_geaendert(db, p, u) == 0      # trägt die Zahlung weiter
    assert b.status == "ABGELOEST"
    setattr(p, feld, wert)
    assert svc.posten_geaendert(db, p, u) == 1
    assert b.status == "FREIGEGEBEN" and b.abloesungen == []
    assert grund in _letztes_journal(db, b).begruendung
    assert svc.als_dict(db, b)["rest_geplant"] == 1000.0
    if feld != "art":   # als Debitor stünde der Posten mit +1.000 auf demselben Konto
        assert _projektion(db, m, b) == {ZAHLTAG: Decimal("-1000")}


# ---------------------------------------------------------------------------
# S4: Exklusivität und gleichzeitige Statuswechsel
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spalte", ["buchung_id", "transaktion_id"])
def test_eindeutiger_index_je_zahlung(db, spalte):
    m = _mandant(db)
    u = _benutzer(db)
    b1 = _freigegeben(db, m, u)
    b2 = _freigegeben(db, m, u, bestellnummer="B-4712")
    if spalte == "buchung_id":
        quelle = _zahlung(db, m, date(2026, 8, 4), "1000").id
    else:
        t = models.BankTransaktion(mandant_id=m.id, konto_id=_konto(db, m, "1200").id,
                                   buchungstag=date(2026, 8, 4), betrag=Decimal("-1000"))
        db.add(t)
        db.flush()
        quelle = t.id
    for b in (b1, b2):
        db.add(models.BestellAbloesung(mandant_id=m.id, bestellung_id=b.id, art="ZAHLUNG",
                                       betrag=Decimal("1000"), datum=date(2026, 8, 4),
                                       **{spalte: quelle}))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_mehrere_ablosungen_ohne_zahlung_erlaubt(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _freigegeben(db, m, u)
    for tag in (date(2026, 8, 3), date(2026, 8, 4)):
        svc.abloesen(db, b, u, art="MANUELL", betrag=Decimal("100"), datum=tag, abschliessend=False)
    db.flush()
    assert len(b.abloesungen) == 2


def _anderswo_geaendert(db, b, status: str) -> None:
    """Simuliert eine zweite Anfrage, die inzwischen committet hat (das geladene
    Objekt behält den alten Status)."""
    db.execute(update(models.Bestellung).where(models.Bestellung.id == b.id)
               .values(status=status).execution_options(synchronize_session=False))


@pytest.mark.parametrize("vorher, anderswo, aktion", [
    ("OFFEN", "ABGELEHNT", lambda db, b, u: svc.entscheiden(db, b, u, "FREIGEBEN", None, {})),
    ("FREIGEGEBEN", "OFFEN", lambda db, b, u: svc.zuruecksetzen(db, b, u, "Korrektur")),
    ("FREIGEGEBEN", "ABGELOEST", lambda db, b, u: svc.stornieren(db, b, u, None)),
    ("FREIGEGEBEN", "STORNIERT", lambda db, b, u: svc.abloesen(
        db, b, u, art="MANUELL", betrag=Decimal("100"), datum=HEUTE, abschliessend=False)),
    ("FREIGEGEBEN", "OFFEN", lambda db, b, u: svc.aendern(db, b, u, {"betrag_netto": 1200})),
])
def test_statuswechsel_scheitert_bei_gleichzeitiger_aenderung(db, vorher, anderswo, aktion):
    m = _mandant(db)
    u = _benutzer(db)
    b = _bestellung(db, m, u, status=vorher, netto="1000", ust_satz=None, zahldatum=ZAHLTAG)
    db.commit()
    _anderswo_geaendert(db, b, anderswo)
    assert b.status == vorher
    with pytest.raises(ValueError, match="bereits von anderer Seite"):
        aktion(db, b, u)
    db.rollback()
    assert db.scalar(select(models.Bestellung.status).where(models.Bestellung.id == b.id)) == vorher


# ---------------------------------------------------------------------------
# Neufreigabe bei wesentlichen Änderungen
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("daten", [
    {"kreditor": "TELKO GmbH"},                  # Schreibkorrektur
    {"kreditor": "Telko-GmbH "},
    {"bestellnummer": "B-4711-a"},
    {"beschreibung": "Router"},
    {"bestelldatum": "2026-07-28"},
    {"lieferdatum": "2026-08-10"},
    {"zahldatum": "2026-08-17"},                 # Montag derselben KW 34
    {"zahldatum": "2026-08-23"},                 # Sonntag derselben KW 34
])
def test_korrektur_behaelt_freigabe(db, daten):
    m = _mandant(db)
    u = _benutzer(db)
    b = _freigegeben(db, m, u)
    assert svc.aendern(db, b, u, daten) == list(daten)
    assert b.status == "FREIGEGEBEN" and b.entschieden_am is not None
    j = _letztes_journal(db, b)
    assert j.aktion == "GEAENDERT" and "erneute Freigabe" not in j.begruendung


@pytest.mark.parametrize("daten", [
    {"kreditor": "Kabel Nord AG"},
    {"zahldatum": "2026-08-24"},                 # Montag KW 35
    {"zahldatum": "2026-08-16"},                 # Sonntag KW 33
    {"betrag_netto": 1100},
])
def test_wesentliche_aenderung_erfordert_neue_freigabe(db, daten):
    m = _mandant(db)
    u = _benutzer(db)
    b = _freigegeben(db, m, u)
    svc.aendern(db, b, u, daten)
    assert b.status == "OFFEN" and b.entschieden_am is None and b.entschieden_von is None
    j = _letztes_journal(db, b)
    assert (j.aktion, j.status_vorher, j.status_nachher) == ("GEAENDERT", "FREIGEGEBEN", "OFFEN")
    assert "erneute Freigabe erforderlich" in j.begruendung


def _datei(name: str) -> DateiRef:
    return DateiRef(ablage="LOKAL", name=name, mime="application/pdf", groesse=10, pfad=f"x/{name}")


def test_datei_ersetzen_erfordert_neue_freigabe(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _freigegeben(db, m, u)
    svc.datei_setzen(db, b, u, _datei("angebot.pdf"))      # nachgereicht: Freigabe bleibt
    assert b.status == "FREIGEGEBEN"
    svc.datei_setzen(db, b, u, _datei("angebot-neu.pdf"))  # ersetzt
    assert b.status == "OFFEN" and b.entschieden_am is None
    j = _letztes_journal(db, b)
    assert (j.aktion, j.status_vorher, j.status_nachher) == ("DATEI", "FREIGEGEBEN", "OFFEN")
    assert "angebot.pdf → angebot-neu.pdf" in j.begruendung and "erneute Freigabe" in j.begruendung
    svc.datei_setzen(db, b, u, _datei("angebot-3.pdf"))    # OFFEN bleibt OFFEN
    assert b.status == "OFFEN"


# ---------------------------------------------------------------------------
# API: Hooks, Sperren, 409 bei gleichzeitigen Anfragen
# ---------------------------------------------------------------------------

API_HEUTE = date.today()
API_ZAHLTAG = API_HEUTE + timedelta(days=21)


def _sitzung():
    import app.database as db_modul

    return db_modul.SessionLocal()


def _api_mandant(client) -> tuple[int, dict]:
    r = client.post("/api/mandanten", json={"name": "Ablauf GmbH", "kurzname": "ablauf"})
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    konten = client.get(f"/api/mandanten/{mid}/konten").json()["konten"]
    return mid, {k["nummer"]: k["id"] for k in konten}


def _api_bestellung(client, mid: int, konto_id: int, **extra) -> dict:
    daten = {"kreditor": "Müller Bürobedarf GmbH", "bestellnummer": "4711",
             "bestelldatum": API_HEUTE.isoformat(), "zahldatum": API_ZAHLTAG.isoformat(),
             "betrag_netto": 1000, "ust_satz": None, "konto_id": konto_id, **extra}
    r = client.post(f"/api/mandanten/{mid}/bestellungen", json=daten)
    assert r.status_code == 201, r.text
    return r.json()


def _api_buchung(mid: int, betrag: str, batch_id: int | None = None) -> int:
    with _sitzung() as s:
        bu = models.Buchung(mandant_id=mid, batch_id=batch_id, datum=API_HEUTE, konto_nr="3400",
                            gegenkonto_nr="1200", betrag=Decimal(betrag), sh="S", text="Toner 4711")
        s.add(bu)
        s.commit()
        return bu.id


def test_api_posten_storniert_hebt_rechnungsabloesung_auf(client):
    _login(client)
    mid, konten = _api_mandant(client)
    b = _api_bestellung(client, mid, konten["3400"])
    posten = client.post(f"/api/mandanten/{mid}/posten", json={
        "art": "KREDITOR", "partner": "Müller Bürobedarf GmbH", "belegnr": "RE-4711",
        "rechnungsdatum": API_HEUTE.isoformat(), "faellig_am": API_ZAHLTAG.isoformat(),
        "betrag_brutto": 1000, "konto_id": konten["3400"]})
    assert posten.status_code == 201, posten.text
    pid = posten.json()["id"]
    r = client.post(f"/api/bestellungen/{b['id']}/abloesung", json={"art": "RECHNUNG", "posten_id": pid})
    assert r.status_code == 200 and r.json()["status"] == "ABGELOEST"
    assert client.patch(f"/api/posten/{pid}", json={"status": "STORNIERT"}).status_code == 200
    d = client.get(f"/api/bestellungen/{b['id']}").json()
    assert d["status"] == "OFFEN" and d["abloesungen"] == []
    assert d["journal"][0]["aktion"] == "ABLOESUNG_AUFGEHOBEN"
    assert "Rechnung storniert" in d["journal"][0]["begruendung"]


def test_api_op_ausgleich_lehnt_umsatz_einer_bestellung_ab(client):
    _login(client)
    mid, konten = _api_mandant(client)
    b = _api_bestellung(client, mid, konten["3400"])
    with _sitzung() as s:
        t = models.BankTransaktion(mandant_id=mid, konto_id=konten["1200"], buchungstag=API_HEUTE,
                                   betrag=Decimal("-1000"), partner="Müller Bürobedarf GmbH")
        s.add(t)
        s.commit()
        tid = t.id
    posten = client.post(f"/api/mandanten/{mid}/posten", json={
        "art": "KREDITOR", "partner": "Müller Bürobedarf GmbH", "belegnr": "RE-9",
        "faellig_am": API_ZAHLTAG.isoformat(), "betrag_brutto": 1000, "konto_id": konten["3400"]})
    pid = posten.json()["id"]
    r = client.post(f"/api/bestellungen/{b['id']}/abloesung", json={"art": "ZAHLUNG", "transaktion_id": tid})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/mandanten/{mid}/op-ausgleich",
                    json={"paare": [{"transaktion_id": tid, "posten_id": pid}]})
    assert r.status_code == 409 and "Bestellung" in r.json()["detail"]


def test_api_gleichzeitige_abloesung_derselben_buchung_409(client, monkeypatch):
    _login(client)
    mid, konten = _api_mandant(client)
    b1 = _api_bestellung(client, mid, konten["3400"])
    b2 = _api_bestellung(client, mid, konten["3400"], bestellnummer="4712")
    buchung_id = _api_buchung(mid, "1000.00")
    assert client.post(f"/api/bestellungen/{b1['id']}/abloesung",
                       json={"art": "ZAHLUNG", "buchung_id": buchung_id}).status_code == 200
    # zweite Anfrage hat ihre Prüfung vor dem Commit der ersten bestanden
    monkeypatch.setattr(svc, "_belegt", lambda *a, **k: False)
    r = client.post(f"/api/bestellungen/{b2['id']}/abloesung",
                    json={"art": "ZAHLUNG", "buchung_id": buchung_id})
    assert r.status_code == 409 and "bereits einer Bestellung" in r.json()["detail"]
    with _sitzung() as s:
        assert len(list(s.scalars(select(models.BestellAbloesung).where(
            models.BestellAbloesung.buchung_id == buchung_id)))) == 1
        assert s.get(models.Bestellung, b2["id"]).status == "OFFEN"


def test_api_gleichzeitige_entscheidung_409(client, monkeypatch):
    _login(client)
    mid, konten = _api_mandant(client)
    b = _api_bestellung(client, mid, konten["3400"])
    original = svc.bestellung_kennzahlen
    with _sitzung() as s:
        postgres = s.get_bind().dialect.name == "postgresql"

    def mit_zweiter_anfrage(*args, **kwargs):
        ergebnis = original(*args, **kwargs)
        with _sitzung() as s:
            if postgres:
                # Zeilensperre der ersten Anfrage: ein zweiter Freigeber muss warten
                with pytest.raises(OperationalError, match="LockNotAvailable|could not obtain lock"):
                    s.get(models.Bestellung, b["id"], with_for_update={"nowait": True})
                return ergebnis
            # SQLite ohne Zeilensperre: währenddessen lehnt ein zweiter Freigeber ab und committet
            x = s.get(models.Bestellung, b["id"])
            svc.entscheiden(s, x, None, "ABLEHNEN", "zu teuer", {})
            s.commit()
        return ergebnis

    monkeypatch.setattr(svc, "bestellung_kennzahlen", mit_zweiter_anfrage)
    r = client.post(f"/api/bestellungen/{b['id']}/entscheidung", json={"entscheidung": "FREIGEBEN"})
    if postgres:
        # die erste Anfrage gewinnt; die wartende zweite sieht danach den neuen Status
        assert r.status_code == 200, r.text
        monkeypatch.setattr(svc, "bestellung_kennzahlen", original)
        r = client.post(f"/api/bestellungen/{b['id']}/entscheidung",
                        json={"entscheidung": "ABLEHNEN", "begruendung": "zu teuer"})
        sieger, verlierer = "FREIGEGEBEN", "ABGELEHNT"
    else:
        sieger, verlierer = "ABGELEHNT", "FREIGEGEBEN"
    assert r.status_code == 409, r.text
    d = client.get(f"/api/bestellungen/{b['id']}").json()
    assert d["status"] == sieger
    aktionen = [j["aktion"] for j in d["journal"]]
    assert aktionen.count(sieger) == 1 and aktionen.count(verlierer) == 0


def test_api_buchung_einer_bestellung_gesperrt(client):
    _login(client)
    mid, konten = _api_mandant(client)
    b = _api_bestellung(client, mid, konten["3400"])
    with _sitzung() as s:
        batch = models.ImportBatch(mandant_id=mid, dateiname="synth.csv", format="DATEV", anzahl=2)
        s.add(batch)
        s.commit()
        batch_id = batch.id
    zugeordnet = _api_buchung(mid, "400.00", batch_id)
    frei = _api_buchung(mid, "50.00", batch_id)
    r = client.post(f"/api/bestellungen/{b['id']}/abloesung",
                    json={"art": "ZAHLUNG", "buchung_id": zugeordnet, "abschliessend": False})
    assert r.status_code == 200 and r.json()["rest_offen"] == 600.0

    saetze = {s["id"]: s for s in client.get(f"/api/importe/{batch_id}/saetze").json()["buchungen"]}
    assert saetze[zugeordnet]["bestellung_id"] == b["id"] and saetze[frei]["bestellung_id"] is None

    pfad = f"/api/buchungen/{zugeordnet}"
    for aenderung in ({"betrag": 40}, {"sh": "H"}, {"datum": (API_HEUTE - timedelta(days=1)).isoformat()},
                      {"konto_nr": "3401"}, {"gegenkonto_nr": "1210"}):
        r = client.patch(pfad, json=aenderung)
        assert r.status_code == 422 and "Ablösung aufheben" in r.json()["detail"], aenderung
    # unveränderte Werte und Text/Beleg bleiben änderbar
    assert client.patch(pfad, json={"betrag": 400, "sh": "S", "text": "Toner Nr. 4711",
                                    "belegfeld": "RE-1"}).status_code == 200
    assert client.patch(f"/api/buchungen/{frei}", json={"betrag": 55}).status_code == 200
    with _sitzung() as s:
        bu = s.get(models.Buchung, zugeordnet)
        assert (bu.betrag, bu.sh, bu.text) == (Decimal("400.00"), "S", "Toner Nr. 4711")
    assert client.get(f"/api/bestellungen/{b['id']}").json()["rest_offen"] == 600.0

    # nach dem Aufheben der Ablösung wieder frei
    aid = client.get(f"/api/bestellungen/{b['id']}").json()["abloesungen"][0]["id"]
    assert client.delete(f"/api/bestell-abloesungen/{aid}").status_code == 200
    assert client.patch(pfad, json={"betrag": 40}).status_code == 200


def test_importseite_sperrt_felder_zugeordneter_buchungen():
    vorlage = (Path(__file__).resolve().parent.parent / "app" / "templates" / "import.html").read_text(
        encoding="utf-8")
    assert "s.bestellung_id" in vorlage and "Ablösung aufheben und neu zuordnen" in vorlage


def test_api_datei_ersetzen_setzt_freigabe_zurueck(client, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "ablage_verzeichnis", str(tmp_path / "ablage"))
    _login(client)
    mid, konten = _api_mandant(client)
    b = _api_bestellung(client, mid, konten["3400"])
    pfad = f"/api/bestellungen/{b['id']}/datei"
    assert client.post(pfad, files={"datei": ("angebot.pdf", PDF, "application/pdf")}).status_code == 200
    r = client.post(f"/api/bestellungen/{b['id']}/entscheidung", json={"entscheidung": "FREIGEBEN"})
    assert r.status_code == 200 and r.json()["status"] == "FREIGEGEBEN"
    r = client.post(pfad, files={"datei": ("angebot-neu.pdf", PDF, "application/pdf")})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "OFFEN" and r.json()["entschieden_am"] is None
    j = client.get(f"/api/bestellungen/{b['id']}").json()["journal"][0]
    assert (j["aktion"], j["status_vorher"], j["status_nachher"]) == ("DATEI", "FREIGEGEBEN", "OFFEN")
