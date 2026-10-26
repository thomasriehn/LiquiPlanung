"""KPI-Seite: Kennzahlen, Soll/Ist-Liquidität, Planvergleich, Sondereffekte."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import models
from app.services.kpi import kpi_daten
from app.services.liquiditaet import erstelle_snapshot

from test_api import _login
from test_liquiditaet import HEUTE, START, _konto, _mandant


@pytest.fixture()
def szenario(db):
    """Bank 10.000 € am Vortag des Fensters, Ist: +1.190 Erlös, −500 Material;
    Planstand vom Fensterbeginn kennt nur den Beratungs-Kreditor (−300);
    danach kommt eine Forderung über 2.000 € hinzu."""
    m = _mandant(db)
    bank = _konto(db, m, "1200")
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=bank.id,
                          datum=START - timedelta(days=1), wert=Decimal("10000")))
    db.add(models.Buchung(mandant_id=m.id, datum=START, konto_nr="1200", gegenkonto_nr="8400",
                          betrag=Decimal("1190"), sh="S"))
    db.add(models.Buchung(mandant_id=m.id, datum=START + timedelta(days=1), konto_nr="3400",
                          gegenkonto_nr="1200", betrag=Decimal("500"), sh="S"))
    db.add(models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="Berater",
                                faellig_am=date(2026, 8, 20), betrag_brutto=Decimal("300"),
                                konto_id=_konto(db, m, "4950").id))
    db.commit()
    snap = erstelle_snapshot(db, m, start=START, wochen=4, heute=START, kommentar="Antrag")
    db.add(models.OffenerPosten(mandant_id=m.id, art="DEBITOR", partner="Kunde",
                                faellig_am=date(2026, 8, 25), betrag_brutto=Decimal("2000"),
                                konto_id=_konto(db, m, "8400").id))
    db.commit()
    return m, snap


def test_kpi_liquiditaet_soll_ist(db, szenario):
    m, snap = szenario
    d = kpi_daten(db, m, start=START, wochen=4, heute=HEUTE)
    assert d["referenz"]["id"] == snap.id and d["vergleich"]["id"] == snap.id
    woche1 = d["liquiditaet_wochen"][0]
    assert woche1["bis"] == "2026-08-09" and woche1["prognose"] is True
    assert woche1["wert"] == 10690.0
    # Soll: Ist-Bestand am Vortag des Planstands + geplante Flüsse (bis 09.08. keine)
    assert woche1["soll"] == 10000.0
    assert d["liquiditaet_wochen"][2]["soll"] == 9700.0        # Berater am 20.08.

    k = d["kennzahlen"]
    assert k["liquiditaet_heute"] == 10690.0
    # noch keine Woche abgeschlossen -> Soll/Ist zum heutigen Tag
    assert k["kw_sollist"] == {"label": "heute", "bis": "2026-08-05", "ist": 10690.0,
                               "soll": 10000.0, "abweichung": 690.0}
    assert k["liquiditaet_ende"] == 10690.0 - 300 + 2000
    assert k["unterdeckung_am"] is None
    assert (k["forderungen_offen"], k["verbindlichkeiten_offen"]) == (2000.0, 300.0)


def test_kpi_planvergleich_je_kategorie(db, szenario):
    m, _ = szenario
    d = kpi_daten(db, m, start=START, wochen=4, heute=HEUTE)
    veraenderung = {k["code"]: k["veraenderung"] for k in d["kategorien"]}
    # Erlöse: Ist 1.190 + neue Forderung 2.000 – im Planstand nicht enthalten
    assert veraenderung["E_UMSATZ"] == 3190.0
    assert veraenderung["A_MAT"] == -500.0
    assert veraenderung["A_SONST"] == 0.0                       # Berater unverändert
    umsatz = next(k for k in d["kategorien"] if k["code"] == "E_UMSATZ")
    assert umsatz["je_monat"] == {"2026-08": 3190.0}
    monat = d["monate"][0]
    assert (monat["schluessel"], monat["status"]) == ("2026-08", "IST/PLAN")
    assert monat["einzahlungen"] == 3190.0 and monat["auszahlungen"] == -800.0
    assert monat["liquiditaet_ende"] - monat["vergleich_liquiditaet_ende"] == pytest.approx(3190 - 500)


def test_kpi_sondereffekte(db, szenario):
    m, _ = szenario
    sonst = _konto(db, m, "4950").gruppe
    sonst.sondereffekt = True
    db.commit()
    d = kpi_daten(db, m, start=START, wochen=4, heute=HEUTE)
    k = d["kennzahlen"]
    assert k["umsatzrentabilitaet"] == round((3190 - 800) / 3190, 4)
    assert k["umsatzrentabilitaet_ohne_effekte"] == round((3190 - 500) / 3190, 4)
    assert sonst.name in k["sondereffekt_gruppen"]
    anteile = {g["code"]: (g["anteil"], g["anteil_ohne"]) for g in d["auszahlungen_gruppen"]}
    assert anteile == {"A_MAT": (0.625, 1.0), "A_SONST": (0.375, None)}
    assert d["einzahlungen_konten"][0]["name"].startswith("8400") and d["einzahlungen_konten"][0]["anteil"] == 1.0


def test_kpi_ohne_planstand(db):
    m = _mandant(db)
    db.commit()
    d = kpi_daten(db, m, start=START, wochen=2, heute=HEUTE)
    assert d["referenz"] is None and d["vergleich"] is None and d["kategorien"] == []
    assert all(w["soll"] is None for w in d["liquiditaet_wochen"])
    assert d["kennzahlen"]["kw_sollist"] is None


def test_kpi_api_seite_und_sondereffekt_kennzeichen(client):
    _login(client)
    mid = client.post("/api/mandanten", json={"name": "KPI GmbH", "kurzname": "kpi"}).json()["id"]
    gruppen = client.get(f"/api/mandanten/{mid}/konten").json()["gruppen"]
    assert all(g["sondereffekt"] is False for g in gruppen)
    sonst = next(g for g in gruppen if g["code"] == "A_SONST")
    assert client.patch(f"/api/gruppen/{sonst['id']}", json={"sondereffekt": True}).status_code == 200
    assert client.post(f"/api/mandanten/{mid}/gruppen", json={
        "code": "A_INSO", "name": "Insolvenzspezifische Kosten", "sondereffekt": True,
    }).status_code in (200, 201)
    sonder = {g["code"]: g["sondereffekt"] for g in client.get(f"/api/mandanten/{mid}/konten").json()["gruppen"]}
    assert sonder["A_SONST"] and sonder["A_INSO"] and not sonder["A_MAT"]

    antwort = client.get(f"/api/mandanten/{mid}/kpi", params={"wochen": 13})
    assert antwort.status_code == 200, antwort.text
    daten = antwort.json()
    assert {"kennzahlen", "liquiditaet_wochen", "monate", "kategorien",
            "einzahlungen_konten", "auszahlungen_gruppen"} <= set(daten)
    assert client.get(f"/api/mandanten/{mid}/kpi", params={"vergleich_id": 999}).status_code == 404
    assert client.get(f"/api/mandanten/{mid}/kpi", params={"wochen": 40}).status_code == 422
    seite = client.get(f"/mandanten/{mid}/kpi")
    assert seite.status_code == 200 and "diagramme.js" in seite.text


def test_kpi_pdf_mit_planvergleich(db, szenario):
    from io import BytesIO

    from pypdf import PdfReader

    from app.services.kpi_pdf import euro_kompakt, kpi_pdf

    m, _ = szenario
    pdf = kpi_pdf(kpi_daten(db, m, start=START, wochen=4, heute=HEUTE), m)
    text = "\n".join(s.extract_text() for s in PdfReader(BytesIO(pdf)).pages)
    for teil in ("KPI-Übersicht", "Liquidität je Wochenende: Soll / Ist", "Veränderung je Kategorie",
                 "Planvergleich: aktueller Stand gegenüber Antrag", "Monatsübersicht",
                 "Wertetabellen", "10.690,00 €"):
        assert teil in text, teil
    assert (euro_kompakt(950), euro_kompakt(12500), euro_kompakt(-125000), euro_kompakt(1.25e6)) == (
        "950", "12,5 T", "-125 T", "1,2 Mio.")


def test_kpi_pdf_api_ohne_daten(client):
    from io import BytesIO

    from pypdf import PdfReader

    _login(client)
    mid = client.post("/api/mandanten", json={"name": "Leer GmbH", "kurzname": "leer"}).json()["id"]
    r = client.get(f"/api/mandanten/{mid}/export/kpi.pdf?wochen=4")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    text = "\n".join(s.extract_text() for s in PdfReader(BytesIO(r.content)).pages)
    assert "Noch kein eingefrorener Planstand" in text
    assert client.get(f"/api/mandanten/{mid}/export/kpi.pdf?vergleich_id=999").status_code == 404
    assert client.get(f"/api/mandanten/{mid}/export/kpi.pdf?wochen=40").status_code == 422
