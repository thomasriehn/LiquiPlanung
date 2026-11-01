"""Sichtunabhängige Zuordnung der PLAN-Verschiebungen (liquiditaet.Zuordnung): eine
Verschiebung wird einmal den Bestandteilen ihres Ursprungstags zugeordnet, und jede Sicht
(angezeigter Plan, Projektion, Planstand, LIVE-Soll, gebunden, Vortrag) führt den
verschobenen Anteil nach ihren Regeln am Zieltag. Konkrete Werte zu den Befunden der
Runde 2 (V1–V6), des Orakels (N2–N8) und der Gegenprüfung Runde 3 (Anlagetag, Rolltag der
Anlage, Budget brutto, gemeinsamer Ratenabgleich, Termine, Zellendialog); synthetische Daten."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app import models
from app.services import bestellungen as svc
from app.services.liquiditaet import Zuordnung, berechne_plan, erstelle_snapshot, snapshot_fluesse

from test_bestellungen import _bank, _benutzer, _bestellung, _budget_woche, _zahlung, _zeile
from test_liquiditaet import _konto, _mandant


def _verschiebe(db, m, von, nach, betrag, konto="4920", angelegt: date | None = None,
                termin_typ: str | None = None):
    """PLAN-Verschiebung; `angelegt`: Anlagetag (wie die API: Zeitpunkt der Anlage, vor dem
    Ursprungstag) – ohne Angabe der Zeitpunkt des Testlaufs (Anlagetag „unbekannt“)."""
    v = models.PlanVerschiebung(mandant_id=m.id, quelle="PLAN", von=von, nach=nach,
                                betrag=Decimal(betrag),
                                konto_id=None if termin_typ else _konto(db, m, konto).id,
                                termin_typ=termin_typ)
    if angelegt is not None:
        v.erstellt_am = datetime(angelegt.year, angelegt.month, angelegt.day, 12)
    db.add(v)
    db.flush()
    return v


def _op(db, m, betrag, faellig, konto="4920", bezahlt=(), **extra) -> models.OffenerPosten:
    """Kreditor-OP; `bezahlt`: (Tag, Betrag) je Zahlung (Ist-Buchung zum Posten). Ohne Angabe
    Altbestand (erstellt_am vor allen Verschiebungen, nicht die Uhrzeit des Testlaufs)."""
    from szenario_bausteine import ALTBESTAND
    extra.setdefault("erstellt_am", ALTBESTAND)
    gezahlt = sum((Decimal(b) for _, b in bezahlt), Decimal("0"))
    voll = gezahlt >= Decimal(betrag)
    p = models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="Lieferant",
                             faellig_am=faellig, betrag_brutto=Decimal(betrag),
                             konto_id=_konto(db, m, konto).id, bezahlt_betrag=gezahlt,
                             status="BEZAHLT" if voll else "OFFEN",
                             bezahlt_am=max(t for t, _ in bezahlt) if voll else None, **extra)
    db.add(p)
    db.flush()
    for tag, b in bezahlt:
        _zahlung(db, m, tag, b, konto=konto).posten_id = p.id
    return p


def _miete(db, m, betrag="2000", stichtag=11, konto="4920"):
    db.add(models.Dauerbuchung(mandant_id=m.id, name="Miete", art="KREDITOR",
                               konto_id=_konto(db, m, konto).id, betrag_brutto=Decimal(betrag),
                               intervall="MONATLICH", stichtag=stichtag,
                               gueltig_von=date(2026, 1, 1)))


def _kz(db, m, kw, heute, konto="4920", basis="LIVE") -> dict:
    r = svc.freigabe_kennzahlen(db, m, konto_id=_konto(db, m, konto).id,
                                betrag_brutto=Decimal("0"), jahr=2026, kw=kw, basis=basis,
                                heute=heute)
    return next(e for e in r["ebenen"] if e["ebene"] == "konto")


def _plan(db, m, start, heute, wochen=3, nummer="4920") -> tuple[dict, dict]:
    db.commit()
    p = berechne_plan(db, m, start=start, wochen=wochen, heute=heute)
    zeile = next((k for g in p["zeilen"] for k in g["kinder"] if k["nummer"] == nummer),
                 {"plan": {}, "verschoben": {}})
    return {t: v for t, v in zeile["plan"].items() if v}, p


# ---------------------------------------------------------------------------
# V1: teilbezahlter überfälliger Posten – kein Budget des vergangenen Ursprungstags
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("heute", [date(2026, 8, 10), date(2026, 8, 12)])
def test_v1_teilzahlung_wird_mitverschoben_statt_budget(db, heute):
    m = _mandant(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 32, "10000")
    _op(db, m, "1500", date(2026, 8, 4), bezahlt=((date(2026, 8, 5), "500"),))
    _verschiebe(db, m, date(2026, 8, 4), date(2026, 8, 18), "-1500")
    plan, p = _plan(db, m, date(2026, 8, 10), heute)
    assert plan == {"2026-08-18": -1000.0}
    assert p["bestaende"]["liquiditaet"]["2026-08-30"] == 98500.0
    k34 = _kz(db, m, 34, heute)
    assert (k34["soll_kw"], k34["gebunden"]["summe"], k34["verfuegbar"]) == (1000.0, 1000.0, 0.0)


# ---------------------------------------------------------------------------
# V2: verschoben und am Zieltag (bzw. vor dem Fenster) bezahlt
# ---------------------------------------------------------------------------

def test_v2_am_zieltag_bezahlter_posten_steht_nur_am_zieltag(db):
    m = _mandant(db)
    _bank(db, m)
    _op(db, m, "1500", date(2026, 8, 4), bezahlt=((date(2026, 8, 18), "1500"),))
    _verschiebe(db, m, date(2026, 8, 4), date(2026, 8, 18), "-1500")
    heute = date(2026, 8, 19)
    plan, _ = _plan(db, m, date(2026, 8, 17), heute)
    assert plan == {"2026-08-18": -1500.0}
    snap = erstelle_snapshot(db, m, start=date(2026, 8, 17), heute=heute)
    assert {d: v for d, v in snapshot_fluesse(snap)[_konto(db, m, "4920").id].items() if v} == {
        date(2026, 8, 18): Decimal("-1500.00")}
    for basis in ("LIVE", snap.id):
        k34, k35 = _kz(db, m, 34, heute, basis=basis), _kz(db, m, 35, heute, basis=basis)
        assert (k34["soll_kw"], k34["abweichung"]["ist_minus_plan"], k34["verfuegbar"]) == (
            1500.0, 0.0, 0.0)
        assert k35["verfuegbar"] == 0.0


def test_v2_vor_dem_fenster_bezahlter_posten_steht_nicht_im_fenster(db):
    m = _mandant(db)
    _bank(db, m)
    _op(db, m, "1500", date(2026, 8, 4), bezahlt=((date(2026, 8, 5), "1500"),))
    _verschiebe(db, m, date(2026, 8, 4), date(2026, 8, 18), "-1500")
    plan, p = _plan(db, m, date(2026, 8, 10), date(2026, 8, 12))
    assert plan == {}
    assert p["bestaende"]["liquiditaet"]["2026-08-30"] == 98500.0
    assert _kz(db, m, 34, date(2026, 8, 12))["verfuegbar"] == 0.0


# ---------------------------------------------------------------------------
# V3/V6/N6: verschobene vergangene Rate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("heute", [date(2026, 8, 10), date(2026, 8, 12), date(2026, 8, 17)])
def test_v3_verschobene_unbezahlte_rate_bleibt_gebunden(db, heute):
    m = _mandant(db)
    _bank(db, m)
    _miete(db, m)
    _verschiebe(db, m, date(2026, 8, 11), date(2026, 8, 19), "-2000")
    k34 = _kz(db, m, 34, heute)
    assert (k34["gebunden"]["summe"], k34["abweichung"]["vortraege"], k34["verfuegbar"]) == (
        2000.0, 0.0, 0.0)
    _, p = _plan(db, m, date(2026, 8, 10), heute)
    assert p["bestaende"]["liquiditaet"]["2026-08-23"] == 98000.0
    assert p["vortraege"] == []


def test_v6_verschoben_wird_die_rate_nicht_der_ueberfaellige_posten(db):
    m = _mandant(db)
    _bank(db, m)
    _miete(db, m)
    _op(db, m, "1500", date(2026, 8, 5))
    _verschiebe(db, m, date(2026, 8, 11), date(2026, 8, 19), "-2000")
    heute = date(2026, 8, 14)
    plan, p = _plan(db, m, date(2026, 8, 10), heute)
    assert plan == {"2026-08-14": -1500.0, "2026-08-19": -2000.0}
    assert p["vortraege"] == []
    assert p["bestaende"]["liquiditaet"]["2026-08-30"] == 96500.0
    k34 = _kz(db, m, 34, heute)
    assert (k34["gebunden"]["summe"], k34["abweichung"]["ueberfaellig"], k34["verfuegbar"]) == (
        2000.0, 1500.0, 0.0)


def test_n6_verschobene_bezahlte_rate_wird_nicht_erneut_geplant(db):
    m = _mandant(db)
    _bank(db, m)
    _miete(db, m, stichtag=8)                           # Mo 08.06.
    _zahlung(db, m, date(2026, 6, 8), "2000")           # trotz Verschiebung bezahlt
    _verschiebe(db, m, date(2026, 6, 8), date(2026, 7, 23), "-2000")
    heute = date(2026, 6, 10)
    plan, p = _plan(db, m, date(2026, 6, 8), heute, wochen=8)
    assert plan["2026-07-23"] == -2000.0                # angezeigt am Zieltag
    assert p["vortraege"] == []
    liq = p["bestaende"]["liquiditaet"]
    assert round(liq["2026-07-23"] - liq["2026-07-22"], 2) == 0.0   # nicht erneut gezahlt
    assert _kz(db, m, 30, heute)["verfuegbar"] == 0.0


# ---------------------------------------------------------------------------
# V4/N3: Ketten
# ---------------------------------------------------------------------------

def test_v4_kette_eines_bezahlten_postens(db):
    m = _mandant(db)
    _bank(db, m)
    _op(db, m, "3000", date(2026, 8, 12), bezahlt=((date(2026, 8, 10), "3000"),))
    _verschiebe(db, m, date(2026, 8, 12), date(2026, 8, 19), "-3000")
    _verschiebe(db, m, date(2026, 8, 19), date(2026, 8, 26), "-3000")
    plan, p = _plan(db, m, date(2026, 8, 17), date(2026, 8, 21))
    assert plan == {}
    assert p["bestaende"]["liquiditaet"]["2026-08-30"] == 97000.0
    plan, _ = _plan(db, m, date(2026, 8, 10), date(2026, 8, 14))
    assert plan == {"2026-08-26": -3000.0}


def test_n3_kette_ueber_tag_ausserhalb_des_fensters(db):
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 11, 1))
    _op(db, m, "2500", date(2026, 11, 30))
    _verschiebe(db, m, date(2026, 11, 30), date(2026, 12, 20), "-2500")
    _verschiebe(db, m, date(2026, 12, 20), date(2026, 12, 6), "-2500")
    heute = date(2026, 11, 18)
    plan, _ = _plan(db, m, date(2026, 11, 16), heute, wochen=3)     # bis 06.12.
    assert plan == {"2026-12-06": -2500.0}
    k49 = _kz(db, m, 49, heute)
    assert (k49["soll_kw"], k49["gebunden"]["summe"], k49["verfuegbar"]) == (2500.0, 2500.0, 0.0)


# ---------------------------------------------------------------------------
# N2: mehrere Verschiebungen vom selben Tag außerhalb des Fensters
# ---------------------------------------------------------------------------

def test_n2_mehrere_verschiebungen_vom_selben_tag_ausserhalb_des_fensters(db):
    m = _mandant(db)
    _bank(db, m)
    _op(db, m, "1000", date(2026, 9, 14))
    _verschiebe(db, m, date(2026, 9, 14), date(2026, 8, 18), "-600")
    _verschiebe(db, m, date(2026, 9, 14), date(2026, 8, 20), "-600")
    plan, p = _plan(db, m, date(2026, 8, 10), date(2026, 8, 12), wochen=2)
    assert plan == {"2026-08-18": -600.0, "2026-08-20": -400.0}
    assert p["bestaende"]["liquiditaet"]["2026-08-23"] == 99000.0


# ---------------------------------------------------------------------------
# V5/V5b: Fenster bzw. Ursprung in der Zukunft
# ---------------------------------------------------------------------------

def test_v5_zukunftsfenster(db):
    m = _mandant(db)
    _bank(db, m)
    _op(db, m, "3000", date(2026, 8, 25), konto="4210")
    _verschiebe(db, m, date(2026, 8, 25), date(2026, 9, 8), "-3000", konto="4210")
    plan, p = _plan(db, m, date(2026, 8, 31), date(2026, 8, 5), wochen=2, nummer="4210")
    assert plan == {"2026-09-08": -3000.0}
    assert p["bestaende"]["liquiditaet"]["2026-09-13"] == 97000.0


def test_v5b_ursprung_in_kuenftiger_woche_nimmt_ueberfaelliges_nicht_mit(db):
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 4, 1))
    _op(db, m, "1200", date(2026, 4, 5), konto="3400")
    _verschiebe(db, m, date(2026, 6, 1), date(2026, 5, 4), "-1200", konto="3400")
    heute = date(2026, 4, 16)
    plan, _ = _plan(db, m, date(2026, 4, 13), heute, wochen=8, nummer="3400")
    assert plan == {"2026-04-16": -1200.0}
    assert _kz(db, m, 19, heute, konto="3400")["soll_kw"] == 0.0


# ---------------------------------------------------------------------------
# N4: Ziel verstrichen, nicht bezahlt – rollt
# ---------------------------------------------------------------------------

def test_n4_verstrichenes_ziel_rollt(db):
    m = _mandant(db)
    _bank(db, m)
    _op(db, m, "1000", date(2026, 9, 10))
    _verschiebe(db, m, date(2026, 9, 10), date(2026, 9, 2), "-1000")
    heute = date(2026, 9, 5)                                       # Samstag
    plan, p = _plan(db, m, date(2026, 8, 31), heute)
    assert plan == {"2026-09-07": -1000.0}
    assert p["bestaende"]["liquiditaet"]["2026-09-20"] == 99000.0
    k36 = _kz(db, m, 36, heute)
    assert (k36["abweichung"]["ist_minus_plan"], k36["abweichung"]["ueberfaellig"],
            k36["verfuegbar"]) == (-1000.0, 1000.0, 0.0)


# ---------------------------------------------------------------------------
# N5/N7: Planstand zum Wochenbeginn wie LIVE, vorab gezahlte Rate
# ---------------------------------------------------------------------------

def test_n5_planstand_zum_wochenbeginn_ueberfaelliges_wie_live(db):
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 7, 1))
    _op(db, m, "3000", date(2026, 7, 9))
    heute = date(2026, 7, 20)
    db.commit()
    snap = erstelle_snapshot(db, m, start=heute, heute=heute)
    for basis in ("LIVE", snap.id):
        k31 = _kz(db, m, 31, heute, basis=basis)
        assert (k31["abweichung"]["ueberfaellig"], k31["verfuegbar"]) == (0.0, 0.0)
        k30 = _kz(db, m, 30, heute, basis=basis)
        assert (k30["soll_kw"], k30["abweichung"]["ueberfaellig"], k30["verfuegbar"]) == (
            3000.0, 3000.0, 0.0)


def test_n7_vorab_gezahlte_rate_der_laufenden_woche(db):
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 4, 1))
    _miete(db, m, betrag="3000", stichtag=18)                      # Sa 18.04. → Mo 20.04.
    _zahlung(db, m, date(2026, 4, 10), "3000")
    heute = date(2026, 4, 26)
    k17, k21 = _kz(db, m, 17, heute), _kz(db, m, 21, heute)
    assert (k17["abweichung"]["ist_minus_plan"], k21["verfuegbar"]) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# N8: Verschiebung vom Rolltag eines überfälligen Postens – auch nach dem Tag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("heute", [date(2026, 4, 11), date(2026, 4, 13), date(2026, 4, 15)])
def test_n8_verschiebung_vom_rolltag_bleibt_wirksam(db, heute):
    """OP 1.000 seit Fr 10.04. überfällig; am Sa 11.04. steht er auf dem Rolltag Mo 13.04.
    und wird von dort zur Hälfte auf den 22.04. verschoben. Auch am Mi 15.04. bleibt der
    Anteil verschoben, der Rest rollt."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 4, 1))
    _op(db, m, "1000", date(2026, 4, 10), konto="4930")
    _verschiebe(db, m, date(2026, 4, 13), date(2026, 4, 22), "-500", konto="4930")
    rolltag = max(heute, date(2026, 4, 13)).isoformat()
    plan, p = _plan(db, m, date(2026, 4, 6), heute, nummer="4930")
    assert plan == {rolltag: -500.0, "2026-04-22": -500.0}
    k17 = _kz(db, m, 17, heute, konto="4930")
    assert (k17["gebunden"]["summe"], k17["verfuegbar"]) == (500.0, 0.0)
    snap = erstelle_snapshot(db, m, start=date(2026, 4, 13), heute=heute)
    werte = {d: v for d, v in snapshot_fluesse(snap)[_konto(db, m, "4930").id].items() if v}
    assert werte == {date(2026, 4, 13): Decimal("-500.00"), date(2026, 4, 22): Decimal("-500.00")}


