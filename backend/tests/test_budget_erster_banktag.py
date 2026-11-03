"""Budget-Aufteilung „erster Banktag“ (ERST): Monatswert voll am ersten Bankarbeitstag des
Monats, Wochenwert voll am ersten Bankarbeitstag der KW – auch bei wöchentlichem
Kontoprofil. Nur synthetische Daten."""

from datetime import date
from decimal import Decimal

import pytest

from app import models
from app.services import liquiditaet as liq
from app.services.budget import budget_netto_tage

from test_api import _login
from test_bestellungen import _benutzer
from test_liquiditaet import _konto, _mandant
from test_verschiebung_iteration4 import _zeile
from test_verschiebung_iteration4_2 import _verschieben


def _monat(db, m, konto, jahr, monat, netto, **felder):
    db.add(models.Budget(mandant_id=m.id, konto_id=_konto(db, m, konto).id, jahr=jahr, monat=monat,
                         betrag_netto=Decimal(netto), aufteilung="ERST", **felder))
    db.commit()


def _netto(db, m, von, bis, konto="4920") -> dict:
    return {d: float(v) for d, v in
            budget_netto_tage(db, m, von, bis).get(_konto(db, m, konto).id, {}).items()}


def test_monatswert_voll_am_ersten_bankarbeitstag(db):
    """Januar 2027: der 01.01. (Freitag) ist Neujahr – erster Bankarbeitstag Mo 04.01.
    August 2026 beginnt an einem Samstag – erster Bankarbeitstag Mo 03.08."""
    m = _mandant(db)
    _monat(db, m, "4920", 2027, 1, "1000")
    _monat(db, m, "4930", 2026, 8, "400")
    assert _netto(db, m, date(2026, 12, 28), date(2027, 1, 31)) == {date(2027, 1, 4): 1000.0}
    assert _netto(db, m, date(2026, 8, 1), date(2026, 8, 31), konto="4930") == {date(2026, 8, 3): 400.0}


def test_monatswert_im_plan_brutto_am_ersten_bankarbeitstag(db):
    m = _mandant(db)
    _monat(db, m, "4920", 2027, 1, "1000")                 # Telefon, 19 % USt
    assert _zeile(db, m, date(2026, 12, 28), date(2026, 12, 20)) == {"2027-01-04": -1190.0}


def test_fortgeschriebener_monatswert_je_monat_am_ersten_bankarbeitstag(db):
    m = _mandant(db)
    _monat(db, m, "4920", 2027, 1, "1000", fortschreiben=True)
    tage = _netto(db, m, date(2027, 1, 1), date(2027, 3, 31))
    assert tage == {date(2027, 1, 4): 1000.0, date(2027, 2, 1): 1000.0, date(2027, 3, 1): 1000.0}


def test_wochenwert_voll_am_ersten_bankarbeitstag_trotz_wochentagsprofil(db):
    """Konto mit Profil „wöchentlich freitags“: ERST legt den Wochenwert trotzdem auf den
    ersten Bankarbeitstag der KW (KW 15/2026: Ostermontag 06.04. → Di 07.04.); je KW bleibt
    beim Freitag."""
    m = _mandant(db)
    k = _konto(db, m, "4920")
    k.verteilung = "WTAG_FR"
    db.add(models.BudgetWoche(mandant_id=m.id, konto_id=k.id, jahr=2026, kw=15,
                              betrag_netto=Decimal("500"), aufteilung="ERST"))
    db.add(models.BudgetWoche(mandant_id=m.id, konto_id=k.id, jahr=2026, kw=16,
                              betrag_netto=Decimal("500"), aufteilung="KW"))
    db.commit()
    assert _netto(db, m, date(2026, 4, 6), date(2026, 4, 19)) == \
        {date(2026, 4, 7): 500.0, date(2026, 4, 17): 500.0}


def test_monatswert_erst_unabhaengig_vom_kontoprofil(db):
    m = _mandant(db)
    k = _konto(db, m, "4920")
    k.verteilung = "MONATSENDE"
    _monat(db, m, "4920", 2026, 9, "300")
    assert _netto(db, m, date(2026, 9, 1), date(2026, 9, 30)) == {date(2026, 9, 1): 300.0}
    assert liq.budget_profil(k, "ERST") == "MONATSANFANG"


def test_ganze_zelle_am_ersten_bankarbeitstag_verschiebbar(db, monkeypatch):
    """Monatswert September 2026 voll am Di 01.09.; am Fr 28.08. die ganze Zelle auf den
    08.09.: die Zelle ist leer, am Ziel stehen die 1.190 brutto."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _monat(db, m, "4920", 2026, 9, "1000")
    heute = date(2026, 8, 28)
    start = liq.wochen_start(heute)
    assert _zeile(db, m, start, heute) == {"2026-09-01": -1190.0}
    _verschieben(db, m, nutzer, monkeypatch, heute, "4920", date(2026, 9, 1), date(2026, 9, 8),
                 "1190")
    assert _zeile(db, m, start, heute) == {"2026-09-08": -1190.0}


@pytest.fixture()
def api_konto(client):
    import app.database as db_modul

    _login(client)
    mid = client.post("/api/mandanten", json={"name": "Budget GmbH", "kurzname": "budget",
                                              "bundesland": "NW"}).json()["id"]
    with db_modul.SessionLocal() as s:
        return mid, _konto(s, s.get(models.Mandant, mid), "4920").id


def test_api_speichert_und_zeigt_erster_banktag(client, api_konto):
    mid, kid = api_konto
    url = f"/api/mandanten/{mid}/budget-zelle"
    r = client.put(url, json={"konto_id": kid, "ebene": "monat", "periode": "2026-09",
                              "betrag_netto": 1000, "aufteilung": "ERST", "fortschreiben": False})
    assert r.status_code == 200, r.text
    info = client.get(url, params={"konto_id": kid, "tag": "2026-09-15"}).json()
    assert info["monat"]["erfasst"]["aufteilung"] == "ERST"
    assert info["monat"]["wirksam"] == 1000.0
    assert info["woche"]["wirksam"] == 0.0                      # KW 38 ohne Budget
    r = client.put(url, json={"konto_id": kid, "ebene": "woche", "periode": "2026-W40",
                              "betrag_netto": 200, "aufteilung": "ERST"})
    assert r.status_code == 200, r.text
    tage = client.get(f"/api/mandanten/{mid}/budget-raster?ansicht=tage&start=2026-08-31&wochen=5").json()
    zeile = next(k for g in tage["gruppen"] for k in g["konten"] if k["id"] == kid)
    assert {t: w for t, w in zeile["werte"].items() if w} == {"2026-09-01": 1000.0, "2026-09-28": 200.0}
    r = client.put(url, json={"konto_id": kid, "ebene": "monat", "periode": "2026-10",
                              "betrag_netto": 10, "aufteilung": "ERSTE"})
    assert r.status_code == 422 and "ERST" in r.json()["detail"]
