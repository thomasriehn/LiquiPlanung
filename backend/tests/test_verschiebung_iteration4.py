"""Verschiebungen, vierte Korrekturrunde: Rückschritte der Iteration 3 gegenüber dem
gesicherten Rechenkern (Zellendialog, Budget je Sicht, Überschuss, aufgeteilte Bestandteile,
Ratenstatus am Fälligkeitstag, offene Bestellung in der Zelle, Laufzeit) sowie vorbestehende
Fehler (Planstand vor der Verschiebung, Anlagezeitpunkt von Posten, Projektion, Zellendialog,
Ketten, Szenario-Ansicht). Jeder Test schlug gegen den Stand der Iteration 3 fehl – außer
den Gegenproben (umgekehrte Anlagefolge bei RD, Planstand nach der Verschiebung, Posten ohne
Anlagezeitpunkt) und der Teilzahlung ohne Beleg (sichert V1 der dritten Runde gegen RA ab);
die Tests der Abschlussprüfung (Zahlstand beim Anlegen nach späterer Schlusszahlung, Ordnung
nach Fälligkeit vor einer späteren Verschiebung, Budget anderer Herkunft, Bestellung bei
unbekanntem Anlagetag der Verschiebung) schlugen auch gegen den ersten Stand dieser Runde fehl;
nur synthetische Daten."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event

from app import models
from app.routers import api
from app.services import bestellungen as svc
from app.services import liquiditaet as liq

from szenario_bausteine import ALTBESTAND
from test_bestellungen import _bank, _benutzer, _budget_woche, _zahlung
from test_liquiditaet import _konto, _mandant
from test_verschiebung_zuordnung import _op, _verschiebe

D = Decimal


# ---------------------------------------------------------------------------
# Hilfen: Zellendialog wie POST /plan/verschiebungen an einem festen Tag
# ---------------------------------------------------------------------------

def _heute_setzen(monkeypatch, heute: date) -> None:
    class _Datum(date):
        @classmethod
        def today(cls):
            return heute

    monkeypatch.setattr(api, "date", _Datum)


def _api_verschieben(db, m, nutzer, konto: str, von: date, nach: date, betrag, start: date,
                     wochen: int = 6, bis: date | None = None, szenario_id: int | None = None):
    db.commit()
    return api.verschiebung_anlegen(m.id, api.VerschiebungNeu(
        key=str(_konto(db, m, konto).id), von=von.isoformat(), bis=(bis or von).isoformat(),
        nach=nach.isoformat(), betrag=float(abs(D(str(betrag)))), start=start.isoformat(),
        wochen=wochen, szenario_id=szenario_id), benutzer=nutzer, db=db)


def _zeile(db, m, start: date, heute: date, wochen: int = 6, konto: str = "4920") -> dict:
    db.commit()
    p = liq.berechne_plan(db, m, start=start, wochen=wochen, heute=heute)
    z = next((k for g in p["zeilen"] for k in g["kinder"] if k["nummer"] == konto), {"plan": {}})
    return {t: v for t, v in z["plan"].items() if v}


def _liq(db, m, start: date, heute: date, tag: str, wochen: int = 6) -> float:
    db.commit()
    return liq.berechne_plan(db, m, start=start, wochen=wochen, heute=heute)["bestaende"][
        "liquiditaet"][tag]


def _posten(db, m, betrag, faellig: date, konto: str = "4920", **felder) -> models.OffenerPosten:
    """OP mit frei wählbaren Feldern (Art, Partner, Status …)."""
    werte = {"art": "KREDITOR", "partner": "Lieferant", "bezahlt_betrag": D("0"),
             "erstellt_am": ALTBESTAND, **felder}
    p = models.OffenerPosten(mandant_id=m.id, faellig_am=faellig, betrag_brutto=D(betrag),
                             konto_id=_konto(db, m, konto).id, **werte)
    db.add(p)
    db.flush()
    return p


def _kz(db, m, kw: int, heute: date, konto: str = "4920", basis="LIVE", jahr: int = 2026) -> dict:
    db.commit()
    r = svc.freigabe_kennzahlen(db, m, konto_id=_konto(db, m, konto).id, betrag_brutto=D("0"),
                                jahr=jahr, kw=kw, basis=basis, heute=heute)
    return next(e for e in r["ebenen"] if e["ebene"] == "konto")


# ---------------------------------------------------------------------------
# RA: unsichtbare, beim Anlegen schon bezahlte Tranchen sind keine Kandidaten
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fall", ["anzahlung", "vorab_bezahlt"])
def test_ganze_zelle_neben_unsichtbarer_tranche_wird_leer(db, monkeypatch, fall):
    """Budget KW 35 1.000 netto (238 je Tag); am 28.08. ein OP 1.000 mit Anzahlung 400 am 05.08.
    (Zelle 600 + 118 Restbudget) bzw. ein schon am 05.08. bezahlter OP 500 (Zelle 238 Budget).
    Heute 12.08. wird die ganze Zelle über den Zellendialog auf den 07.09. verschoben: die Zelle
    ist leer, am Ziel kommt der volle Zellwert an, die Freigabe verschiebt den Rahmen mit."""
    heute = date(2026, 8, 12)
    _heute_setzen(monkeypatch, heute)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 35, "1000")
    if fall == "anzahlung":
        _op(db, m, "1000", date(2026, 8, 28), bezahlt=((date(2026, 8, 5), "400"),))
        zelle, kw35, kw37 = -718.0, 472.0, 118.0
    else:
        _op(db, m, "500", date(2026, 8, 28), bezahlt=((date(2026, 8, 5), "500"),))
        zelle, kw35, kw37 = -238.0, 952.0, 238.0
    start = date(2026, 8, 10)
    assert _zeile(db, m, start, heute)["2026-08-28"] == zelle
    _api_verschieben(db, m, nutzer, "4920", date(2026, 8, 28), date(2026, 9, 7), zelle, start)
    nachher = _zeile(db, m, start, heute)
    assert "2026-08-28" not in nachher and nachher["2026-09-07"] == zelle
    assert (_kz(db, m, 35, heute)["verfuegbar"], _kz(db, m, 37, heute)["verfuegbar"]) == (kw35, kw37)


def test_verschiebung_ueber_der_zelle_nimmt_keine_heute_gezahlte_teilzahlung_mit(db):
    """Heute Mi 12.08.: OP 1.200 fällig Fr 14.08., heute 600 angezahlt – die Zelle zeigt den
    Rest 600, die Anzahlung steht in keiner Sicht. Eine Verschiebung von 1.700 (mehr als die
    Zelle) auf den 07.09. bewegt nur den Rest, der Überschuss bleibt wirkungslos: Die Freigabe
    („Stand gestern“ – die heutige Anzahlung gilt dort noch als offen) führt die Anzahlung
    weiter in KW 33 und in KW 37 nur den Rest."""
    heute = date(2026, 8, 12)
    m = _mandant(db)
    _bank(db, m)
    _op(db, m, "1200", date(2026, 8, 14), bezahlt=((heute, "600"),))
    _verschiebe(db, m, date(2026, 8, 14), date(2026, 9, 7), "-1700", angelegt=heute)
    assert _zeile(db, m, date(2026, 8, 10), heute) == {"2026-09-07": -600.0}
    gebunden = [_kz(db, m, kw, heute)["gebunden"]["summe"] for kw in (33, 37)]
    assert gebunden == [600.0, 600.0]


def test_teilzahlung_ohne_beleg_zaehlt_bei_passender_hoehe_als_offen(db):
    """OP 1.500 fällig 04.08. (Budget KW 32), 500 als bezahlt erfasst ohne zugeordnete Zahlung
    (Zahltag unbekannt). Die am 01.08. angelegte Verschiebung über 1.500 auf den 18.08.
    entspricht dem ganzen Posten: die Teilzahlung war beim Anlegen noch offen – kein Budget
    der vergangenen KW 32 wandert mit, am 18.08. steht nur der offene Rest."""
    heute = date(2026, 8, 10)
    m = _mandant(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 32, "10000")
    _posten(db, m, "1500", date(2026, 8, 4), bezahlt_betrag=D("500"))
    _zahlung(db, m, date(2026, 8, 5), "500")
    _verschiebe(db, m, date(2026, 8, 4), date(2026, 8, 18), "-1500", angelegt=date(2026, 8, 1))
    assert _zeile(db, m, heute, heute, 3) == {"2026-08-18": -1000.0}
    assert _liq(db, m, heute, heute, "2026-08-30", 3) == 98500.0


def test_ganze_rolltagzelle_mit_heute_gezahlter_teilzahlung_wird_leer(db, monkeypatch):
    """Karfreitag 03.04.2026 (BY): Rolltag Di 07.04. zeigt OP1 500 und den Rest 2.000 von OP2
    (2.500, heute 500 angezahlt). Die ganze Zelle −2.500 auf den 08.04.: beide wandern ganz,
    die heutige Teilzahlung (in keiner Sicht zu sehen) zählt nicht als offen."""
    heute = date(2026, 4, 3)
    _heute_setzen(monkeypatch, heute)
    m = _mandant(db)
    m.bundesland = "BY"
    nutzer = _benutzer(db)
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=_konto(db, m, "1200").id,
                          datum=date(2026, 1, 23), wert=D("100000")))
    _op(db, m, "500", date(2026, 3, 12), konto="4210")
    _op(db, m, "2500", date(2026, 3, 20), konto="4210", bezahlt=((heute, "500"),))
    start = date(2026, 3, 30)
    assert _zeile(db, m, start, heute, 3, "4210") == {"2026-04-07": -2500.0}
    _api_verschieben(db, m, nutzer, "4210", date(2026, 4, 7), date(2026, 4, 8), "2500", start, 3)
    assert _zeile(db, m, start, heute, 3, "4210") == {"2026-04-08": -2500.0}


# ---------------------------------------------------------------------------
# RB: je Sicht nur bewegen, was die Ursprungswoche abgeben kann
# ---------------------------------------------------------------------------

def test_ganze_zelle_einer_offenen_bestellung_ohne_restbudget_ohne_phantom(db, monkeypatch):
    """Budget KW 35 1.190 brutto, eine offene (eingeplante) Bestellung 1.190 am 24.08. verbraucht
    es ganz – die Zelle zeigt nur die Bestellung. Ganz auf den 07.09. verschoben: Zelle leer,
    am Ziel 1.190, Zeilensumme und Liquidität unverändert (kein verschobenes Budget dazu)."""
    heute = date(2026, 8, 12)
    _heute_setzen(monkeypatch, heute)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 35, "1000")
    svc.anlegen(db, m, nutzer, {"kreditor": "K", "bestellnummer": "B-1",
                                "bestelldatum": date(2026, 8, 1), "zahldatum": date(2026, 8, 24),
                                "betrag_netto": "1000", "konto_id": _konto(db, m, "4920").id})
    start = date(2026, 8, 10)
    assert _zeile(db, m, start, heute) == {"2026-08-24": -1190.0}
    liq0 = _liq(db, m, start, heute, "2026-09-20")
    _api_verschieben(db, m, nutzer, "4920", date(2026, 8, 24), date(2026, 9, 7), "1190", start)
    assert _zeile(db, m, start, heute) == {"2026-09-07": -1190.0}
    assert _liq(db, m, start, heute, "2026-09-20") == liq0
    snap = liq.erstelle_snapshot(db, m, start=start, wochen=6, heute=heute)
    kid = _konto(db, m, "4920").id
    assert sum(liq.snapshot_fluesse(snap).get(kid, {}).values()) == D("-1190")


def test_budgetverschiebung_ohne_restbudget_in_der_projektion_aendert_liquiditaet_nicht(
        db, monkeypatch):
    """Heute Fr 08.05., Budget KW 20 2.500 (625 je Bankarbeitstag), ein überfälliger OP 3.000;
    die Budgetzelle Mo 11.05. wird auf Mi 13.05. verschoben: die Liquidität am 24.05. bleibt."""
    heute = date(2026, 5, 8)
    _heute_setzen(monkeypatch, heute)
    m = _mandant(db)
    nutzer = _benutzer(db)
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=_konto(db, m, "1200").id,
                          datum=date(2026, 4, 1), wert=D("100000")))
    _budget_woche(db, m, "4210", 20, "2500")
    _op(db, m, "3000", date(2026, 5, 5), konto="4210")
    start = date(2026, 5, 4)
    liq0 = _liq(db, m, start, heute, "2026-05-24", 3)
    _api_verschieben(db, m, nutzer, "4210", date(2026, 5, 11), date(2026, 5, 13), "625", start, 3)
    assert _zeile(db, m, start, heute, 3, "4210")["2026-05-13"] == -1250.0
    assert _liq(db, m, start, heute, "2026-05-24", 3) == liq0


# ---------------------------------------------------------------------------
# RC: Überschuss in der Folge der Verschiebungen, nie auf vergangene Tage
# ---------------------------------------------------------------------------

def test_ganz_verschobene_zelle_nach_ueberschuss_bleibt_leer(db, monkeypatch):
    """Budget KW 35 238 je Tag; die Montagszelle geht nach KW 37, danach wird eine Rechnung 600
    für Mittwoch erfasst (die übrigen Tage zeigen nun je 88). Die ganze Donnerstagszelle (88)
    auf den 08.09.: Donnerstag leer, Dienstag und Freitag unverändert, am Ziel 88."""
    heute = date(2026, 8, 12)
    _heute_setzen(monkeypatch, heute)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 35, "1000")
    start = date(2026, 8, 10)
    _api_verschieben(db, m, nutzer, "4920", date(2026, 8, 24), date(2026, 9, 7), "238", start)
    _op(db, m, "600", date(2026, 8, 26))
    vorher = _zeile(db, m, start, heute)
    assert vorher["2026-08-27"] == -88.0
    _api_verschieben(db, m, nutzer, "4920", date(2026, 8, 27), date(2026, 9, 8), "88", start)
    nachher = _zeile(db, m, start, heute)
    assert "2026-08-27" not in nachher and nachher["2026-09-08"] == -88.0
    assert (nachher["2026-08-25"], nachher["2026-08-28"]) == (-88.0, -88.0)
    assert sum(nachher.values()) == pytest.approx(sum(vorher.values()))


def test_ueberschuss_faellt_nicht_auf_vergangene_tage(db):
    """Budget KW 33 1.000 netto (238 je Tag), heute Mi 12.08.; am 10.08. das Budget des Do
    13.08. nach KW 35 verschoben, danach eine Rechnung 952 für Fr 14.08.: die Woche hat nur noch
    238 Restbudget. Mo/Di (Soll der Vergangenheit, C1) bleiben, was die Woche ab heute nicht
    abgeben kann, fehlt am Ziel – die Summe der Sicht bleibt."""
    m = _mandant(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 33, "1000")
    _verschiebe(db, m, date(2026, 8, 13), date(2026, 8, 24), "-238", angelegt=date(2026, 8, 10))
    _op(db, m, "952", date(2026, 8, 14))
    heute = date(2026, 8, 12)
    plan = _zeile(db, m, date(2026, 8, 10), heute, 3)
    assert (plan["2026-08-10"], plan["2026-08-11"]) == (-47.6, -47.6)
    assert "2026-08-12" not in plan and "2026-08-13" not in plan
    assert plan["2026-08-24"] == -142.8
    assert sum(plan.values()) == pytest.approx(-1190.0)


# ---------------------------------------------------------------------------
# RD: aufgeteilter Bestandteil – Zahlungen nach Fälligkeit der Teile
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("folge", [(2, 9), (9, 2)])
def test_aufgeteilter_posten_rest_nach_puenktlicher_zahlung_am_spaeteren_ziel(db, folge):
    """OP 2.000 fällig 26.08.; am 24.08. je 1.000 auf den 02.09. und den 09.09. verschoben (in
    beiden Reihenfolgen), am 02.09. pünktlich 1.000 gezahlt: am 03.09. steht der Rest am 09.09.,
    nichts ist überfällig."""
    m = _mandant(db)
    _bank(db, m)
    p = _op(db, m, "2000", date(2026, 8, 26))
    for tag in folge:
        _verschiebe(db, m, date(2026, 8, 26), date(2026, 9, tag), "-1000",
                    angelegt=date(2026, 8, 24))
    p.bezahlt_betrag = D("1000")
    _zahlung(db, m, date(2026, 9, 2), "1000").posten_id = p.id
    heute = date(2026, 9, 3)
    assert _zeile(db, m, date(2026, 8, 31), heute, 2) == {"2026-09-09": -1000.0}
    for kw in (36, 37):
        assert _kz(db, m, kw, heute)["abweichung"]["ueberfaellig"] == 0.0


@pytest.mark.parametrize("folge", [(18, 25), (25, 18)])
def test_aufgeteilte_rate_nach_zahlung_des_ersten_teils_ohne_vortrag(db, folge):
    """Miete 2.000 am Fr 11.09.; am 07.09. je 1.000 auf den 18.09. und den 25.09. verschoben, am
    18.09. 1.000 gezahlt: die zweite Hälfte steht am 25.09., kein Vortrag, gebunden in KW 39."""
    m = _mandant(db)
    _bank(db, m)
    db.add(models.Dauerbuchung(mandant_id=m.id, name="Miete", art="KREDITOR",
                               konto_id=_konto(db, m, "4920").id, betrag_brutto=D("2000"),
                               intervall="MONATLICH", stichtag=11, gueltig_von=date(2026, 9, 1),
                               aktiv=True, vortragen=True))
    for tag in folge:
        _verschiebe(db, m, date(2026, 9, 11), date(2026, 9, tag), "-1000",
                    angelegt=date(2026, 9, 7))
    _zahlung(db, m, date(2026, 9, 18), "1000")
    for heute in (date(2026, 9, 18), date(2026, 9, 21)):
        db.commit()
        p = liq.berechne_plan(db, m, start=liq.wochen_start(heute), wochen=2, heute=heute)
        z = next(k for g in p["zeilen"] for k in g["kinder"] if k["nummer"] == "4920")
        assert z["plan"]["2026-09-25"] == -1000.0 and p["vortraege"] == []
        assert p["bestaende"]["liquiditaet"]["2026-09-27"] == 98000.0
        k = _kz(db, m, 39, heute)
        assert (k["gebunden"]["summe"], k["abweichung"]["vortraege"]) == (1000.0, 0.0)


# ---------------------------------------------------------------------------
# RE: Rate am Fälligkeitstag (Freigabe, Stand gestern) bleibt gebunden
# ---------------------------------------------------------------------------

def test_rate_am_faelligkeitstag_bleibt_gebunden_trotz_op_zahlung_per_ausgleich(db):
    """Miete 800 am 14., heute Fr 14.08.; ein OP 1.200 (fällig 12.08.) am 12.08. per Bankumsatz
    mit OP-Ausgleich bezahlt, der Umsatz als Buchung ohne Posten-Referenz verbucht: die heute
    fällige Rate bleibt in der Freigabe gebunden, kein Spielraum."""
    m = _mandant(db)
    bank, k = _konto(db, m, "1200"), _konto(db, m, "4920")
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=bank.id, datum=date(2026, 8, 1),
                          wert=D("100000")))
    db.add(models.Dauerbuchung(mandant_id=m.id, name="Miete", art="KREDITOR", konto_id=k.id,
                               betrag_brutto=D("800"), intervall="MONATLICH", stichtag=14,
                               gueltig_von=date(2026, 1, 1), aktiv=True, vortragen=True))
    p = models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="Lieferant",
                             faellig_am=date(2026, 8, 12), betrag_brutto=D("1200"), konto_id=k.id,
                             status="BEZAHLT", bezahlt_am=date(2026, 8, 12), bezahlt_betrag=D("1200"))
    db.add(p)
    db.flush()
    t = models.BankTransaktion(mandant_id=m.id, konto_id=bank.id, buchungstag=date(2026, 8, 12),
                               betrag=D("-1200"), partner="Lieferant")
    db.add(t)
    db.flush()
    db.add(models.AusgleichZuordnung(mandant_id=m.id, transaktion_id=t.id, posten_id=p.id,
                                     betrag=D("1200")))
    db.add(models.Buchung(mandant_id=m.id, datum=date(2026, 8, 12), konto_nr="4920",
                          gegenkonto_nr="1200", betrag=D("1200"), sh="S"))
    e = _kz(db, m, 33, date(2026, 8, 14))
    assert (e["gebunden"]["summe"], e["verfuegbar"]) == (800.0, 0.0)


# ---------------------------------------------------------------------------
# RF: ganze Zelle mit offener Bestellung und Restbudget
# ---------------------------------------------------------------------------

def test_ganze_zelle_mit_offener_bestellung_und_restbudget_wird_leer(db, monkeypatch):
    """Wochenbudget KW 36 2.000 netto (2.380 brutto), offene Bestellung 1.190 am 02.09.: die
    Zelle zeigt 1.428 (Bestellung und 238 Restbudget). Ganz auf den 09.09. verschoben: die
    Zelle ist leer, die übrigen Tage bleiben bei 238, am Ziel 1.428; der Zellendialog nennt die
    Bestellung mit 1.190 am 09.09."""
    heute = date(2026, 8, 24)
    _heute_setzen(monkeypatch, heute)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 36, "2000")
    _budget_woche(db, m, "4920", 37, "2000")
    svc.anlegen(db, m, nutzer, {"kreditor": "K", "bestellnummer": "B-1",
                                "bestelldatum": date(2026, 8, 1), "zahldatum": date(2026, 9, 2),
                                "betrag_netto": "1000", "konto_id": _konto(db, m, "4920").id})
    start = date(2026, 8, 24)
    vorher = _zeile(db, m, start, heute, 3)
    assert vorher["2026-09-02"] == -1428.0
    _api_verschieben(db, m, nutzer, "4920", date(2026, 9, 2), date(2026, 9, 9), "1428", start, 3)
    nachher = _zeile(db, m, start, heute, 3)
    erwartet = {t: v for t, v in vorher.items() if t != "2026-09-02"}
    erwartet["2026-09-09"] = vorher["2026-09-09"] - 1428.0
    assert nachher == erwartet
    dialog = svc.plan_bestellungen(db, m, konto_id=_konto(db, m, "4920").id,
                                   von=date(2026, 8, 31), bis=date(2026, 9, 13), heute=heute)
    assert [(e["plan_tag"], e["plan_betrag"]) for e in dialog] == [("2026-09-09", 1190.0)]


# ---------------------------------------------------------------------------
# RG: Laufzeit – keine Historie laden, keine unbeschränkten ID-Listen, Zellendialog je Konto
# ---------------------------------------------------------------------------

def test_lange_historie_ohne_literale_id_listen(db):
    """300 vor Monaten bezahlte OPs auf dem Konto einer Verschiebung vom Rolltag (am Samstag
    vom Montag angelegt): keine SQL-Anweisung der Berechnung trägt eine ID-Liste dieser Länge
    (PostgreSQL/psycopg 3 erlaubt höchstens 65.535 Parameter)."""
    m = _mandant(db)
    _bank(db, m)
    kid = _konto(db, m, "4920").id
    for i in range(300):
        tag = date(2026, 3, 2) + timedelta(days=i % 120)
        p = models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner=f"A{i}", faellig_am=tag,
                                 betrag_brutto=D("100"), konto_id=kid, status="BEZAHLT",
                                 bezahlt_am=tag, bezahlt_betrag=D("100"))
        db.add(p)
        db.flush()
        db.add(models.Buchung(mandant_id=m.id, datum=tag, konto_nr="4920", gegenkonto_nr="1200",
                              betrag=D("100"), sh="S", posten_id=p.id))
    _op(db, m, "700", date(2026, 8, 5))
    v = _verschiebe(db, m, date(2026, 8, 10), date(2026, 8, 24), "-500")
    v.erstellt_am = datetime(2026, 8, 8, 10, tzinfo=timezone.utc)
    db.commit()
    groesste = []

    def zaehlen(conn, cursor, statement, parameters, context, executemany):
        groesste.append(len(parameters) if isinstance(parameters, (list, tuple, dict)) else 0)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", zaehlen)
    try:
        heute = date(2026, 8, 12)
        liq.berechne_plan(db, m, start=date(2026, 8, 10), wochen=4, heute=heute)
        liq.erstelle_snapshot(db, m, start=date(2026, 8, 10), wochen=4, heute=heute)
        db.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", zaehlen)
    assert max(groesste) < 50


def test_zellendialog_gleicht_raten_nur_fuer_sein_konto_ab(db, monkeypatch):
    """Der Zellendialog einer Zeile rechnet den Ratenabgleich nur für deren Konto (der Abgleich
    rechnet je Sachkonto) – nicht für alle Dauerbuchungen und Ist-Buchungen des Mandanten."""
    m = _mandant(db)
    _bank(db, m)
    for konto in ("4920", "4210"):
        db.add(models.Dauerbuchung(mandant_id=m.id, name=f"Rate {konto}", art="KREDITOR",
                                   konto_id=_konto(db, m, konto).id, betrag_brutto=D("500"),
                                   intervall="MONATLICH", stichtag=11, gueltig_von=date(2026, 1, 1)))
    _verschiebe(db, m, date(2026, 8, 11), date(2026, 8, 25), "-500", angelegt=date(2026, 8, 3))
    db.commit()
    aufrufe = []
    original = liq._raten_zahlstatus

    def mitschreiben(*args, **kwargs):
        aufrufe.append(kwargs.get("nur_konten"))
        return original(*args, **kwargs)

    monkeypatch.setattr(liq, "_raten_zahlstatus", mitschreiben)
    svc.plan_bestellungen(db, m, konto_id=_konto(db, m, "4920").id, von=date(2026, 8, 10),
                          bis=date(2026, 8, 30), heute=date(2026, 8, 12))
    assert aufrufe and all(a == {_konto(db, m, "4920").id} for a in aufrufe)


# ---------------------------------------------------------------------------
# P1: Planstand (Soll-Referenz) vor der Verschiebung eingefroren
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("art", ["posten", "rate"])
@pytest.mark.parametrize("zeitpunkt", ["bekannt", "unbekannt"])
def test_planstand_vor_der_verschiebung_ohne_phantom_spielraum(db, art, zeitpunkt):
    """Planstand Mo 17.08. als Soll-Referenz mit 1.500 am 19.08. (OP bzw. Rate); am 18.08. auf
    den 02.09. verschoben, heute 20.08.: die Minderausgabe der KW 34 ist kein Rahmen – in keiner
    KW verfügbar > 0 (KW 36 bindet nicht doppelt). Der Anlagezeitpunkt des Planstands ist
    bekannt bzw. liegt nach dem Datenstand (dann gilt sein Stichtag)."""
    m = _mandant(db)
    _bank(db, m)
    if art == "posten":
        _op(db, m, "1500", date(2026, 8, 19))
    else:
        db.add(models.Dauerbuchung(mandant_id=m.id, name="Miete", art="KREDITOR",
                                   konto_id=_konto(db, m, "4920").id, betrag_brutto=D("1500"),
                                   intervall="MONATLICH", stichtag=19, gueltig_von=date(2026, 8, 1),
                                   aktiv=True, vortragen=True))
    db.commit()
    snap = liq.erstelle_snapshot(db, m, start=date(2026, 8, 17), wochen=13, heute=date(2026, 8, 17))
    snap.referenz = True
    if zeitpunkt == "bekannt":
        snap.erstellt_am = datetime(2026, 8, 17, 8, tzinfo=timezone.utc)
    _verschiebe(db, m, date(2026, 8, 19), date(2026, 9, 2), "-1500", angelegt=date(2026, 8, 18))
    heute = date(2026, 8, 20)
    for kw in (34, 35, 36, 37, 38):
        assert _kz(db, m, kw, heute, basis="AUTO")["verfuegbar"] <= 0.005
    k36 = _kz(db, m, 36, heute, basis="AUTO")
    assert (k36["gebunden"]["summe"], k36["abweichung"]["ueberfaellig"]) == (0.0, 1500.0)


def test_planstand_nach_der_verschiebung_bindet_im_ziel(db):
    """Gegenprobe: der Planstand ist nach der Verschiebung eingefroren (er führt sie schon am
    Ziel) – keine Minderausgabe, gebunden in KW 36, nichts überfällig."""
    m = _mandant(db)
    _bank(db, m)
    _op(db, m, "1500", date(2026, 8, 19))
    _verschiebe(db, m, date(2026, 8, 19), date(2026, 9, 2), "-1500", angelegt=date(2026, 8, 17))
    db.commit()
    snap = liq.erstelle_snapshot(db, m, start=date(2026, 8, 17), wochen=13, heute=date(2026, 8, 17))
    snap.referenz = True
    snap.erstellt_am = datetime(2026, 8, 17, 15, tzinfo=timezone.utc)
    heute = date(2026, 8, 20)
    k36 = _kz(db, m, 36, heute, basis="AUTO")
    assert (k36["gebunden"]["summe"], k36["abweichung"]["ueberfaellig"], k36["verfuegbar"]) == \
        (1500.0, 0.0, 0.0)
    assert _kz(db, m, 35, heute, basis="AUTO")["verfuegbar"] == 0.0


# ---------------------------------------------------------------------------
# P2: Anlagezeitpunkt von Posten und Terminen
# ---------------------------------------------------------------------------

def _rechnung_nach_budgetverschiebung(db, erstellt_am):
    m = _mandant(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 35, "5000")
    _budget_woche(db, m, "4920", 37, "5000")
    _verschiebe(db, m, date(2026, 8, 26), date(2026, 9, 9), "-1190", angelegt=date(2026, 8, 3))
    p = _op(db, m, "1190", date(2026, 8, 26))
    p.erstellt_am = erstellt_am
    return m


def test_nach_der_budgetverschiebung_erfasste_rechnung_bleibt_am_ursprungstag(db):
    """Budget KW 35 5.000 netto (1.190 je Tag); am 03.08. das Budget des 26.08. auf den 09.09.
    verschoben, am 10.08. eine Rechnung 1.190 fällig 26.08. erfasst: sie bleibt in KW 35
    (gebunden), das Budget am 09.09.; Spielraum KW 35 = 5.950 − 1.190 − 1.190."""
    m = _rechnung_nach_budgetverschiebung(db, datetime(2026, 8, 10, 9, tzinfo=timezone.utc))
    heute = date(2026, 8, 10)
    plan = _zeile(db, m, date(2026, 8, 24), heute, 3)
    assert plan["2026-08-26"] == -1190.0 and plan["2026-09-09"] == -2380.0
    k35, k37 = _kz(db, m, 35, heute), _kz(db, m, 37, heute)
    assert (k35["gebunden"]["summe"], k35["verfuegbar"]) == (1190.0, 3570.0)
    assert (k37["gebunden"]["summe"], k37["verfuegbar"]) == (0.0, 7140.0)


def test_posten_ohne_anlagezeitpunkt_verhaelt_sich_wie_bisher(db):
    """Bestand ohne erstellt_am (vor der Migration angelegt): unbekannt, gilt als alt – die
    Verschiebung nimmt ihn wie bisher (Rechnung am 09.09., Budget bleibt in KW 35)."""
    m = _rechnung_nach_budgetverschiebung(db, None)
    plan = _zeile(db, m, date(2026, 8, 24), date(2026, 8, 10), 3)
    assert "2026-08-26" not in plan or plan["2026-08-26"] != -1190.0
    assert plan["2026-09-09"] == -2380.0


def test_nach_der_verschiebung_angelegter_termin_bleibt_am_ursprungstag(db):
    """USt-VA (ohne Konto) 2.000 am 10.09., am 20.08. angelegt und am 01.09. auf den 21.09.
    verschoben; ein am 03.09. angelegter, schon erledigter zweiter Termin am 10.09. (kleinere ID)
    bleibt dort – verschoben ist der offene: die Projektion führt ihn am 21.09., nicht am 10.09.
    (ohne Anlagezeitpunkt nähme die Verschiebung den Termin mit der kleineren ID)."""
    m = _mandant(db)
    _bank(db, m)
    spaet = models.Zahlungstermin(mandant_id=m.id, typ="UST_VA", datum=date(2026, 9, 10),
                                  betrag=D("2000"), generiert=False,
                                  status=models.TerminStatus.ERLEDIGT.value,
                                  erstellt_am=datetime(2026, 9, 3, tzinfo=timezone.utc))
    db.add(spaet)
    db.flush()
    frueh = models.Zahlungstermin(mandant_id=m.id, typ="UST_VA", datum=date(2026, 9, 10),
                                  betrag=D("2000"), generiert=False,
                                  erstellt_am=datetime(2026, 8, 20, tzinfo=timezone.utc))
    db.add(frueh)
    _verschiebe(db, m, date(2026, 9, 10), date(2026, 9, 21), "-2000", termin_typ="UST_VA",
                angelegt=date(2026, 9, 1))
    db.commit()
    assert spaet.id < frueh.id
    heute = date(2026, 9, 4)
    assert liq.Zuordnung(db, m, heute).termin_ids == {frueh.id}
    p = liq.berechne_plan(db, m, start=date(2026, 9, 7), wochen=3, heute=heute)
    z = next(k for g in p["zeilen"] for k in g["kinder"] if k["key"] == "TERMIN:UST_VA")
    assert {t: v for t, v in z["plan"].items() if v} == {"2026-09-10": -2000.0,
                                                          "2026-09-21": -2000.0}
    # Projektion: der erledigte Termin zählt nicht, der offene erst am 21.09.
    liquiditaet = p["bestaende"]["liquiditaet"]
    assert (liquiditaet["2026-09-13"], liquiditaet["2026-09-27"]) == (100000.0, 98000.0)


def test_neue_posten_und_termine_erhalten_den_anlagezeitpunkt(db):
    """Neu angelegte Posten (Erfassung, OP-Import, Onboarding legen sie per Modell an) und von
    den Terminregeln erzeugte Termine erhalten erstellt_am (Modell-Standard)."""
    from app.services.zahlungskalender import generiere_termine

    m = _mandant(db)
    p = models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="X",
                             faellig_am=date(2026, 9, 1), betrag_brutto=D("100"))
    db.add(p)
    db.commit()
    generiere_termine(db, m, date(2026, 8, 3), date(2026, 11, 1), heute=date(2026, 8, 5))
    db.commit()
    termine = db.query(models.Zahlungstermin).all()
    assert p.erstellt_am is not None
    assert termine and all(t.erstellt_am is not None for t in termine)


def test_migration_erstellt_am_posten_und_termine(leere_datenbank):
    """Upgrade: Spalte erstellt_am (nullable) auf offene_posten und zahlungstermine, Bestand
    NULL; Downgrade entfernt beide Spalten, erneutes Upgrade läuft durch."""
    from alembic import command
    from sqlalchemy import inspect, text
    from sqlalchemy.orm import Session
    from test_migrationen import _cfg

    engine = leere_datenbank
    cfg = _cfg()
    with engine.begin() as verbindung:
        cfg.attributes["connection"] = verbindung
        command.upgrade(cfg, "a4d7c2e9b310")
        sitzung = Session(bind=verbindung)
        m = models.Mandant(name="T", kurzname="t", kontenrahmen="SKR03", bundesland="NW")
        sitzung.add(m)
        sitzung.flush()
        mid = m.id
        sitzung.close()
        verbindung.execute(text(
            "INSERT INTO offene_posten (id, mandant_id, art, partner, faellig_am, betrag_brutto, "
            f"bezahlt_betrag, status, sammel) VALUES (1, {mid}, 'KREDITOR', 'X', '2026-09-01', "
            "100, 0, 'OFFEN', false)"))
        verbindung.execute(text(
            "INSERT INTO zahlungstermine (id, mandant_id, typ, datum, betrag, status, generiert) "
            f"VALUES (1, {mid}, 'SV', '2026-09-01', 100, 'GEPLANT', false)"))
        command.upgrade(cfg, "head")
        for tabelle in ("offene_posten", "zahlungstermine"):
            assert verbindung.execute(text(f"SELECT erstellt_am FROM {tabelle}")).scalar_one() is None
        command.downgrade(cfg, "a4d7c2e9b310")
    for tabelle in ("offene_posten", "zahlungstermine"):
        assert "erstellt_am" not in {s["name"] for s in inspect(engine).get_columns(tabelle)}
    with engine.begin() as verbindung:
        cfg.attributes["connection"] = verbindung
        command.upgrade(cfg, "head")
    for tabelle in ("offene_posten", "zahlungstermine"):
        assert "erstellt_am" in {s["name"] for s in inspect(engine).get_columns(tabelle)}


# ---------------------------------------------------------------------------
# P3: Projektion – Überfälliges verbraucht das Budget am Rolltag der vollen Sicht
# ---------------------------------------------------------------------------

def test_ueberfaelliger_posten_donnerstags_und_freitags_gleiche_liquiditaet(db):
    """Budget KW 20 2.500 (4210), OP 3.000 fällig 05.05. offen: am Do 07.05. und am Fr 08.05.
    dieselbe Liquidität am 24.05. (freitags rollt die Projektion in die KW 20, ihr Budget
    bleibt)."""
    m = _mandant(db)
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=_konto(db, m, "1200").id,
                          datum=date(2026, 4, 1), wert=D("100000")))
    _budget_woche(db, m, "4210", 20, "2500")
    _op(db, m, "3000", date(2026, 5, 5), konto="4210")
    do = _liq(db, m, date(2026, 5, 4), date(2026, 5, 7), "2026-05-24", 3)
    fr = _liq(db, m, date(2026, 5, 4), date(2026, 5, 8), "2026-05-24", 3)
    assert do == fr == 94500.0


# ---------------------------------------------------------------------------
# P4: Zellendialog – abgelöste Bestellung mit Zahldatum vor dem Fenster
# ---------------------------------------------------------------------------

def test_zellendialog_nennt_abgeloeste_bestellung_mit_zahldatum_vor_dem_fenster(db):
    """Bestellung 1.190 brutto, Zahldatum 05.08., am 10.08. manuell abgelöst, heute 11.08.: der
    Plan führt sie am ersten Bankarbeitstag des Fensters (10.08.), der Zellendialog ebenso."""
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    b = svc.anlegen(db, m, nutzer, {"kreditor": "K", "bestellnummer": "B-1",
                                    "bestelldatum": date(2026, 7, 20), "zahldatum": date(2026, 8, 5),
                                    "betrag_netto": "1000", "konto_id": _konto(db, m, "4920").id})
    b.erstellt_am = datetime(2026, 7, 20)
    svc.entscheiden(db, b, nutzer, "FREIGEBEN", "Test", {})
    svc.abloesen(db, b, nutzer, art="MANUELL", betrag=D("1190"), datum=date(2026, 8, 10))
    heute = date(2026, 8, 11)
    assert _zeile(db, m, date(2026, 8, 10), heute, 2) == {"2026-08-10": -1190.0}
    dialog = svc.plan_bestellungen(db, m, konto_id=_konto(db, m, "4920").id,
                                   von=date(2026, 8, 10), bis=date(2026, 8, 10), heute=heute)
    assert [(e["bestellnummer"], e["plan_tag"], e["plan_betrag"]) for e in dialog] == \
        [("B-1", "2026-08-10", 1190.0)]


# ---------------------------------------------------------------------------
# P5: heute ohne Bankkonto als bezahlt erfasst, Ist über den Buchhaltungsimport
# ---------------------------------------------------------------------------

def test_heute_ohne_beleg_bezahlter_posten_bleibt_gebunden(db):
    """OP 1.500 fällig 02.09., heute (02.09.) über „Zahlung erfassen“ ohne Bankkonto bezahlt,
    die Ist-Buchung (ohne Posten-Referenz) kommt aus dem Import: das Ist zählt erst morgen –
    der Posten bleibt in KW 36 gebunden, kein Spielraum."""
    m = _mandant(db)
    _bank(db, m)
    _posten(db, m, "1500", date(2026, 9, 2), status="BEZAHLT", bezahlt_am=date(2026, 9, 2),
            bezahlt_betrag=D("1500"))
    _zahlung(db, m, date(2026, 9, 2), "1500")
    k = _kz(db, m, 36, date(2026, 9, 2))
    assert (k["gebunden"]["summe"], k["verfuegbar"]) == (1500.0, 0.0)


# ---------------------------------------------------------------------------
# P6: Rücknahme des ersten Glieds einer Kette
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("heute", [date(2026, 8, 18), date(2026, 8, 24)])
def test_ruecknahme_des_ersten_kettenglieds_nimmt_die_folgeverschiebung_mit(db, monkeypatch, heute):
    """A 1.500 (19.08.) auf den 26.08. verschoben (V1), danach 1.500 aus der Zelle 26.08.
    (A + B 700) auf den 02.09. (V2 nimmt A). Wird V1 zurückgenommen, bleibt B am 26.08. – V2
    hatte nur das von V1 Gebrachte bewegt und wird mit zurückgenommen."""
    _heute_setzen(monkeypatch, date(2026, 8, 17))
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    _posten(db, m, "1500", date(2026, 8, 19), partner="A")
    _posten(db, m, "700", date(2026, 8, 26), partner="B")
    start = date(2026, 8, 17)
    v1 = _api_verschieben(db, m, nutzer, "4920", date(2026, 8, 19), date(2026, 8, 26), "1500",
                          start, 3)["ids"][0]
    _heute_setzen(monkeypatch, heute)
    _api_verschieben(db, m, nutzer, "4920", date(2026, 8, 26), date(2026, 9, 2), "1500",
                     liq.wochen_start(heute), 3)
    assert _zeile(db, m, liq.wochen_start(heute), heute, 3) == {"2026-08-26": -700.0,
                                                                 "2026-09-02": -1500.0}
    erg = api.verschiebung_loeschen(m.id, v1, benutzer=nutzer, db=db)
    assert len(erg["angepasst"]) == 1
    plan = _zeile(db, m, liq.wochen_start(heute), heute, 3)
    assert plan.get("2026-08-26") == -700.0 and "2026-09-02" not in plan
    assert db.query(models.PlanVerschiebung).count() == 0


def test_ruecknahme_kuerzt_folgeverschiebung_um_das_gebrachte(db, monkeypatch):
    """Wie oben, aber V2 verschiebt die ganze Zelle (A + B = 2.200): nach der Rücknahme von V1
    bleibt V2 mit 700 (B) bestehen."""
    _heute_setzen(monkeypatch, date(2026, 8, 17))
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    _posten(db, m, "1500", date(2026, 8, 19), partner="A")
    _posten(db, m, "700", date(2026, 8, 26), partner="B")
    start = date(2026, 8, 17)
    v1 = _api_verschieben(db, m, nutzer, "4920", date(2026, 8, 19), date(2026, 8, 26), "1500",
                          start, 3)["ids"][0]
    v2 = _api_verschieben(db, m, nutzer, "4920", date(2026, 8, 26), date(2026, 9, 2), "2200",
                          start, 3)["ids"][0]
    api.verschiebung_loeschen(m.id, v1, benutzer=nutzer, db=db)
    assert db.get(models.PlanVerschiebung, v2).betrag == D("-700")
    assert _zeile(db, m, start, date(2026, 8, 17), 3) == {"2026-08-19": -1500.0,
                                                           "2026-09-02": -700.0}


# ---------------------------------------------------------------------------
# P7: Deckung erledigter Termine unabhängig von der geprüften KW
# ---------------------------------------------------------------------------

def test_deckung_erledigter_termine_unabhaengig_von_der_kw(db, monkeypatch):
    """GewSt 500 (erledigt, Ist 04.01. auf 4320, auf den 23.01. verschoben) und USt-VA 2.500 am
    15.01. (am 28.12. vorab bezahlt), heute 06.01.2027. Über die API werden USt 750 auf den
    20.01. und GewSt auf den 25.01. verschoben: die Summe verfügbar über KW 2–4 bleibt, keine
    KW wird positiv (die GewSt-Zahlung deckt nur die GewSt)."""
    heute = date(2027, 1, 6)
    _heute_setzen(monkeypatch, heute)
    m = _mandant(db)
    nutzer = _benutzer(db)
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=_konto(db, m, "1200").id,
                          datum=date(2026, 10, 28), wert=D("100000")))
    db.add(models.Zahlungstermin(mandant_id=m.id, typ="GEWST", datum=date(2027, 1, 3),
                                 betrag=D("500"), status="ERLEDIGT", generiert=False,
                                 erstellt_am=ALTBESTAND))
    _zahlung(db, m, date(2027, 1, 4), "500", konto="4320")
    db.add(models.PlanVerschiebung(mandant_id=m.id, termin_typ="GEWST", quelle="PLAN",
                                   von=date(2027, 1, 3), nach=date(2027, 1, 23), betrag=D("-500"),
                                   erstellt_am=datetime(2027, 1, 2, 12)))
    db.add(models.Zahlungstermin(mandant_id=m.id, typ="UST_VA", datum=date(2027, 1, 15),
                                 betrag=D("2500"), status="ERLEDIGT", generiert=False,
                                 erstellt_am=ALTBESTAND))
    _zahlung(db, m, date(2026, 12, 28), "2500", konto="1780")
    db.commit()

    def summe():
        werte = []
        for kw in (2, 3, 4):
            r = svc.freigabe_kennzahlen(db, m, konto_id=None, betrag_brutto=D("0"), jahr=2027,
                                        kw=kw, basis="LIVE", heute=heute)
            werte.append(next(e for e in r["ebenen"] if e["ebene"] == "gesamt")["verfuegbar"])
        assert max(werte) <= 0.005
        return round(sum(werte), 2)

    vorher = summe()
    for key, von, nach, betrag in (("TERMIN:UST_VA", "2027-01-15", "2027-01-20", 750),
                                   ("TERMIN:GEWST", "2027-01-23", "2027-01-25", 500)):
        api.verschiebung_anlegen(m.id, api.VerschiebungNeu(
            key=key, von=von, bis=von, nach=nach, betrag=betrag, start="2027-01-04", wochen=6),
            benutzer=nutzer, db=db)
        assert summe() == vorher


# ---------------------------------------------------------------------------
# P8: Restbudget eines verschobenen Bestandteils mit Plantag vor dem Fenster
# ---------------------------------------------------------------------------

def test_verschobener_posten_vor_dem_fenster_verbraucht_kein_budget_im_fenster(db):
    """Wochenbudget je 2.000 netto (KW 35/36); OP 500 am 26.08., die Zelle (500 + 376) am 24.08.
    auf den 02.09. verschoben. Im Fenster ab 31.08. hat KW 36 weiter 2.380 + 876 (der Posten
    hat das Budget seiner vergangenen Ursprungswoche verbraucht), LIVE verfügbar 2.756."""
    m = _mandant(db)
    _bank(db, m)
    for kw in (35, 36, 37):
        _budget_woche(db, m, "4920", kw, "2000")
    _op(db, m, "500", date(2026, 8, 26))
    _verschiebe(db, m, date(2026, 8, 26), date(2026, 9, 2), "-876", angelegt=date(2026, 8, 24))
    heute = date(2026, 8, 31)
    plan = _zeile(db, m, date(2026, 8, 31), heute, 2)
    assert round(-sum(v for t, v in plan.items() if t <= "2026-09-06"), 2) == 3256.0
    assert _kz(db, m, 36, heute)["verfuegbar"] == 2756.0


# ---------------------------------------------------------------------------
# S1: Szenario-Ansicht – Beträge aus dem Basisplan
# ---------------------------------------------------------------------------

def test_verschiebung_in_der_szenarioansicht(db, monkeypatch):
    """Im Szenario (Einzahlungen 50 %) weicht die Debitorzeile vom Basisplan ab: die
    Verschiebung wird mit 422 abgelehnt (bitte in der Basisansicht); eine Kreditorzeile ohne
    Budget ist gleich – ihre Verschiebung speichert den Basisbetrag."""
    from fastapi import HTTPException

    heute = date(2026, 8, 12)
    _heute_setzen(monkeypatch, heute)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    _posten(db, m, "1000", date(2026, 8, 26), konto="8400", art="DEBITOR")
    _op(db, m, "600", date(2026, 8, 26))
    sz = models.Szenario(mandant_id=m.id, name="Schlecht", ein_faktor=D("50"),
                         aus_faktor=D("100"))
    db.add(sz)
    db.commit()
    start = date(2026, 8, 10)
    with pytest.raises(HTTPException) as fehler:
        _api_verschieben(db, m, nutzer, "8400", date(2026, 8, 26), date(2026, 9, 2), "500", start,
                         szenario_id=sz.id)
    assert fehler.value.status_code == 422 and "Basisansicht" in fehler.value.detail
    _api_verschieben(db, m, nutzer, "4920", date(2026, 8, 26), date(2026, 9, 2), "600", start,
                     szenario_id=sz.id)
    assert [v.betrag for v in db.query(models.PlanVerschiebung)] == [D("-600")]


# ---------------------------------------------------------------------------
# Abschlussprüfung 4.1: Zahlstand beim Anlegen auch nach späterer Zahlung (I15) und
# Ordnung nach Fälligkeit vor einer späteren Verschiebung (I24)
# ---------------------------------------------------------------------------

def _api_verschieben_heute(db, m, nutzer, heute: date, *args, **kwargs) -> dict:
    """Wie `_api_verschieben`, Anlagezeitpunkt wie im Betrieb am (gesetzten) heutigen Tag – der
    Zeitstempel des Testlaufs läge nach dem Datenstand und gälte als unbekannt."""
    antwort = _api_verschieben(db, m, nutzer, *args, **kwargs)
    for vid in antwort["ids"]:
        db.get(models.PlanVerschiebung, vid).erstellt_am = datetime(
            heute.year, heute.month, heute.day, 12, tzinfo=timezone.utc)
    db.commit()
    return antwort


def test_spaeter_ganz_bezahlter_posten_aendert_die_zuordnung_nicht(db, monkeypatch):
    """Budget KW 35 1.000 netto; OP 1.000 fällig Fr 28.08., Anzahlung 400 am Mo 24.08. Am Mi
    26.08. wird die ganze Zelle 28.08. (Rest 600 + Restbudget) auf den 07.09. verschoben, am
    27.08. der Rest bezahlt. Die Anzahlung war beim Anlegen eine Teilzahlung (in keiner Sicht):
    auch nachdem der Posten ganz bezahlt ist, nimmt die Verschiebung sie nicht vor dem Budget –
    sie bewegt denselben Posten- und Budgetanteil wie vor der Zahlung."""
    heute = date(2026, 8, 26)
    _heute_setzen(monkeypatch, heute)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 35, "1000")
    p = _op(db, m, "1000", date(2026, 8, 28), bezahlt=((date(2026, 8, 24), "400"),))
    start = date(2026, 8, 24)
    zelle = _zeile(db, m, start, heute)["2026-08-28"]
    _api_verschieben_heute(db, m, nutzer, heute, "4920", date(2026, 8, 28), date(2026, 9, 7),
                           -zelle, start)
    assert "2026-08-28" not in _zeile(db, m, start, heute)
    v = db.query(models.PlanVerschiebung).one()

    def zuordnung(tag: date) -> tuple:
        db.commit()
        z = liq.Zuordnung(db, m, tag)
        bewegt = sum((b for ref in (("OP", p.id, "offen"), ("OP", p.id, "erledigt"))
                      for t, b, _ in z.verteilung(ref, p.faellig_am, D("0"))
                      if t == date(2026, 9, 7)), D("0"))
        return bewegt, z.budget_anspruch.get(v.id, D("0"))

    vorher = zuordnung(date(2026, 8, 26))
    assert vorher == (D("600"), D(str(-zelle)) - D("600"))
    _zahlung(db, m, date(2026, 8, 27), "600").posten_id = p.id
    p.bezahlt_betrag, p.status, p.bezahlt_am = D("1000"), "BEZAHLT", date(2026, 8, 27)
    assert zuordnung(date(2026, 8, 31)) == vorher


def test_spaetere_verschiebung_sieht_den_bestandteil_nach_faelligkeit_geordnet(db, monkeypatch):
    """OP 2.500 fällig Mi 29.04.: am 14.04. 1.500 auf den 22.04. verschoben, am 24.04. der Rest
    1.000 auf den 28.05.; am 04.05. 750 angezahlt (früheste Fälligkeit zuerst: der Teil am 22.04.).
    Sa 09.05.: der Plan zeigt den überfälligen Rest 750 am Rolltag Mo 11.05. und 1.000 am 28.05.
    Die ganze Rolltagszelle auf den 18.05. verschoben: 11.05. leer, 28.05. unverändert."""
    heute = date(2026, 5, 9)
    _heute_setzen(monkeypatch, heute)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 4, 1))
    p = _op(db, m, "2500", date(2026, 4, 29))
    _verschiebe(db, m, date(2026, 4, 29), date(2026, 4, 22), "-1500", angelegt=date(2026, 4, 14))
    _verschiebe(db, m, date(2026, 4, 29), date(2026, 5, 28), "-1000", angelegt=date(2026, 4, 24))
    _zahlung(db, m, date(2026, 5, 4), "750").posten_id = p.id
    p.bezahlt_betrag = D("750")
    start = date(2026, 5, 4)
    assert _zeile(db, m, start, heute) == {"2026-05-11": -750.0, "2026-05-28": -1000.0}
    _api_verschieben_heute(db, m, nutzer, heute, "4920", date(2026, 5, 11), date(2026, 5, 18),
                           "750", start)
    assert _zeile(db, m, start, heute) == {"2026-05-18": -750.0, "2026-05-28": -1000.0}


def test_budget_anderer_herkunft_aendert_die_fruehere_verschiebung_nicht(db, monkeypatch):
    """Miete 4210 (ohne USt) 2.000 am 13.07., Budget KW 29 2.500 (500 je Tag; die Rate
    verbraucht 2.000 des Restbudgets). Am 01.07.: 13.07. ganz (Rate + 500 Budget) auf den
    17.07. und 300 Budget vom 16.07. auf den 10.07. (KW 28 – die Woche hat dafür kein
    Restbudget mehr). Heute 02.07. wird die Zelle 17.07. auf den 16.07. gelegt: eine Verschiebung
    innerhalb der KW 29 – das Budget vom 13.07. füllt nicht die Lücke, die die zweite dem 16.07.
    entnommen hat, KW 28 bleibt unverändert."""
    heute = date(2026, 7, 2)
    _heute_setzen(monkeypatch, heute)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 4, 1))
    _budget_woche(db, m, "4210", 29, "2500")
    db.add(models.Dauerbuchung(mandant_id=m.id, name="Miete", art="KREDITOR",
                               konto_id=_konto(db, m, "4210").id, betrag_brutto=D("2000"),
                               intervall="MONATLICH", stichtag=13, gueltig_von=date(2026, 1, 1)))
    _verschiebe(db, m, date(2026, 7, 13), date(2026, 7, 17), "-2500", konto="4210",
                angelegt=date(2026, 7, 1))
    _verschiebe(db, m, date(2026, 7, 16), date(2026, 7, 10), "-300", konto="4210",
                angelegt=date(2026, 7, 1))
    start = date(2026, 6, 29)

    def kennzahlen() -> tuple:
        return tuple(_kz(db, m, kw, heute, konto="4210")["verfuegbar"] for kw in (28, 29))

    vorher = kennzahlen()
    assert _zeile(db, m, start, heute, konto="4210")["2026-07-17"] == -2500.0
    _api_verschieben_heute(db, m, nutzer, heute, "4210", date(2026, 7, 17), date(2026, 7, 16),
                           "2500", start)
    zeile = _zeile(db, m, start, heute, konto="4210")
    assert "2026-07-17" not in zeile and zeile["2026-07-16"] == -2500.0
    assert kennzahlen() == vorher


def test_nach_der_verschiebung_freigegebene_bestellung_bei_unbekanntem_anlagetag_spaet(db):
    """OP 1.500 fällig 02.06., am 11.05. bezahlt; am 12.05. auf So 24.05. verschoben, eine
    Folgeverschiebung 24.05. → 18.06. ohne verlässlichen Anlagezeitpunkt (gilt als heute
    angelegt, Do 14.05. mittags). Eine erst am Abend des 14.05. erfasste und freigegebene
    Bestellung am 24.05. stand beim Anlegen nicht in der Zelle: die Folgeverschiebung nimmt den
    Posten, die Bestellung bleibt ganz am 24.05. (wie Posten und Termine verglichen mit dem
    angenommenen Anlagezeitpunkt)."""
    from test_bestellungen import _bestellung

    heute = date(2026, 5, 14)
    m = _mandant(db)
    nutzer = _benutzer(db)
    _bank(db, m, tag=date(2026, 3, 1))
    p = _op(db, m, "1500", date(2026, 6, 2), bezahlt=((date(2026, 5, 11), "1500"),))
    _verschiebe(db, m, date(2026, 6, 2), date(2026, 5, 24), "-1500", angelegt=date(2026, 5, 12))
    _verschiebe(db, m, date(2026, 5, 24), date(2026, 6, 18), "-1500")     # Zeitpunkt des Laufs
    b = _bestellung(db, m, nutzer, konto="4920", netto="2000", zahldatum=date(2026, 5, 24),
                    status="FREIGEGEBEN")
    b.erstellt_am = datetime(2026, 5, 14, 20, tzinfo=timezone.utc)
    b.entschieden_am = datetime(2026, 5, 14, 20, 30, tzinfo=timezone.utc)
    db.commit()
    z = liq.Zuordnung(db, m, heute)
    assert [t for t, _, _ in z.verteilung(("BEST", b.id, "offen"), b.zahldatum, D("0"))] == \
        [date(2026, 5, 24)]
    assert [t for t, _, _ in z.verteilung(("OP", p.id, "erledigt"), p.faellig_am, D("0"))] == \
        [date(2026, 6, 18)]
    assert _kz(db, m, 21, heute)["gebunden"]["summe"] == 2380.0
