"""Verschiebungen, dritte Korrekturrunde: Befunde der Gegenprüfung zur Iteration 2 (Budget,
Zahlstand beim Anlegen, Teilbeträge, Rückverschiebung, Rolltag, Raten, Zahlungstermine,
Teilzahlungen vor dem Fenster, manuell abgelöste Bestellungen). Jeder Test schlug gegen den
Stand der Iteration 2 fehl; nur synthetische Daten."""

from datetime import date, datetime
from decimal import Decimal

import pytest

from app import models

from test_bestellungen import _bank, _budget_woche, _zahlung
from test_liquiditaet import _konto, _mandant
from test_verschiebung_zuordnung import _kz_ebene, _op, _plan, _verschiebe


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

def test_restbudget_bezahlter_posten_mit_vorabzahlung_gleich_in_plan_und_projektion(db):
    """Budget KW 33 10.000 netto (TAG); OP 2.000 fällig 11.08., 1.000 am 05.08. (vor dem
    Fenster) und 1.000 am 11.08. bezahlt; heute 12.08.: die angezeigte Planzelle eines
    künftigen Tages ist die Liquiditätsänderung des Tages (der Posten verbraucht in beiden
    Sichten sein volles Restbudget)."""
    m = _mandant(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 33, "10000")
    _op(db, m, "2000", date(2026, 8, 11),
        bezahlt=((date(2026, 8, 5), "1000"), (date(2026, 8, 11), "1000")))
    plan, p = _plan(db, m, date(2026, 8, 10), date(2026, 8, 12), wochen=2)
    liq = p["bestaende"]["liquiditaet"]
    for tag, vortag in (("2026-08-13", "2026-08-12"), ("2026-08-14", "2026-08-13")):
        assert plan[tag] == round(liq[tag] - liq[vortag], 2) == -1980.0


@pytest.mark.parametrize("start", [date(2026, 8, 24), date(2026, 8, 31)])
def test_verschobenes_tagesbudget_bleibt_wenn_die_ursprungswoche_ausserhalb_liegt(db, start):
    """Nur ein Tagesbudget am 25.08. (1.000 netto), 1.190 auf den 02.09. verschoben: auch im
    Fenster ab 31.08. (Ursprung davor) steht das Budget am 02.09."""
    m = _mandant(db)
    _bank(db, m)
    db.add(models.BudgetTag(mandant_id=m.id, konto_id=_konto(db, m, "4920").id,
                            datum=date(2026, 8, 25), betrag_netto=Decimal("1000")))
    _verschiebe(db, m, date(2026, 8, 25), date(2026, 9, 2), "-1190", angelegt=date(2026, 8, 20))
    plan, _ = _plan(db, m, start, date(2026, 8, 20), wochen=2)
    assert plan == {"2026-09-02": -1190.0}


def test_ganz_verschobene_restbudgetzelle_ist_leer(db):
    """Budget KW 34 10.000 netto (TAG), OP 9.520 am 17.08. → Restbudget 476 je Tag; der Wert
    des 18.08. wird auf den 25.08. verschoben: 18.08. leer, die übrigen Tage unverändert."""
    m = _mandant(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 34, "10000")
    _op(db, m, "9520", date(2026, 8, 17))
    heute = date(2026, 8, 13)
    vorher, _ = _plan(db, m, date(2026, 8, 17), heute, wochen=2)
    _verschiebe(db, m, date(2026, 8, 18), date(2026, 8, 25), str(vorher["2026-08-18"]),
                angelegt=heute)
    nachher, _ = _plan(db, m, date(2026, 8, 17), heute, wochen=2)
    erwartet = {t: v for t, v in vorher.items() if t != "2026-08-18"}
    erwartet["2026-08-25"] = -476.0
    assert nachher == erwartet


