"""Freigabe-Rechnung und Planstände: Verschiebungen aus vergangenen Tagen (gerollter
Posten zuerst, bezahlte nicht erneut in der Projektion) und Planstände, die den
angezeigten Plan samt gekennzeichnetem Anteil offener Bestellungen einfrieren."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app import models
from app.services import bestellungen as svc
from app.services.liquiditaet import (
    berechne_plan,
    erstelle_snapshot,
    snapshot_fluesse,
    snapshot_liquiditaet,
    soll_ist_vergleich,
)

from test_bestellungen import _bank, _benutzer, _bestellung, _budget_woche, _woche, _zahlung, _zeile
from test_liquiditaet import _konto, _mandant

MONTAG = date(2026, 8, 10)      # KW 33
MITTWOCH = date(2026, 8, 12)


def _e(r, ebene="konto") -> dict:
    return next(x for x in r["ebenen"] if x["ebene"] == ebene)


def _verschiebe(db, m, von, nach, betrag, konto="4920"):
    db.add(models.PlanVerschiebung(mandant_id=m.id, konto_id=_konto(db, m, konto).id,
                                   quelle="PLAN", von=von, nach=nach, betrag=Decimal(betrag)))


def _op(db, m, betrag, faellig, konto="4920", **extra) -> models.OffenerPosten:
    p = models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="Lieferant",
                             faellig_am=faellig, betrag_brutto=Decimal(betrag),
                             konto_id=_konto(db, m, konto).id, **extra)
    db.add(p)
    db.flush()
    return p


# ---------------------------------------------------------------------------
# N1: überfälliger Posten aus einem vergangenen Tag mit Budget verschoben
# ---------------------------------------------------------------------------

def _verschobener_ueberfaelliger_posten(db):
    """Budget nur in KW 32 (Ursprungstag), OP 1.500 fällig 04.08., verschoben auf 18.08."""
    m = _mandant(db)
    _budget_woche(db, m, "4920", 32, "10000")
    _op(db, m, "1500", date(2026, 8, 4))
    _verschiebe(db, m, date(2026, 8, 4), date(2026, 8, 18), "-1500")
    db.commit()
    return m


@pytest.mark.parametrize("heute", [MONTAG, MITTWOCH])
@pytest.mark.parametrize("basis", ["LIVE", None])       # None: AUTO ohne Planstand → LIVE
def test_verschobener_ueberfaelliger_posten_gibt_keinen_rahmen_frei(db, heute, basis):
    m = _verschobener_ueberfaelliger_posten(db)
    tel = _konto(db, m, "4920")

    def kw(n):
        r = svc.freigabe_kennzahlen(db, m, konto_id=tel.id, betrag_brutto=Decimal("1500"),
                                    jahr=2026, kw=n, basis=basis, heute=heute)
        assert r["basis"]["typ"] == "LIVE"
        return r

    for n in (34, 35, 38):
        r = kw(n)
        assert (_e(r)["verfuegbar"], _e(r)["nach_freigabe"], r["ampel"]) == (0.0, -1500.0, "rot")
    k34 = _e(kw(34))
    assert (k34["soll_kw"], k34["gebunden"]["summe"], k34["abweichung"]["summe"]) == (
        1500.0, 1500.0, 0.0)
    k33 = _e(kw(33))
    assert (k33["soll_kw"], k33["abweichung"]["summe"], k33["verfuegbar"]) == (0.0, 0.0, 0.0)


def test_planstand_und_plan_fuehren_den_verschobenen_posten_einmal(db):
    m = _verschobener_ueberfaelliger_posten(db)
    tel = _konto(db, m, "4920")
    snap = erstelle_snapshot(db, m, start=MONTAG, heute=MONTAG)
    werte = {d: v for d, v in snapshot_fluesse(snap)[tel.id].items() if v}
    assert werte == {date(2026, 8, 18): Decimal("-1500.00")}
    # angezeigter Plan am Montag: der Posten steht nur am Zieltag, nicht zusätzlich heute
    z = _zeile(berechne_plan(db, m, start=MONTAG, wochen=2, heute=MONTAG))
    assert z["plan"] == {"2026-08-18": -1500.0}
    for n in (34, 35, 38):
        r = svc.freigabe_kennzahlen(db, m, konto_id=tel.id, betrag_brutto=Decimal("1500"),
                                    jahr=2026, kw=n, basis=snap.id, heute=MITTWOCH)
        assert (_e(r)["abweichung"]["summe"], _e(r)["verfuegbar"], r["ampel"]) == (0.0, 0.0, "rot")


# ---------------------------------------------------------------------------
# N2: Planstand = angezeigter Plan, offene Bestellungen gekennzeichnet
# ---------------------------------------------------------------------------

def test_planstand_entspricht_angezeigtem_plan_mit_offener_bestellung(db):
    m = _mandant(db)
    u = _benutzer(db)
    _bank(db, m)                                                     # 100.000
    _budget_woche(db, m, "4920", 34, "10000")                        # 11.900
    b = _bestellung(db, m, u, netto="50000", zahldatum=date(2026, 8, 19))   # 59.500, offen
    db.commit()
    start, kw34 = date(2026, 8, 3), date(2026, 8, 17)
    plan = berechne_plan(db, m, start=start, wochen=4, heute=start)
    snap = erstelle_snapshot(db, m, start=start, wochen=13, heute=start)
    snap.referenz = True
    db.commit()

    # eingefroren ist der angezeigte Plan – auch für Liquidität (KPI) und Basis-Spalte
    def woche(werte, von=kw34):
        return sum((v for d, v in werte.items() if von <= d <= von + timedelta(days=6)), Decimal(0))

    assert woche(snapshot_fluesse(snap)[b.konto_id]) == Decimal(str(_woche(_zeile(plan), kw34)))
    assert woche(snapshot_fluesse(snap)[b.konto_id]) == Decimal("-59500.00")
    konten = {k.id: k for k in db.query(models.Konto).filter_by(mandant_id=m.id)}
    liq = snapshot_liquiditaet(db, m, snap, konten, lambda key: None, heute=start)
    assert float(liq[date(2026, 8, 23)]) == plan["bestaende"]["liquiditaet"]["2026-08-23"] == 40500.0
    spaeter = berechne_plan(db, m, start=start, wochen=4, heute=date(2026, 8, 5))
    assert _woche(_zeile(spaeter), kw34, "basis") == -59500.0
    # die Bestellfreigabe rechnet ohne den gekennzeichneten Anteil (keine Selbstfreigabe)
    r = svc.bestellung_kennzahlen(db, m, b, jahr=2026, kw=34, heute=date(2026, 8, 5))
    assert r["basis"]["snapshot"]["id"] == snap.id
    assert (_e(r)["soll_kw"], _e(r)["verfuegbar"], r["ampel"]) == (11900.0, 11900.0, "rot")
    # freigegeben und wie geplant gezahlt: Soll/Ist ohne Mehrausgabe
    svc.entscheiden(db, b, u, "FREIGEBEN", "Test", {})
    svc.abloesen(db, b, u, art="ZAHLUNG",
                 buchung_id=_zahlung(db, m, date(2026, 8, 19), "59500").id)
    db.commit()
    vergleich = soll_ist_vergleich(db, m, heute=date(2026, 8, 24))
    zeile = next(z for z in vergleich["zeilen"] if z["key"] == str(b.konto_id))
    assert zeile["plan"]["2026-34"] == zeile["ist"]["2026-34"] == -59500.0


def test_planstand_ohne_offene_bestellungen_unveraendert(db):
    """Ohne noch nicht entschiedene Bestellungen (oder mit Schalter aus) gibt es keinen
    gekennzeichneten Anteil."""
    m = _mandant(db)
    u = _benutzer(db)
    _budget_woche(db, m, "4920", 34, "10000")
    _bestellung(db, m, u, status="FREIGEGEBEN", zahldatum=date(2026, 8, 19))
    db.commit()
    snap = erstelle_snapshot(db, m, start=date(2026, 8, 3), heute=date(2026, 8, 3))
    assert snap.werte and not any(w.bestellung_offen for w in snap.werte)
    m.bestellungen_offen_einplanen = False
    _bestellung(db, m, u, netto="1000", zahldatum=date(2026, 8, 20), kreditor="Andere")
    db.commit()
    snap2 = erstelle_snapshot(db, m, start=date(2026, 8, 3), heute=date(2026, 8, 3))
    assert not any(w.bestellung_offen for w in snap2.werte)
    assert snapshot_fluesse(snap2) == snapshot_fluesse(snap2, ohne_offene_bestellungen=True)


def test_migration_kennzeichen_offene_bestellungen(leere_datenbank):
    """Upgrade: bestehende Werte erhalten false; Downgrade entfernt die gekennzeichneten
    Zeilen (die alte Freigabe behandelte sie sonst als Soll) und die Spalte."""
    from alembic import command
    from test_migrationen import _cfg

    engine = leere_datenbank
    cfg = _cfg()
    with engine.begin() as verbindung:
        cfg.attributes["connection"] = verbindung
        command.upgrade(cfg, "799a2322545e")
        sitzung = Session(bind=verbindung)
        m = models.Mandant(name="T", kurzname="t", kontenrahmen="SKR03", bundesland="NW")
        sitzung.add(m)
        sitzung.flush()
        snap = models.PlanSnapshot(mandant_id=m.id, stichtag=date(2026, 8, 3), wochen=13)
        sitzung.add(snap)
        sitzung.flush()
        sitzung.close()
        verbindung.execute(text(
            "INSERT INTO plan_snapshot_werte (id, snapshot_id, termin_typ, datum, betrag) "
            f"VALUES (1, {snap.id}, 'SV', '2026-08-05', -100)"))
        command.upgrade(cfg, "head")
        assert verbindung.execute(text(
            "SELECT bestellung_offen FROM plan_snapshot_werte")).scalar_one() in (False, 0)
        verbindung.execute(text(
            "INSERT INTO plan_snapshot_werte (id, snapshot_id, termin_typ, datum, betrag, "
            f"bestellung_offen) VALUES (2, {snap.id}, 'BESTELLUNG', '2026-08-06', -200, true)"))
        command.downgrade(cfg, "799a2322545e")
        assert verbindung.execute(text("SELECT id FROM plan_snapshot_werte")).scalars().all() == [1]
    spalten = {s["name"] for s in inspect(engine).get_columns("plan_snapshot_werte")}
    assert "bestellung_offen" not in spalten
    with engine.begin() as verbindung:
        cfg.attributes["connection"] = verbindung
        command.upgrade(cfg, "head")


# ---------------------------------------------------------------------------
# P1: bezahlter Posten aus einem vergangenen Tag verschoben
# ---------------------------------------------------------------------------

def _bezahlter_posten(db, m, faellig, bezahlt_am, betrag="3000", konto="4920"):
    p = _op(db, m, betrag, faellig, konto=konto, status="BEZAHLT", bezahlt_am=bezahlt_am,
            bezahlt_betrag=Decimal(betrag))
    _zahlung(db, m, bezahlt_am, betrag, konto=konto).posten_id = p.id
    return p


@pytest.mark.parametrize("mit_budget", [False, True])
def test_verschobener_bezahlter_posten_zaehlt_nicht_erneut(db, mit_budget):
    m = _mandant(db)
    _bank(db, m)                                                      # 100.000
    if mit_budget:                                                    # Restbudget am Ursprungstag
        _budget_woche(db, m, "4920", 33, "10000")
    _bezahlter_posten(db, m, date(2026, 8, 12), date(2026, 8, 10))   # vorab gezahlt
    db.commit()
    heute = date(2026, 8, 14)
    ohne = berechne_plan(db, m, start=date(2026, 8, 3), wochen=4, heute=heute)
    _verschiebe(db, m, date(2026, 8, 12), date(2026, 8, 19), "-3000")
    db.commit()
    mit = berechne_plan(db, m, start=date(2026, 8, 3), wochen=4, heute=heute)
    assert mit["bestaende"]["liquiditaet"]["2026-08-23"] == 97000.0
    assert mit["bestaende"]["liquiditaet"] == ohne["bestaende"]["liquiditaet"]
    assert _zeile(mit)["plan"]["2026-08-19"] == -3000.0              # Plan zeigt die Verschiebung
    # Liquiditäts-Gegenprobe der Freigabe
    r = svc.freigabe_kennzahlen(db, m, konto_id=_konto(db, m, "4920").id,
                                betrag_brutto=Decimal("100"), jahr=2026, kw=34, heute=heute)
    assert r["liquiditaet"]["ende_kw"] == 97000.0


def test_verschobener_bezahlter_posten_nimmt_heute_faelligen_nicht_mit(db):
    """Der heute fällige Posten liegt in der Projektion auf dem nächsten Banktag; die
    Verschiebung des bezahlten Postens darf ihn dort nicht herausnehmen."""
    m = _mandant(db)
    _bank(db, m)
    heute = date(2026, 8, 14)                                         # Freitag
    _bezahlter_posten(db, m, date(2026, 8, 12), date(2026, 8, 10))
    _op(db, m, "2000", heute)                                         # offen, heute fällig
    _verschiebe(db, m, date(2026, 8, 12), date(2026, 8, 19), "-3000")
    db.commit()
    liq = berechne_plan(db, m, start=date(2026, 8, 3), wochen=4, heute=heute)["bestaende"]["liquiditaet"]
    assert round(liq["2026-08-17"] - liq["2026-08-16"], 2) == -2000.0
    assert liq["2026-08-23"] == 95000.0


def test_verschiebung_eines_vorab_bezahlten_postens_bewegt_kein_budget(db):
    """Künftiger Ursprungstag: der vorab bezahlte Posten steht nicht in der Projektion,
    die Verschiebung bewegt dort auch kein Restbudget."""
    m = _mandant(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 34, "10000")                         # 11.900, 2.380 je Banktag
    _bezahlter_posten(db, m, date(2026, 8, 20), date(2026, 8, 10))
    db.commit()
    heute = date(2026, 8, 11)
    ohne = berechne_plan(db, m, start=date(2026, 8, 10), wochen=3, heute=heute)
    _verschiebe(db, m, date(2026, 8, 20), date(2026, 8, 27), "-3000")
    db.commit()
    mit = berechne_plan(db, m, start=date(2026, 8, 10), wochen=3, heute=heute)
    assert mit["bestaende"]["liquiditaet"] == ohne["bestaende"]["liquiditaet"]


def test_verschiebung_vergangener_dauerrate_bleibt_vor_bezahltem_posten(db):
    """Am Ursprungstag eine offene Dauerrate und ein bezahlter Posten: die Verschiebung
    in Höhe der Rate trägt die Rate auf den Zieltag (wie im Vortrag)."""
    m = _mandant(db)
    _bank(db, m)
    db.add(models.Dauerbuchung(mandant_id=m.id, name="Miete", art="KREDITOR",
                               konto_id=_konto(db, m, "4210").id, betrag_brutto=Decimal("1500"),
                               intervall="MONATLICH", stichtag=12, gueltig_von=date(2026, 1, 1)))
    _bezahlter_posten(db, m, date(2026, 8, 12), date(2026, 8, 10), konto="4210")
    _verschiebe(db, m, date(2026, 8, 12), date(2026, 8, 19), "-1500", konto="4210")
    db.commit()
    plan = berechne_plan(db, m, start=date(2026, 8, 3), wochen=4, heute=date(2026, 8, 14))
    assert plan["vortraege"] == []
    liq = plan["bestaende"]["liquiditaet"]
    assert round(liq["2026-08-19"] - liq["2026-08-18"], 2) == -1500.0
