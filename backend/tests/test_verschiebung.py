"""Manuelle Verschiebungen von Planwerten und Vorträgen, Ist-Buchungen einer
Planzelle und Umbuchen des Sachkontos."""

from datetime import date, timedelta
from decimal import Decimal

from app import models
from app.services.liquiditaet import berechne_plan, erstelle_snapshot

from test_api import _login
from test_liquiditaet import _konto, _mandant

START = date(2026, 7, 27)
HEUTE = date(2026, 8, 5)          # Mittwoch


def _miete(db, m, betrag="6000", aktiv=True):
    d = models.Dauerbuchung(
        mandant_id=m.id, name="Miete", art="KREDITOR", konto_id=_konto(db, m, "4210").id,
        betrag_brutto=Decimal(betrag), intervall="MONATLICH", stichtag=1,
        gueltig_von=date(2026, 1, 1), aktiv=aktiv,
    )
    db.add(d)
    return d


def _zahlung(db, m, tag: date, betrag: str, konto="4210"):
    db.add(models.Buchung(mandant_id=m.id, datum=tag, konto_nr=konto, gegenkonto_nr="1200",
                          betrag=Decimal(betrag), sh="S"))


def _bank(db, m, wert="50000"):
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=_konto(db, m, "1200").id,
                          datum=date(2026, 7, 26), wert=Decimal(wert)))


def _verschiebe(db, m, von, nach, betrag, quelle="PLAN", konto="4210"):
    db.add(models.PlanVerschiebung(mandant_id=m.id, konto_id=_konto(db, m, konto).id,
                                   quelle=quelle, von=von, nach=nach, betrag=Decimal(betrag)))


def _plan(db, m, heute=HEUTE, wochen=8):
    db.commit()
    return berechne_plan(db, m, start=START, wochen=wochen, heute=heute)


def _zeile(plan, nummer="4210"):
    return next(k for g in plan["zeilen"] for k in g["kinder"] if k["nummer"] == nummer)


def _fluss(plan, tag: str) -> float:
    """Veränderung der fortgeschriebenen Liquidität an einem Tag."""
    vortag = (date.fromisoformat(tag) - timedelta(days=1)).isoformat()
    return round(plan["bestaende"]["liquiditaet"][tag] - plan["bestaende"]["liquiditaet"][vortag], 2)


def test_teilbetrag_einer_dauerschuld_verschieben(db):
    m = _mandant(db)
    _bank(db, m)
    _miete(db, m)
    _zahlung(db, m, date(2026, 8, 3), "6000")             # Augustrate gezahlt
    _verschiebe(db, m, date(2026, 9, 1), date(2026, 9, 15), "-4000")
    plan = _plan(db, m)
    z = _zeile(plan)
    assert z["plan"]["2026-09-01"] == -2000.0 and z["plan"]["2026-09-15"] == -4000.0
    assert z["verschoben"] == {"2026-09-01": 4000.0, "2026-09-15": -4000.0}
    assert plan["vortraege"] == []
    # Fortschreibung: 2.000 am Ursprungstag, 4.000 am Zieltag
    assert _fluss(plan, "2026-09-01") == -2000.0
    assert _fluss(plan, "2026-09-15") == -4000.0


def test_verschiebung_begrenzt_auf_geplanten_betrag(db):
    m = _mandant(db)
    _bank(db, m)
    _miete(db, m, aktiv=False)                             # Dauerschuld beendet
    _verschiebe(db, m, date(2026, 9, 1), date(2026, 9, 15), "-4000")
    plan = _plan(db, m)
    assert not any(k["nummer"] == "4210" for g in plan["zeilen"] for k in g["kinder"])
    assert _fluss(plan, "2026-09-15") == 0.0


def test_restbetrag_am_ursprungstag_wird_bei_teilzahlung_nicht_vorgetragen(db):
    m = _mandant(db)
    _bank(db, m)
    _miete(db, m)
    _zahlung(db, m, date(2026, 8, 3), "6000")
    _zahlung(db, m, date(2026, 9, 1), "2000")              # nur der verbliebene Teil
    _verschiebe(db, m, date(2026, 9, 1), date(2026, 9, 15), "-4000")
    plan = _plan(db, m, heute=date(2026, 9, 2))
    assert plan["vortraege"] == []
    assert _zeile(plan)["plan"]["2026-09-15"] == -4000.0
    assert _fluss(plan, "2026-09-15") == -4000.0           # weiterhin erwartet