def test_monatsbudget_mit_rundungsrest_vollstaendig_verschiebbar(db):
    """Monatsbudget September 4.000 netto (Profil gleich): der Planwert des Do 04.09. trägt den
    Rundungsrest der Woche; als ganzer Betrag verschoben bleibt kein Cent zurück."""
    m = _mandant(db)
    _bank(db, m)
    db.add(models.Budget(mandant_id=m.id, konto_id=_konto(db, m, "4920").id, jahr=2026,
                         monat=9, betrag_netto=Decimal("4000")))
    heute = date(2026, 8, 10)
    vorher, _ = _plan(db, m, date(2026, 8, 31), heute, wochen=6)
    _verschiebe(db, m, date(2026, 9, 4), date(2026, 10, 1), str(vorher["2026-09-04"]),
                angelegt=heute)
    nachher, _ = _plan(db, m, date(2026, 8, 31), heute, wochen=6)
    assert "2026-09-04" not in nachher
    assert nachher["2026-10-01"] == vorher["2026-09-04"]


# ---------------------------------------------------------------------------
# Zuordnung: Zahlstand je Zahlung, Teilbeträge, Rückverschiebung, Rolltag, Freigabe danach
# ---------------------------------------------------------------------------

D = Decimal


def _sb_kz(sz, montag, konto, ebene="gesamt", basis="LIVE"):
    import szenario_bausteine as sb
    aw = sb.Auswertung(sz)
    try:
        return aw.kz(montag, basis, konto)[ebene]
    finally:
        aw.schliessen()


def _sb_plan(sz, start, konto, wochen=4) -> tuple[dict, dict]:
    import szenario_bausteine as sb
    aw = sb.Auswertung(sz)
    try:
        p = aw.plan(start, wochen)
        return {k: v for k, v in aw.zeile(p, konto)["plan"].items() if v}, p
    finally:
        aw.schliessen()


@pytest.mark.parametrize("heute", [date(2026, 12, 23), date(2026, 12, 25)])
def test_vorab_gezahlter_teil_und_restzahlung_in_allen_sichten_gleich_zugeordnet(heute):
    """Orakel P1 Seed 8836: OP 3.000 fällig 26.12., 900 am 09.12. (vor dem Fenster) und 2.100
    am 23.12. bezahlt; am 05.12. −1.700 auf den 11.01. und −1.300 auf den 18.12. verschoben.
    Plan, Soll und „Stand gestern“ ordnen die Zahlungen denselben Teilen zu (die Vorabzahlung
    dem früheren Teil): Plan 21.12. −400 / 11.01. −1.700, nirgends Spielraum."""
    import szenario_bausteine as sb
    op = sb.Posten(1, "4920", D("3000"), date(2026, 12, 26),
                   ((date(2026, 12, 9), D("900")), (date(2026, 12, 23), D("2100"))))
    vs = (sb.Verschiebung(1, "4920", date(2026, 12, 26), date(2027, 1, 11), D("-1700"),
                          erstellt=date(2026, 12, 5)),
          sb.Verschiebung(2, "4920", date(2026, 12, 26), date(2026, 12, 18), D("-1300"),
                          erstellt=date(2026, 12, 5)))
    sz = sb.Szenario(seed=0, heute=heute, konten=("4920",), posten=(op,), verschiebungen=vs)
    plan, _ = _sb_plan(sz, date(2026, 12, 21), "4920", wochen=4)
    assert plan == {"2026-12-21": -400.0, "2027-01-11": -1700.0}
    for montag in (date(2026, 12, 21), date(2027, 1, 4), date(2027, 1, 11)):
        assert _sb_kz(sz, montag, "4920")["verfuegbar"] <= 0.005


