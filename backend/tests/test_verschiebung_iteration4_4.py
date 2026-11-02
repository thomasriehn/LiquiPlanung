"""Verschiebungen, Korrekturrunde 4.4: Rückschritte der Iteration 4.3 gegenüber dem gesicherten
Rechenkern (Planstand-Ausgleich nach dem Löschen einer im Planstand enthaltenen Verschiebung;
Verschiebung vom ersten Bankarbeitstag eines Fensters, das mit einem Feiertag beginnt) sowie der
Planstand-Ausgleich für nach dem Planstand verschobenes Budget (P1-Lücke Budgetzellen). Jeder
Test schlug gegen den Stand der Iteration 4.3 fehl, außer den ausgewiesenen Gegenproben; nur
synthetische Daten."""

from datetime import date

import pytest

from app import models
from app.routers import api
from app.services import liquiditaet as liq

from test_bestellungen import _bank, _benutzer, _budget_woche
from test_liquiditaet import _konto, _mandant
from test_verschiebung_iteration4 import _heute_setzen, _kz, _liq, _posten, _zeile
from test_verschiebung_iteration4_2 import _verschieben
from test_verschiebung_iteration4_3 import _planstand, _zahlen


def _loeschen(db, m, nutzer, monkeypatch, heute: date, verschiebung_id: int) -> None:
    """Rücknahme einer Verschiebung am Tag `heute` (DELETE /plan/verschiebungen/{id})."""
    _heute_setzen(monkeypatch, heute)
    db.commit()
    api.verschiebung_loeschen(m.id, verschiebung_id, benutzer=nutzer, db=db)
    db.commit()


