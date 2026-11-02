"""Verschiebungen, Korrekturrunde 4.3: Rückschritte der Iteration 4.2 gegenüber dem gesicherten
Rechenkern (Teilbetrag einer Budgetzelle mit noch nicht entschiedener Bestellung, Teilzahlungen
am Anlagetag vor und nach der Verschiebung, Ausgleich eines Planstands trotz früher Zahlung eines
anderen Postens bzw. seitdem dorthin verschobener Anteile, Herkunftswoche eines verschobenen
Budgets vor dem Fenster, Schlusszahlung eines Postens mit Tranche vor dem Fenster, Schlusszahlung
nach Teilzahlung ohne Beleg) sowie die Ablösung einer nie freigegebenen Bestellung am heutigen
Tag. Jeder Test schlug gegen den Stand der Iteration 4.2 fehl, außer den ausgewiesenen
Gegenproben; nur synthetische Daten."""

from datetime import date

import pytest

from app import models
from app.routers import api
from app.services import bestellungen as svc
from app.services import liquiditaet as liq

from test_bestellungen import _bank, _benutzer, _bestellung, _budget_woche, _zahlung
from test_liquiditaet import _konto, _mandant
from test_verschiebung_iteration4 import _heute_setzen, _kz, _liq, _posten, _zeile
from test_verschiebung_iteration4_2 import _verschieben, _zeitpunkt
from test_verschiebung_zuordnung import _op


def _zahlen(db, m, nutzer, p, betrag, tag: date, bank: bool = True) -> None:
    """„Zahlung erfassen“ am Posten (mit Bankkonto: Ist-Buchung zum Posten)."""
    api.zahlung_erfassen(p.id, api.ZahlungNeu(
        betrag=float(betrag), datum=tag.isoformat(),
        bank_konto_id=_konto(db, m, "1200").id if bank else None), benutzer=nutzer, db=db)
    db.commit()


def _planstand(db, m, heute: date, start: date, wochen: int = 13) -> models.PlanSnapshot:
    """Planstand am Tag `heute` (Stichtag `start`) als Referenz der Freigabe."""
    db.commit()
    snap = liq.erstelle_snapshot(db, m, start=start, wochen=wochen, heute=heute)
    snap.erstellt_am = _zeitpunkt(heute, 12, 1)
    snap.referenz = True
    db.commit()
    return snap


# ---------------------------------------------------------------------------
# Budgetzelle mit noch nicht entschiedener Bestellung: die Zelle verliert genau den Betrag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("betrag", ["500", "714"])
def test_teilbetrag_ueber_dem_angezeigten_budget_nimmt_die_bestellung_mit(db, monkeypatch, betrag):
    """Budget KW 41 3.000 netto (714 je Tag), offene eingeplante Bestellung 2.380 am Do 08.10.:
    die Zelle zeigt 238 Budget und 2.380 Bestellung. Mo 28.09.: 500 bzw. 714 (das Bruttobudget
    des Tages) auf den 14.10. – zuerst das angezeigte Budget 238, dann die Bestellung; die Zelle
    verliert genau den Betrag, die übrigen Tage der Woche und die Liquidität bleiben (nicht das
    Bruttobudget der übrigen Tage). Nach der Freigabe bindet die Bestellung in KW 41 nur noch
    ihren verbliebenen Anteil, der Rest steht in KW 42."""
    heute = date(2026, 9, 28)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 41, "3000")
    b = _bestellung(db, m, nutzer, netto="2000", zahldatum=date(2026, 10, 8))
    b.erstellt_am = _zeitpunkt(date(2026, 9, 18))
    db.commit()
    vorher = _zeile(db, m, heute, heute)
    assert vorher["2026-10-08"] == -2618.0
    liq_vorher = _liq(db, m, heute, heute, "2026-11-08")
    _verschieben(db, m, nutzer, monkeypatch, heute, "4920", date(2026, 10, 8), date(2026, 10, 14),
                 betrag)
    nachher = _zeile(db, m, heute, heute)
    x = float(betrag)
    assert nachher["2026-10-08"] == round(vorher["2026-10-08"] + x, 2)
    assert nachher["2026-10-14"] == -x
    assert {t: w for t, w in nachher.items() if t not in ("2026-10-08", "2026-10-14")} == \
        {t: w for t, w in vorher.items() if t != "2026-10-08"}
    assert _liq(db, m, heute, heute, "2026-11-08") == liq_vorher
    svc.entscheiden(db, b, nutzer, "FREIGEBEN", "Test", {"test": True})
    b.entschieden_am = _zeitpunkt(heute, 13)
    assert _kz(db, m, 41, heute)["gebunden"]["summe"] == 2380.0 - (x - 238.0)
    assert _kz(db, m, 42, heute)["gebunden"]["summe"] == x - 238.0