@pytest.mark.parametrize("rest", ["700", "200"])
def test_spaetere_zahlung_eines_anderen_postens_laesst_die_verschiebung_nicht_wandern(rest):
    """Orakel I15: OP1 1.000 (300 am 31.07. bezahlt) und OP2 1.000, beide fällig 10.08.; am
    03.08. −1.000 auf den 24.08. verschoben – betragsgleich war damals nur OP2. Danach wird OP1
    (ganz oder teilweise) bezahlt; heute 19.08.: OP2 bleibt verschoben, nichts überfällig."""
    import szenario_bausteine as sb
    posten = (sb.Posten(1, "4920", D("1000"), date(2026, 8, 10),
                        ((date(2026, 7, 31), D("300")), (date(2026, 8, 12), D(rest)))),
              sb.Posten(2, "4920", D("1000"), date(2026, 8, 10)))
    v = sb.Verschiebung(1, "4920", date(2026, 8, 10), date(2026, 8, 24), D("-1000"),
                        erstellt=date(2026, 8, 3))
    sz = sb.Szenario(seed=0, heute=date(2026, 8, 19), konten=("4920",), posten=posten,
                     verschiebungen=(v,))
    plan, p = _sb_plan(sz, date(2026, 8, 17), "4920")
    assert plan["2026-08-24"] == -1000.0
    # 100.000 − 300 − Zahlung 12.08. − (überfälliger Rest von OP1, 20.08.); OP2 erst am 24.08.
    assert p["bestaende"]["liquiditaet"]["2026-08-23"] == 99000.0
    k35 = _sb_kz(sz, date(2026, 8, 24), "4920")
    assert (k35["gebunden"]["summe"], k35["verfuegbar"]) == (1000.0, 0.0)


@pytest.mark.parametrize("teile", [["-1000"], ["-500", "-500"]])
def test_teilbetraege_setzen_beim_gewaehlten_bestandteil_fort(teile):
    """Orakel I9b: Q 500 (am 10.08. bezahlt) und P 1.000 (offen), fällig 10.08.; 1.000 auf den
    24.08. verschoben – als Ganzes oder in zwei Hälften: beide Male wandert P, nichts bleibt
    überfällig."""
    import szenario_bausteine as sb
    posten = (sb.Posten(1, "4920", D("500"), date(2026, 8, 10), ((date(2026, 8, 10), D("500")),)),
              sb.Posten(2, "4920", D("1000"), date(2026, 8, 10)))
    vs = tuple(sb.Verschiebung(i, "4920", date(2026, 8, 10), date(2026, 8, 24), D(b),
                               erstellt=date(2026, 8, 3)) for i, b in enumerate(teile, 1))
    sz = sb.Szenario(seed=0, heute=date(2026, 8, 19), konten=("4920",), posten=posten,
                     verschiebungen=vs)
    plan, p = _sb_plan(sz, date(2026, 8, 17), "4920")
    assert plan == {"2026-08-24": -1000.0}
    assert p["bestaende"]["liquiditaet"]["2026-08-23"] == 99500.0
    assert _sb_kz(sz, date(2026, 8, 17), "4920")["abweichung"]["ueberfaellig"] == 0.0


@pytest.mark.parametrize("teile", [["-5499.99"], ["-2035", "-3464.99"]])
def test_teilbetraege_vom_rolltag_verbrauchen_sich_nicht_am_zieltag(teile):
    """Orakel I9 Seed 1214: Rate 4.999,99 am 21.12. (Stichtag 19.), freigegebene Bestellung
    2.500 vom Do 17.12. überfällig auf den Rolltag gerollt; am So 20.12. 5.499,99 vom 21.12.
    auf den 17.12. verschoben – als Ganzes oder in zwei Teilen: gleicher Plan, gleiche
    Kennzahlen. Was schon am Zieltag liegt (die Bestellung an ihrem Plantag, ein früherer Teil),
    nimmt die Verschiebung nicht – sie bewegte es nicht und verbrauchte sich nur."""
    import szenario_bausteine as sb
    heute = date(2026, 12, 21)
    vs = tuple(sb.Verschiebung(i, "4210", heute, date(2026, 12, 17), D(b))
               for i, b in enumerate(teile, 1))
    sz = sb.Szenario(seed=0, heute=heute, konten=("4210",),
                     dauer=(sb.Dauer(1, "4210", D("4999.99"), 19),),
                     bestellungen=(sb.Bestellung(1, "4210", D("2500"), date(2026, 12, 17)),),
                     verschiebungen=vs)
    plan, _ = _sb_plan(sz, heute, "4210", wochen=6)
    assert plan == {"2026-12-21": -2500.0, "2027-01-19": -4999.99}
    k = _sb_kz(sz, heute, "4210")
    assert (k["soll_kw"], k["abweichung"]["ueberfaellig"], k["verfuegbar"]) == (2500.0, 2500.0, 0.0)


