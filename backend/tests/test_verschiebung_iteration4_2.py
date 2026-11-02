"""Verschiebungen, Korrekturrunde 4.2: Rückschritte der Iteration 4.1 gegenüber dem gesicherten
Rechenkern (ungedecktes Fremdbudget in der Zelle, Teilbetrag einer Zelle mit offener Bestellung
ohne Restbudget, mehrere Entnahmen einer Woche, Zellendialog bei anderem Fensterbeginn,
Planstand vor der Verschiebung eines überfälligen Postens, Zahlung am Anlagetag nach der
Verschiebung, Zahlung ohne Beleg mit Import vom Vortag, Laufzeit des angezeigten Budgets) sowie
vorbestehende Fehler (Rest einer Verschiebung nach Storno, Teilzahlungen ohne Beleg). Jeder Test
schlug gegen den Stand der Iteration 4.1 fehl, außer den ausgewiesenen Gegenproben; nur
synthetische Daten."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import models
from app.routers import api
from app.routers import bestellungen as bestell_router
from app.services import bestellungen as svc
from app.services import liquiditaet as liq

from szenario_bausteine import ALTBESTAND
from test_bestellungen import _bank, _benutzer, _budget_woche, _zahlung
from test_liquiditaet import _konto, _mandant
from test_verschiebung_iteration4 import _heute_setzen, _kz, _liq, _posten, _zeile
from test_verschiebung_zuordnung import _op

D = Decimal


def _zeitpunkt(tag: date, stunde: int = 12, minute: int = 0) -> datetime:
    return datetime(tag.year, tag.month, tag.day, stunde, minute, tzinfo=timezone.utc)


def _verschieben(db, m, nutzer, monkeypatch, heute: date, konto: str, von: date, nach: date,
                 betrag, start: date | None = None, wochen: int = 6, stunde: int = 12,
                 minute: int = 0) -> dict:
    """Zellendialog am Tag `heute` (POST /plan/verschiebungen) mit Anlagezeitpunkt wie im
    Betrieb (heute, `stunde`:`minute` UTC)."""
    _heute_setzen(monkeypatch, heute)
    db.commit()
    antwort = api.verschiebung_anlegen(m.id, api.VerschiebungNeu(
        key=str(_konto(db, m, konto).id), von=von.isoformat(), bis=von.isoformat(),
        nach=nach.isoformat(), betrag=float(abs(D(str(betrag)))),
        start=(start or liq.wochen_start(heute)).isoformat(), wochen=wochen),
        benutzer=nutzer, db=db)
    for vid in antwort["ids"]:
        db.get(models.PlanVerschiebung, vid).erstellt_am = _zeitpunkt(heute, stunde, minute)
    db.commit()
    return antwort


# ---------------------------------------------------------------------------
# Ungedecktes Fremdbudget: dorthin verschobenes Budget nur, soweit die Zelle es zeigte
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mit_op", [True, False])
def test_ganze_zelle_neben_ungedecktem_fremdbudget_wird_leer(db, monkeypatch, mit_op):
    """4210 (ohne USt), Budget KW 19 und KW 21 je 2.500 (500 je Tag). Do 16.04.: die Zelle
    04.05. auf den 20.05. verschoben (V1). Mo 04.05.: ein seit 01.05. überfälliger OP 3.000 rollt
    auf den 04.05. und verbraucht das Restbudget der KW 19 – V1 ist nicht gedeckt, die Zelle
    20.05. zeigt nur ihr eigenes Budget 500. Die ganze Zelle auf den 26.05.: 20.05. leer,
    26.05. −500, übrige Tage und Liquidität unverändert. Gegenprobe ohne OP: 1.000 wandern."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 3, 1))
    _budget_woche(db, m, "4210", 19, "2500")
    _budget_woche(db, m, "4210", 21, "2500")
    if mit_op:
        _posten(db, m, "3000", date(2026, 5, 1), konto="4210")
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 4, 16), "4210", date(2026, 5, 4),
                 date(2026, 5, 20), "500")
    heute = date(2026, 5, 4)
    vorher = _zeile(db, m, heute, heute, konto="4210")
    liq_vorher = _liq(db, m, heute, heute, "2026-06-14")
    zelle = vorher["2026-05-20"]
    assert zelle == (-500.0 if mit_op else -1000.0)
    _verschieben(db, m, nutzer, monkeypatch, heute, "4210", date(2026, 5, 20), date(2026, 5, 26),
                 -zelle)
    nachher = _zeile(db, m, heute, heute, konto="4210")
    assert "2026-05-20" not in nachher and nachher["2026-05-26"] == zelle
    assert {t: w for t, w in nachher.items() if t != "2026-05-26"} == \
        {t: w for t, w in vorher.items() if t != "2026-05-20"}
    assert _liq(db, m, heute, heute, "2026-06-14") == liq_vorher