def test_teilbetrag_in_hoehe_des_bruttobudgets_bei_breiterem_budget(db, monkeypatch):
    """Budget KW 33/34 je 5.000 netto (1.190 je Tag), offene eingeplante Bestellung 2.380 am
    Di 11.08. (Tageswert 714 angezeigt). Fr 07.08.: 1.190 auf den 13.08. – Budget 714 und 476
    der Bestellung; Zelle −1.190, 13.08. +1.190, sonst nichts (Gegenprobe 714: nur Budget)."""
    heute = date(2026, 8, 7)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    for kw in (33, 34):
        _budget_woche(db, m, "4930", kw, "5000")
    b = _bestellung(db, m, nutzer, konto="4930", netto="2000", zahldatum=date(2026, 8, 11))
    b.erstellt_am = _zeitpunkt(date(2026, 7, 28))
    db.commit()
    vorher = _zeile(db, m, liq.wochen_start(heute), heute, konto="4930")
    assert vorher["2026-08-11"] == -3094.0
    for betrag, ziel in (("714", date(2026, 8, 12)), ("1190", date(2026, 8, 13))):
        _verschieben(db, m, nutzer, monkeypatch, heute, "4930", date(2026, 8, 11), ziel, betrag)
        nachher = _zeile(db, m, liq.wochen_start(heute), heute, konto="4930")
        x = float(betrag)
        assert nachher["2026-08-11"] == round(vorher["2026-08-11"] + x, 2)
        assert nachher[ziel.isoformat()] == round(vorher.get(ziel.isoformat(), 0.0) - x, 2)
        assert {t: w for t, w in nachher.items() if t not in ("2026-08-11", ziel.isoformat())} == \
            {t: w for t, w in vorher.items() if t not in ("2026-08-11", ziel.isoformat())}
        vorher = nachher


def test_teilbetrag_aus_der_zelle_mit_nur_noch_der_bestellung_nach_frueherer_verschiebung(
        db, monkeypatch):
    """Wie oben; zuerst 1.238 (Budget 238 und 1.000 Bestellung) vom 08.10. auf Mo 05.10., dann
    226,62 aus der Zelle 08.10. (sie zeigt nur noch 1.380 Bestellung) auf den 14.10.: die Zelle
    sinkt auf 1.153,38, die übrigen Tage bleiben."""
    heute = date(2026, 9, 28)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 41, "3000")
    b = _bestellung(db, m, nutzer, netto="2000", zahldatum=date(2026, 10, 8))
    b.erstellt_am = _zeitpunkt(date(2026, 9, 18))
    _verschieben(db, m, nutzer, monkeypatch, heute, "4920", date(2026, 10, 8), date(2026, 10, 5),
                 "1238", stunde=9)
    vorher = _zeile(db, m, heute, heute)
    assert vorher["2026-10-08"] == -1380.0
    _verschieben(db, m, nutzer, monkeypatch, heute, "4920", date(2026, 10, 8), date(2026, 10, 14),
                 "226.62", stunde=10)
    nachher = _zeile(db, m, heute, heute)
    assert nachher["2026-10-08"] == -1153.38 and nachher["2026-10-14"] == -226.62
    assert {t: w for t, w in nachher.items() if t not in ("2026-10-08", "2026-10-14")} == \
        {t: w for t, w in vorher.items() if t != "2026-10-08"}