def test_rueckverschiebung_einer_offenen_bestellung_ohne_wirkung():
    """Orakel I7 Seed 7134: Wochenbudget KW 52 (3400), offene Bestellung 714 brutto am 28.12.;
    −714 vom 28.12. auf den 23.12. und zurück – Kennzahlen KW 52/53 wie ohne Verschiebungen."""
    import szenario_bausteine as sb
    heute = date(2026, 12, 22)
    teile = dict(konten=("3400",), budgets=(sb.Budget("3400", date(2026, 12, 21), D("5000")),),
                 bestellungen=(sb.Bestellung(1, "3400", D("600"), date(2026, 12, 28), status="OFFEN",
                                             angelegt=date(2026, 12, 1)),))
    vs = (sb.Verschiebung(1, "3400", date(2026, 12, 28), date(2026, 12, 23), D("-714"), erstellt=heute),
          sb.Verschiebung(2, "3400", date(2026, 12, 23), date(2026, 12, 28), D("-714"), erstellt=heute))
    ohne = sb.Szenario(seed=0, heute=heute, **teile)
    mit = sb.Szenario(seed=0, heute=heute, verschiebungen=vs, **teile)
    for montag in (date(2026, 12, 21), date(2026, 12, 28)):
        assert _sb_kz(mit, montag, "3400") == _sb_kz(ohne, montag, "3400")


@pytest.mark.parametrize("variante", ["am_rolltag_bezahlt", "vorher_bezahlt"])
def test_verschiebung_vom_rolltag_nimmt_den_ueberfaelligen_posten_in_ihrer_hoehe(db, variante):
    """OP1 1.500 fällig Mi 19.08., am Sa 22.08. überfällig auf Mo 24.08. gerollt; genau 1.500
    vom 24.08. auf den 09.09. verschoben. Am 24.08. ist auch OP2 800 fällig (am 24.08. bezahlt)
    bzw. OP3 600 (schon am 20.08. bezahlt): verschoben wird OP1 ganz, nichts bleibt überfällig."""
    m = _mandant(db)
    _bank(db, m)
    _op(db, m, "1500", date(2026, 8, 19))
    if variante == "am_rolltag_bezahlt":
        _op(db, m, "800", date(2026, 8, 24), bezahlt=((date(2026, 8, 24), "800"),))
    else:
        _op(db, m, "600", date(2026, 8, 24), bezahlt=((date(2026, 8, 20), "600"),))
    _verschiebe(db, m, date(2026, 8, 24), date(2026, 9, 9), "-1500", angelegt=date(2026, 8, 22))
    heute = date(2026, 8, 25)
    plan, _ = _plan(db, m, date(2026, 8, 24), heute, wochen=3)
    assert plan.get("2026-09-09") == -1500.0 and "2026-08-25" not in plan
    assert _kz_ebene(db, m, 35, heute)["abweichung"]["ueberfaellig"] == 0.0
    assert _kz_ebene(db, m, 37, heute)["gebunden"]["summe"] == 1500.0


def test_nach_der_verschiebung_freigegebene_bestellung_nimmt_ihr_kein_budget(db):
    """Budget KW 34 5.000 netto (TAG); Bestellung 2.380 brutto am 19.08. am 03.08. 09:00 offen
    angelegt, 12:00 1.190 vom 19.08. auf den 31.08. verschoben (Budget, die Bestellung war noch
    nicht entschieden), am 06.08. freigegeben: die Bestellung bleibt am 19.08."""
    from test_bestellungen import _benutzer, _bestellung
    from app.services import bestellungen as svc
    m = _mandant(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 34, "5000")
    nutzer = _benutzer(db)
    b = _bestellung(db, m, nutzer, netto="2000", zahldatum=date(2026, 8, 19), status="OFFEN")
    b.erstellt_am = datetime(2026, 8, 3, 9)
    v = _verschiebe(db, m, date(2026, 8, 19), date(2026, 8, 31), "-1190")
    v.erstellt_am = datetime(2026, 8, 3, 12)
    svc.entscheiden(db, b, nutzer, "FREIGEBEN", "Test", {"test": True})
    b.entschieden_am = datetime(2026, 8, 6, 10)
    heute = date(2026, 8, 6)
    plan, _ = _plan(db, m, date(2026, 8, 17), heute, wochen=3)
    assert plan["2026-08-31"] == -1190.0 and plan["2026-08-19"] == -2380.0
    k34 = _kz_ebene(db, m, 34, heute)
    assert (k34["gebunden"]["summe"], k34["verfuegbar"]) == (2380.0, 2380.0)
    assert _kz_ebene(db, m, 36, heute)["verfuegbar"] == 1190.0