# ---------------------------------------------------------------------------
# Zuordnung: Reihenfolge, Budget, Termine, Markierung
# ---------------------------------------------------------------------------

def test_zuordnung_offenes_vor_erledigtem_vor_budget(db):
    """Am Ursprungstag: bezahlter Posten 1.000, offener Posten 500, Budget. Die Verschiebung
    über 2.000 nimmt den offenen, dann den bezahlten Posten, den Rest (500) aus dem Budget."""
    m = _mandant(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 35, "5000")
    _op(db, m, "1000", date(2026, 8, 26), bezahlt=((date(2026, 8, 11), "1000"),))
    offen = _op(db, m, "500", date(2026, 8, 26))
    _verschiebe(db, m, date(2026, 8, 26), date(2026, 9, 9), "-2000")
    db.commit()
    z = Zuordnung(db, m, date(2026, 8, 12))
    v = z.verschiebungen[0]
    assert z.budget_anspruch == {v.id: Decimal("500")}
    assert z.verteilung(("OP", offen.id, "offen"), date(2026, 8, 26), Decimal("500")) == [
        (date(2026, 9, 9), Decimal("500"), True)]
    plan, p = _plan(db, m, date(2026, 8, 10), date(2026, 8, 12), wochen=5)
    # KW 35: Budget 5.950 brutto − 1.500 explizit = 4.450 Rest, davon 500 verschoben
    assert round(sum(v for t, v in plan.items() if t <= "2026-08-30"), 2) == -3950.0
    assert plan["2026-09-09"] == -2000.0
    zeile = _zeile(p)
    assert round(sum(zeile["verschoben"].values()), 2) == 0.0
    assert zeile["verschoben"]["2026-09-09"] == -2000.0