# ---------------------------------------------------------------------------
# Teilzahlungen am Anlagetag vor und nach der Verschiebung
# ---------------------------------------------------------------------------

def test_teilzahlungen_am_anlagetag_vor_und_nach_der_verschiebung_in_einer_zelle(db, monkeypatch):
    """OP A 1.000 und OP B 500 fällig 19.08. Mi 12.08.: A mit 300 angezahlt, dann die ganze
    Zelle (1.200) auf den 26.08., danach B mit 100 angezahlt. Die am Anlagetag gezahlte Tranche
    von A steht in keiner Sicht – sie kommt erst nach allen sichtbaren offenen Resten: die
    Zelle 19.08. ist leer, am 26.08. stehen 1.100 (nicht 900 mit 200 Rest am Ursprung)."""
    heute = date(2026, 8, 12)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    a = _posten(db, m, "1000", date(2026, 8, 19), konto="3400")
    b = _posten(db, m, "500", date(2026, 8, 19), konto="3400")
    _zahlen(db, m, nutzer, a, "300", heute)
    assert _zeile(db, m, liq.wochen_start(heute), heute, konto="3400") == {"2026-08-19": -1200.0}
    _verschieben(db, m, nutzer, monkeypatch, heute, "3400", date(2026, 8, 19), date(2026, 8, 26),
                 "1200")
    _zahlen(db, m, nutzer, b, "100", heute)
    assert _zeile(db, m, liq.wochen_start(heute), heute, konto="3400") == {"2026-08-26": -1100.0}


def test_teilzahlungen_am_anlagetag_auf_zwei_posten_gleicher_hoehe(db, monkeypatch):
    """Zwei OP je 1.000 fällig Mo 17.08. Fr 14.08.: A mit 400 angezahlt, die ganze Zelle (1.600)
    auf Di 18.08. verschoben, danach B mit 300 angezahlt (Fuzz „Zahlungen am Anlagetag“,
    Seed 259): am 18.08. liegen 1.300, am 17.08. bleibt nichts – nicht 100 von B, weil die
    unsichtbare Tranche von A vor dem offenen Rest von B genommen würde."""
    heute = date(2026, 8, 14)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    a = _posten(db, m, "1000", date(2026, 8, 17), konto="3400")
    b = _posten(db, m, "1000", date(2026, 8, 17), konto="3400")
    _zahlen(db, m, nutzer, a, "400", heute)
    _verschieben(db, m, nutzer, monkeypatch, heute, "3400", date(2026, 8, 17), date(2026, 8, 18),
                 "1600")
    _zahlen(db, m, nutzer, b, "300", heute)
    assert _zeile(db, m, liq.wochen_start(heute), heute, konto="3400") == {"2026-08-18": -1300.0}