def test_teilzahlung_vor_der_anlage_dann_vollzahlung_verschiebung_bleibt_beim_posten(db):
    """OP1 3.000 und OP2 2.000 fällig 26.08.; 1.000 auf OP1 am 05.08., am 06.08. 2.000 (der
    Rest von OP1) auf den 09.09. verschoben, am 20.08. OP1 vollständig bezahlt: verschoben
    bleibt OP1 – OP2 steht am 26.08. (gebunden), Liquidität am 30.08. 95.000."""
    m = _mandant(db)
    _bank(db, m)
    _op(db, m, "3000", date(2026, 8, 26),
        bezahlt=((date(2026, 8, 5), "1000"), (date(2026, 8, 20), "2000")))
    _op(db, m, "2000", date(2026, 8, 26))
    _verschiebe(db, m, date(2026, 8, 26), date(2026, 9, 9), "-2000", angelegt=date(2026, 8, 6))
    heute = date(2026, 8, 21)
    plan, p = _plan(db, m, date(2026, 8, 17), heute, wochen=4)
    assert p["bestaende"]["liquiditaet"]["2026-08-30"] == 95000.0
    assert _kz_ebene(db, m, 35, heute)["gebunden"]["summe"] == 2000.0


# ---------------------------------------------------------------------------
# Raten: vorgezogen und am Zieltag bezahlt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bezahlt", [True, False])
def test_vorgezogene_und_bezahlte_rate_wird_nicht_vorgetragen(db, bezahlt):
    """Miete 2.000 am 11.; am 31.08. die Septemberrate auf Mi 02.09. vorgezogen und dort bezahlt
    (bzw. nicht bezahlt); heute Do 03.09.: bezahlt kein Vortrag und kein Spielraum, unbezahlt
    Vortrag −2.000."""
    m = _mandant(db)
    _bank(db, m)
    from test_verschiebung_zuordnung import _miete
    _miete(db, m)
    if bezahlt:
        _zahlung(db, m, date(2026, 9, 2), "2000")
    _verschiebe(db, m, date(2026, 9, 11), date(2026, 9, 2), "-2000", angelegt=date(2026, 8, 31))
    heute = date(2026, 9, 3)
    plan, p = _plan(db, m, date(2026, 8, 31), heute, wochen=13)
    assert plan["2026-09-02"] == -2000.0 and "2026-09-11" not in plan
    assert sum(v["betrag"] for v in p["vortraege"]) == (0.0 if bezahlt else -2000.0)
    if bezahlt:
        for kw in (36, 37, 38):
            k = _kz_ebene(db, m, kw, heute)
            assert (k["abweichung"]["vortraege"], k["verfuegbar"]) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Zahlungstermine (ohne Erledigungsdatum) in der Freigabe
# ---------------------------------------------------------------------------

def _termin(db, m, typ, tag, betrag, konto=None, erledigt=True):
    from szenario_bausteine import ALTBESTAND   # Altbestand, nicht die Uhrzeit des Testlaufs
    t = models.Zahlungstermin(mandant_id=m.id, typ=typ, datum=tag, betrag=Decimal(betrag),
                              konto_id=_konto(db, m, konto).id if konto else None,
                              status="ERLEDIGT" if erledigt else "GEPLANT", generiert=False,
                              erstellt_am=ALTBESTAND)
    db.add(t)
    db.flush()
    return t