def _fremdbudget_gekuerzt(db, m, nutzer, monkeypatch):
    """Budget KW 35–37 je 1.000 netto (238 je Tag). Mo 24.08.: Budget 26.08. auf den 02.09.
    (V1, die Zelle 02.09. zeigt 476); Di 25.08. 09:00 eine Rechnung 1.190 fällig Do 27.08.: sie
    verbraucht das Restbudget der KW 35, V1 ist nicht gedeckt, die Zelle 02.09. zeigt nur ihr
    eigenes Budget 238."""
    _bank(db, m)
    for kw in (35, 36, 37):
        _budget_woche(db, m, "4920", kw, "1000")
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 8, 24), "4920", date(2026, 8, 26),
                 date(2026, 9, 2), "238", stunde=9)
    _posten(db, m, "1190", date(2026, 8, 27), erstellt_am=_zeitpunkt(date(2026, 8, 25), 9))
    heute = date(2026, 8, 25)
    assert _zeile(db, m, liq.wochen_start(heute), heute)["2026-09-02"] == -238.0
    return heute


@pytest.mark.parametrize("betrag, zelle_danach, ziel_danach",
                         [("238", None, -476.0), ("100", -138.0, -338.0)])
def test_budgetzelle_mit_gekuerztem_fremdbudget_wirkt(db, monkeypatch, betrag, zelle_danach,
                                                       ziel_danach):
    """Die ganze Zelle 02.09. (238) bzw. 100 daraus auf den 09.09.: die Zelle verliert genau den
    Betrag, der 09.09. gewinnt ihn; die Freigabe (LIVE) verschiebt den Rahmen von KW 36 nach
    KW 37 (vorher 1.190 / 1.190)."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    heute = _fremdbudget_gekuerzt(db, m, nutzer, monkeypatch)
    start = liq.wochen_start(heute)
    _verschieben(db, m, nutzer, monkeypatch, heute, "4920", date(2026, 9, 2), date(2026, 9, 9),
                 betrag, stunde=10)
    nachher = _zeile(db, m, start, heute)
    assert nachher.get("2026-09-02") == zelle_danach and nachher["2026-09-09"] == ziel_danach
    b = float(betrag)
    assert (_kz(db, m, 36, heute)["verfuegbar"], _kz(db, m, 37, heute)["verfuegbar"]) == \
        (1190.0 - b, 1190.0 + b)


def test_folgeverschiebung_nach_verbrauchtem_restbudget_der_herkunftswoche(db, monkeypatch):
    """Budget KW 35–38 je 1.000 netto. Mo 24.08.: Budget 26.08. → 02.09. (V1); Di 25.08.: die
    ganze Zelle 02.09. (476) auf den 09.09. (V2). Mo 31.08.: ein seit 27.08. überfälliger OP
    1.190 rollt auf den 31.08. und verbraucht das Restbudget der KW 36 – die Zelle 09.09. zeigt
    nur ihr eigenes Budget. Die ganze Zelle 09.09. auf den 16.09.: 09.09. leer, 16.09. +Zellwert."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    for kw in (35, 36, 37, 38):
        _budget_woche(db, m, "4920", kw, "1000")
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 8, 24), "4920", date(2026, 8, 26),
                 date(2026, 9, 2), "238")
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 8, 25), "4920", date(2026, 9, 2),
                 date(2026, 9, 9), "476")
    _posten(db, m, "1190", date(2026, 8, 27))
    heute = date(2026, 8, 31)
    vorher = _zeile(db, m, heute, heute)
    zelle = vorher["2026-09-09"]
    assert zelle == -238.0
    _verschieben(db, m, nutzer, monkeypatch, heute, "4920", date(2026, 9, 9), date(2026, 9, 16),
                 -zelle)
    nachher = _zeile(db, m, heute, heute)
    assert "2026-09-09" not in nachher and nachher["2026-09-16"] == vorher["2026-09-16"] + zelle
    assert (_kz(db, m, 37, heute)["verfuegbar"], _kz(db, m, 38, heute)["verfuegbar"]) == \
        (952.0, 1428.0)