def test_verschobener_teil_wird_am_zieltag_faellig(db):
    m = _mandant(db)
    _bank(db, m)
    _miete(db, m)
    _zahlung(db, m, date(2026, 8, 3), "6000")
    _zahlung(db, m, date(2026, 9, 1), "2000")
    _verschiebe(db, m, date(2026, 9, 1), date(2026, 9, 15), "-4000")
    plan = _plan(db, m, heute=date(2026, 9, 16))           # Zieltag vorbei, nicht gezahlt
    v = plan["vortraege"]
    assert [(x["faellig"], x["ziel"], x["betrag"]) for x in v] == [
        ("2026-09-15", "2026-09-17", -4000.0)]


def test_ueberfaelliger_posten_rollt_und_wird_anteilig_verschoben(db):
    m = _mandant(db)
    _bank(db, m)
    db.add(models.OffenerPosten(
        mandant_id=m.id, art="KREDITOR", konto_id=_konto(db, m, "4210").id,
        belegnr="R-1", partner="Vermieter", rechnungsdatum=date(2026, 7, 20),
        faellig_am=date(2026, 8, 10), betrag_brutto=Decimal("6000"),
    ))
    _verschiebe(db, m, date(2026, 8, 10), date(2026, 8, 20), "-4000")
    # vor Fälligkeit: wie verschoben
    z = _zeile(_plan(db, m))
    assert z["plan"]["2026-08-10"] == -2000.0 and z["plan"]["2026-08-20"] == -4000.0
    # Fälligkeit verstrichen, nichts gezahlt: Rest rollt, verschobener Teil bleibt am Zieltag
    plan = _plan(db, m, heute=date(2026, 8, 12))
    z = _zeile(plan)
    assert z["plan"].get("2026-08-12") == -2000.0 and z["plan"]["2026-08-20"] == -4000.0
    assert _fluss(plan, "2026-08-13") == -2000.0
    assert _fluss(plan, "2026-08-20") == -4000.0


def test_vortrag_verschieben_bis_zum_zieltag(db):
    m = _mandant(db)
    _bank(db, m)
    _miete(db, m, betrag="1500")                           # Augustrate (03.08.) offen
    _verschiebe(db, m, date(2026, 8, 6), date(2026, 8, 20), "-1000", quelle="VORTRAG")
    z = _zeile(_plan(db, m))
    assert z["vortrag"] == {"2026-08-06": -500.0, "2026-08-20": -1000.0}
    # Zieltag vorbei und weiter nicht gezahlt: wieder voll vorgetragen
    z = _zeile(_plan(db, m, heute=date(2026, 8, 21)))
    assert z["vortrag"] == {"2026-08-24": -1500.0}


def test_snapshot_enthaelt_verschiebungen(db):
    m = _mandant(db)
    _miete(db, m)
    _verschiebe(db, m, date(2026, 9, 1), date(2026, 9, 15), "-4000")
    db.commit()
    snap = erstelle_snapshot(db, m, start=date(2026, 8, 31), wochen=4, heute=HEUTE)
    werte = {w.datum.isoformat(): float(w.betrag) for w in snap.werte}
    assert werte["2026-09-01"] == -2000.0 and werte["2026-09-15"] == -4000.0


# ------------------------------------------------------------------ API

def _api_mandant(client, kurz):
    _login(client)
    mid = client.post("/api/mandanten", json={"name": f"{kurz} GmbH", "kurzname": kurz}).json()["id"]
    konten = {k["nummer"]: k for k in client.get(f"/api/mandanten/{mid}/konten").json()["konten"]}
    return mid, konten