def test_vorgezogener_termin_heute_bezahlt_ohne_spielraum(db):
    """USt-VA 2.000 am Fr 14.08., am 12.07. auf Di 11.08. vorgezogen, heute (14.08.) bezahlt
    und erledigt: das Ist bis gestern zeigt die Zahlung nicht – KW 33 ohne Spielraum."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 7, 1))
    _termin(db, m, "UST_VA", date(2026, 8, 14), "2000", konto="1780")
    _zahlung(db, m, date(2026, 8, 14), "2000", konto="1780")
    _verschiebe(db, m, date(2026, 8, 14), date(2026, 8, 11), "-2000", konto="1780",
                angelegt=date(2026, 7, 12))
    db.commit()
    assert _kz_ebene(db, m, 33, date(2026, 8, 14))["verfuegbar"] == 0.0


@pytest.mark.parametrize("erledigt", [True, False])
def test_termin_im_fenster_ohne_zahlung_im_ist_ohne_spielraum(db, erledigt):
    """LSt 4.999,99 am Mi 11.11. – schon am 04.11. (vor dem Fenster) bezahlt und erledigt bzw.
    gar nicht bezahlt; heute So 15.11.: die Minderausgabe gibt keinen Spielraum frei."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 10, 1))
    _termin(db, m, "LST", date(2026, 11, 11), "4999.99", konto="1741", erledigt=erledigt)
    if erledigt:
        _zahlung(db, m, date(2026, 11, 4), "4999.99", konto="1741")
    db.commit()
    for kw in (46, 47):
        assert _kz_ebene(db, m, kw, date(2026, 11, 15))["verfuegbar"] == 0.0