def test_kette_ueber_leeren_tag_nimmt_das_ganze_gebrachte_budget(db):
    """4210, Budget KW 22 10.000 (Pfingstmontag frei, 2.500 je Tag); überfälliger OP 2.500
    (fällig 28.04., 1.100 angezahlt); OP 1.200 fällig 13.05., am 11.05. auf den 03.06. verschoben
    und dort bezahlt (verbraucht im Fenster ab KW 22 nichts mehr); OP 4.999,99 fällig 28.05.; OP
    2.000 fällig 29.05. Am Do 28.05.: 2.500 vom 29.05. auf den leeren 19.06., gleich danach 2.500
    vom 19.06. auf den 07.06. – wie direkt 29.05. → 07.06.: am 19.06. bleibt nichts, kein
    wirkungsloser Rest (das gebrachte Budget war beim Anlegen ganz gedeckt)."""
    from test_verschiebung_zuordnung import _verschiebe
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 3, 26))
    _budget_woche(db, m, "4210", 22, "10000")
    _posten(db, m, "2500", date(2026, 4, 28), konto="4210", bezahlt_betrag=D("1100"))
    _op(db, m, "1200", date(2026, 5, 13), konto="4210", bezahlt=((date(2026, 6, 3), "1200"),))
    _posten(db, m, "4999.99", date(2026, 5, 28), konto="4210")
    _posten(db, m, "2000", date(2026, 5, 29), konto="4210")
    _verschiebe(db, m, date(2026, 5, 13), date(2026, 6, 3), "-1200", konto="4210",
                angelegt=date(2026, 5, 11))
    v1 = _verschiebe(db, m, date(2026, 5, 29), date(2026, 6, 19), "-2500", konto="4210",
                     angelegt=date(2026, 5, 28))
    v2 = _verschiebe(db, m, date(2026, 6, 19), date(2026, 6, 7), "-2500", konto="4210",
                     angelegt=date(2026, 5, 28))
    db.commit()
    heute = date(2026, 6, 4)
    z = liq.Zuordnung(db, m, heute)
    assert z.rest == {} and z.budget_anspruch == {v1.id: D("500.00"), v2.id: D("500.00")}
    zeile = _zeile(db, m, date(2026, 6, 1), heute, 4, "4210")
    assert "2026-06-19" not in zeile and zeile["2026-06-07"] == -2500.0