# ---------------------------------------------------------------------------
# Planstand als Basis: Ausgleich für vom Rolltag verschobene Anteile
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("zahltag", [date(2026, 6, 29), date(2026, 7, 1)])
def test_planstand_ausgleich_trotz_frueher_zahlung_eines_anderen_postens_am_rolltag(
        db, monkeypatch, zahltag):
    """OP P5 3.000 fällig 10.06. offen; OP P4 3.000 fällig 01.07., am Mo 29.06. (dem ersten
    Bankarbeitstag ab dem Stichtag, an dem der Planstand P5 gerollt führt) bzw. am 01.07.
    bezahlt. Planstand Di 30.06. (Stichtag 29.06., Referenz). Sa 04.07.: 2.310 der
    Rolltagszelle 06.07. (P5) auf den 31.08. Der verschobene Anteil ist beim Stand gestern
    offen, seine Zahlung fehlt dem Ist gewiss – die frühe Zahlung von P4 am selben Tag verdeckt
    das nicht: KW 28, 29 und 31 (AUTO) bleiben bei 0 verfügbar."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 6, 1))
    _posten(db, m, "3000", date(2026, 6, 10))
    p4 = _posten(db, m, "3000", date(2026, 7, 1))
    db.commit()
    if zahltag <= date(2026, 6, 30):
        _zahlen(db, m, nutzer, p4, "3000", zahltag)
    _planstand(db, m, date(2026, 6, 30), date(2026, 6, 29))
    if zahltag > date(2026, 6, 30):
        _zahlen(db, m, nutzer, p4, "3000", zahltag)
    heute = date(2026, 7, 4)
    vorher = {kw: _kz(db, m, kw, heute, basis=None)["verfuegbar"] for kw in (28, 29, 31)}
    assert vorher == {28: 0.0, 29: 0.0, 31: 0.0}
    _verschieben(db, m, nutzer, monkeypatch, heute, "4920", date(2026, 7, 6), date(2026, 8, 31),
                 "2310", start=date(2026, 6, 29), wochen=13)
    nachher = {kw: _kz(db, m, kw, heute, basis=None) for kw in (28, 29, 31)}
    assert {kw: e["verfuegbar"] for kw, e in nachher.items()} == vorher
    assert nachher[28]["abweichung"]["ueberfaellig"] == 3000.0


def test_planstand_ausgleich_nicht_durch_seitdem_dorthin_verschobenen_anteil_verdeckt(
        db, monkeypatch):
    """OP P1 2.500 fällig Mo 01.06., OP P2 2.000 fällig Mo 08.06.; Planstand Fr 29.05. (Stichtag
    25.05., Referenz). Sa 30.05.: P2 vom 08.06. auf den 01.06. verschoben. So 07.06.: beide sind
    überfällig (Rolltagszelle 08.06. zeigt 4.500); 2.500 davon (P1) auf den 11.06. Der am 01.06.
    seit dem Planstand hinzugekommene P2 verdeckt nicht, dass P1 von dort weg ist: der Ausgleich
    in C3 bleibt 2.500, die unbeteiligte KW 31 (AUTO) ändert sich nicht (Gegenprobe: ohne die
    Verschiebung von P2 ebenso)."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 5, 1))
    _posten(db, m, "2500", date(2026, 6, 1))
    _posten(db, m, "2000", date(2026, 6, 8))
    _planstand(db, m, date(2026, 5, 29), date(2026, 5, 25))
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 5, 30), "4920", date(2026, 6, 8),
                 date(2026, 6, 1), "2000", start=date(2026, 5, 25))
    heute = date(2026, 6, 7)
    assert _zeile(db, m, date(2026, 5, 25), heute, wochen=3)["2026-06-08"] == -4500.0
    vorher = _kz(db, m, 31, heute, basis=None)
    _verschieben(db, m, nutzer, monkeypatch, heute, "4920", date(2026, 6, 8), date(2026, 6, 11),
                 "2500", start=date(2026, 5, 25))
    assert _zeile(db, m, date(2026, 5, 25), heute, wochen=3) == \
        {"2026-06-08": -2000.0, "2026-06-11": -2500.0}
    nachher = _kz(db, m, 31, heute, basis=None)
    assert nachher["verfuegbar"] == vorher["verfuegbar"] == -2000.0
    assert nachher["abweichung"]["ueberfaellig"] == vorher["abweichung"]["ueberfaellig"] == 4500.0