# ---------------------------------------------------------------------------
# R1: Planstand enthält eine inzwischen gelöschte Verschiebung
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("planstand_enthaelt_verschiebung", [True, False])
def test_planstand_ausgleich_nach_geloeschter_verschiebung_vom_rolltag(
        db, monkeypatch, planstand_enthaelt_verschiebung):
    """OP 4930 1.500 fällig Mi 15.07. (kein Budget). Fr 03.07.: 15.07. → Mo 20.07. (V1). Mo 06.07.:
    Planstand (Referenz) – er führt den Posten am 20.07. Mi 22.07.: V1 gelöscht (der Posten liegt
    wieder am 15.07., überfällig). Sa 25.07.: die Rolltagszelle 27.07. (1.500) auf Do 30.07.
    Der Planstand führte den Posten nicht am 15.07., sondern am Zieltag der gelöschten
    Verschiebung – der Ausgleich bemisst sich dann an den übrigen Tagen der Zeile in [s0,
    gestern]: KW 30 (AUTO) bleibt bei 0 verfügbar, der Posten zählt weiter in C3 (1.500), KW 31
    unverändert. Gegenprobe: Planstand vor V1 eingefroren (er führt den Posten am 15.07.) –
    dasselbe Ergebnis wie bisher."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 5, 1))
    _posten(db, m, "1500", date(2026, 7, 15), konto="4930")
    db.commit()
    if planstand_enthaelt_verschiebung:
        v1 = _verschieben(db, m, nutzer, monkeypatch, date(2026, 7, 3), "4930", date(2026, 7, 15),
                          date(2026, 7, 20), "1500", start=date(2026, 6, 29))
        _planstand(db, m, date(2026, 7, 6), date(2026, 7, 6))
    else:
        _planstand(db, m, date(2026, 7, 6), date(2026, 7, 6))
        v1 = _verschieben(db, m, nutzer, monkeypatch, date(2026, 7, 8), "4930", date(2026, 7, 15),
                          date(2026, 7, 20), "1500", start=date(2026, 7, 6))
    _loeschen(db, m, nutzer, monkeypatch, date(2026, 7, 22), v1["ids"][0])
    heute = date(2026, 7, 25)
    vorher = {kw: _kz(db, m, kw, heute, konto="4930", basis=None) for kw in (30, 31)}
    assert {kw: e["verfuegbar"] for kw, e in vorher.items()} == {30: 0.0, 31: 0.0}
    assert vorher[30]["abweichung"]["ueberfaellig"] == 1500.0
    _verschieben(db, m, nutzer, monkeypatch, heute, "4930", date(2026, 7, 27), date(2026, 7, 30),
                 "1500", start=date(2026, 7, 20))
    assert _zeile(db, m, date(2026, 7, 20), heute, konto="4930") == {"2026-07-30": -1500.0}
    nachher = {kw: _kz(db, m, kw, heute, konto="4930", basis=None) for kw in (30, 31)}
    assert {kw: e["verfuegbar"] for kw, e in nachher.items()} == {30: 0.0, 31: 0.0}
    # der Posten ist beim Stand gestern offen: in C3 (wie Überfälliges), in KW 31 nicht noch
    # einmal gebunden
    assert nachher[30]["abweichung"]["ueberfaellig"] == 1500.0
    assert nachher[31]["abweichung"]["ueberfaellig"] == 1500.0
    assert nachher[31]["gebunden"]["summe"] == 0.0


# ---------------------------------------------------------------------------
# R2: Fensterbeginn an einem Feiertag – bezahlter Posten mit Plantag vor dem Fenster
# ---------------------------------------------------------------------------

def test_bezahlter_posten_vor_dem_fenster_am_ersten_bankarbeitstag_verschiebbar(db, monkeypatch):
    """Heute Pfingstmontag 25.05. (Feiertag, Fensterbeginn; erster Bankarbeitstag Di 26.05.).
    OP 3400 1.000 fällig Do 21.05. (vor dem Fenster), 500 am 16.05. und 500 heute mit Bankkonto
    bezahlt. Die volle Sicht zeigt die heutige Tranche am 26.05. (> heute, im Zellendialog
    verschiebbar). 250 der Zelle 26.05. auf den 27.05.: die Zelle verliert 250, am Ziel stehen
    250, die Liquidität bleibt – kein wirkungsloser Rest (die Zuordnung kennt den Bestandteil
    nicht nur an seinem Plantag)."""
    heute = date(2026, 5, 25)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 3, 16))
    p = _posten(db, m, "1000", date(2026, 5, 21), konto="3400")
    db.commit()
    _heute_setzen(monkeypatch, date(2026, 5, 16))
    _zahlen(db, m, nutzer, p, "500", date(2026, 5, 16))
    _heute_setzen(monkeypatch, heute)
    _zahlen(db, m, nutzer, p, "500", heute)
    assert _zeile(db, m, heute, heute, konto="3400") == {"2026-05-26": -500.0}
    liq_vorher = _liq(db, m, heute, heute, "2026-06-28")
    _verschieben(db, m, nutzer, monkeypatch, heute, "3400", date(2026, 5, 26), date(2026, 5, 27),
                 "250")
    assert _zeile(db, m, heute, heute, konto="3400") == \
        {"2026-05-26": -250.0, "2026-05-27": -250.0}
    assert _liq(db, m, heute, heute, "2026-06-28") == liq_vorher
    assert liq.Zuordnung(db, m, heute).rest == {}


# ---------------------------------------------------------------------------
# B1: nach dem Planstand verschobenes Budget (P1-Lücke Budgetzellen)
# ---------------------------------------------------------------------------

def test_nach_dem_planstand_verschobenes_budget_zaehlt_in_c3_und_im_soll_der_zielwoche(
        db, monkeypatch):
    """Budget 4920 KW 36 und KW 37 je 1.000 netto (238 je Tag), OP 500 fällig Do 03.09. Mo 31.08.:
    Planstand (Referenz). Di 01.09.: die Mi-Zelle (138) auf den 09.09., die Do-Zelle (500 + 138)
    auf den 10.09. Fr 04.09. (alle Ursprungstage vorbei): der Planstand plant 276 Budget an
    vergangenen Tagen, an denen nichts ausgegeben wurde (Minderausgabe) – heute ist es in KW 37
    geplant: es zählt in C3 (776 mit dem Posten) und im Soll der KW 37 (1.466 statt 1.190); AUTO
    rechnet wie ein frischer Planstand (KW 36 414, KW 37 1.742, KW 38 276 – nicht 690/1.742/552).
    Mo 07.09.: KW 38 zeigt 414 (den Vortrag der KW 36, nicht 690). Fr 11.09. (Zieltage vorbei,
    Posten am 10.09. bezahlt): kein Ausgleich mehr, KW 38 1.642 (frischer Planstand 1.228 und
    der Vortrag der KW 36)."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    for kw in (36, 37):
        _budget_woche(db, m, "4920", kw, "1000")
    p = _posten(db, m, "500", date(2026, 9, 3))
    db.commit()
    _planstand(db, m, date(2026, 8, 31), date(2026, 8, 31))
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 9, 1), "4920", date(2026, 9, 2),
                 date(2026, 9, 9), "138")
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 9, 1), "4920", date(2026, 9, 3),
                 date(2026, 9, 10), "638", minute=5)
    heute = date(2026, 9, 4)
    auto = {kw: _kz(db, m, kw, heute, basis=None) for kw in (36, 37, 38)}
    assert {kw: e["verfuegbar"] for kw, e in auto.items()} == {36: 414.0, 37: 1742.0, 38: 276.0}
    assert auto[36]["abweichung"] == {"ist_minus_plan": -1052.0, "vortraege": 0.0,
                                      "ueberfaellig": 776.0, "summe": -276.0}
    assert (auto[37]["soll_kw"], auto[37]["soll_kw_rest"], auto[37]["gebunden"]["summe"]) == \
        (1466.0, 1466.0, 0.0)
    frisch = _planstand(db, m, heute, liq.wochen_start(heute))
    frisch.referenz = False
    db.commit()
    assert {kw: _kz(db, m, kw, heute, basis=frisch.id)["verfuegbar"] for kw in (36, 37, 38)} == \
        {36: 414.0, 37: 1742.0, 38: 276.0}
    assert _kz(db, m, 38, date(2026, 9, 7), basis=None)["verfuegbar"] == 414.0
    _heute_setzen(monkeypatch, date(2026, 9, 10))
    _zahlen(db, m, nutzer, p, "500", date(2026, 9, 10))
    spaeter = _kz(db, m, 38, date(2026, 9, 11), basis=None)
    assert (spaeter["verfuegbar"], spaeter["abweichung"]["ueberfaellig"]) == (1642.0, 0.0)
