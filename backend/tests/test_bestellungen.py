"""Bestellungen: Planintegration (Restbudget, Rollen, Ablösung), Status-Workflow,
Journal, Löschhaken, Ablösekandidaten und -vorschläge."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import models
from app.services import bestellungen as svc
from app.services.liquiditaet import (
    berechne_plan,
    erstelle_snapshot,
    plan_fluesse,
    snapshot_fluesse,
)
from app.services.zugaenge import DateiRef

from test_liquiditaet import _konto, _mandant

HEUTE = date(2026, 8, 5)   # Mittwoch, KW 32
START = date(2026, 8, 3)   # Montag


def _benutzer(db, name="Sabine Sachwalter", email="sw@example.org") -> models.Benutzer:
    b = models.Benutzer(email=email, name=name, passwort_hash="x", rolle="ADMIN",
                        darf_freigeben=True)
    db.add(b)
    db.flush()
    return b


def _bestellung(db, m, benutzer=None, *, konto="4920", netto="5000", zahldatum=date(2026, 8, 12),
                status="OFFEN", kreditor="Telko GmbH", bestellnummer="B-4711",
                bestelldatum=date(2026, 7, 27), **extra) -> models.Bestellung:
    daten = {"kreditor": kreditor, "bestellnummer": bestellnummer, "bestelldatum": bestelldatum,
             "zahldatum": zahldatum, "betrag_netto": netto, "konto_id": _konto(db, m, konto).id,
             **extra}
    b = svc.anlegen(db, m, benutzer, daten)
    if status in ("FREIGEGEBEN", "ABGELEHNT"):
        svc.entscheiden(db, b, benutzer, "FREIGEBEN" if status == "FREIGEGEBEN" else "ABLEHNEN",
                        "Test", {"test": True})
    elif status == "STORNIERT":
        svc.stornieren(db, b, benutzer, "Test")
    db.flush()
    return b


def _budget_woche(db, m, konto, kw, netto, aufteilung="TAG"):
    db.add(models.BudgetWoche(mandant_id=m.id, konto_id=_konto(db, m, konto).id, jahr=2026,
                              kw=kw, betrag_netto=Decimal(netto), aufteilung=aufteilung))


def _bank(db, m, wert="100000", tag=date(2026, 8, 2)):
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=_konto(db, m, "1200").id,
                          datum=tag, wert=Decimal(wert)))


def _zahlung(db, m, tag, betrag, konto="4920", text=None) -> models.Buchung:
    """Auszahlung: Sachkonto an Bank."""
    bu = models.Buchung(mandant_id=m.id, datum=tag, konto_nr=konto, gegenkonto_nr="1200",
                        betrag=Decimal(betrag), sh="S", text=text)
    db.add(bu)
    db.flush()
    return bu


def _plan(db, m, start=START, wochen=3, heute=HEUTE):
    db.commit()
    return berechne_plan(db, m, start=start, wochen=wochen, heute=heute)


def _zeile(plan, nummer="4920"):
    return next(k for g in plan["zeilen"] for k in g["kinder"] if k["nummer"] == nummer)


def _woche(zeile, w_von: date, feld="plan") -> float:
    tage = {(w_von + timedelta(days=i)).isoformat() for i in range(7)}
    return round(sum(v for t, v in zeile[feld].items() if t in tage), 2)


KW33 = date(2026, 8, 10)


# ---------------------------------------------------------------------------
# Plan-Engine
# ---------------------------------------------------------------------------

def test_freigegebene_bestellung_verbraucht_restbudget(db):
    m = _mandant(db)
    _budget_woche(db, m, "4920", 33, "10000")      # 11.900 brutto, 2.380 je Banktag
    _bestellung(db, m, status="FREIGEGEBEN", netto="5000")   # 5.950 brutto am 12.08.
    zeile = _zeile(_plan(db, m))
    assert _woche(zeile, KW33) == -11900.0          # max(Budget, explizit) – keine Doppelzählung
    assert zeile["plan"]["2026-08-12"] == -7140.0   # 5.950 Bestellung + 1.190 Restbudget
    assert zeile["plan"]["2026-08-10"] == -1190.0


def test_bestellung_ueber_budget_rest_null(db):
    m = _mandant(db)
    _budget_woche(db, m, "4920", 33, "10000")
    _bestellung(db, m, status="FREIGEGEBEN", netto="12000")  # 14.280 brutto > 11.900
    zeile = _zeile(_plan(db, m))
    assert _woche(zeile, KW33) == -14280.0
    assert "2026-08-10" not in zeile["plan"]


def test_brutto_ueber_ust_satz_und_ohne_ust(db):
    m = _mandant(db)
    _bestellung(db, m, status="FREIGEGEBEN", netto="1000", ust_satz="7")
    _bestellung(db, m, status="FREIGEGEBEN", netto="500", ust_satz=None,
                zahldatum=date(2026, 8, 13))
    zeile = _zeile(_plan(db, m))
    assert zeile["plan"]["2026-08-12"] == -1070.0
    assert zeile["plan"]["2026-08-13"] == -500.0


def test_ueberfaellige_bestellung_rollt(db):
    m = _mandant(db)
    _bank(db, m)
    b = _bestellung(db, m, status="FREIGEGEBEN", zahldatum=date(2026, 8, 3))
    plan = _plan(db, m)
    zeile = _zeile(plan)
    assert "2026-08-03" not in zeile["plan"]
    assert zeile["plan"][HEUTE.isoformat()] == -5950.0
    # Projektion: strikt hinter heute
    assert plan["bestaende"]["liquiditaet"]["2026-08-05"] == 100000.0
    assert plan["bestaende"]["liquiditaet"]["2026-08-06"] == 94050.0
    gerollt: dict = {}
    plan_fluesse(db, m, START, START + timedelta(days=6), heute=HEUTE, gerollt=gerollt)
    assert gerollt == {b.konto_id: Decimal("-5950.00")}


def test_abloesung_durch_zahlung_minderlieferung_ohne_wiederaufblaehen(db):
    m = _mandant(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 32, "10000")      # 2.380 je Banktag in KW 32
    u = _benutzer(db)
    b = _bestellung(db, m, u, status="FREIGEGEBEN", zahldatum=date(2026, 8, 7))
    bu = _zahlung(db, m, date(2026, 8, 4), "5000", text="Telko GmbH B-4711")
    a = svc.abloesen(db, b, u, art="ZAHLUNG", buchung_id=bu.id)
    assert (a.betrag, a.datum, a.abschliessend) == (Decimal("5000.00"), date(2026, 8, 4), True)
    assert b.status == "ABGELOEST" and b.abgeloest_durch == "ZAHLUNG"
    assert b.abgeloest_am == date(2026, 8, 4)
    d = svc.als_dict(db, b)
    assert (d["abgeloest_betrag"], d["abweichung"], d["rest_geplant"]) == (5000.0, -950.0, 0.0)
    eintrag = db.scalars(select(models.BestellJournal).where(
        models.BestellJournal.aktion == "ABGELOEST")).one()
    assert "5.000,00 €" in eintrag.begruendung and "-950,00 €" in eintrag.begruendung

    plan = _plan(db, m)
    zeile = _zeile(plan)
    # Plan: voller Bestellbetrag am Zahldatum (wie ein bezahlter Posten); Ist: 5.000
    assert zeile["plan"]["2026-08-04"] == -1190.0
    assert zeile["ist"]["2026-08-04"] == -5000.0
    assert zeile["plan"]["2026-08-07"] == -5950.0 - 1190.0
    # Projektion: nur das Restbudget (5.950 / 5 Tage) der Folgetage, nicht das volle Budget
    liq = plan["bestaende"]["liquiditaet"]
    assert liq["2026-08-04"] == 95000.0
    assert liq["2026-08-07"] == 95000.0 - 2 * 1190.0


def test_fruehe_abloesung_verbraucht_budget_der_zahlwoche(db):
    """Vor der Zahlwoche gezahlt: die Bestellung verbraucht weiter das Budget ihrer
    Zahlwoche (wie ein bezahlter Posten) – auch wenn die Zahlung vor dem Fenster liegt."""
    m = _mandant(db)
    u = _benutzer(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 34, "10000")      # KW 34: 11.900 brutto
    b = _bestellung(db, m, u, status="FREIGEGEBEN", zahldatum=date(2026, 8, 19))

    def liq(start=START, heute=HEUTE):
        return _plan(db, m, start=start, wochen=4, heute=heute)["bestaende"]["liquiditaet"]["2026-08-23"]

    assert liq() == 88100.0
    svc.abloesen(db, b, u, art="ZAHLUNG", buchung_id=_zahlung(db, m, date(2026, 8, 4), "5950").id)
    assert b.status == "ABGELOEST"
    assert liq() == 88100.0                                   # nicht 82.150 (doppelt)
    kw33 = date(2026, 8, 10)
    assert liq(start=kw33, heute=kw33) == 88100.0             # Zahlung vor dem Fenster
    zeile = _zeile(_plan(db, m, start=kw33, wochen=2, heute=kw33))
    assert zeile["plan"]["2026-08-19"] == -5950.0 - 1190.0    # Bestellung + Restbudget
    eintrag = svc.plan_bestellungen(db, m, konto_id=b.konto_id, von=date(2026, 8, 17),
                                    bis=date(2026, 8, 23), heute=HEUTE)
    assert [(x["id"], x["plan_tag"], x["plan_betrag"]) for x in eintrag] == [
        (b.id, "2026-08-19", 5950.0)]
    # Zahldatum und Ablösung vor dem Fenster: nicht mehr im Plan
    assert date(2026, 8, 4) not in plan_fluesse(db, m, kw33, kw33 + timedelta(days=13),
                                                heute=kw33).get(b.konto_id, {})


def test_abloesung_durch_rechnung_nie_geplant(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _bestellung(db, m, u, status="FREIGEGEBEN", zahldatum=date(2026, 8, 7))
    p = models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="Telko GmbH",
                             belegnr="R-1", faellig_am=date(2026, 8, 12),
                             betrag_brutto=Decimal("6000"), konto_id=b.konto_id)
    db.add(p)
    db.flush()
    a = svc.abloesen(db, b, u, art="RECHNUNG", posten_id=p.id, abschliessend=False)
    assert a.abschliessend is True and a.betrag == Decimal("6000.00")
    assert b.status == "ABGELOEST" and b.abgeloest_durch == "RECHNUNG"
    zeile = _zeile(_plan(db, m))
    assert "2026-08-07" not in zeile["plan"]
    assert zeile["plan"]["2026-08-12"] == -6000.0   # nur der Posten
    # Fehlerfälle
    b2 = _bestellung(db, m, u, status="FREIGEGEBEN")
    with pytest.raises(ValueError):
        svc.abloesen(db, b2, u, art="RECHNUNG")
    deb = models.OffenerPosten(mandant_id=m.id, art="DEBITOR", partner="Kunde",
                               faellig_am=HEUTE, betrag_brutto=Decimal("10"))
    db.add(deb)
    db.flush()
    with pytest.raises(ValueError, match="Kreditoren"):
        svc.abloesen(db, b2, u, art="RECHNUNG", posten_id=deb.id)


def test_manuelle_abloesung(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _bestellung(db, m, u, status="FREIGEGEBEN", zahldatum=date(2026, 8, 12))
    with pytest.raises(ValueError, match="Betrag und Datum"):
        svc.abloesen(db, b, u, art="MANUELL", betrag=Decimal("100"))
    with pytest.raises(ValueError, match="keinen Beleg"):
        svc.abloesen(db, b, u, art="MANUELL", betrag=Decimal("100"), datum=HEUTE, buchung_id=1)
    svc.abloesen(db, b, u, art="MANUELL", betrag=Decimal("4000"), datum=date(2026, 8, 4),
                 notiz="bar bezahlt")
    assert b.status == "ABGELOEST" and b.abgeloest_durch == "MANUELL"
    zeile = _zeile(_plan(db, m))
    assert zeile["plan"]["2026-08-12"] == -5950.0         # am Zahldatum, nicht am Ablösetag
    assert "2026-08-04" not in zeile["plan"]


def test_teilzahlung_mindert_geplanten_rest(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _bestellung(db, m, u, status="FREIGEGEBEN", zahldatum=date(2026, 8, 12))
    bu1 = _zahlung(db, m, date(2026, 8, 4), "2000")
    svc.abloesen(db, b, u, art="ZAHLUNG", buchung_id=bu1.id, abschliessend=False)
    assert b.status == "FREIGEGEBEN"
    assert svc.als_dict(db, b)["rest_geplant"] == 3950.0
    letzter = db.scalars(select(models.BestellJournal).order_by(models.BestellJournal.id.desc())).first()
    assert letzter.aktion == "TEILZAHLUNG" and "3.950,00 €" in letzter.begruendung
    zeile = _zeile(_plan(db, m))
    assert zeile["plan"]["2026-08-12"] == -3950.0
    # zweite Teilzahlung deckt den Rest → abgelöst; gespeichert bleibt die Wahl des
    # Nutzers (nicht abschließend), damit ein späteres Aufheben neu bewerten kann
    bu2 = _zahlung(db, m, date(2026, 8, 5), "3950")
    a2 = svc.abloesen(db, b, u, art="ZAHLUNG", buchung_id=bu2.id, abschliessend=False)
    assert a2.abschliessend is False and b.status == "ABGELOEST"
    assert svc.als_dict(db, b)["abweichung"] == 0.0


def test_projektionsfix_bezahlter_posten_blaeht_restbudget_nicht_auf(db):
    """§5.4: Ein in der Woche bezahlter Posten verbraucht das Wochenbudget auch in der
    Projektion (früher erschien das volle Budget erneut → 8.000 doppelt)."""
    m = _mandant(db)
    tel = _konto(db, m, "4920")
    tel.verteilung = "WTAG_FR"
    _bank(db, m)
    db.add(models.BudgetWoche(mandant_id=m.id, konto_id=tel.id, jahr=2026, kw=32,
                              betrag_netto=Decimal("10000"), aufteilung="KW"))
    op = models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="X",
                              faellig_am=date(2026, 8, 4), betrag_brutto=Decimal("8000"),
                              konto_id=tel.id)
    db.add(op)
    offen = _plan(db, m, wochen=1)["bestaende"]["liquiditaet"]["2026-08-07"]
    _zahlung(db, m, date(2026, 8, 4), "8000")
    op.status, op.bezahlt_am, op.bezahlt_betrag = "BEZAHLT", date(2026, 8, 4), Decimal("8000")
    bezahlt = _plan(db, m, wochen=1)["bestaende"]["liquiditaet"]["2026-08-07"]
    assert offen == bezahlt == 88100.0


def test_projektionsfix_vergangene_dauerrate_verbraucht_budget(db):
    m = _mandant(db)
    _bank(db, m)
    _budget_woche(db, m, "4210", 32, "5000")        # Miete ohne USt, 1.000 je Banktag
    db.add(models.Dauerbuchung(mandant_id=m.id, name="Miete", art="KREDITOR",
                               konto_id=_konto(db, m, "4210").id, betrag_brutto=Decimal("3000"),
                               intervall="MONATLICH", stichtag=3, gueltig_von=date(2026, 1, 1)))
    _zahlung(db, m, START, "3000", konto="4210")
    liq = _plan(db, m, wochen=1)["bestaende"]["liquiditaet"]
    # Woche: max(5.000, 3.000) → Restbudget 2.000 (400 je Tag); Do+Fr noch offen
    assert liq["2026-08-07"] == 100000.0 - 3000.0 - 800.0


def test_projektion_gerollter_offener_posten_unveraendert_am_freitag(db):
    """Offene, überfällige Posten verbrauchen das Budget der Woche, in die die
    Projektion sie rollt (heute = Freitag → Montag) – wie vor dem §5.4-Fix."""
    m = _mandant(db)
    _bank(db, m)
    _budget_woche(db, m, "4920", 33, "10000")
    db.add(models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="X",
                                faellig_am=START, betrag_brutto=Decimal("3000"),
                                konto_id=_konto(db, m, "4920").id))
    freitag = _plan(db, m, wochen=2, heute=date(2026, 8, 7))
    montag = _plan(db, m, wochen=2, heute=date(2026, 8, 10))
    assert freitag["bestaende"]["liquiditaet"]["2026-08-16"] == 88100.0     # max(11.900, 3.000)
    assert montag["bestaende"]["liquiditaet"]["2026-08-16"] == 89880.0      # Montag ist Ist-Tag


def test_snapshot_friert_bestellung_ein(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _bestellung(db, m, u, status="FREIGEGEBEN")
    db.commit()
    snap = erstelle_snapshot(db, m, start=START, heute=START)
    assert snapshot_fluesse(snap)[b.konto_id][date(2026, 8, 12)] == Decimal("-5950.00")
    svc.stornieren(db, b, u, "entfällt")
    db.commit()
    live = plan_fluesse(db, m, START, START + timedelta(days=13), heute=HEUTE)
    assert date(2026, 8, 12) not in live.get(b.konto_id, {})
    db.refresh(snap)
    assert snapshot_fluesse(snap)[b.konto_id][date(2026, 8, 12)] == Decimal("-5950.00")


def test_planstand_friert_nur_entschiedene_bestellungen_ein(db):
    """Noch nicht entschiedene Bestellungen gehören nicht in den Planstand – auch
    wenn der Mandant sie im aktuellen Plan mitführt."""
    m = _mandant(db)
    u = _benutzer(db)
    assert m.bestellungen_offen_einplanen is not False
    offen = _bestellung(db, m, u, zahldatum=date(2026, 8, 12))
    _bestellung(db, m, u, status="FREIGEGEBEN", netto="1000", zahldatum=date(2026, 8, 13))
    ueberfaellig = _bestellung(db, m, u, netto="2000", zahldatum=date(2026, 7, 31))
    db.commit()
    live = plan_fluesse(db, m, START, START + timedelta(days=13), heute=START)
    assert live[offen.konto_id][date(2026, 8, 12)] == Decimal("-5950.00")
    assert live[offen.konto_id][START] == Decimal("-2380.00")          # gerollt
    werte = snapshot_fluesse(erstelle_snapshot(db, m, start=START, heute=START))[offen.konto_id]
    assert {d: v for d, v in werte.items() if v} == {date(2026, 8, 13): Decimal("-1190.00")}


def test_mandantenschalter_und_bestellfilter(db):
    m = _mandant(db)
    b = _bestellung(db, m)                       # OFFEN
    fenster = (START, START + timedelta(days=13))
    tag = date(2026, 8, 12)
    assert plan_fluesse(db, m, *fenster, heute=HEUTE)[b.konto_id][tag] == Decimal("-5950.00")
    assert tag not in plan_fluesse(db, m, *fenster, heute=HEUTE,
                                   bestellungen="FREIGEGEBEN").get(b.konto_id, {})
    m.bestellungen_offen_einplanen = False
    assert tag not in plan_fluesse(db, m, *fenster, heute=HEUTE).get(b.konto_id, {})
    svc.entscheiden(db, b, None, "FREIGEBEN", None, {})
    assert plan_fluesse(db, m, *fenster, heute=HEUTE)[b.konto_id][tag] == Decimal("-5950.00")
    assert tag not in plan_fluesse(db, m, *fenster, heute=HEUTE,
                                   bestellungen="KEINE").get(b.konto_id, {})
    assert tag not in plan_fluesse(db, m, *fenster, heute=HEUTE,
                                   ohne_bestellung_id=b.id).get(b.konto_id, {})
    with pytest.raises(ValueError):
        plan_fluesse(db, m, *fenster, heute=HEUTE, bestellungen="ALLES")


def test_abgelehnte_und_stornierte_nie_im_plan(db):
    m = _mandant(db)
    u = _benutzer(db)
    _bestellung(db, m, u, status="ABGELEHNT")
    _bestellung(db, m, u, status="STORNIERT", zahldatum=date(2026, 8, 13))
    fluesse = plan_fluesse(db, m, START, START + timedelta(days=13), heute=HEUTE)
    assert not any(v for v in fluesse.get(_konto(db, m, "4920").id, {}).values())


def test_bestellung_ohne_konto_eigene_zeile(db):
    m = _mandant(db)
    db.add(models.Bestellung(mandant_id=m.id, kreditor="Ohne", bestelldatum=START,
                             zahldatum=date(2026, 8, 12), betrag_netto=Decimal("100"),
                             ust_satz=None, konto_id=None, status="FREIGEGEBEN"))
    plan = _plan(db, m)
    gruppe = next(g for g in plan["zeilen"] if g["code"] == "A_SONST")
    zeile = next(k for k in gruppe["kinder"] if k["key"] == "TERMIN:BESTELLUNG")
    assert zeile["name"] == "Bestellungen (ohne Konto)"
    assert zeile["plan"]["2026-08-12"] == -100.0
    assert svc.plan_bestellungen(db, m, konto_id=None, von=KW33, bis=KW33 + timedelta(days=6),
                                 heute=HEUTE)[0]["kreditor"] == "Ohne"


def test_plan_bestellungen_fuer_zellendialog(db):
    m = _mandant(db)
    u = _benutzer(db)
    tel = _konto(db, m, "4920")
    ueberfaellig = _bestellung(db, m, u, status="FREIGEGEBEN", zahldatum=date(2026, 8, 3))
    kuenftig = _bestellung(db, m, u, zahldatum=date(2026, 8, 12))
    _bestellung(db, m, u, konto="4930", zahldatum=date(2026, 8, 12))
    _bestellung(db, m, u, status="ABGELEHNT", zahldatum=date(2026, 8, 12))
    diese_woche = svc.plan_bestellungen(db, m, konto_id=tel.id, von=START,
                                        bis=START + timedelta(days=6), heute=HEUTE)
    assert [(x["id"], x["plan_tag"], x["gerollt"]) for x in diese_woche] == [
        (ueberfaellig.id, "2026-08-05", True)]
    naechste = svc.plan_bestellungen(db, m, konto_id=tel.id, von=KW33,
                                     bis=KW33 + timedelta(days=6), heute=HEUTE)
    assert [(x["id"], x["plan_betrag"]) for x in naechste] == [(kuenftig.id, 5950.0)]
    m.bestellungen_offen_einplanen = False
    assert svc.plan_bestellungen(db, m, konto_id=tel.id, von=KW33,
                                 bis=KW33 + timedelta(days=6), heute=HEUTE) == []


# ---------------------------------------------------------------------------
# Erfassung und Status-Workflow
# ---------------------------------------------------------------------------

def test_anlegen_validierung_und_ust_standard(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _bestellung(db, m, u)
    assert b.status == "OFFEN" and b.ust_satz == Decimal("19.00")
    assert svc.brutto(b) == Decimal("5950.00")
    assert svc.bestellung_text(b) == "Telko GmbH · Nr. B-4711 · 5.000,00 € netto"
    eintrag = db.scalars(select(models.BestellJournal)).one()
    assert (eintrag.aktion, eintrag.status_nachher, eintrag.benutzer_name, eintrag.bestellung_id) == (
        "ERFASST", "OFFEN", "Sabine Sachwalter", b.id)
    assert _bestellung(db, m, u, ust_satz=None).ust_satz is None

    anderer = models.Mandant(name="Fremd", kurzname="fremd")
    db.add(anderer)
    db.flush()
    fremdes = models.Konto(mandant_id=anderer.id, nummer="4920", bezeichnung="Telefon")
    db.add(fremdes)
    db.flush()
    gueltig = {"kreditor": "K", "bestelldatum": START, "zahldatum": HEUTE, "betrag_netto": "10",
               "konto_id": _konto(db, m, "4920").id}
    for aenderung, meldung in [
        ({"kreditor": "  "}, "Kreditor fehlt"),
        ({"betrag_netto": "0"}, "größer als 0"),
        ({"betrag_netto": "abc"}, "ungültiger Betrag"),
        ({"zahldatum": None}, "Zahldatum fehlt"),
        ({"bestelldatum": "31.12.2026"}, "ungültiges Datum"),
        ({"konto_id": _konto(db, m, "1200").id}, "Bank- und Kassenkonten"),
        ({"konto_id": fremdes.id}, "Konto nicht gefunden"),
        ({"konto_id": None}, "Konto wählen"),
        ({"ust_satz": "120"}, "USt-Satz"),
    ]:
        with pytest.raises(ValueError, match=meldung):
            svc.anlegen(db, m, u, {**gueltig, **aenderung})
    # Zusatzschlüssel des Routers (Datei-Token, KI-Daten) stören nicht
    assert svc.anlegen(db, m, u, {**gueltig, "token": "x", "ki_daten": None}).status == "OFFEN"


def test_freigeben_und_ablehnen_mit_journal(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _bestellung(db, m, u)
    with pytest.raises(ValueError, match="begründen"):
        svc.entscheiden(db, b, u, "ABLEHNEN", "  ", {})
    with pytest.raises(ValueError, match="Unbekannte Entscheidung"):
        svc.entscheiden(db, b, u, "VIELLEICHT", None, {})
    kennzahlen = {"kw": {"label": "KW 33/2026"}, "ampel": "gruen", "ebenen": [{"verfuegbar": 1.0}]}
    svc.entscheiden(db, b, u, "FREIGEBEN", "im Rahmen", kennzahlen)
    assert b.status == "FREIGEGEBEN" and b.entschieden_von == u.id and b.entschieden_am
    eintrag = db.scalars(select(models.BestellJournal).where(
        models.BestellJournal.aktion == "FREIGEGEBEN")).one()
    assert eintrag.kennzahlen == kennzahlen and eintrag.begruendung == "im Rahmen"
    assert (eintrag.status_vorher, eintrag.status_nachher) == ("OFFEN", "FREIGEGEBEN")
    with pytest.raises(ValueError, match="zur Freigabe"):
        svc.entscheiden(db, b, u, "ABLEHNEN", "doch nicht", {})
    d = svc.als_dict(db, b)
    assert d["entschieden_von"] == "Sabine Sachwalter" and d["status_text"] == "freigegeben"


def test_aendern_regeln(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _bestellung(db, m, u, status="FREIGEGEBEN")
    assert svc.aendern(db, b, u, {"kreditor": "Telko GmbH"}) == []      # unverändert
    # Schreibkorrektur des Kreditors und Zahldatum in derselben KW: Freigabe bleibt
    assert svc.aendern(db, b, u, {"kreditor": "TELKO GmbH", "zahldatum": "2026-08-13"}) == [
        "kreditor", "zahldatum"]
    assert b.status == "FREIGEGEBEN"
    assert svc.aendern(db, b, u, {"betrag_netto": 5200}) == ["betrag_netto"]
    assert b.status == "OFFEN" and b.entschieden_am is None
    eintrag = db.scalars(select(models.BestellJournal).order_by(models.BestellJournal.id.desc())).first()
    assert eintrag.aktion == "GEAENDERT" and "erneute Freigabe" in eintrag.begruendung
    assert "5.000,00 € → 5.200,00 €" in eintrag.begruendung
    svc.entscheiden(db, b, u, "ABLEHNEN", "zu teuer", {})
    svc.aendern(db, b, u, {"beschreibung": "günstigeres Angebot"})
    assert b.status == "OFFEN"
    with pytest.raises(ValueError, match="Unbekannte Felder"):
        svc.aendern(db, b, u, {"status": "FREIGEGEBEN"})
    svc.abloesen(db, b, u, art="MANUELL", betrag=Decimal("1"), datum=HEUTE)
    with pytest.raises(ValueError, match="nicht geändert"):
        svc.aendern(db, b, u, {"kreditor": "X"})


def test_zuruecksetzen_und_stornieren(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _bestellung(db, m, u)
    with pytest.raises(ValueError):
        svc.zuruecksetzen(db, b, u, "Grund")
    svc.entscheiden(db, b, u, "FREIGEBEN", None, {})
    with pytest.raises(ValueError, match="begründen"):
        svc.zuruecksetzen(db, b, u, "")
    svc.zuruecksetzen(db, b, u, "Budget gekürzt")
    assert b.status == "OFFEN" and b.entschieden_am is None and b.entschieden_von is None
    svc.stornieren(db, b, u, None)
    assert b.status == "STORNIERT"
    with pytest.raises(ValueError):
        svc.stornieren(db, b, u, None)
    with pytest.raises(ValueError):
        svc.abloesen(db, b, u, art="MANUELL", betrag=Decimal("1"), datum=HEUTE)
    aktionen = [j.aktion for j in db.scalars(select(models.BestellJournal).order_by(
        models.BestellJournal.id))]
    assert aktionen == ["ERFASST", "FREIGEGEBEN", "ZURUECKGESETZT", "STORNIERT"]


def test_loeschen_nur_ohne_entscheidung(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _bestellung(db, m, u)
    svc.aendern(db, b, u, {"beschreibung": "x"})
    bid = b.id
    svc.loeschen(db, b, u)
    assert db.get(models.Bestellung, bid) is None
    eintraege = list(db.scalars(select(models.BestellJournal).order_by(models.BestellJournal.id)))
    assert [j.aktion for j in eintraege] == ["ERFASST", "GEAENDERT", "GELOESCHT"]
    assert all(j.bestellung_id is None for j in eintraege)
    assert eintraege[-1].bestellung_text.startswith("Telko GmbH")
    # entschieden (auch nach Zurücksetzen) → nicht löschbar
    b2 = _bestellung(db, m, u, status="FREIGEGEBEN")
    svc.zuruecksetzen(db, b2, u, "neu prüfen")
    with pytest.raises(ValueError, match="stornieren"):
        svc.loeschen(db, b2, u)
    b3 = _bestellung(db, m, u)
    svc.abloesen(db, b3, u, art="MANUELL", betrag=Decimal("10"), datum=HEUTE, abschliessend=False)
    with pytest.raises(ValueError, match="Ablösungen"):
        svc.loeschen(db, b3, u)


def test_datei_setzen_und_als_dict(db):
    m = _mandant(db)
    u = _benutzer(db)
    ref = DateiRef(ablage="LOKAL", name="Bestellung 4711.pdf", mime="application/pdf",
                   groesse=1234, pfad="test/Bestellung 4711.pdf")
    b = svc.anlegen(db, m, u, {"kreditor": "Telko GmbH", "bestelldatum": START,
                               "zahldatum": date(2026, 11, 2), "betrag_netto": "100",
                               "konto_id": _konto(db, m, "4920").id},
                    datei=ref, ki_daten={"modell": "test"})
    d = svc.als_dict(db, b)
    assert d["datei"] == {"name": "Bestellung 4711.pdf", "mime": "application/pdf",
                          "groesse": 1234, "ablage": "LOKAL", "pfad": "test/Bestellung 4711.pdf",
                          "web_url": None, "url": f"/mandanten/{m.id}/bestellungen/{b.id}/datei"}
    assert d["ki"] is True and d["kw"] == {"jahr": 2026, "kw": 45, "label": "KW 45/2026"}
    assert d["konto"]["nummer"] == "4920" and d["erstellt_von"] == "Sabine Sachwalter"
    svc.datei_setzen(db, b, u, DateiRef(ablage="SHAREPOINT", name="neu.pdf", mime="application/pdf",
                                        groesse=5, drive_id="d", item_id="i", web_url="https://x"))
    assert b.datei_item_id == "i" and b.datei_pfad is None
    letzter = db.scalars(select(models.BestellJournal).order_by(models.BestellJournal.id.desc())).first()
    assert letzter.aktion == "DATEI" and "neu.pdf" in letzter.begruendung


def test_liste_suche_und_kreditoren(db):
    m = _mandant(db)
    u = _benutzer(db)
    a = _bestellung(db, m, u, kreditor="Müller Bürobedarf", zahldatum=date(2026, 8, 20))
    b = _bestellung(db, m, u, status="FREIGEGEBEN", zahldatum=date(2026, 8, 10))
    db.add(models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="telko gmbh",
                                faellig_am=HEUTE, betrag_brutto=Decimal("1")))
    db.add(models.OffenerPosten(mandant_id=m.id, art="DEBITOR", partner="Kunde AG",
                                faellig_am=HEUTE, betrag_brutto=Decimal("1")))
    db.flush()
    assert [x["id"] for x in svc.liste(db, m)] == [b.id, a.id]          # nach Zahldatum
    assert [x["id"] for x in svc.liste(db, m, status="FREIGEGEBEN")] == [b.id]
    assert [x["id"] for x in svc.liste(db, m, status="OFFEN,FREIGEGEBEN")] == [b.id, a.id]
    assert [x["id"] for x in svc.liste(db, m, suche="mueller")] == [a.id]
    assert [x["id"] for x in svc.liste(db, m, suche="b-4711")] == [b.id, a.id]
    with pytest.raises(ValueError):
        svc.liste(db, m, status="FALSCH")
    assert svc.kreditoren(db, m) == ["Müller Bürobedarf", "Telko GmbH"]


# ---------------------------------------------------------------------------
# Ablösung: Prüfungen, Aufheben, Löschhaken
# ---------------------------------------------------------------------------

def test_abloesung_zahlung_pruefungen(db):
    m = _mandant(db)
    u = _benutzer(db)
    b = _bestellung(db, m, u, status="FREIGEGEBEN")
    eingang = models.Buchung(mandant_id=m.id, datum=HEUTE, konto_nr="1200", gegenkonto_nr="8400",
                             betrag=Decimal("100"), sh="S")
    op_zahlung = _zahlung(db, m, HEUTE, "50")
    op_posten = models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="Y",
                                     faellig_am=HEUTE, betrag_brutto=Decimal("50"))
    db.add(op_posten)
    db.flush()   # echter Posten: PostgreSQL prüft den Fremdschlüssel
    op_zahlung.posten_id = op_posten.id
    db.add(eingang)
    db.flush()
    bank = _konto(db, m, "1200")
    t_ein = models.BankTransaktion(mandant_id=m.id, konto_id=bank.id, buchungstag=HEUTE,
                                   betrag=Decimal("10"))
    t_aus = models.BankTransaktion(mandant_id=m.id, konto_id=bank.id, buchungstag=HEUTE,
                                   betrag=Decimal("-5950"), partner="Telko GmbH")
    t_abgeglichen = models.BankTransaktion(mandant_id=m.id, konto_id=bank.id, buchungstag=HEUTE,
                                           betrag=Decimal("-20"))
    db.add_all([t_ein, t_aus, t_abgeglichen])
    db.flush()
    p = models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="X", faellig_am=HEUTE,
                             betrag_brutto=Decimal("20"))
    db.add(p)
    db.flush()
    db.add(models.AusgleichZuordnung(mandant_id=m.id, transaktion_id=t_abgeglichen.id,
                                     posten_id=p.id, betrag=Decimal("20")))
    fremd = models.Mandant(name="F", kurzname="f")
    db.add(fremd)
    db.flush()
    fremde_buchung = models.Buchung(mandant_id=fremd.id, datum=HEUTE, konto_nr="4920",
                                    gegenkonto_nr="1200", betrag=Decimal("1"), sh="S")
    db.add(fremde_buchung)
    db.flush()
    for kwargs, meldung in [
        ({}, "genau eine Zahlung"),
        ({"buchung_id": eingang.id, "transaktion_id": t_aus.id}, "genau eine Zahlung"),
        ({"buchung_id": eingang.id}, "keine Auszahlung"),
        ({"buchung_id": op_zahlung.id}, "offenen Posten zugeordnet"),
        ({"buchung_id": fremde_buchung.id}, "nicht gefunden"),
        ({"transaktion_id": t_ein.id}, "keine Auszahlung"),
        ({"transaktion_id": t_abgeglichen.id}, "abgeglichen"),
    ]:
        with pytest.raises(ValueError, match=meldung):
            svc.abloesen(db, b, u, art="ZAHLUNG", **kwargs)
    with pytest.raises(ValueError, match="Ablöseart"):
        svc.abloesen(db, b, u, art="TAUSCH")
    a = svc.abloesen(db, b, u, art="ZAHLUNG", transaktion_id=t_aus.id)
    assert (a.betrag, a.datum) == (Decimal("5950.00"), HEUTE)
    b2 = _bestellung(db, m, u, status="FREIGEGEBEN")
    with pytest.raises(ValueError, match="bereits einer Bestellung"):
        svc.abloesen(db, b2, u, art="ZAHLUNG", transaktion_id=t_aus.id)
    abgelehnt = _bestellung(db, m, u, status="ABGELEHNT")
    with pytest.raises(ValueError, match="nicht abgelöst"):
        svc.abloesen(db, abgelehnt, u, art="MANUELL", betrag=Decimal("1"), datum=HEUTE)


def test_abloesung_aufheben_oeffnet_wieder(db):
    m = _mandant(db)
    u = _benutzer(db)
    freigegeben = _bestellung(db, m, u, status="FREIGEGEBEN")
    offen = _bestellung(db, m, u)
    a1 = svc.abloesen(db, freigegeben, u, art="MANUELL", betrag=Decimal("5950"), datum=HEUTE)
    a2 = svc.abloesen(db, offen, u, art="MANUELL", betrag=Decimal("5950"), datum=HEUTE)
    assert freigegeben.status == offen.status == "ABGELOEST"
    svc.abloesung_aufheben(db, a1, u, "falsch zugeordnet")
    svc.abloesung_aufheben(db, a2, u)
    assert freigegeben.status == "FREIGEGEBEN" and offen.status == "OFFEN"
    assert freigegeben.abgeloest_am is None and freigegeben.abgeloest_durch is None
    assert freigegeben.abloesungen == [] and db.scalars(select(models.BestellAbloesung)).all() == []
    eintrag = db.scalars(select(models.BestellJournal).where(
        models.BestellJournal.aktion == "ABLOESUNG_AUFGEHOBEN",
        models.BestellJournal.bestellung_id == freigegeben.id)).one()
    assert eintrag.status_vorher == "ABGELOEST" and eintrag.status_nachher == "FREIGEGEBEN"
    assert "falsch zugeordnet" in eintrag.begruendung


def test_loeschhaken_heben_abloesungen_auf(db):
    m = _mandant(db)
    u = _benutzer(db)
    bank = _konto(db, m, "1200")
    b1 = _bestellung(db, m, u, status="FREIGEGEBEN")
    b2 = _bestellung(db, m, u, status="FREIGEGEBEN")
    b3 = _bestellung(db, m, u)
    bu = _zahlung(db, m, HEUTE, "5950")
    t = models.BankTransaktion(mandant_id=m.id, konto_id=bank.id, buchungstag=HEUTE,
                               betrag=Decimal("-1000"))
    p = models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="Telko", faellig_am=HEUTE,
                             betrag_brutto=Decimal("5950"))
    db.add_all([t, p])
    db.flush()
    svc.abloesen(db, b1, u, art="ZAHLUNG", buchung_id=bu.id)
    svc.abloesen(db, b2, u, art="ZAHLUNG", transaktion_id=t.id, abschliessend=False)
    svc.abloesen(db, b3, u, art="RECHNUNG", posten_id=p.id)
    assert (b1.status, b2.status, b3.status) == ("ABGELOEST", "FREIGEGEBEN", "ABGELOEST")
    assert svc.buchungen_entfernt(db, []) == 0
    assert svc.buchungen_entfernt(db, [bu.id, 12345]) == 1
    assert svc.transaktionen_entfernt(db, [t.id]) == 1
    assert svc.posten_entfernt(db, [p.id], u) == 1
    assert (b1.status, b2.status, b3.status) == ("FREIGEGEBEN", "FREIGEGEBEN", "OFFEN")
    assert svc.als_dict(db, b2)["rest_geplant"] == 5950.0
    systemeintraege = db.scalars(select(models.BestellJournal).where(
        models.BestellJournal.aktion == "ABLOESUNG_AUFGEHOBEN",
        models.BestellJournal.benutzer_id.is_(None))).all()
    assert len(systemeintraege) == 2 and {j.benutzer_name for j in systemeintraege} == {"System"}
    assert "Buchung gelöscht" in next(j.begruendung for j in systemeintraege
                                      if j.bestellung_id == b1.id)
    assert db.scalars(select(models.BestellAbloesung)).all() == []


# ---------------------------------------------------------------------------
# Ablösekandidaten und Vorschläge
# ---------------------------------------------------------------------------

def test_abloese_kandidaten_scoring(db):
    m = _mandant(db)
    u = _benutzer(db)
    bank = _konto(db, m, "1200")
    b = _bestellung(db, m, u, status="FREIGEGEBEN", zahldatum=date(2026, 8, 7),
                    bestelldatum=date(2026, 7, 27))
    treffer = _zahlung(db, m, date(2026, 8, 7), "5950", text="Telko GmbH Best. B-4711")
    aehnlich = _zahlung(db, m, date(2026, 8, 6), "5900", konto="4930", text="TELKO Rechnung")
    _zahlung(db, m, date(2026, 8, 7), "45", konto="4930", text="Bäckerei")     # nur Datum
    _zahlung(db, m, date(2026, 7, 1), "5950", text="Telko GmbH")               # zu alt
    belegt = _zahlung(db, m, date(2026, 8, 7), "5950", text="Telko GmbH")
    andere = _bestellung(db, m, u, status="FREIGEGEBEN", kreditor="Andere")
    svc.abloesen(db, andere, u, art="ZAHLUNG", buchung_id=belegt.id)
    t = models.BankTransaktion(mandant_id=m.id, konto_id=bank.id, buchungstag=date(2026, 8, 10),
                               betrag=Decimal("-8000"), partner="Telko GmbH",
                               verwendungszweck="Abschlag")
    p = models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="Telko GmbH",
                             belegnr="R-77", faellig_am=date(2026, 8, 12),
                             betrag_brutto=Decimal("5950"), konto_id=b.konto_id)
    db.add_all([t, p])
    db.flush()
    kandidaten = svc.abloese_kandidaten(db, m, b, heute=HEUTE)
    kurz = [(k["typ"], k["id"], k["score"]) for k in kandidaten]
    # Buchung: Kreditor 40 + Bestellnr. 40 + Konto 25 + exakt 35 + Datum 10
    assert kurz[0] == ("BUCHUNG", treffer.id, 150)
    # Posten: Kreditor 40 + Konto 25 + exakt 35 + Datum 10 (12.08. ±14 Tage)
    assert kurz[1] == ("POSTEN", p.id, 110)
    assert kandidaten[1]["art"] == "RECHNUNG"
    # Buchung „TELKO“ (Kreditor-Token 1 von 1 → stark) + ±3 % + Datum
    assert ("BUCHUNG", aehnlich.id, 75) in kurz
    # Bankumsatz: Kreditor 40 + >120 % −20 + Datum 10
    assert ("TRANSAKTION", t.id, 30) in kurz
    assert kurz[-1][2] == 10                       # nur Datumsnähe
    assert belegt.id not in [k["id"] for k in kandidaten if k["typ"] == "BUCHUNG"]
    assert len(kandidaten) == 5
    assert kandidaten[0]["betrag"] == 5950.0 and kandidaten[0]["konto"] == "4920 Telefon"
    assert "Bestellnummer im Text" in kandidaten[0]["gruende"]


def test_abloese_vorschlaege_eindeutig(db):
    m = _mandant(db)
    u = _benutzer(db)
    b1 = _bestellung(db, m, u, status="FREIGEGEBEN", zahldatum=date(2026, 8, 7))
    b2 = _bestellung(db, m, u, zahldatum=date(2026, 8, 7), bestellnummer=None)
    _bestellung(db, m, u, status="ABGELEHNT", zahldatum=date(2026, 8, 7))
    bu = _zahlung(db, m, date(2026, 8, 7), "5950", text="Telko GmbH B-4711")
    vorschlaege = svc.abloese_vorschlaege(db, m)
    assert len(vorschlaege) == 1
    v = vorschlaege[0]
    assert v["bestellung"]["id"] == b1.id and v["kandidat"]["id"] == bu.id
    assert v["konfidenz"] == "SICHER" and v["score"] == 150
    # nach der Übernahme bleibt für b2 kein Beleg
    svc.abloesen(db, b1, u, art="ZAHLUNG", buchung_id=bu.id)
    assert svc.abloese_vorschlaege(db, m) == []
    # schwacher Treffer (Kreditor + Datum = 50) wird nicht vorgeschlagen
    _zahlung(db, m, date(2026, 8, 7), "100", konto="4930", text="Telko GmbH")
    assert svc.abloese_vorschlaege(db, m) == []
    # Kreditor + exakt + Konto → 110 → sicher
    bu2 = _zahlung(db, m, date(2026, 8, 7), "5950", text="Telko")
    v2 = svc.abloese_vorschlaege(db, m)
    assert [(x["bestellung"]["id"], x["kandidat"]["id"], x["konfidenz"]) for x in v2] == [
        (b2.id, bu2.id, "SICHER")]