def _naechster_werktag(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def test_api_verschieben_in_woche_und_loeschen(client):
    mid, konten = _api_mandant(client, "verschieben")
    heute = date.today()
    montag = heute - timedelta(days=heute.weekday())
    # Zahlungstermin übernächste Woche Mittwoch, 6.000 €
    faellig = montag + timedelta(days=16)
    client.post(f"/api/mandanten/{mid}/posten", json={
        "art": "KREDITOR", "konto_id": konten["4210"]["id"], "belegnr": "M-1",
        "partner": "Vermieter", "rechnungsdatum": heute.isoformat(),
        "faellig_am": faellig.isoformat(), "betrag_brutto": 6000,
    }).raise_for_status()
    key = str(konten["4210"]["id"])
    woche_von, woche_bis = montag + timedelta(days=14), montag + timedelta(days=20)
    basis = {"key": key, "von": woche_von.isoformat(), "bis": woche_bis.isoformat(),
             "ansicht": "wochen", "start": montag.isoformat(), "wochen": 13}

    # mehr als geplant → abgewiesen; gleiche Woche → abgewiesen
    r = client.post(f"/api/mandanten/{mid}/plan/verschiebungen",
                    json={**basis, "nach": (montag + timedelta(days=30)).isoformat(), "betrag": 7000})
    assert r.status_code == 422 and "6.000,00" in r.json()["detail"]
    r = client.post(f"/api/mandanten/{mid}/plan/verschiebungen",
                    json={**basis, "nach": woche_von.isoformat(), "betrag": 1000})
    assert r.status_code == 422

    # 4.000 € in die Woche darauf → erster Bankarbeitstag der Woche
    erg = client.post(f"/api/mandanten/{mid}/plan/verschiebungen",
                      json={**basis, "nach": (montag + timedelta(days=25)).isoformat(),
                            "betrag": 4000, "kommentar": "Stundung"})
    assert erg.status_code == 201, erg.text
    ziel = erg.json()["ziel"]
    assert date.fromisoformat(ziel).isocalendar()[1] == (montag + timedelta(days=21)).isocalendar()[1]
    assert date.fromisoformat(ziel).weekday() < 5

    plan = client.get(f"/api/mandanten/{mid}/plan?start={montag.isoformat()}").json()
    zeile = next(k for g in plan["zeilen"] for k in g["kinder"] if k["key"] == key)
    assert zeile["plan"][faellig.isoformat()] == -2000.0
    assert zeile["plan"][ziel] == -4000.0

    liste = client.get(f"/api/mandanten/{mid}/plan/verschiebungen?key={key}").json()
    assert [(v["von"], v["nach"], v["betrag"], v["kommentar"]) for v in liste] == [
        (faellig.isoformat(), ziel, -4000.0, "Stundung")]
    assert client.delete(f"/api/mandanten/{mid}/plan/verschiebungen/{liste[0]['id']}").status_code == 200
    plan = client.get(f"/api/mandanten/{mid}/plan?start={montag.isoformat()}").json()
    zeile = next(k for g in plan["zeilen"] for k in g["kinder"] if k["key"] == key)
    assert zeile["plan"] == {faellig.isoformat(): -6000.0}
    assert client.delete(f"/api/mandanten/{mid}/plan/verschiebungen/{liste[0]['id']}").status_code == 404


def test_api_ist_buchungen_und_umbuchen(client):
    mid, konten = _api_mandant(client, "istzelle")
    tag = date.today() - timedelta(days=1)
    import app.database as db_modul

    with db_modul.SessionLocal() as s:
        for betrag, text in (("900", "Miete Juni"), ("100", "Nebenkosten")):
            s.add(models.Buchung(mandant_id=mid, datum=tag, konto_nr="4210", gegenkonto_nr="1200",
                                 betrag=Decimal(betrag), sh="S", text=text))
        s.commit()
    key = str(konten["4210"]["id"])
    liste = client.get(f"/api/mandanten/{mid}/plan/ist-buchungen",
                       params={"key": key, "von": tag.isoformat(), "bis": tag.isoformat()}).json()
    assert [(b["text"], b["fluss"]) for b in liste] == [("Miete Juni", -900.0), ("Nebenkosten", -100.0)]
    assert liste[0]["finanzkonto"].startswith("1200")

    # Nebenkosten auf ein anderes Sachkonto umbuchen
    r = client.post(f"/api/buchungen/{liste[1]['id']}/sachkonto",
                    json={"konto_id": konten["4240"]["id"]})
    assert r.status_code == 200 and r.json()["konto"].startswith("4240")
    liste = client.get(f"/api/mandanten/{mid}/plan/ist-buchungen",
                       params={"key": key, "von": tag.isoformat(), "bis": tag.isoformat()}).json()
    assert [b["text"] for b in liste] == ["Miete Juni"]
    # Bank als Sachkonto ist unzulässig
    r = client.post(f"/api/buchungen/{liste[0]['id']}/sachkonto",
                    json={"konto_id": konten["1200"]["id"]})
    assert r.status_code == 422