def test_verschobener_zahlungstermin_von_ausserhalb_des_fensters(db):
    m = _mandant(db)
    _bank(db, m)
    db.add(models.Zahlungstermin(mandant_id=m.id, typ="UST_VA", datum=date(2026, 9, 10),
                                 betrag=Decimal("5000")))
    db.add(models.PlanVerschiebung(mandant_id=m.id, termin_typ="UST_VA", quelle="PLAN",
                                   von=date(2026, 9, 10), nach=date(2026, 8, 20),
                                   betrag=Decimal("-5000")))
    db.commit()
    p = berechne_plan(db, m, start=date(2026, 8, 10), wochen=2, heute=date(2026, 8, 12))
    zeile = next(k for g in p["zeilen"] for k in g["kinder"] if k["key"] == "TERMIN:UST_VA")
    assert {t: v for t, v in zeile["plan"].items() if v} == {"2026-08-20": -5000.0}
    assert p["bestaende"]["liquiditaet"]["2026-08-23"] == 95000.0


# ---------------------------------------------------------------------------
# Orakel Runde 3: vorgezogene künftige Rate, externes Budget
# ---------------------------------------------------------------------------

def test_kuenftige_rate_auf_vergangenen_tag_vorgezogen_wird_vorgetragen(db):
    """Rate 1.200 am 12., Augustrate auf Mo 22.06. vorgezogen und nicht gezahlt; die
    Junirate ist am 12.06. bezahlt – deren Zahlung gilt nicht der vorgezogenen Rate."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 6, 1))
    _miete(db, m, betrag="1200", stichtag=12, konto="3400")
    _zahlung(db, m, date(2026, 6, 12), "1200", konto="3400")
    _verschiebe(db, m, date(2026, 8, 12), date(2026, 6, 22), "-1200", konto="3400")
    heute = date(2026, 6, 28)                                       # Sonntag
    _, p = _plan(db, m, date(2026, 6, 22), heute, wochen=13, nummer="3400")
    assert [(v["faellig"], v["betrag"]) for v in p["vortraege"]] == [("2026-06-22", -1200.0)]
    assert p["bestaende"]["liquiditaet_vortrag"]["2026-09-20"] == 95200.0
    k26 = _kz(db, m, 26, heute, konto="3400")
    assert (k26["abweichung"]["ist_minus_plan"], k26["abweichung"]["vortraege"],
            k26["verfuegbar"]) == (-1200.0, 1200.0, 0.0)


def test_budget_aus_woche_ausserhalb_des_fensters_wie_im_planstand(db):
    """Budget KW 38 (2.975 brutto), Verschiebung ohne explizit Geplantes vom 14.09. auf den
    10.09.; ein heute fälliger offener Posten rollt in einer eigenen Wochensicht ab 14.09.
    auf deren Montag – das Budget ist dennoch dasselbe wie im Planstand (595 je Tag)."""
    m = _mandant(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 38, "2500")
    _op(db, m, "1000", date(2026, 9, 3))
    _verschiebe(db, m, date(2026, 9, 14), date(2026, 9, 10), "-5499.99")
    heute = date(2026, 9, 3)
    db.commit()
    snap = erstelle_snapshot(db, m, start=date(2026, 8, 31), heute=heute)
    assert _kz(db, m, 37, heute)["soll_kw"] == 595.0
    assert _kz(db, m, 37, heute, basis=snap.id)["soll_kw"] == 595.0


# ---------------------------------------------------------------------------
# Runde 3 (Gegenprüfung Iteration 1): Anlagetag, Rolltag der Anlage, Budget brutto,
# gemeinsamer Ratenabgleich – alle gegen den Stand der Iteration 1 fehlgeschlagen
# ---------------------------------------------------------------------------

def _kz_ebene(db, m, kw, heute, ebene="gesamt", konto=None, basis="LIVE", jahr=2026) -> dict:
    r = svc.freigabe_kennzahlen(db, m, konto_id=_konto(db, m, konto).id if konto else None,
                                betrag_brutto=Decimal("0"), jahr=jahr, kw=kw, basis=basis,
                                heute=heute)
    return next(e for e in r["ebenen"] if e["ebene"] == ebene)


def _liq_ende(p) -> float:
    b = p["bestaende"]
    return (b.get("liquiditaet_vortrag") or b["liquiditaet"])[p["tage"][-1]]


@pytest.mark.parametrize("variante", ["storniert", "umterminiert"])
def test_v6_verschiebung_nimmt_nur_am_rolltag_ihrer_anlage_ueberfaelliges_mit(db, variante):
    """OP_A 1.000 seit Fr 07.08. überfällig; am Mo 10.08. werden 600 von OP_B vom 11.08. auf
    den 25.08. verschoben, danach wird OP_B storniert bzw. umterminiert. OP_A wurde nie
    verschoben: er rollt weiter (C3), am 25.08. steht nichts von ihm."""
    m = _mandant(db)
    _bank(db, m)
    _op(db, m, "1000", date(2026, 8, 7))
    op_b = _op(db, m, "600", date(2026, 8, 11))
    _verschiebe(db, m, date(2026, 8, 11), date(2026, 8, 25), "-600", angelegt=date(2026, 8, 10))
    if variante == "storniert":
        op_b.status = "STORNIERT"
    else:
        op_b.faellig_am = date(2026, 8, 14)
    heute = date(2026, 8, 12)
    plan, p = _plan(db, m, date(2026, 8, 10), heute)
    assert plan == ({"2026-08-12": -1000.0} if variante == "storniert"
                    else {"2026-08-12": -1000.0, "2026-08-14": -600.0})
    k35 = _kz(db, m, 35, heute)
    assert (k35["abweichung"]["ueberfaellig"], k35["verfuegbar"]) == (1000.0, 0.0)


def test_v6_budgetverschiebung_nimmt_nach_ihrem_ursprungstag_keinen_ueberfaelligen_op(db):
    """Budget KW 35 10.000; am Mi 19.08. 2.000 Budget vom 25.08. auf den 08.09. verschoben,
    ein OP 1.000 ist seit 10.08. überfällig. Am 27.08. bleibt er überfällig (C3), am 08.09.
    steht nur das Budget."""
    m = _mandant(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 35, "10000")
    _op(db, m, "1000", date(2026, 8, 10))
    _verschiebe(db, m, date(2026, 8, 25), date(2026, 9, 8), "-2000", angelegt=date(2026, 8, 19))
    heute = date(2026, 8, 27)
    plan, _ = _plan(db, m, date(2026, 8, 24), heute)
    assert plan["2026-09-08"] == -2000.0
    for kw in (36, 37):
        assert _kz(db, m, kw, heute)["abweichung"]["ueberfaellig"] == 1000.0


@pytest.mark.parametrize("heute", [date(2026, 8, 10), date(2026, 8, 12)])
def test_v1_rolltag_teilzahlung_nach_der_anlage_wird_mitverschoben(db, heute):
    """OP 1.500 fällig Fr 31.07.; am So 02.08. steht er auf dem Rolltag Mo 03.08. und wird
    von dort über −1.500 auf den 17.08. verschoben; am 04.08. Teilzahlung 500, Budget nur in
    KW 32. Verschoben wird der ganze Posten (Rest und Teilzahlung), kein vergangenes Budget."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 7, 30))
    _budget_woche(db, m, "4920", 32, "10000")
    _op(db, m, "1500", date(2026, 7, 31), bezahlt=((date(2026, 8, 4), "500"),))
    _verschiebe(db, m, date(2026, 8, 3), date(2026, 8, 17), "-1500", angelegt=date(2026, 8, 2))
    plan, p = _plan(db, m, date(2026, 8, 10), heute)
    assert plan == {"2026-08-17": -1000.0}
    assert p["bestaende"]["liquiditaet"]["2026-08-30"] == 98500.0
    assert _kz(db, m, 34, heute)["verfuegbar"] == 0.0