# ---------------------------------------------------------------------------
# Offene Bestellung in der Zelle: nicht abgebbares Budget nie vor der Bestellung
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ganz", [False, True])
def test_teilbetrag_einer_zelle_mit_offener_bestellung_ohne_restbudget(db, monkeypatch, ganz):
    """4210, Budget KW 33 1.000 (200 je Tag); offene Bestellung 2.000 am 12.08. (eingeplant), die
    Woche hat kein Restbudget: die Zelle zeigt nur die Bestellung. 150 davon (bzw. die ganze
    Zelle – Gegenprobe) auf den 19.08.: 12.08. −1.850 / 19.08. −150 (bzw. 19.08. −2.000)."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 7, 1))
    _budget_woche(db, m, "4210", 33, "1000")
    b = svc.anlegen(db, m, nutzer, {
        "kreditor": "K", "bestellnummer": "B-1", "bestelldatum": date(2026, 7, 20),
        "zahldatum": date(2026, 8, 12), "betrag_netto": "2000", "konto_id": _konto(db, m, "4210").id})
    b.erstellt_am = _zeitpunkt(date(2026, 7, 20), 10)
    heute = date(2026, 8, 3)
    assert _zeile(db, m, heute, heute, 4, "4210") == {"2026-08-12": -2000.0}
    _verschieben(db, m, nutzer, monkeypatch, heute, "4210", date(2026, 8, 12), date(2026, 8, 19),
                 "2000" if ganz else "150", wochen=4)
    erwartet = {"2026-08-19": -2000.0} if ganz else {"2026-08-12": -1850.0, "2026-08-19": -150.0}
    assert _zeile(db, m, heute, heute, 4, "4210") == erwartet
    assert _liq(db, m, heute, heute, "2026-08-30", 4) == 98000.0


# ---------------------------------------------------------------------------
# Mehrere Entnahmen einer Woche: jede sieht die Zelle wie bei ihrer Anlage
# ---------------------------------------------------------------------------

def test_ganze_zelle_nach_mehreren_entnahmen_und_rechnung_wird_leer(db, monkeypatch):
    """Budget KW 13 5.000 netto (1.190 je Tag). 27.02.: 357 vom 27.03. auf den 07.04.; 06.03.:
    die ganze Zelle 26.03. auf den 27.03. und 357 vom 23.03. auf den 30.03.; 13.03.: Rechnung
    2.500 fällig 23.03. Am 20.03. die ganze Zelle 27.03. (1.453,71) auf den 26.05.: 27.03. leer,
    23.–25.03. unverändert."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 1, 1))
    _budget_woche(db, m, "4920", 13, "5000")
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 2, 27), "4920", date(2026, 3, 27),
                 date(2026, 4, 7), "357", wochen=13)
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 3, 6), "4920", date(2026, 3, 26),
                 date(2026, 3, 27), "1190", wochen=13, minute=1)
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 3, 6), "4920", date(2026, 3, 23),
                 date(2026, 3, 30), "357", wochen=13, minute=2)
    _posten(db, m, "2500", date(2026, 3, 23), erstellt_am=_zeitpunkt(date(2026, 3, 13), 15))
    heute = date(2026, 3, 20)
    vorher = _zeile(db, m, liq.wochen_start(heute), heute, 13)
    zelle = vorher["2026-03-27"]
    assert zelle == -1453.71
    _verschieben(db, m, nutzer, monkeypatch, heute, "4920", date(2026, 3, 27), date(2026, 5, 26),
                 -zelle, wochen=13)
    nachher = _zeile(db, m, liq.wochen_start(heute), heute, 13)
    assert "2026-03-27" not in nachher and nachher["2026-05-26"] == zelle
    for tag in ("2026-03-23", "2026-03-24", "2026-03-25", "2026-03-30", "2026-04-07"):
        assert nachher[tag] == vorher[tag]


# ---------------------------------------------------------------------------
# Zellendialog: Fensterbeginn der Planseite
# ---------------------------------------------------------------------------

def _dialog(db, m, konto_id: int, von: date, bis: date, start: date | None) -> list:
    """GET /plan/bestellungen wie plan.js (mit dem Fensterbeginn der Planseite)."""
    db.commit()
    nutzer = db.query(models.Benutzer).first() or _benutzer(db)
    return [(e["bestellnummer"], e["plan_tag"], e["plan_betrag"]) for e in
            bestell_router.plan_bestellungen(m.id, von=von.isoformat(), bis=bis.isoformat(),
                                             konto_id=str(konto_id),
                                             start=start.isoformat() if start else None,
                                             benutzer=nutzer, db=db)]


@pytest.mark.parametrize("abgeloest", [date(2026, 7, 29), date(2026, 8, 4)])
def test_zellendialog_mit_fensterbeginn_in_der_vorwoche(db, monkeypatch, abgeloest):
    """Heute Mi 05.08., Planseite ab Mo 27.07.: abgelöste Bestellung B-1 (1.190, Zahldatum
    29.07., abgelöst am 29.07. bzw. 04.08.). Der Plan führt sie am 29.07.; der Zellendialog mit
    dem Fensterbeginn der Planseite nennt sie dort (und nicht in der laufenden Woche) – ohne
    Angabe gilt der späteste Fensterbeginn, der die Zelle zeigt."""
    heute = date(2026, 8, 5)

    class _Datum(date):
        @classmethod
        def today(cls):
            return heute

    monkeypatch.setattr(svc, "date", _Datum)
    m = _mandant(db)
    k = _konto(db, m, "3400")
    _bank(db, m, tag=date(2026, 7, 17))
    b = models.Bestellung(mandant_id=m.id, kreditor="Lieferant", bestellnummer="B-1",
                          bestelldatum=date(2026, 7, 1), zahldatum=date(2026, 7, 29),
                          betrag_netto=D("1000"), ust_satz=D("19"), konto_id=k.id,
                          status="ABGELOEST", erstellt_am=_zeitpunkt(date(2026, 7, 1), 9),
                          entschieden_am=_zeitpunkt(date(2026, 7, 2), 9),
                          abgeloest_am=abgeloest, abgeloest_durch="ZAHLUNG")
    db.add(b)
    db.flush()
    bu = _zahlung(db, m, abgeloest, "1190", konto="3400")
    db.add(models.BestellAbloesung(mandant_id=m.id, bestellung_id=b.id, art="ZAHLUNG",
                                   buchung_id=bu.id, betrag=D("1190"), datum=abgeloest,
                                   abschliessend=True))
    vorwoche, woche = date(2026, 7, 27), date(2026, 8, 3)
    assert _zeile(db, m, vorwoche, heute, 3, "3400") == {"2026-07-29": -1190.0}
    eintrag = [("B-1", "2026-07-29", 1190.0)]
    assert _dialog(db, m, k.id, vorwoche, vorwoche + timedelta(days=4), vorwoche) == eintrag
    assert _dialog(db, m, k.id, woche, woche + timedelta(days=4), vorwoche) == []
    assert _dialog(db, m, k.id, vorwoche, vorwoche + timedelta(days=4), None) == eintrag