def test_termin_vor_dem_fenster_heute_bezahlt_planstand_ohne_spielraum(db):
    """USt-VA 750,50 am Di 03.11., heute (Do 19.11.) bezahlt und erledigt; Basis ein Planstand
    vom 26.10.: kein Spielraum."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 10, 1))
    _termin(db, m, "UST_VA", date(2026, 11, 3), "750.50", konto="1780", erledigt=False)
    db.commit()
    snap = erstelle_snapshot_fuer(db, m, date(2026, 10, 26))
    t = db.query(models.Zahlungstermin).one()
    t.status = "ERLEDIGT"
    _zahlung(db, m, date(2026, 11, 19), "750.50", konto="1780")
    db.commit()
    assert _kz_ebene(db, m, 47, date(2026, 11, 19), basis=snap.id)["verfuegbar"] == 0.0


def erstelle_snapshot_fuer(db, m, start):
    from app.services.liquiditaet import erstelle_snapshot
    return erstelle_snapshot(db, m, start=start, wochen=13, heute=start)


@pytest.mark.parametrize("mit_konto", [True, False])
def test_gestundeter_termin_am_neuen_termin_bezahlt_steht_im_plan(db, mit_konto):
    """USt-VA 3.000 am Do 10.09., am 07.09. auf Mo 21.09. verschoben (Stundung), am 21.09.
    bezahlt (Ist 1780) und erledigt – mit Konto 1780 bzw. ohne Konto (Zeile der Gruppe
    Steuern); heute Di 22.09.: Plan 21.09. −3.000 wie das Ist, kein Spielraum."""
    m = _mandant(db)
    _bank(db, m)
    _termin(db, m, "UST_VA", date(2026, 9, 10), "3000", konto="1780" if mit_konto else None)
    _zahlung(db, m, date(2026, 9, 21), "3000", konto="1780")
    _verschiebe(db, m, date(2026, 9, 10), date(2026, 9, 21), "-3000", konto="1780",
                termin_typ=None if mit_konto else "UST_VA", angelegt=date(2026, 9, 7))
    heute = date(2026, 9, 22)
    db.commit()
    p = berechne_plan_fuer(db, m, date(2026, 9, 21), heute)
    key = str(_konto(db, m, "1780").id) if mit_konto else "TERMIN:UST_VA"
    zeile = next(k for g in p["zeilen"] for k in g["kinder"] if k["key"] == key)
    assert {t: v for t, v in zeile["plan"].items() if v} == {"2026-09-21": -3000.0}
    for kw in (39, 40):
        assert _kz_ebene(db, m, kw, heute)["verfuegbar"] == 0.0


def berechne_plan_fuer(db, m, start, heute):
    from app.services.liquiditaet import berechne_plan
    return berechne_plan(db, m, start=start, wochen=3, heute=heute)


# ---------------------------------------------------------------------------
# Manuell abgelöste Bestellung
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("basis", ["LIVE", "PLANSTAND"])
def test_manuell_abgeloeste_bestellung_gibt_ihren_rahmen_nicht_frei(db, basis):
    """Bestellung 1.200 netto (1.428 brutto) am Sa 20.06. freigegeben, am 04.06. MANUELL über
    1.428 abgelöst (keine Ist-Buchung); heute 07.06.: KW 25 ohne Spielraum."""
    from test_bestellungen import _benutzer, _bestellung
    from app.services import bestellungen as svc
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 5, 1))
    nutzer = _benutzer(db)
    b = _bestellung(db, m, nutzer, konto="3400", netto="1200", zahldatum=date(2026, 6, 20),
                    status="FREIGEGEBEN")
    snap = erstelle_snapshot_fuer(db, m, date(2026, 5, 11)) if basis == "PLANSTAND" else None
    svc.abloesen(db, b, nutzer, art="MANUELL", betrag=Decimal("1428"), datum=date(2026, 6, 4))
    db.commit()
    k = _kz_ebene(db, m, 25, date(2026, 6, 7), basis=snap.id if snap else "LIVE")
    assert k["verfuegbar"] == 0.0


# ---------------------------------------------------------------------------
# Zellendialog und Szenario-Generator
# ---------------------------------------------------------------------------

def test_zellendialog_ordnet_nur_die_verschiebungen_seiner_zeile_zu(db, monkeypatch):
    """Der Zellendialog rechnet die Zuordnung nur mit den Verschiebungen der abgefragten Zeile
    (sie hängt nur von diesen ab) – nicht für den ganzen Mandanten."""
    from app.services import bestellungen as svc
    m = _mandant(db)
    _bank(db, m)
    eigene = _verschiebe(db, m, date(2026, 8, 19), date(2026, 9, 2), "-100", angelegt=date(2026, 8, 3))
    _verschiebe(db, m, date(2026, 8, 19), date(2026, 9, 2), "-100", konto="3400",
                angelegt=date(2026, 8, 3))
    db.commit()
    gesehen = []
    echt = svc.Zuordnung

    def zuordnung(db_, m_, heute, verschiebungen=None, **kw):
        gesehen.append(verschiebungen)
        return echt(db_, m_, heute, verschiebungen=verschiebungen, **kw)

    monkeypatch.setattr(svc, "Zuordnung", zuordnung)
    svc.plan_bestellungen(db, m, konto_id=_konto(db, m, "4920").id, von=date(2026, 8, 17),
                          bis=date(2026, 8, 23), heute=date(2026, 8, 10))
    assert [None if vs is None else [v.id for v in vs] for vs in gesehen] == [[eigene.id]]


def test_generator_legt_bestellungen_hoechstens_heute_an_und_gibt_sie_gleich_frei():
    """Orakel-Generator: Anlage einer Bestellung ausdrücklich und nie nach heute, Freigabe nicht
    zur Uhrzeit des Testlaufs (sonst gälten alle als nach den Verschiebungen angelegt bzw.
    freigegeben, und der Stand zu einem früheren Tag hätte einen anderen Anlagetag)."""
    import szenario_bausteine as sb
    from sqlalchemy import select
    geprueft = 0
    for seed in range(120):
        sz = sb.erzeuge(seed)
        assert all(b.angelegt is not None and b.angelegt <= sz.heute for b in sz.bestellungen)
        if not sz.bestellungen:
            continue
        a = sb.aufbauen(sz, planstaende=False)
        try:
            for b in a.db.scalars(select(models.Bestellung)):
                assert b.erstellt_am.date() <= sz.heute
                if b.entschieden_am is not None:
                    assert b.entschieden_am.date() <= sz.heute
                    geprueft += 1
        finally:
            a.schliessen()
    assert geprueft


def test_eine_zahlung_deckt_nur_einen_termin(db):
    """Orakel P1 Seed 780 (ohne Termin-Ausschluss): zwei erledigte GewSt-Termine je 1.200 am
    Mo 13.07. – einer ohne Konto, am 09.07. bezahlt (Ist 4320), einer auf 4320, schon am 03.07.
    (vor dem Fenster) bezahlt; heute Sa 11.07.: die eine Zahlung im Ist deckt nur einen Termin,
    der andere bleibt gebunden – KW 29 ohne Spielraum."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 5, 1))
    _termin(db, m, "GEWST", date(2026, 7, 13), "1200")
    _termin(db, m, "GEWST", date(2026, 7, 13), "1200", konto="4320")
    _zahlung(db, m, date(2026, 7, 9), "1200", konto="4320")
    _zahlung(db, m, date(2026, 7, 3), "1200", konto="4320")
    db.commit()
    assert _kz_ebene(db, m, 29, date(2026, 7, 11))["verfuegbar"] == 0.0