def test_zahlung_nach_der_anlage_laesst_die_verschiebung_beim_posten(db):
    """OP1 1.500 und OP2 800 am 26.08.; am 03.08. werden 1.500 (OP1) auf den 09.09. verschoben,
    OP1 wird am 10.08. bezahlt. OP2 bleibt am 26.08. geplant und gebunden."""
    m = _mandant(db)
    _bank(db, m)
    _op(db, m, "1500", date(2026, 8, 26), bezahlt=((date(2026, 8, 10), "1500"),))
    _op(db, m, "800", date(2026, 8, 26))
    _verschiebe(db, m, date(2026, 8, 26), date(2026, 9, 9), "-1500", angelegt=date(2026, 8, 3))
    heute = date(2026, 8, 11)
    plan, p = _plan(db, m, date(2026, 8, 10), heute, wochen=6)
    assert plan == {"2026-08-26": -800.0, "2026-09-09": -1500.0}
    assert p["bestaende"]["liquiditaet"]["2026-08-30"] == 97700.0
    assert _kz(db, m, 35, heute)["gebunden"]["summe"] == 800.0


def test_bezahlte_rate_der_kette_zieht_die_bestellung_nicht_nach(db):
    """Miete 2.000 am 18.05. am 04.05. auf den 02.06. verschoben, am 05.05. 2.000 (= Miete)
    vom 02.06. auf den 11.05. vorgezogen; am 02.06. steht auch eine freigegebene Bestellung
    über 1.500. Die Miete wird am 18.05. abgebucht. Die Bestellung bleibt am 02.06., nichts
    ist überfällig, LIVE und frischer Planstand rechnen gleich."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 5, 1))
    _miete(db, m, stichtag=18)
    u = _benutzer(db)
    b = _bestellung(db, m, u, netto="1260.50", zahldatum=date(2026, 6, 2), status="FREIGEGEBEN",
                    bestelldatum=date(2026, 5, 1))
    b.erstellt_am = datetime(2026, 5, 1, 9)
    _verschiebe(db, m, date(2026, 5, 18), date(2026, 6, 2), "-2000", angelegt=date(2026, 5, 4))
    _verschiebe(db, m, date(2026, 6, 2), date(2026, 5, 11), "-2000", angelegt=date(2026, 5, 5))
    _zahlung(db, m, date(2026, 5, 18), "2000")
    heute = date(2026, 5, 19)
    plan, _ = _plan(db, m, date(2026, 5, 18), heute, wochen=5)
    assert plan == {"2026-06-02": -1500.0, "2026-06-18": -2000.0}
    snap = erstelle_snapshot(db, m, start=date(2026, 5, 18), heute=heute)
    for kw in (21, 23):
        live, frisch = _kz(db, m, kw, heute), _kz(db, m, kw, heute, basis=snap.id)
        assert live["abweichung"]["ueberfaellig"] == 0.0
        assert live["verfuegbar"] == frisch["verfuegbar"]
    assert _kz(db, m, 23, heute)["gebunden"]["summe"] == 1500.0


def test_zahlstand_gestern_und_heute_ordnen_gleich_zu(db):
    """Orakel P6 Seed 4720: Rate 4.999,99 heute (Mo 18.05.) bezahlt, verschoben 18.05. → 02.06.
    und von dort 2.999,99 auf den 10.05.; am 02.06. eine freigegebene Bestellung 1.500. Die
    Freigabe (Zahlstand gestern) und der frische Planstand (Stand heute) bewegen dieselben
    Bestandteile."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 3, 9))
    _miete(db, m, betrag="4999.99", stichtag=17, konto="4210")
    u = _benutzer(db)
    _bestellung(db, m, u, konto="4210", netto="1500", zahldatum=date(2026, 6, 2),
                status="FREIGEGEBEN")
    _zahlung(db, m, date(2026, 5, 18), "4999.99", konto="4210")
    _verschiebe(db, m, date(2026, 5, 18), date(2026, 6, 2), "-4999.99", konto="4210")
    _verschiebe(db, m, date(2026, 6, 2), date(2026, 5, 10), "-2999.99", konto="4210")
    heute = date(2026, 5, 18)
    db.commit()
    snap = erstelle_snapshot(db, m, start=heute, heute=heute)
    for kw in (21, 23):
        live = _kz(db, m, kw, heute, konto="4210")
        frisch = _kz(db, m, kw, heute, konto="4210", basis=snap.id)
        assert (live["soll_kw"], live["verfuegbar"]) == (frisch["soll_kw"], frisch["verfuegbar"])
    assert _kz(db, m, 21, heute, konto="4210")["verfuegbar"] == 0.0