def test_zellendialog_mit_fensterbeginn_vor_dem_zahldatum_und_abloesung_heute(db, monkeypatch):
    """Heute Di 11.08., Planseite ab Mo 03.08.: B-1 (1.190, Zahldatum 05.08.) am 10.08. manuell
    abgelöst. Der Plan führt sie am 05.08.; der Dialog mit start 03.08. nennt sie dort und nicht
    am 10.08. (Gegenprobe P4: mit start 10.08. am ersten Bankarbeitstag des Fensters)."""
    heute = date(2026, 8, 11)

    class _Datum(date):
        @classmethod
        def today(cls):
            return heute

    monkeypatch.setattr(svc, "date", _Datum)
    m = _mandant(db)
    k = _konto(db, m, "4920")
    _bank(db, m, tag=date(2026, 7, 20))
    b = models.Bestellung(mandant_id=m.id, kreditor="Lieferant", bestellnummer="B-1",
                          bestelldatum=date(2026, 7, 1), zahldatum=date(2026, 8, 5),
                          betrag_netto=D("1000"), ust_satz=D("19"), konto_id=k.id,
                          status="ABGELOEST", erstellt_am=_zeitpunkt(date(2026, 7, 1), 9),
                          entschieden_am=_zeitpunkt(date(2026, 7, 2), 9),
                          abgeloest_am=date(2026, 8, 10), abgeloest_durch="MANUELL")
    db.add(b)
    db.flush()
    db.add(models.BestellAbloesung(mandant_id=m.id, bestellung_id=b.id, art="MANUELL",
                                   betrag=D("1190"), datum=date(2026, 8, 10), abschliessend=True))
    tag5, tag10 = date(2026, 8, 5), date(2026, 8, 10)
    assert _dialog(db, m, k.id, tag5, tag5, date(2026, 8, 3)) == [("B-1", "2026-08-05", 1190.0)]
    assert _dialog(db, m, k.id, tag10, tag10, date(2026, 8, 3)) == []
    assert _dialog(db, m, k.id, tag10, tag10, tag10) == [("B-1", "2026-08-10", 1190.0)]


# ---------------------------------------------------------------------------
# Planstand vor der Verschiebung eines überfälligen Postens
# ---------------------------------------------------------------------------

