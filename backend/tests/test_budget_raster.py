"""Budget in Tages-, Wochen- und Monatsansicht (Vorrang Tag > Woche > Monat)
und die gemeinsam gespeicherte Spaltenansicht von Plan und Budget."""

from datetime import date
from decimal import Decimal

import pytest

from app import models
from app.services.budget import budget_netto_tage
from app.services.liquiditaet import berechne_plan

from test_api import _login
from test_liquiditaet import _konto, _mandant


def _budget(db, m, konto="3400"):
    """August 2026 (NW, 21 Bankarbeitstage): Monat 2.100 € netto = 100 € je
    Banktag; KW 33 übersteuert mit 1.000 € (200 € je Banktag); am 20.08. kein
    Budget, am 24.08. 300 €."""
    k = _konto(db, m, konto)
    db.add(models.Budget(mandant_id=m.id, konto_id=k.id, jahr=2026, monat=8, betrag_netto=Decimal("2100")))
    db.add(models.BudgetWoche(mandant_id=m.id, konto_id=k.id, jahr=2026, kw=33, betrag_netto=Decimal("1000")))
    db.add(models.BudgetTag(mandant_id=m.id, konto_id=k.id, datum=date(2026, 8, 20), betrag_netto=Decimal("0")))
    db.add(models.BudgetTag(mandant_id=m.id, konto_id=k.id, datum=date(2026, 8, 24), betrag_netto=Decimal("300")))
    db.commit()
    return k


def test_vorrang_tag_vor_woche_vor_monat(db):
    m = _mandant(db)
    k = _budget(db, m)
    tage = {d: round(float(v), 2) for d, v in budget_netto_tage(db, m, date(2026, 8, 1), date(2026, 8, 31))[k.id].items()}
    assert tage[date(2026, 8, 3)] == 100.0                      # Monatsbudget verteilt
    assert tage[date(2026, 8, 11)] == 200.0                     # Wochenwert KW 33
    assert date(2026, 8, 20) not in tage                         # Tageswert 0
    assert tage[date(2026, 8, 24)] == 300.0                     # Tageswert
    assert date(2026, 8, 8) not in tage                          # Samstag
    assert round(sum(tage.values()), 2) == 2100 - 500 + 1000 - 100 - 100 + 300


def test_tageswert_wirkt_im_plan(db):
    m = _mandant(db)
    k = _budget(db, m)
    plan = berechne_plan(db, m, start=date(2026, 8, 3), wochen=4, heute=date(2026, 8, 1))
    zeile = next(z for g in plan["zeilen"] for z in g["kinder"] if z["key"] == str(k.id))
    assert zeile["plan"]["2026-08-24"] == -357.0                # 300 € netto + 19 % USt
    assert "2026-08-20" not in zeile["plan"]
    assert zeile["plan"]["2026-08-11"] == -238.0                # 200 € netto aus dem Wochenwert
    assert zeile["plan"]["2026-08-04"] == -119.0                # 100 € netto aus dem Monatsbudget


# ------------------------------------------------------------------ API

@pytest.fixture()
def api_mandant(client):
    import app.database as db_modul

    _login(client)
    mid = client.post("/api/mandanten", json={"name": "Budget GmbH", "kurzname": "budget",
                                              "bundesland": "NW"}).json()["id"]
    with db_modul.SessionLocal() as s:
        m = s.get(models.Mandant, mid)
        k = _budget(s, m)
        return mid, k.id


def _zeile(raster, konto_id):
    return next(k for k in raster["konten"] if k["id"] == konto_id)


def test_raster_monate_wochen_tage(client, api_mandant):
    mid, kid = api_mandant
    monate = client.get(f"/api/mandanten/{mid}/budget-raster?ansicht=monate&jahr=2026").json()
    assert [p["id"] for p in monate["perioden"]][:2] == ["2026-01", "2026-02"]
    z = _zeile(monate, kid)
    assert z["erfasst"] == {"2026-08": 2100.0} and z["werte"]["2026-08"] == 2700.0
    assert monate["summen"]["AUS"]["2026-08"] == 2700.0

    wochen = client.get(f"/api/mandanten/{mid}/budget-raster?ansicht=wochen&start=2026-08-05&wochen=4").json()
    assert [p["label"] for p in wochen["perioden"]] == ["KW 32", "KW 33", "KW 34", "KW 35"]
    z = _zeile(wochen, kid)
    assert z["erfasst"] == {"2026-W33": 1000.0}
    assert z["werte"] == {"2026-W32": 500.0, "2026-W33": 1000.0, "2026-W34": 400.0, "2026-W35": 700.0}

    tage = client.get(f"/api/mandanten/{mid}/budget-raster?ansicht=tage&start=2026-08-17&wochen=2").json()
    assert len(tage["perioden"]) == 14 and tage["perioden"][5]["banktag"] is False   # Samstag
    z = _zeile(tage, kid)
    assert z["erfasst"] == {"2026-08-20": 0.0, "2026-08-24": 300.0}
    assert "2026-08-20" not in z["werte"] and z["werte"]["2026-08-24"] == 300.0
    assert client.get(f"/api/mandanten/{mid}/budget-raster?ansicht=jahr").status_code == 422