def test_offene_bestellung_am_ursprungstag_nimmt_der_budgetverschiebung_nichts(db):
    """Budget KW 34 5.000 netto (1.190 brutto je Banktag); am 03.08. das Budget des 19.08. auf
    den 31.08. verschoben, am 05.08. kommt eine Bestellung über 1.190 brutto mit Zahldatum
    19.08. zur Freigabe. Spielraum KW 34 = 5.950 − 1.190, KW 36 trägt die 1.190."""
    m = _mandant(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 34, "5000")
    _verschiebe(db, m, date(2026, 8, 19), date(2026, 8, 31), "-1190", angelegt=date(2026, 8, 3))
    b = _bestellung(db, m, _benutzer(db), netto="1000", zahldatum=date(2026, 8, 19))
    heute = date(2026, 8, 5)
    db.commit()
    r = svc.bestellung_kennzahlen(db, m, b, jahr=2026, kw=34, heute=heute)
    assert next(e for e in r["ebenen"] if e["ebene"] == "konto")["verfuegbar"] == 4760.0
    assert _kz(db, m, 36, heute)["soll_kw"] == 1190.0


def _budget_kw34_nach_kw36(db, m):
    """Budget KW 34 5.000 netto (5.950 brutto); am 03.08. in der Wochenansicht 3.000 nach
    KW 36 verschoben (17.08. 1.190, 18.08. 1.190, 19.08. 620)."""
    _bank(db, m)
    _budget_woche(db, m, "4920", 34, "5000")
    for tag, betrag in ((17, "-1190"), (18, "-1190"), (19, "-620")):
        _verschiebe(db, m, date(2026, 8, tag), date(2026, 8, 31), betrag,
                    angelegt=date(2026, 8, 3))