def test_ueberfaelliger_posten_nach_dem_planstand_vom_rolltag_verschoben(db, monkeypatch):
    """OP 1.500 fällig Mi 12.08., offen. Planstand Mo 17.08. (Referenz) führt ihn gerollt am
    17.08. Sa 22.08.: die Rolltagszelle Mo 24.08. auf den 02.09. verschoben. In allen KWs ist
    nichts verfügbar (AUTO): der verschobene, im Planstand am 17.08. geführte Anteil zählt wie
    Überfälliges in C3, in der Zielwoche nicht noch einmal."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 7, 1))
    _posten(db, m, "1500", date(2026, 8, 12))
    db.commit()
    snap = liq.erstelle_snapshot(db, m, start=date(2026, 8, 17), wochen=13, heute=date(2026, 8, 17))
    snap.erstellt_am = _zeitpunkt(date(2026, 8, 17), 6)
    snap.referenz = True
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 8, 22), "4920", date(2026, 8, 24),
                 date(2026, 9, 2), "1500", start=date(2026, 8, 17), wochen=4)
    for heute in (date(2026, 8, 23), date(2026, 8, 25), date(2026, 8, 27)):
        for kw in [k for k in (34, 35, 36, 37) if k >= heute.isocalendar().week]:
            e = _kz(db, m, kw, heute, basis=None)
            assert e["verfuegbar"] <= 0, (heute, kw, e)
        assert _kz(db, m, 36, heute, basis=None)["abweichung"]["ueberfaellig"] == 1500.0


def test_planstand_minderausgabe_nicht_mit_frueh_bezahltem_posten_verrechnet(db, monkeypatch):
    """4210, Planstand Mo 21.09. (Referenz): OP A 2.000 (fällig 20.09., offen, gerollt am
    21.09.) und OP B 1.000 (fällig Sa 31.10., schon am 25.09. bezahlt). So 11.10.: 1.540 der
    Rolltagszelle 12.10. auf den 16.10. verschoben. Die Minderausgabe von A erklärt die
    Mehrausgabe der frühen Zahlung von B nicht: KW 44 (Soll 1.000 von B) zeigt nichts verfügbar."""
    m = _mandant(db)
    m.bundesland = "SN"
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 9, 1))
    _posten(db, m, "2000", date(2026, 9, 20), konto="4210")
    _op(db, m, "1000", date(2026, 10, 31), konto="4210", bezahlt=((date(2026, 9, 25), "1000"),))
    db.commit()
    snap = liq.erstelle_snapshot(db, m, start=date(2026, 9, 21), wochen=13, heute=date(2026, 9, 21))
    snap.erstellt_am = _zeitpunkt(date(2026, 9, 21), 6)
    snap.referenz = True
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 10, 11), "4210", date(2026, 10, 12),
                 date(2026, 10, 16), "1540", start=date(2026, 9, 21), wochen=6)
    heute = date(2026, 10, 12)
    e44 = _kz(db, m, 44, heute, konto="4210", basis=None)
    assert e44["abweichung"]["ueberfaellig"] == 2000.0 and e44["verfuegbar"] <= 0
    assert _kz(db, m, 42, heute, konto="4210", basis=None)["verfuegbar"] <= 0


# ---------------------------------------------------------------------------
# Zahlungen am Anlagetag und ohne Beleg
# ---------------------------------------------------------------------------

def test_teilzahlung_am_anlagetag_nach_der_verschiebung_aendert_die_freigabe_nicht(db, monkeypatch):
    """OP 2.500 fällig Fr 17.07. Mi 24.06.: 1.925 der Zelle auf Mo 20.07. verschoben, danach
    1.000 mit Bankkonto gezahlt. Die Zelle zeigte beim Anlegen 2.500 (mehr als der Rest nach
    der Zahlung): die Zahlung kam danach – die Freigabe (Stand gestern) bleibt KW 29 575 /
    KW 30 1.925 gebunden."""
    heute = date(2026, 6, 24)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 5, 1))
    p = _posten(db, m, "2500", date(2026, 7, 17))
    _verschieben(db, m, nutzer, monkeypatch, heute, "4920", date(2026, 7, 17), date(2026, 7, 20),
                 "1925", start=date(2026, 6, 22), stunde=10)
    vorher = [_kz(db, m, kw, heute)["gebunden"]["summe"] for kw in (29, 30)]
    assert vorher == [575.0, 1925.0]
    api.zahlung_erfassen(p.id, api.ZahlungNeu(betrag=1000.0, datum=heute.isoformat(),
                                              bank_konto_id=_konto(db, m, "1200").id),
                         benutzer=nutzer, db=db)
    assert [_kz(db, m, kw, heute)["gebunden"]["summe"] for kw in (29, 30)] == vorher
    # am Folgetag ist die Zahlung im Ist: am 17.07. bleibt nichts, am 20.07. der offene Rest
    assert _zeile(db, m, date(2026, 6, 22), date(2026, 6, 25), 6) == {"2026-07-20": -1500.0}


def test_heute_ohne_beleg_bezahlt_und_import_mit_buchungstag_gestern_nicht_doppelt(db, monkeypatch):
    """OP 1.500 fällig Mi 02.09. Am 02.09. „Zahlung erfassen“ ohne Bankkonto, das Ist kommt über
    den Buchhaltungsimport mit Buchungstag 01.09. (ohne Posten-Bezug): die Zahlung steht im Ist
    bis gestern – KW 36 zeigt weder gebunden noch einen negativen Spielraum (Gegenprobe P5:
    Import erst heute, dann bleibt der Posten gebunden)."""
    heute = date(2026, 9, 2)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    a = _posten(db, m, "1500", date(2026, 9, 2))
    b = _posten(db, m, "800", date(2026, 9, 2), konto="4930")
    for p, konto, tag in ((a, "4920", date(2026, 9, 1)), (b, "4930", heute)):
        api.zahlung_erfassen(p.id, api.ZahlungNeu(betrag=float(p.betrag_brutto),
                                                  datum=heute.isoformat()), benutzer=nutzer, db=db)
        _zahlung(db, m, tag, str(p.betrag_brutto), konto=konto, text="Import")
    e = _kz(db, m, 36, heute)
    assert (e["gebunden"]["summe"], e["verfuegbar"]) == (0.0, 0.0)
    e = _kz(db, m, 36, heute, konto="4930")
    assert (e["gebunden"]["summe"], e["verfuegbar"]) == (800.0, 0.0)


def test_heute_ohne_beleg_ganz_bezahlter_ueberfaelliger_posten_aendert_spaetere_kw_nicht(db):
    """Mo 14.09.: ein seit Do 10.09. überfälliger OP 750 (Teilzahlung 375 am 02.09.) wird heute
    ohne Bankkonto ganz bezahlt, das Ist kommt heute über den Import. Stand gestern war er
    überfällig (C3), das Soll der laufenden Woche führt ihn am 14.09. – jetzt bezahlt statt
    gerollt; der Gegenposten bleibt, KW 39 ändert sich nicht."""
    heute = date(2026, 9, 14)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    p = _op(db, m, "750", date(2026, 9, 10), konto="4210", bezahlt=((date(2026, 9, 2), "375"),))
    vorher = _kz(db, m, 39, heute, konto="4210")
    api.zahlung_erfassen(p.id, api.ZahlungNeu(betrag=375.0, datum=heute.isoformat()),
                         benutzer=nutzer, db=db)
    _zahlung(db, m, heute, "375", konto="4210", text="Import")
    nachher = _kz(db, m, 39, heute, konto="4210")
    assert (nachher["abweichung"]["ueberfaellig"], nachher["verfuegbar"]) == \
        (vorher["abweichung"]["ueberfaellig"], vorher["verfuegbar"])


# ---------------------------------------------------------------------------
# Laufzeit: angezeigtes Budget je Zeile und Anlagetag einmal gerechnet
# ---------------------------------------------------------------------------

def test_angezeigtes_budget_je_zeile_und_anlagetag_einmal_gerechnet(db, monkeypatch):
    """Zwölf ganz verschobene Budgetzellen mit offener Bestellung auf einer Zeile, am selben Tag
    zu verschiedenen Uhrzeiten angelegt: die Zuordnung rechnet die expliziten Beträge der Zeile
    am Anlagetag einmal (nur diese Zeile), nicht je Verschiebung über den ganzen Mandanten."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    for kw in range(33, 48):
        _budget_woche(db, m, "4920", kw, "5000")
    k = _konto(db, m, "4920")
    anlage = date(2026, 8, 3)
    for i in range(12):
        von = liq.naechster_bankarbeitstag(date(2026, 8, 11) + timedelta(days=7 * i), m.bundesland)
        b = svc.anlegen(db, m, nutzer, {
            "kreditor": f"K{i}", "bestellnummer": f"B-{i}", "bestelldatum": date(2026, 7, 20),
            "zahldatum": von, "betrag_netto": "1000", "konto_id": k.id})
        b.erstellt_am = _zeitpunkt(date(2026, 7, 20), 10)
        db.add(models.PlanVerschiebung(mandant_id=m.id, quelle="PLAN", konto_id=k.id, von=von,
                                       nach=von + timedelta(days=14), betrag=D("-2380"),
                                       erstellt_am=_zeitpunkt(anlage, 8, i)))
    db.commit()
    aufrufe = []
    original = liq._explizite_fluesse

    def zaehlen(*args, **kwargs):
        aufrufe.append(kwargs.get("nur_konto"))
        return original(*args, **kwargs)

    monkeypatch.setattr(liq, "_explizite_fluesse", zaehlen)
    z = liq.Zuordnung(db, m, date(2026, 8, 10))
    assert aufrufe == [k.id]
    assert sum(z.budget_anspruch.values(), D("0")) == D("12") * D("1190")