# ---------------------------------------------------------------------------
# Erst nach der Verschiebung angelegte Dauerbuchung (P2 für Raten)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("angelegt", [_zeitpunkt(date(2026, 9, 8), 9), None])
def test_rate_einer_nach_der_budgetverschiebung_angelegten_dauerbuchung_bleibt(db, monkeypatch,
                                                                              angelegt):
    """Budget KW 37/38 je 1.000 netto (238 je Tag). Mo 07.09.: die ganze Zelle 09.09. (238) auf
    den 16.09. Di 08.09.: Dauerbuchung 500 monatlich am 9. angelegt (`erstellt_am`). Die Rate
    stand beim Anlegen nicht in der Zelle und übernimmt die Verschiebung nicht: 09.09. zeigt
    500, der 16.09. weiter 476. Gegenprobe: ohne Anlagezeitpunkt (Bestand) gilt die Rate als
    alt und gibt wie bisher 100 ab."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    for kw in (37, 38):
        _budget_woche(db, m, "4920", kw, "1000")
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 9, 7), "4920", date(2026, 9, 9),
                 date(2026, 9, 16), "238")
    db.add(models.Dauerbuchung(mandant_id=m.id, name="Leasing", art="KREDITOR",
                               konto_id=_konto(db, m, "4920").id, betrag_brutto=500,
                               intervall="MONATLICH", stichtag=9, gueltig_von=date(2026, 9, 1),
                               erstellt_am=angelegt))
    heute = date(2026, 9, 8)
    zeile = _zeile(db, m, liq.wochen_start(heute), heute, wochen=2)
    assert (zeile["2026-09-09"], zeile["2026-09-16"]) == \
        ((-500.0, -476.0) if angelegt is not None else (-400.0, -476.0))


# ---------------------------------------------------------------------------
# Herkunftswoche eines dorthin verschobenen Budgets vor dem Fenster
# ---------------------------------------------------------------------------

def test_ungedecktes_budget_aus_einer_woche_vor_dem_fenster_bleibt_gekuerzt(db, monkeypatch):
    """Budget KW 35–37 je 1.000 netto (238 je Tag). Mo 24.08.: Budget 26.08. → 02.09. (V1).
    Di 25.08.: Rechnung 1.190 fällig 27.08. – KW 35 hat kein Restbudget mehr, V1 ist ungedeckt,
    die Zelle 02.09. zeigt 238; V2: die ganze Zelle 02.09. → 09.09. Fr 28.08. und Mo 31.08.
    (KW 35 vor dem Fenster, die Rechnung überfällig auf den 31.08. gerollt): die Zelle 02.09.
    bleibt leer, KW 36 (LIVE) zeigt nichts verfügbar, die Liquidität am Horizont bleibt bei
    97.620 (Rechnung und das Budget der KW 37); Do 03.09.: KW 37 höchstens 1.190 verfügbar."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    for kw in (35, 36, 37):
        _budget_woche(db, m, "4920", kw, "1000")
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 8, 24), "4920", date(2026, 8, 26),
                 date(2026, 9, 2), "238", stunde=9)
    _posten(db, m, "1190", date(2026, 8, 27), erstellt_am=_zeitpunkt(date(2026, 8, 25), 9))
    assert _zeile(db, m, date(2026, 8, 24), date(2026, 8, 25))["2026-09-02"] == -238.0
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 8, 25), "4920", date(2026, 9, 2),
                 date(2026, 9, 9), "238", stunde=10)
    for heute in (date(2026, 8, 25), date(2026, 8, 28)):
        assert "2026-09-02" not in _zeile(db, m, date(2026, 8, 24), heute)
    heute = date(2026, 8, 31)
    zeile = _zeile(db, m, heute, heute)
    assert "2026-09-02" not in zeile and zeile["2026-09-09"] == -238.0
    assert _kz(db, m, 36, heute)["verfuegbar"] == 0.0
    assert _liq(db, m, heute, heute, "2026-09-27") == 97620.0
    assert _kz(db, m, 37, date(2026, 9, 3))["verfuegbar"] == 1190.0