def _kw_summen(plan: dict) -> dict:
    return {kw: round(sum(v for t, v in plan.items()
                          if date.fromisoformat(t).isocalendar().week == kw), 2) for kw in (34, 36)}


def test_budgetanteil_bleibt_wenn_eine_andere_zahlung_das_restbudget_verbraucht(db):
    """Nach der Verschiebung wird eine Bestellung über 2.380 mit Zahldatum Fr 21.08.
    freigegeben: KW 34 = max(2.950, 2.380), KW 36 behält 3.000, Spielraum KW 34 570."""
    m = _mandant(db)
    _budget_kw34_nach_kw36(db, m)
    _bestellung(db, m, _benutzer(db), netto="2000", zahldatum=date(2026, 8, 21),
                status="FREIGEGEBEN")
    heute = date(2026, 8, 6)
    plan, _ = _plan(db, m, date(2026, 8, 17), heute)
    assert _kw_summen(plan) == {34: -2950.0, 36: -3000.0}
    assert _kz(db, m, 34, heute)["verfuegbar"] == 570.0


def test_nach_der_verschiebung_angelegte_bestellung_bleibt_am_zahldatum(db):
    """Die Bestellung (2.380, Zahldatum 19.08.) wird erst am 05.08. angelegt und freigegeben:
    die Verschiebung vom 19.08. bewegt weiter Budget, die Bestellung bleibt in KW 34."""
    m = _mandant(db)
    _budget_kw34_nach_kw36(db, m)
    b = _bestellung(db, m, _benutzer(db), netto="2000", zahldatum=date(2026, 8, 19),
                    status="FREIGEGEBEN")
    b.erstellt_am = datetime(2026, 8, 5, 10)
    heute = date(2026, 8, 6)
    plan, _ = _plan(db, m, date(2026, 8, 17), heute)
    assert _kw_summen(plan) == {34: -2950.0, 36: -3000.0}
    k34 = _kz(db, m, 34, heute)
    assert (k34["gebunden"]["summe"], k34["verfuegbar"]) == (2380.0, 570.0)
    assert _kz(db, m, 36, heute)["gebunden"]["summe"] == 0.0


@pytest.mark.parametrize("teile", [None, ["1200"], ["1080", "120"]])
def test_vorgezogene_kuenftige_rate_rechnet_keine_fruehere_zahlung_an(db, teile):
    """Miete 1.200 am 13., Junirate am 01.07. verspätet bezahlt; Julirate auf heute (Mi 08.07.)
    vorgezogen und nicht bezahlt – als eine Verschiebung oder in Teilen: Vortrag 1.200,
    Liquidität wie ohne Verschiebung."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 5, 1))
    _miete(db, m, betrag="1200", stichtag=13, konto="4930")
    _zahlung(db, m, date(2026, 7, 1), "1200", konto="4930")
    for betrag in teile or []:
        _verschiebe(db, m, date(2026, 7, 13), date(2026, 7, 8), f"-{betrag}", konto="4930")
    _, p = _plan(db, m, date(2026, 7, 6), date(2026, 7, 8), wochen=13, nummer="4930")
    assert sum(v["betrag"] for v in p["vortraege"]) == (-1200.0 if teile else 0.0)
    assert _liq_ende(p) == 95200.0


def test_ratenzahlung_wird_nur_einmal_angerechnet(db):
    """Rate 500 am 9.; Aprilrate am 09.04. bezahlt und (trotzdem) auf den 26.05. verschoben,
    Mairate auf den 08.04. vorgezogen und nicht bezahlt; heute Fr 10.04.: die Zahlung gilt
    der Aprilrate, die Mairate wird vorgetragen."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 2, 1))
    _miete(db, m, betrag="500", stichtag=9, konto="3400")
    _zahlung(db, m, date(2026, 4, 9), "500", konto="3400")
    _verschiebe(db, m, date(2026, 5, 11), date(2026, 4, 8), "-500", konto="3400")
    _verschiebe(db, m, date(2026, 4, 9), date(2026, 5, 26), "-500", konto="3400")
    heute = date(2026, 4, 10)
    _, p = _plan(db, m, date(2026, 4, 6), heute, wochen=13, nummer="3400")
    assert [(v["faellig"], v["betrag"]) for v in p["vortraege"]] == [("2026-04-08", -500.0)]
    assert _liq_ende(p) == 98500.0
    assert _kz(db, m, 22, heute, konto="3400")["verfuegbar"] == 0.0