# ---------------------------------------------------------------------------
# Vorbestehend: Rest einer Verschiebung, Vortrag-Ketten
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("art", ["storno", "faellig"])
def test_rest_einer_verschiebung_nimmt_keine_spaeter_erfasste_rechnung(db, monkeypatch, art):
    """Di 11.08.: Rechnung A (1.000, fällig 19.08.) ganz auf den 02.09. verschoben. Mi 12.08.:
    A storniert bzw. ihre Fälligkeit auf den 30.09. geändert. Do 13.08.: Rechnung B (1.000,
    fällig 19.08.) erfasst – sie stand beim Anlegen nicht in der Zelle und bleibt am 19.08.;
    der Rest der Verschiebung bleibt wirkungslos."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 7, 1))
    a = _posten(db, m, "1000", date(2026, 8, 19))
    start = date(2026, 8, 10)
    _verschieben(db, m, nutzer, monkeypatch, date(2026, 8, 11), "4920", date(2026, 8, 19),
                 date(2026, 9, 2), "1000", start=start)
    if art == "storno":
        a.status = "STORNIERT"
    else:
        a.faellig_am = date(2026, 9, 30)
    _posten(db, m, "1000", date(2026, 8, 19), erstellt_am=_zeitpunkt(date(2026, 8, 13), 15))
    zeile = _zeile(db, m, start, date(2026, 8, 13))
    assert zeile.get("2026-08-19") == -1000.0 and "2026-09-02" not in zeile


def test_folgeverschiebung_eines_vortrags_nimmt_nur_den_dorthin_verschobenen_anteil(db, monkeypatch):
    """Leasingrate 800 am 07.09. ungebucht (Vortrag). Di 08.09.: 600 des Vortrags 09.09. auf den
    21.09.; Do 10.09. (Vortrag 11.09. −200, 21.09. −600): die ganze Zelle 21.09. auf den 28.09. –
    der Rest −200 bleibt am Vortragstag 11.09."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 7, 1))
    kid = _konto(db, m, "4920").id
    db.add(models.Dauerbuchung(mandant_id=m.id, name="Leasing", art="KREDITOR", konto_id=kid,
                               betrag_brutto=D("800"), intervall="MONATLICH", stichtag=7,
                               gueltig_von=date(2026, 1, 1), vortragen=True))
    for t in (date(2026, 7, 7), date(2026, 8, 7)):
        _zahlung(db, m, t, "800")

    def vortrag(heute: date) -> dict:
        db.commit()
        p = liq.berechne_plan(db, m, start=liq.wochen_start(heute), wochen=6, heute=heute)
        z = next(k for g in p["zeilen"] for k in g["kinder"] if k["nummer"] == "4920")
        return {t: w for t, w in (z.get("vortrag") or {}).items() if w}

    def verschieben(heute: date, von: str, nach: str) -> None:
        _heute_setzen(monkeypatch, heute)
        r = api.verschiebung_anlegen(m.id, api.VerschiebungNeu(
            key=str(kid), quelle="VORTRAG", von=von, bis=von, nach=nach, betrag=600.0,
            start=liq.wochen_start(heute).isoformat(), wochen=6), benutzer=nutzer, db=db)
        for vid in r["ids"]:
            db.get(models.PlanVerschiebung, vid).erstellt_am = _zeitpunkt(heute)
        db.commit()

    assert vortrag(date(2026, 9, 8)) == {"2026-09-09": -800.0}
    verschieben(date(2026, 9, 8), "2026-09-09", "2026-09-21")
    assert vortrag(date(2026, 9, 10)) == {"2026-09-11": -200.0, "2026-09-21": -600.0}
    verschieben(date(2026, 9, 10), "2026-09-21", "2026-09-28")
    assert vortrag(date(2026, 9, 10)) == {"2026-09-11": -200.0, "2026-09-28": -600.0}