def test_unbezahlter_termin_bleibt_ueberfaellig_trotz_mehrausgabe_anderer_gruppe(db):
    """Orakel P1 Seed 554: Planstand vom 21.09.; USt-VA ohne Konto 2.500 am Mi 07.10., schon am
    25.09. bezahlt (Ist 1780) und erledigt; SV 1.200 am 22.09. (1742) nicht bezahlt; heute
    07.10.: die USt-Zahlung deckt nicht den SV-Termin – KW 41 ohne Spielraum."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 7, 1))
    _termin(db, m, "SV", date(2026, 9, 22), "1200", konto="1742", erledigt=False)
    t = _termin(db, m, "UST_VA", date(2026, 10, 7), "2500", erledigt=False)
    db.commit()
    snap = erstelle_snapshot_fuer(db, m, date(2026, 9, 21))
    t.status = "ERLEDIGT"
    _zahlung(db, m, date(2026, 9, 25), "2500", konto="1780")
    db.commit()
    assert _kz_ebene(db, m, 41, date(2026, 10, 7), basis=snap.id)["verfuegbar"] <= 0.0


def test_vorauszahlung_eines_kuenftigen_termins_deckt_keinen_vergangenen(db):
    """Orakel P1 Seed 1481: Planstand vom 18.05.; GewSt ohne Konto 500 am 21.05. nicht bezahlt;
    USt-VA 1.200 (1780) am 18.07., schon am 05.06. bezahlt und erledigt; heute So 14.06.: die
    Vorauszahlung gehört dem USt-Termin – die GewSt bleibt überfällig, KW 29 ohne Spielraum."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 4, 5))
    _termin(db, m, "GEWST", date(2026, 5, 21), "500", erledigt=False)
    t = _termin(db, m, "UST_VA", date(2026, 7, 18), "1200", konto="1780", erledigt=False)
    db.commit()
    snap = erstelle_snapshot_fuer(db, m, date(2026, 5, 18))
    t.status = "ERLEDIGT"
    _zahlung(db, m, date(2026, 6, 5), "1200", konto="1780")
    db.commit()
    assert _kz_ebene(db, m, 29, date(2026, 6, 14), basis=snap.id)["verfuegbar"] <= 0.0


def test_termin_ohne_konto_verbraucht_zuerst_die_eigene_minderausgabe(db):
    """Orakel P1 Seed 2639: Planstand vom 08.06.; LSt ohne Konto 750,50 am 24.06. und USt-VA
    1.000 (1780, am 28.05. vom 02.07. auf So 21.06. verschoben), beide nicht bezahlt; heute
    Fr 26.06.: beide überfällig (C3 1.750,50), KW 26 ohne Spielraum."""
    m = _mandant(db)
    _bank(db, m, tag=date(2026, 4, 17))
    _termin(db, m, "LST", date(2026, 6, 24), "750.50", erledigt=False)
    _termin(db, m, "UST_VA", date(2026, 7, 2), "1000", konto="1780", erledigt=False)
    _verschiebe(db, m, date(2026, 7, 2), date(2026, 6, 21), "-1500", konto="1780",
                angelegt=date(2026, 5, 28))
    db.commit()
    heute = date(2026, 6, 26)
    snap = erstelle_snapshot_fuer(db, m, date(2026, 6, 8))
    k = _kz_ebene(db, m, 26, heute, basis=snap.id)
    assert (k["abweichung"]["ueberfaellig"], k["verfuegbar"]) == (1750.5, 0.0)