def test_teilweise_gedecktes_budget_aus_einer_woche_vor_dem_fenster(db, monkeypatch):
    """Nur Budget KW 35 1.000 netto (238 je Tag). Mo 24.08.: die Zelle Fr 28.08. (238) auf Mo
    07.09.; Di 25.08.: Rechnung 1.000 fällig 28.08. – KW 35 behält 190 Restbudget. Mo 31.08.
    (KW 35 vor dem Fenster): am 07.09. stehen 190 (ein Überschuss von allen Tagen der
    Herkunftswoche), KW 37 (LIVE) zeigt 190 verfügbar – nicht 238."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 35, "1000")
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 8, 24), "4920", date(2026, 8, 28),
                 date(2026, 9, 7), "238", stunde=9)
    _posten(db, m, "1000", date(2026, 8, 28), erstellt_am=_zeitpunkt(date(2026, 8, 25), 9))
    heute = date(2026, 8, 31)
    assert _zeile(db, m, heute, heute) == {"2026-08-31": -1000.0, "2026-09-07": -190.0}
    assert _kz(db, m, 37, heute)["verfuegbar"] == 190.0


# ---------------------------------------------------------------------------
# Bezahlte Posten verbrauchen das Restbudget nur mit ihren Tranchen ab Fensterbeginn
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("basis", ["LIVE", None])
def test_schlusszahlung_mit_tranche_vor_dem_fenster_aendert_freigabe_und_plan_nicht(
        db, monkeypatch, basis):
    """Budget KW 40 2.000 netto (2.380). OP 1.000 fällig Mi 30.09., Teilzahlung 400 am Sa 12.09.
    (vor dem Fenster). Mo 21.09.: Planstand, dann Schlusszahlung 600 mit Bankkonto. Der Posten
    verbraucht das Restbudget mit seiner Tranche ab Fensterbeginn (600, wie der Plan ihn zeigt),
    nicht mehr mit dem Bruttobetrag: Plan und Freigabe KW 40 (Stand gestern, LIVE und AUTO)
    bleiben unverändert – Soll-Rest 2.380, verfügbar 1.780."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 40, "2000")
    p = _posten(db, m, "1000", date(2026, 9, 30))
    db.commit()
    _zahlen(db, m, nutzer, p, "400", date(2026, 9, 12))
    heute = date(2026, 9, 21)
    _planstand(db, m, heute, heute)
    vorher = _kz(db, m, 40, heute, basis=basis)
    plan_vorher = _zeile(db, m, heute, heute)
    assert (vorher["soll_kw_rest"], vorher["verfuegbar"]) == (2380.0, 1780.0)
    _zahlen(db, m, nutzer, p, "600", heute)
    assert _kz(db, m, 40, heute, basis=basis) == vorher
    assert _zeile(db, m, heute, heute) == plan_vorher


def test_schlusszahlung_in_der_laufenden_woche_aendert_vergangene_solltage_nicht(db, monkeypatch):
    """Budget KW 40 3.000 netto (714 je Tag). OP 736 fällig Fr 02.10., Teilzahlung 256 am Fr
    25.09. (vor dem Fenster); heute Fr 02.10.: Zahlung des Rests 480 mit Bankkonto. KW 40 (LIVE)
    bleibt ganz unverändert – auch C1 (das Budget der vergangenen Tage schrumpft nicht) und das
    Soll des Tages."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 40, "3000")
    p = _posten(db, m, "736", date(2026, 10, 2))
    db.commit()
    _zahlen(db, m, nutzer, p, "256", date(2026, 9, 25))
    heute = date(2026, 10, 2)
    vorher = _kz(db, m, 40, heute)
    assert (vorher["soll_kw_rest"], vorher["abweichung"]["ist_minus_plan"],
            vorher["verfuegbar"]) == (1098.0, -2472.0, 3090.0)
    _zahlen(db, m, nutzer, p, "480", heute)
    assert _kz(db, m, 40, heute) == vorher


def test_wochensumme_bleibt_beim_budget_nach_schlusszahlung_mit_tranche_vor_dem_fenster(db,
                                                                                     monkeypatch):
    """Budget KW 11 2.500 netto (2.975). OP 750,50 fällig Sa 14.03., Teilzahlung 300,20 am Fr
    27.02. (vor dem Fenster ab Mo 02.03.); So 08.03.: Rest 450,30 bezahlt. Die Wochensumme der
    Zeile in KW 11 bleibt beim Budget 2.975 (vorher wie nachher), die Freigabe KW 11 (LIVE)
    ändert sich nicht."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 2, 1))
    _budget_woche(db, m, "4930", 11, "2500")
    p = _posten(db, m, "750.50", date(2026, 3, 14), konto="4930")
    db.commit()
    _zahlen(db, m, nutzer, p, "300.20", date(2026, 2, 27))
    heute = date(2026, 3, 8)

    def woche() -> float:
        return round(sum(w for t, w in _zeile(db, m, date(2026, 3, 2), heute, konto="4930").items()
                         if "2026-03-09" <= t <= "2026-03-15"), 2)

    vorher = _kz(db, m, 11, heute, konto="4930")
    assert woche() == -2975.0
    _zahlen(db, m, nutzer, p, "450.30", heute)
    assert woche() == -2975.0
    assert _kz(db, m, 11, heute, konto="4930") == vorher