@pytest.mark.parametrize("art", ["ohne", "hin", "hin_und_zurueck"])
def test_verschiebung_einer_kuenftigen_rate_aendert_den_vortrag_nicht(db, art):
    """Miete 1.200 am 27.; Junirate in zwei Hälften (29.06., 15.07.) bezahlt, Julirate offen;
    heute Sa 01.08. Eine Verschiebung der Augustrate (hin bzw. hin und zurück) ändert Vortrag
    und Spielraum nicht."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 5, 1))
    _miete(db, m, betrag="1200", stichtag=27, konto="4210")
    _zahlung(db, m, date(2026, 6, 29), "600", konto="4210")
    _zahlung(db, m, date(2026, 7, 15), "600", konto="4210")
    if art != "ohne":
        _verschiebe(db, m, date(2026, 8, 27), date(2026, 9, 7), "-1200", konto="4210")
    if art == "hin_und_zurueck":
        _verschiebe(db, m, date(2026, 9, 7), date(2026, 8, 27), "-1200", konto="4210")
    heute = date(2026, 8, 1)
    _, p = _plan(db, m, date(2026, 7, 27), heute, wochen=13, nummer="4210")
    assert sum(v["betrag"] for v in p["vortraege"]) == -600.0
    assert _kz(db, m, 31, heute, konto="4210")["verfuegbar"] == 0.0


@pytest.mark.parametrize("mit_verschiebung", [False, True])
def test_verschiebung_auf_anderer_zeile_aendert_den_ratenabgleich_nicht(db, mit_verschiebung):
    """Orakel I12: Rate 1.500 auf 4930 am 07.09. (Stichtag 5.) vorab in zwei Teilen am 03./04.09.
    bezahlt, Augustrate offen. Eine Verschiebung einer vergangenen Rate auf 3400 verlegte den
    Abgleichsbeginn vor die Augustrate, die dann die Vorauszahlung erhielt (Spielraum 1.500)."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 7, 1))
    _miete(db, m, betrag="1500", stichtag=5, konto="4930")
    _zahlung(db, m, date(2026, 9, 3), "750", konto="4930")
    _zahlung(db, m, date(2026, 9, 4), "750", konto="4930")
    _miete(db, m, betrag="800", stichtag=3, konto="3400")
    if mit_verschiebung:
        _verschiebe(db, m, date(2026, 8, 3), date(2026, 9, 14), "-800", konto="3400",
                    angelegt=date(2026, 7, 31))
    heute = date(2026, 9, 8)
    db.commit()
    k37 = _kz(db, m, 37, heute, konto="4930")
    assert (k37["abweichung"]["ist_minus_plan"], k37["verfuegbar"]) == (0.0, 0.0)


def test_insolvenzgeld_entfallende_rate_rechnet_keine_zahlung_an(db):
    """Igeld-Zeitraum 01.08.–31.10.: Gehälter (4120, 10.000 am 15.) entfallen im Plan; eine
    Zahlung von 10.000 am 03.08. (vor s0) wird keiner Rate zugeordnet und nicht nachgetragen."""
    m = _mandant(db)
    _bank(db, m)
    m.insolvenzgeld_aktiv = True
    m.insolvenzgeld_von = date(2026, 8, 1)
    m.insolvenzgeld_bis = date(2026, 10, 31)
    db.add(models.Dauerbuchung(mandant_id=m.id, name="Gehälter", art="KREDITOR",
                               konto_id=_konto(db, m, "4120").id, betrag_brutto=Decimal("10000"),
                               intervall="MONATLICH", stichtag=15, gueltig_von=date(2026, 1, 1)))
    _zahlung(db, m, date(2026, 8, 3), "10000", konto="4120")
    db.commit()
    for ebene in ("gruppe", "gesamt"):
        e = _kz_ebene(db, m, 34, date(2026, 8, 19), ebene=ebene, konto="4120")
        assert (e["abweichung"]["ist_minus_plan"], e["verfuegbar"]) == (0.0, 0.0)


@pytest.mark.parametrize("mit_konto", [True, False])
def test_verschobener_termin_vor_dem_wochenbeginn_erledigt(db, mit_konto):
    """USt-VA 3.000 am 10.08., verschoben auf den 26.08., am 14.08. bezahlt (Ist auf 1780) und
    erledigt; heute Mi 19.08.: der Termin steht am Zieltag im Soll, das Ist seit s0 zeigt seine
    Zahlung nicht – er bleibt gebunden, KW 35 ohne Spielraum (Iteration 3: der verschobene
    erledigte Termin steht wieder im Plan, siehe test_gestundeter_termin_…)."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 7, 1))
    ust = _konto(db, m, "1780")
    db.add(models.Zahlungstermin(mandant_id=m.id, typ="UST_VA", datum=date(2026, 8, 10),
                                 betrag=Decimal("3000"), konto_id=ust.id if mit_konto else None,
                                 status="ERLEDIGT", generiert=False))
    _zahlung(db, m, date(2026, 8, 14), "3000", konto="1780")
    _verschiebe(db, m, date(2026, 8, 10), date(2026, 8, 26), "-3000", konto="1780",
                termin_typ=None if mit_konto else "UST_VA", angelegt=date(2026, 8, 3))
    db.commit()
    for kw in (34, 35):
        assert _kz_ebene(db, m, kw, date(2026, 8, 19))["verfuegbar"] == 0.0


@pytest.mark.parametrize("verschoben", [False, True])
def test_bezahlter_posten_steht_nur_mit_zahlungen_ab_fensterbeginn_im_plan(db, verschoben):
    """OP 3.000 fällig 12.08., Teilzahlung 1.000 am 05.08., Rest 2.000 am 26.08. bezahlt (ggf.
    vorher 12.08. → 26.08. verschoben); heute Do 27.08.: im Fenster ab 24.08. steht der Posten
    mit 2.000, kein Spielraum."""
    m = _mandant(db)
    _bank(db, m)
    _op(db, m, "3000", date(2026, 8, 12),
        bezahlt=((date(2026, 8, 5), "1000"), (date(2026, 8, 26), "2000")))
    if verschoben:
        _verschiebe(db, m, date(2026, 8, 12), date(2026, 8, 26), "-2000",
                    angelegt=date(2026, 8, 6))
    heute = date(2026, 8, 27)
    plan, _ = _plan(db, m, date(2026, 8, 24), heute, wochen=2)
    assert plan == {("2026-08-26" if verschoben else "2026-08-24"): -2000.0}
    for kw in (35, 36, 38):
        for basis in ("LIVE", "AUTO"):
            assert _kz_ebene(db, m, kw, heute, basis=basis)["verfuegbar"] == 0.0


def test_folgeverschiebung_nimmt_den_bestandteil_in_ihrer_hoehe(db):
    """Jahreswechsel: OP 2.000 am 14.12. vom 30.12. auf den 04.01. verschoben, dann 1.000
    (= Januarmiete am 04.01.) nach Februar. Verschoben wird die Miete, nicht der dorthin
    gewanderte Posten – am 05.01. ist nichts vorzutragen."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 12, 1))
    _op(db, m, "2000", date(2026, 12, 30))
    _miete(db, m, betrag="1000", stichtag=1)
    _zahlung(db, m, date(2026, 12, 1), "1000")
    _verschiebe(db, m, date(2026, 12, 30), date(2027, 1, 4), "-2000", angelegt=date(2026, 12, 14))
    _verschiebe(db, m, date(2027, 1, 4), date(2027, 2, 1), "-1000", angelegt=date(2026, 12, 14))
    plan, p = _plan(db, m, date(2026, 12, 28), date(2027, 1, 5), wochen=6)
    assert p["vortraege"] == []
    assert plan["2027-02-01"] == -2000.0