def test_raster_speichern(client, api_mandant):
    mid, kid = api_mandant
    url = f"/api/mandanten/{mid}/budget-raster"
    # Tageswert ändern und Übersteuerung entfernen
    r = client.put(url, json={"ansicht": "tage", "zellen": [
        {"konto_id": kid, "periode": "2026-08-24", "betrag_netto": 450},
        {"konto_id": kid, "periode": "2026-08-20", "betrag_netto": None}]})
    assert r.status_code == 200 and r.json()["anzahl"] == 2
    z = _zeile(client.get(f"{url}?ansicht=tage&start=2026-08-17&wochen=2").json(), kid)
    assert z["erfasst"] == {"2026-08-24": 450.0} and z["werte"]["2026-08-20"] == 100.0
    # Wochenwert 0 = kein Budget in der Woche (Tageswerte gelten weiter)
    client.put(url, json={"ansicht": "wochen", "zellen": [{"konto_id": kid, "periode": "2026-W35", "betrag_netto": 0}]})
    z = _zeile(client.get(f"{url}?ansicht=wochen&start=2026-08-03&wochen=5").json(), kid)
    assert z["erfasst"]["2026-W35"] == 0.0 and z["werte"]["2026-W35"] == 450.0
    # Monat: 0 löscht das Budget
    client.put(url, json={"ansicht": "monate", "zellen": [{"konto_id": kid, "periode": "2026-08", "betrag_netto": 0}]})
    z = _zeile(client.get(f"{url}?ansicht=monate&jahr=2026").json(), kid)
    assert "2026-08" not in z["erfasst"]
    # Fehler
    for zelle, ansicht in (({"konto_id": kid, "periode": "2026-08-24", "betrag_netto": -5}, "tage"),
                           ({"konto_id": kid, "periode": "2026-13", "betrag_netto": 5}, "monate"),
                           ({"konto_id": kid, "periode": "2026-W60", "betrag_netto": 5}, "wochen"),
                           ({"konto_id": 99999, "periode": "2026-08", "betrag_netto": 5}, "monate")):
        assert client.put(url, json={"ansicht": ansicht, "zellen": [zelle]}).status_code == 422, zelle


def test_ansicht_wird_gespeichert_und_uebernommen(client, api_mandant):
    mid, _ = api_mandant
    assert client.get("/api/profil").json()["einstellungen"] == {}
    assert 'const ANSICHT = "tage"' in client.get(f"/mandanten/{mid}/plan").text
    assert client.put("/api/profil/einstellungen", json={"ansicht": "monate"}).json() == {"ansicht": "monate"}
    assert client.put("/api/profil/einstellungen", json={"ansicht": "jahre"}).status_code == 422
    assert client.get("/api/profil").json()["einstellungen"] == {"ansicht": "monate"}
    for seite in ("plan", "budget"):
        assert 'const ANSICHT = "monate"' in client.get(f"/mandanten/{mid}/{seite}").text


# ------------------------------------------- Aufteilung und Fortschreibung

def test_kw_aufteilung_eine_zahlung_je_woche():
    from app.services.liquiditaet import _monats_verteilung

    # Dezember 2026 (NW): eine Zahlung je Kalenderwoche am ersten Banktag
    kw = _monats_verteilung(2026, 12, "KW", "NW")
    assert sorted(kw) == [date(2026, 12, 1), date(2026, 12, 7), date(2026, 12, 14),
                          date(2026, 12, 21), date(2026, 12, 28)]
    assert round(sum(kw.values()), 10) == 1
    freitags = _monats_verteilung(2026, 12, "KW:4", "NW")
    assert date(2026, 12, 4) in freitags and date(2026, 12, 11) in freitags
    # erster Teil-KW-Anteil: 4 Banktage (01.–04.12.) von allen Banktagen
    assert kw[date(2026, 12, 1)] == freitags[date(2026, 12, 4)]


def test_fortschreibung_bis_zum_naechsten_monatswert(db):
    m = _mandant(db)
    k = _konto(db, m, "4920")                    # Telefon, 19 % USt
    db.add(models.Budget(mandant_id=m.id, konto_id=k.id, jahr=2026, monat=9, betrag_netto=Decimal("100"),
                         aufteilung="KW", fortschreiben=True))
    db.add(models.Budget(mandant_id=m.id, konto_id=k.id, jahr=2026, monat=11, betrag_netto=Decimal("50"),
                         aufteilung="TAG", fortschreiben=False))
    db.commit()
    from app.services.liquiditaet import MonatsBudgets

    mb = MonatsBudgets(db.query(models.Budget).all())
    assert mb.wert(k.id, 2026, 8) is None
    assert mb.wert(k.id, 2026, 10) == (Decimal("100.00"), "KW")      # fortgeschrieben
    assert mb.wert(k.id, 2026, 11) == (Decimal("50.00"), "TAG")      # eigener Wert
    assert mb.wert(k.id, 2026, 12) is None                           # ohne Fortschreibung
    netto = budget_netto_tage(db, m, date(2026, 10, 1), date(2026, 12, 31))[k.id]
    oktober = {d: v for d, v in netto.items() if d.month == 10}
    assert len(oktober) == 5 and round(sum(oktober.values()), 2) == 100     # je KW
    assert round(sum(v for d, v in netto.items() if d.month == 11), 2) == 50
    assert not any(d.month == 12 for d in netto)