# ---------------------------------------------------------------------------
# Stand gestern: Schlusszahlung nach Teilzahlung ohne Beleg, Ablösung einer offenen Bestellung
# ---------------------------------------------------------------------------

def test_schlusszahlung_nach_teilzahlung_ohne_beleg_bindet_nur_den_rest(db, monkeypatch):
    """OP 2.500 (4930) fällig Mi 29.04.; Planstand Mo 30.03. (Referenz). Fr 03.04.: 1.000 ohne
    Bankkonto bezahlt, das Ist kommt über den Import (Buchung ohne Posten-Bezug). Mo 13.04.:
    Rest 1.500 mit Bankkonto. Stand gestern war der Posten mit 1.500 offen: KW 18 (AUTO) bindet
    weiter 1.500, verfügbar bleibt 0 – die Teilzahlung zählt nicht in C1 und gebunden doppelt."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 3, 1))
    p = _posten(db, m, "2500", date(2026, 4, 29), konto="4930")
    _planstand(db, m, date(2026, 3, 30), date(2026, 3, 30))
    _heute_setzen(monkeypatch, date(2026, 4, 3))
    _zahlen(db, m, nutzer, p, "1000", date(2026, 4, 3), bank=False)
    _zahlung(db, m, date(2026, 4, 3), "1000", konto="4930", text="Überweisung P (Import)")
    heute = date(2026, 4, 13)
    vorher = _kz(db, m, 18, heute, konto="4930", basis=None)
    assert (vorher["gebunden"]["summe"], vorher["verfuegbar"]) == (1500.0, 0.0)
    _zahlen(db, m, nutzer, p, "1500", heute)
    nachher = _kz(db, m, 18, heute, konto="4930", basis=None)
    assert (nachher["gebunden"]["summe"], nachher["verfuegbar"]) == (1500.0, 0.0)


@pytest.mark.parametrize("zahldatum", [date(2026, 2, 24), date(2026, 3, 3)])
def test_abloesung_einer_nie_freigegebenen_bestellung_heute_schafft_keinen_spielraum(
        db, monkeypatch, zahldatum):
    """Offene (nie freigegebene) Bestellung 1.190 brutto, Zahldatum Di 24.02. bzw. Di 03.03.;
    Sa 28.02.: Ablösung durch eine Ist-Buchung von heute. Das Soll (Stand gestern) führt die
    abgelöste Bestellung wie einen bezahlten Posten am Zahldatum, das Ist bis gestern kennt die
    Zahlung noch nicht – sie bleibt gebunden: KW 9 und KW 10 (LIVE) bleiben bei 0 verfügbar."""
    heute = date(2026, 2, 28)
    _heute_setzen(monkeypatch, heute)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 1, 1))
    b = _bestellung(db, m, nutzer, konto="3400", netto="1000", zahldatum=zahldatum)
    b.erstellt_am = _zeitpunkt(date(2026, 2, 10))
    db.commit()
    vorher = {kw: _kz(db, m, kw, heute, konto="3400")["verfuegbar"] for kw in (9, 10)}
    assert vorher == {9: 0.0, 10: 0.0}
    bu = _zahlung(db, m, heute, "1190", konto="3400", text="Bestellzahlung")
    svc.abloesen(db, b, nutzer, art="ZAHLUNG", buchung_id=bu.id, abschliessend=True)
    db.commit()
    assert {kw: _kz(db, m, kw, heute, konto="3400")["verfuegbar"] for kw in (9, 10)} == vorher