def test_zellendialog_zeigt_verschobene_bestellung_am_zieltag(db):
    """Freigegebene Bestellung 1.190 mit Zahldatum 19.08., am 03.08. auf den 02.09. verschoben:
    der Zellendialog listet sie wie der Plan in der KW-36-Zelle, nicht in KW 34/35."""
    m = _mandant(db)
    _bank(db, m)
    b = _bestellung(db, m, _benutzer(db), netto="1000", zahldatum=date(2026, 8, 19),
                    status="FREIGEGEBEN")
    b.erstellt_am = datetime(2026, 7, 27, 9)
    _verschiebe(db, m, date(2026, 8, 19), date(2026, 9, 2), "-1190", angelegt=date(2026, 8, 3))
    heute = date(2026, 8, 25)
    db.commit()
    plan, _ = _plan(db, m, date(2026, 8, 17), heute)
    assert plan == {"2026-09-02": -1190.0}
    kid = _konto(db, m, "4920").id

    def zelle(von):
        return [(x["id"], x["plan_tag"], x["plan_betrag"], x["gerollt"]) for x in
                svc.plan_bestellungen(db, m, konto_id=kid, von=von,
                                      bis=von + timedelta(days=6), heute=heute)]

    assert zelle(date(2026, 8, 17)) == [] and zelle(date(2026, 8, 24)) == []
    assert zelle(date(2026, 8, 31)) == [(b.id, "2026-09-02", 1190.0, False)]


@pytest.mark.parametrize("teile", [["1200"], ["600", "600"]])
def test_teilbetraege_wirken_wie_das_ganze(db, teile):
    """Orakel I9: zwei Raten am 20.03. auf 3400 (500 bezahlt, 1.200 offen); 1.200 vom 20.03.
    auf den 11.04. verschoben – als eine Verschiebung oder in zwei Hälften: gleicher Plan,
    gleiche Liquidität, gleicher Vortrag."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 1, 23))
    _miete(db, m, betrag="500", stichtag=20, konto="3400")
    _miete(db, m, betrag="1200", stichtag=20, konto="3400")
    _zahlung(db, m, date(2026, 3, 20), "500", konto="3400")
    for betrag in teile:
        _verschiebe(db, m, date(2026, 3, 20), date(2026, 4, 11), f"-{betrag}", konto="3400")
    plan, p = _plan(db, m, date(2026, 3, 30), date(2026, 4, 3), wochen=13, nummer="3400")
    assert plan["2026-04-11"] == -1200.0
    assert p["vortraege"] == []
    # verschoben wird die offene Rate in Höhe der Verschiebung: 100.000 − 500 Ist − 1.200 − 3 × 1.700
    assert _liq_ende(p) == 93200.0


def test_hin_und_rueckverschiebung_von_budget_ohne_wirkung(db):
    """Orakel I7: Budget KW 25 und eine freigegebene Bestellung am 19.06.; 595 Budget vom 19.06.
    auf den 14.06. und zurück verschoben – der Plan ist je Tag derselbe wie ohne."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 5, 1))
    _budget_woche(db, m, "4920", 25, "2500")
    _bestellung(db, m, _benutzer(db), netto="750.50", zahldatum=date(2026, 6, 19),
                status="FREIGEGEBEN")
    heute = date(2026, 5, 25)
    ohne, _ = _plan(db, m, date(2026, 6, 15), heute, wochen=1)
    _verschiebe(db, m, date(2026, 6, 19), date(2026, 6, 14), "-595", angelegt=date(2026, 5, 20))
    _verschiebe(db, m, date(2026, 6, 14), date(2026, 6, 19), "-595", angelegt=date(2026, 5, 21))
    mit, _ = _plan(db, m, date(2026, 6, 15), heute, wochen=1)
    assert mit == ohne


@pytest.mark.parametrize("zahltag", [date(2026, 6, 5), date(2026, 6, 4)])
def test_erledigter_termin_ohne_konto_heute_bezahlt_bleibt_gebunden(db, zahltag):
    """Orakel P1 (Seeds 1200, 1370, 993): Lohnsteuer-Termin ohne Konto 1.000 am 13.07., heute
    (Fr 05.06.) bzw. gestern bezahlt (Ist auf 1741) und erledigt: heute bezahlt bleibt er
    gebunden (das Ist zählt erst morgen), gestern bezahlt deckt ihn das Ist der Gruppe."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 3, 27))
    db.add(models.Zahlungstermin(mandant_id=m.id, typ="LST", datum=date(2026, 7, 13),
                                 betrag=Decimal("1000"), status="ERLEDIGT", generiert=False))
    _zahlung(db, m, zahltag, "1000", konto="1741")
    db.commit()
    heute = date(2026, 6, 5)
    k29 = _kz_ebene(db, m, 29, heute)
    assert (k29["soll_kw"], k29["verfuegbar"]) == (1000.0, 0.0)
    assert k29["gebunden"]["summe"] == (1000.0 if zahltag == heute else 0.0)