def test_wochenwert_je_kw_oder_tag(db):
    m = _mandant(db)
    k = _konto(db, m, "4920")
    db.add(models.BudgetWoche(mandant_id=m.id, konto_id=k.id, jahr=2026, kw=46,
                              betrag_netto=Decimal("500"), aufteilung="KW"))
    db.add(models.BudgetWoche(mandant_id=m.id, konto_id=k.id, jahr=2026, kw=47,
                              betrag_netto=Decimal("500"), aufteilung="TAG"))
    db.commit()
    netto = budget_netto_tage(db, m, date(2026, 11, 9), date(2026, 11, 22))[k.id]
    assert {d: float(v) for d, v in netto.items() if d <= date(2026, 11, 15)} == {date(2026, 11, 9): 500.0}
    assert sorted(float(v) for d, v in netto.items() if d >= date(2026, 11, 16)) == [100.0] * 5


def test_ust_vorschau_mit_fortgeschriebenem_budget(db):
    from app.services.zahlungskalender import ust_zahllast_aus_budget

    m = _mandant(db)
    db.add(models.Budget(mandant_id=m.id, konto_id=_konto(db, m, "8400").id, jahr=2026, monat=9,
                         betrag_netto=Decimal("10000"), fortschreiben=True))
    db.commit()
    assert ust_zahllast_aus_budget(db, m, [(2026, 11)]) == Decimal("1900.00")


def test_api_budget_zelle_dialog(client, api_mandant):
    mid, kid = api_mandant
    url = f"/api/mandanten/{mid}/budget-zelle"
    # Monatswert Okt 2026 je KW mit Fortschreibung
    r = client.put(url, json={"konto_id": kid, "ebene": "monat", "periode": "2026-10", "betrag_netto": 2000})
    assert r.status_code == 200
    info = client.get(url, params={"konto_id": kid, "tag": "2026-11-11"}).json()
    assert info["monat"]["erfasst"] is None
    assert info["monat"]["fortgeschrieben_aus"] == {"periode": "2026-10", "label": "Okt 2026",
                                                     "betrag": 2000.0, "aufteilung": "KW"}
    assert info["monat"]["wirksam"] == 2000.0 and info["woche"]["label"] == "KW 46"
    # August hat eigene Werte; spätere Monate werden beim Speichern optional ersetzt
    info = client.get(url, params={"konto_id": kid, "tag": "2026-07-15"}).json()
    assert info["monat"]["spaetere"] == ["2026-08", "2026-10"]
    r = client.put(url, json={"konto_id": kid, "ebene": "monat", "periode": "2026-07", "betrag_netto": 900,
                              "aufteilung": "TAG", "fortschreiben": True, "spaetere_entfernen": True})
    assert r.status_code == 200
    info = client.get(url, params={"konto_id": kid, "tag": "2026-12-01"}).json()
    assert info["monat"]["fortgeschrieben_aus"]["periode"] == "2026-07"
    assert info["monat"]["spaetere"] == []
    # Raster: Monate des Fensters, fortgeschriebene Monate gekennzeichnet, Gruppen
    raster = client.get(f"/api/mandanten/{mid}/budget-raster?ansicht=monate&start=2026-10-26&wochen=13").json()
    assert [p["id"] for p in raster["perioden"]] == ["2026-10", "2026-11", "2026-12", "2027-01"]
    zeile = next(k for g in raster["gruppen"] for k in g["konten"] if k["id"] == kid)
    assert zeile["fortgeschrieben"]["2027-01"] == "Jul 2026" and "2027-01" not in zeile["erfasst"]
    # Woche und Tag, Eintrag entfernen, Fehler
    assert client.put(url, json={"konto_id": kid, "ebene": "woche", "periode": "2026-W46",
                                 "betrag_netto": 300, "aufteilung": "TAG"}).status_code == 200
    info = client.get(url, params={"konto_id": kid, "tag": "2026-11-11"}).json()
    assert info["woche"]["erfasst"] == {"betrag": 300.0, "aufteilung": "TAG"} and info["woche"]["wirksam"] == 300.0
    assert client.put(url, json={"konto_id": kid, "ebene": "woche", "periode": "2026-W46",
                                 "betrag_netto": None}).status_code == 200
    assert client.get(url, params={"konto_id": kid, "tag": "2026-11-11"}).json()["woche"]["erfasst"] is None
    for falsch in ({"ebene": "jahr", "periode": "2026"}, {"ebene": "monat", "periode": "2026-13"},
                   {"ebene": "monat", "periode": "2026-10", "aufteilung": "QUARTAL"},
                   {"ebene": "tag", "periode": "2026-11-11", "betrag_netto": -1}):
        r = client.put(url, json={"konto_id": kid, "betrag_netto": 10, **falsch})
        assert r.status_code == 422, falsch
