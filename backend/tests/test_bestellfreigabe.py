"""Freigabe-Kennzahlen je KW (Rechenweg Spec §3.2): Plan der KW ab heute,
gebundene Zahlungen, bisherige Abweichung inkl. Vorträge und Überfälligem."""

import json
from datetime import date
from decimal import Decimal

import pytest

from app import models
from app.services import bestellungen as svc
from app.services.liquiditaet import erstelle_snapshot

from test_bestellungen import _bank, _benutzer, _bestellung, _budget_woche, _zahlung
from test_liquiditaet import _konto, _mandant

HEUTE = date(2026, 8, 5)   # Mittwoch, KW 32
START = date(2026, 8, 3)   # Montag KW 32


def _kz(db, m, konto="4920", brutto="5950", kw=34, heute=HEUTE, basis=None, ohne=None):
    db.flush()
    return svc.freigabe_kennzahlen(
        db, m, konto_id=_konto(db, m, konto).id if konto else None,
        betrag_brutto=Decimal(str(brutto)), jahr=2026, kw=kw, basis=basis,
        ohne_bestellung_id=ohne, heute=heute)


def _e(r, ebene="konto") -> dict:
    return next(x for x in r["ebenen"] if x["ebene"] == ebene)


def _op(db, m, betrag, faellig, konto="4920", art="KREDITOR", partner="Lieferant") -> models.OffenerPosten:
    p = models.OffenerPosten(mandant_id=m.id, art=art, partner=partner, faellig_am=faellig,
                             betrag_brutto=Decimal(betrag), konto_id=_konto(db, m, konto).id)
    db.add(p)
    db.flush()
    return p


def _snapshot(db, m, wochen=13):
    db.commit()
    return erstelle_snapshot(db, m, start=START, wochen=wochen, heute=START)


def test_laufende_kw_nur_resttage(db):
    m = _mandant(db)
    u = _benutzer(db)
    _budget_woche(db, m, "4920", 32, "10000")     # 11.900 brutto, 2.380 je Banktag Mo–Fr
    _zahlung(db, m, START, "2000")                  # Montag: 2.000 gezahlt
    _zahlung(db, m, HEUTE, "999")                   # heute: zählt noch nicht (Ist bis gestern)
    b = _bestellung(db, m, u, zahldatum=date(2026, 8, 7))
    r = _kz(db, m, kw=32, brutto=svc.brutto(b), ohne=b.id)
    assert (r["heute"], r["ist_bis"], r["ab"]) == ("2026-08-05", "2026-08-04", "2026-08-05")
    assert r["kw"] == {"jahr": 2026, "kw": 32, "label": "KW 32/2026",
                       "von": "2026-08-03", "bis": "2026-08-09"}
    assert r["basis"]["typ"] == "LIVE" and r["basis"]["s0"] == "2026-08-03"
    k = _e(r)
    assert (k["soll_kw"], k["soll_kw_rest"], k["soll_vergangen"]) == (11900.0, 7140.0, 4760.0)
    assert k["gebunden"] == {"bestellungen": 0.0, "sonstige": 0.0, "summe": 0.0}
    # Mo/Di geplant 4.760, gezahlt 2.000 → Minderausgabe 2.760 erhöht den Spielraum
    assert k["abweichung"] == {"ist_minus_plan": -2760.0, "vortraege": 0.0, "ueberfaellig": 0.0,
                               "summe": -2760.0}
    assert (k["verfuegbar"], k["nach_freigabe"]) == (9900.0, 3950.0)    # 11.900 − 2.000
    assert r["massgeblich"] == "konto" and r["ampel"] == "gruen"
    assert r["betrag_brutto"] == 5950.0


def test_zukuenftige_kw_gegen_snapshot(db):
    m = _mandant(db)
    _budget_woche(db, m, "4920", 34, "10000")
    _op(db, m, "3000", date(2026, 8, 19))
    snap = _snapshot(db, m)
    r = _kz(db, m, kw=34)
    assert r["basis"]["typ"] == "SNAPSHOT" and r["basis"]["snapshot"]["id"] == snap.id
    assert r["basis"]["s0"] == "2026-08-03"
    assert "Keine Soll-Referenz" in r["basis"]["hinweis"]
    k = _e(r)
    assert (k["soll_kw"], k["soll_kw_rest"], k["soll_vergangen"]) == (11900.0, 11900.0, 0.0)
    assert k["gebunden"] == {"bestellungen": 0.0, "sonstige": 3000.0, "summe": 3000.0}
    assert k["abweichung"]["summe"] == 0.0
    assert (k["verfuegbar"], k["nach_freigabe"]) == (8900.0, 2950.0)
    assert r["ampel"] == "gruen"
    assert _kz(db, m, kw=34, brutto="9000")["ampel"] == "rot"


@pytest.mark.parametrize("op, snap_vorher, snap_nachher, live", [
    ("3000", 8900.0, 8900.0, 8900.0),       # unter Budget: überall max(Budget, explizit) − gebunden
    ("15000", -3100.0, 0.0, 0.0),           # über Budget: nach dem Einfrieren neu → Spielraum negativ
])
def test_neuer_op_nach_snapshot_und_max_konsistenz(db, op, snap_vorher, snap_nachher, live):
    m = _mandant(db)
    _budget_woche(db, m, "4920", 34, "10000")
    ohne_op = _snapshot(db, m)
    _op(db, m, op, date(2026, 8, 19))
    mit_op = _snapshot(db, m)
    assert _e(_kz(db, m, basis=ohne_op.id))["verfuegbar"] == snap_vorher
    assert _e(_kz(db, m, basis=str(mit_op.id)))["verfuegbar"] == snap_nachher
    assert _e(_kz(db, m, basis="LIVE"))["verfuegbar"] == live


@pytest.mark.parametrize("vortragen, gezahlt, ist_minus_plan, vortraege, verfuegbar", [
    (True, None, -1500.0, 1500.0, 2000.0),     # unbezahlte Miete neutralisiert die Minderausgabe
    (True, "700", -800.0, 800.0, 2000.0),      # Teilzahlung: Rest bleibt vorgetragen
    (False, None, -1500.0, 0.0, 3500.0),       # ohne Vortrag zählt die Minderausgabe
])
def test_vortrag_neutralisiert_minderausgabe(db, vortragen, gezahlt, ist_minus_plan, vortraege,
                                             verfuegbar):
    m = _mandant(db)
    db.add(models.Dauerbuchung(mandant_id=m.id, name="Miete", art="KREDITOR",
                               konto_id=_konto(db, m, "4210").id, betrag_brutto=Decimal("1500"),
                               intervall="MONATLICH", stichtag=3, gueltig_von=date(2026, 1, 1),
                               vortragen=vortragen))
    _budget_woche(db, m, "4210", 33, "2000")       # Miete ohne USt → 2.000
    _snapshot(db, m)
    if gezahlt:
        _zahlung(db, m, date(2026, 8, 4), gezahlt, konto="4210")
    k = _e(_kz(db, m, konto="4210", kw=33, brutto="500"))
    assert k["soll_kw"] == 2000.0
    assert k["abweichung"]["ist_minus_plan"] == ist_minus_plan
    assert k["abweichung"]["vortraege"] == vortraege
    assert k["verfuegbar"] == verfuegbar


def test_ueberfaelliger_op_neutralisiert(db):
    m = _mandant(db)
    _budget_woche(db, m, "4920", 34, "10000")
    op = _op(db, m, "3000", date(2026, 8, 4))       # Dienstag, unbezahlt
    _snapshot(db, m)
    k34 = _e(_kz(db, m, kw=34, brutto="100"))
    assert k34["gebunden"]["summe"] == 0.0           # gerollt → zählt in der Abweichung
    assert k34["abweichung"] == {"ist_minus_plan": -3000.0, "vortraege": 0.0,
                                 "ueberfaellig": 3000.0, "summe": 0.0}
    assert k34["verfuegbar"] == 11900.0
    r32 = _kz(db, m, kw=32, brutto="100")
    k32 = _e(r32)
    assert (k32["soll_kw"], k32["soll_kw_rest"], k32["soll_vergangen"]) == (3000.0, 0.0, 3000.0)
    assert (k32["gebunden"]["summe"], k32["abweichung"]["summe"], k32["verfuegbar"]) == (0.0, 0.0, 0.0)
    assert r32["massgeblich"] == "konto" and r32["ampel"] == "rot"
    # bezahlt (Buchung + Ausgleich) → weder Abweichung noch Überfälliges
    bu = _zahlung(db, m, date(2026, 8, 4), "3000")
    bu.posten_id = op.id
    op.status, op.bezahlt_am, op.bezahlt_betrag = "BEZAHLT", date(2026, 8, 4), Decimal("3000")
    k34 = _e(_kz(db, m, kw=34, brutto="100"))
    assert k34["abweichung"] == {"ist_minus_plan": 0.0, "vortraege": 0.0, "ueberfaellig": 0.0,
                                 "summe": 0.0}
    assert k34["verfuegbar"] == 11900.0


def test_ueberfaellige_freigegebene_bestellung_neutralisiert(db):
    m = _mandant(db)
    u = _benutzer(db)
    _budget_woche(db, m, "4920", 34, "10000")
    alt = _bestellung(db, m, u, status="FREIGEGEBEN", zahldatum=date(2026, 8, 4))
    _snapshot(db, m)
    k = _e(_kz(db, m, kw=34, brutto="100"))
    assert k["abweichung"] == {"ist_minus_plan": -5950.0, "vortraege": 0.0,
                               "ueberfaellig": 5950.0, "summe": 0.0}
    assert (k["gebunden"]["summe"], k["verfuegbar"]) == (0.0, 11900.0)
    r32 = _kz(db, m, kw=32, brutto="100")
    assert [x["id"] for x in r32["freigegebene_bestellungen"]] == [alt.id]   # laufende KW: auch überfällige
    assert _kz(db, m, kw=34)["freigegebene_bestellungen"] == []


def test_basiswahl_und_hinweise(db):
    m = _mandant(db)
    kurz = _snapshot(db, m, wochen=4)                # KW 32–35
    r = _kz(db, m, kw=40)
    assert r["basis"]["typ"] == "LIVE" and "Kein Planstand deckt die KW ab" in r["basis"]["hinweis"]
    assert r["basis"]["hinweis"] in r["hinweise"]
    r = _kz(db, m, kw=40, basis=kurz.id)
    assert r["basis"]["typ"] == "SNAPSHOT" and "deckt die KW" in r["basis"]["hinweis"]
    r = _kz(db, m, kw=34, basis="LIVE")
    assert r["basis"]["typ"] == "LIVE" and "aktuelle Plan" in r["basis"]["hinweis"]
    r = _kz(db, m, kw=34, basis="")
    assert r["basis"]["snapshot"]["id"] == kurz.id and "Keine Soll-Referenz" in r["basis"]["hinweis"]
    kurz.referenz = True
    r = _kz(db, m, kw=34, basis="AUTO")
    assert r["basis"]["snapshot"] == {"id": kurz.id, "stichtag": "2026-08-03", "wochen": 4,
                                      "kommentar": None, "referenz": True}
    assert r["basis"]["hinweis"] is None
    lang = _snapshot(db, m, wochen=13)
    r = _kz(db, m, kw=40)
    assert r["basis"]["snapshot"]["id"] == lang.id
    assert "Soll-Referenz (Planstand vom 03.08.2026) deckt die KW nicht ab" in r["basis"]["hinweis"]
    # künftiger Planstand zählt für „automatisch“ nicht
    db.add(models.PlanSnapshot(mandant_id=m.id, stichtag=date(2026, 8, 10), wochen=13))
    assert _kz(db, m, kw=40)["basis"]["snapshot"]["id"] == lang.id
    fremd = models.Mandant(name="F", kurzname="fremd")
    db.add(fremd)
    db.flush()
    fremder = models.PlanSnapshot(mandant_id=fremd.id, stichtag=START, wochen=13)
    db.add(fremder)
    db.flush()
    with pytest.raises(ValueError, match="Planstand nicht gefunden"):
        _kz(db, m, basis=fremder.id)
    with pytest.raises(ValueError, match="Unbekannte Basis"):
        _kz(db, m, basis="GESTERN")


def test_brutto_und_freigegebene_bestellungen_mindern_spielraum(db):
    m = _mandant(db)
    u = _benutzer(db)
    _budget_woche(db, m, "4920", 34, "10000")
    a = _bestellung(db, m, u, status="FREIGEGEBEN", netto="1000", ust_satz="7",
                    zahldatum=date(2026, 8, 19))                       # 1.070 brutto
    c = _bestellung(db, m, u, status="FREIGEGEBEN", netto="500", ust_satz=None,
                    zahldatum=date(2026, 8, 20))                       # 500 ohne USt
    weitere = _bestellung(db, m, u, netto="100", zahldatum=date(2026, 8, 21))
    kandidat = _bestellung(db, m, u, netto="5000", zahldatum=date(2026, 8, 19))
    assert svc.brutto(kandidat) == Decimal("5950.00")
    r = _kz(db, m, kw=34, brutto=svc.brutto(kandidat), ohne=kandidat.id, basis="LIVE")
    k = _e(r)
    assert k["soll_kw"] == 11900.0
    assert k["gebunden"] == {"bestellungen": 1570.0, "sonstige": 0.0, "summe": 1570.0}
    assert (k["verfuegbar"], k["nach_freigabe"]) == (10330.0, 4380.0)
    assert r["freigegebene_bestellungen"] == [
        {"id": a.id, "kreditor": "Telko GmbH", "betrag_brutto": 1070.0, "zahldatum": "2026-08-19"},
        {"id": c.id, "kreditor": "Telko GmbH", "betrag_brutto": 500.0, "zahldatum": "2026-08-20"},
    ]
    assert [x["id"] for x in r["offene_bestellungen"]] == [weitere.id]


def test_kandidat_zaehlt_nie_als_gebunden(db):
    m = _mandant(db)
    u = _benutzer(db)
    _budget_woche(db, m, "4920", 34, "10000")
    kandidat = _bestellung(db, m, u, status="FREIGEGEBEN", netto="5000", zahldatum=date(2026, 8, 19))
    r = _kz(db, m, kw=34, basis="LIVE", ohne=kandidat.id)
    k = _e(r)
    assert k["gebunden"]["summe"] == 0.0 and k["soll_kw"] == 11900.0
    assert (k["verfuegbar"], k["nach_freigabe"]) == (11900.0, 5950.0)
    assert r["freigegebene_bestellungen"] == []
    assert r["liquiditaet"]["bestellung_im_plan"] is True
    assert r["liquiditaet"]["nach_freigabe"] == r["liquiditaet"]["ende_kw"]
    # ohne Ausschluss wäre sie gebunden
    assert _e(_kz(db, m, kw=34, basis="LIVE"))["gebunden"]["bestellungen"] == 5950.0
    # auch eine offene, mit eingeplante Bestellung zieht sich nicht selbst ab
    offen = _bestellung(db, m, u, netto="5000", zahldatum=date(2026, 8, 20))
    k = _e(_kz(db, m, kw=34, basis="LIVE", ohne=offen.id))
    assert (k["soll_kw"], k["gebunden"]["summe"], k["verfuegbar"]) == (11900.0, 5950.0, 5950.0)


def test_ebenen_konto_gruppe_gesamt(db):
    m = _mandant(db)
    _op(db, m, "1000", date(2026, 8, 18), konto="4920")         # A_SONST
    _op(db, m, "500", date(2026, 8, 18), konto="4930")          # A_SONST
    _op(db, m, "700", date(2026, 8, 19), konto="3400")          # A_MAT
    _op(db, m, "2000", date(2026, 8, 19), konto="8400", art="DEBITOR")   # Einzahlung: nie
    r = _kz(db, m, kw=34, brutto="100")
    assert [(e["ebene"], e["name"]) for e in r["ebenen"]] == [
        ("konto", "4920 Telefon"), ("gruppe", "Sonstige Auszahlungen"),
        ("gesamt", "Auszahlungen gesamt")]
    assert [(e["soll_kw"], e["gebunden"]["summe"], e["verfuegbar"]) for e in r["ebenen"]] == [
        (1000.0, 1000.0, 0.0), (1500.0, 1500.0, 0.0), (2200.0, 2200.0, 0.0)]
    assert r["massgeblich"] == "konto" and r["ampel"] == "rot"
    # Konto ohne Plan in der KW → die Gruppe ist maßgeblich
    _budget_woche(db, m, "4930", 34, "1000")                     # 1.190 ≥ Posten 500
    r = _kz(db, m, konto="4950", kw=34, brutto="100")
    assert _e(r)["soll_kw"] == 0.0 and r["massgeblich"] == "gruppe"
    g = _e(r, "gruppe")
    assert (g["soll_kw"], g["gebunden"]["summe"], g["verfuegbar"], g["nach_freigabe"]) == (
        2190.0, 1500.0, 690.0, 590.0)
    assert r["ampel"] == "gruen"
    # ohne Konto nur die Gesamtebene
    r = _kz(db, m, konto=None, kw=34, brutto="100")
    assert [e["ebene"] for e in r["ebenen"]] == ["gesamt"] and r["massgeblich"] == "gesamt"


def test_vergangene_kw_und_ungueltige_eingaben(db):
    m = _mandant(db)
    with pytest.raises(ValueError, match="Vergangenheit"):
        _kz(db, m, kw=31)
    with pytest.raises(ValueError, match="Kalenderwoche"):
        _kz(db, m, kw=54)
    sonntag = _kz(db, m, kw=32, heute=date(2026, 8, 9))          # KW läuft noch
    assert sonntag["ab"] == "2026-08-09"
    with pytest.raises(ValueError, match="Vergangenheit"):
        _kz(db, m, kw=32, heute=date(2026, 8, 10))
    fremd = models.Mandant(name="F", kurzname="fremd")
    db.add(fremd)
    db.flush()
    k = models.Konto(mandant_id=fremd.id, nummer="4920", bezeichnung="Telefon")
    db.add(k)
    db.flush()
    with pytest.raises(ValueError, match="Konto nicht gefunden"):
        svc.freigabe_kennzahlen(db, m, konto_id=k.id, betrag_brutto=Decimal("1"), jahr=2026,
                                kw=34, heute=HEUTE)


def test_liquiditaet_gegenprobe(db):
    m = _mandant(db)
    u = _benutzer(db)
    _bank(db, m)                                                   # 100.000
    _budget_woche(db, m, "4920", 34, "10000")
    m.bestellungen_offen_einplanen = False
    kandidat = _bestellung(db, m, u, netto="5000", zahldatum=date(2026, 8, 19))
    r = _kz(db, m, kw=34, brutto=svc.brutto(kandidat), ohne=kandidat.id)
    assert r["liquiditaet"] == {"ende_kw": 88100.0, "verfuegbar_ende_kw": 88100.0,
                                "nach_freigabe": 82150.0, "bestellung_im_plan": False}
    m.bestellungen_offen_einplanen = True                          # steht schon im Plan
    r = _kz(db, m, kw=34, brutto=svc.brutto(kandidat), ohne=kandidat.id)
    assert r["liquiditaet"] == {"ende_kw": 88100.0, "verfuegbar_ende_kw": 88100.0,
                                "nach_freigabe": 88100.0, "bestellung_im_plan": True}


def test_verschiebung_wirkt_wie_im_plan(db):
    m = _mandant(db)
    tel = _konto(db, m, "4920")
    _budget_woche(db, m, "4920", 34, "10000")
    _op(db, m, "3000", date(2026, 8, 12))                          # KW 33 …
    db.add(models.PlanVerschiebung(mandant_id=m.id, konto_id=tel.id, quelle="PLAN",
                                   von=date(2026, 8, 12), nach=date(2026, 8, 19),
                                   betrag=Decimal("-3000")))      # … in KW 34 verschoben
    k33 = _e(_kz(db, m, kw=33, basis="LIVE", brutto="100"))
    assert (k33["soll_kw"], k33["gebunden"]["summe"], k33["verfuegbar"]) == (0.0, 0.0, 0.0)
    k34 = _e(_kz(db, m, kw=34, basis="LIVE", brutto="100"))
    assert (k34["soll_kw"], k34["gebunden"]["summe"], k34["verfuegbar"]) == (14900.0, 3000.0, 11900.0)
    snap = _snapshot(db, m)                                        # Snapshot enthält die Verschiebung
    k34 = _e(_kz(db, m, kw=34, basis=snap.id, brutto="100"))
    assert (k34["soll_kw"], k34["gebunden"]["summe"], k34["verfuegbar"]) == (14900.0, 3000.0, 11900.0)


def test_heute_erledigtes_bleibt_gebunden_bis_das_ist_vorliegt(db):
    """Das Ist zählt nur bis gestern: heute gezahlte/abgelöste Vorgänge bleiben
    gebunden, damit der Spielraum nicht für einen Tag springt."""
    m = _mandant(db)
    u = _benutzer(db)
    _budget_woche(db, m, "4920", 34, "10000")
    a = _bestellung(db, m, u, status="FREIGEGEBEN", netto="5000", zahldatum=date(2026, 8, 19))
    teil = _bestellung(db, m, u, status="FREIGEGEBEN", netto="1000", ust_satz=None,
                       zahldatum=date(2026, 8, 20))
    _snapshot(db, m)                                  # KW 34: 11.900 (Bestellungen im Budget)
    op = _op(db, m, "2000", date(2026, 8, 20))        # neu nach dem Snapshot
    vorher = _e(_kz(db, m, kw=34, brutto="100"))
    assert vorher["gebunden"] == {"bestellungen": 6950.0, "sonstige": 2000.0, "summe": 8950.0}
    assert vorher["verfuegbar"] == 2950.0
    # heute: Bestellung a bezahlt (abgelöst), Teilzahlung auf „teil“, Posten bezahlt
    svc.abloesen(db, a, u, art="ZAHLUNG", buchung_id=_zahlung(db, m, HEUTE, "5950").id)
    svc.abloesen(db, teil, u, art="ZAHLUNG", buchung_id=_zahlung(db, m, HEUTE, "400").id,
                 abschliessend=False)
    bu = _zahlung(db, m, HEUTE, "2000")
    bu.posten_id = op.id
    op.status, op.bezahlt_am, op.bezahlt_betrag = "BEZAHLT", HEUTE, Decimal("2000")
    heute = _e(_kz(db, m, kw=34, brutto="100"))
    assert heute["gebunden"] == vorher["gebunden"] and heute["verfuegbar"] == 2950.0
    # morgen: Ist enthält die Zahlungen von heute, die Vorgänge sind erledigt
    morgen = _e(_kz(db, m, kw=34, brutto="100", heute=date(2026, 8, 6)))
    assert morgen["gebunden"] == {"bestellungen": 600.0, "sonstige": 0.0, "summe": 600.0}
    assert morgen["abweichung"]["ist_minus_plan"] == 8350.0
    assert morgen["verfuegbar"] == 2950.0


def test_ist_auf_nicht_angelegtem_konto(db):
    m = _mandant(db)
    _zahlung(db, m, date(2026, 8, 4), "300", konto="70001")     # Kreditorenkonto, nicht angelegt
    db.add(models.Buchung(mandant_id=m.id, datum=date(2026, 8, 4), konto_nr="1200",
                          gegenkonto_nr="10001", betrag=Decimal("5000"), sh="S"))  # Eingang: ignoriert
    r = _kz(db, m, kw=34, brutto="100")
    assert _e(r)["abweichung"]["ist_minus_plan"] == 0.0
    assert _e(r, "gesamt")["abweichung"]["ist_minus_plan"] == 300.0
    assert any("70001" in h for h in r["hinweise"])


def test_freigabe_uebersicht(db):
    m = _mandant(db)
    u = _benutzer(db)
    _budget_woche(db, m, "4920", 34, "10000")
    m.bestellungen_offen_einplanen = False
    o1 = _bestellung(db, m, u, netto="5000", zahldatum=date(2026, 8, 19))       # 5.950
    o2 = _bestellung(db, m, u, netto="1000", zahldatum=date(2026, 8, 20))       # 1.190
    f = _bestellung(db, m, u, status="FREIGEGEBEN", netto="2000", zahldatum=date(2026, 8, 21))
    alt = _bestellung(db, m, u, netto="10", zahldatum=date(2026, 8, 3))
    db.flush()
    r = svc.freigabe_uebersicht(db, m, jahr=2026, kw=34, basis="LIVE", heute=HEUTE)
    assert [x["id"] for x in r["offen"]] == [o1.id, o2.id]
    assert [x["id"] for x in r["freigegeben"]] == [f.id]
    assert r["summe_offen"] == 7140.0
    g = r["gesamt"]
    assert g["ebene"] == "gesamt" and g["soll_kw"] == 11900.0
    assert g["gebunden"] == {"bestellungen": 2380.0, "sonstige": 0.0, "summe": 2380.0}
    assert (g["verfuegbar"], g["nach_freigabe"]) == (9520.0, 2380.0)
    assert r["ampel"] == "gruen" and r["kw"]["label"] == "KW 34/2026"
    assert r["liquiditaet"]["bestellung_im_plan"] is False
    laufend = svc.freigabe_uebersicht(db, m, jahr=2026, kw=32, heute=HEUTE)
    assert [x["id"] for x in laufend["offen"]] == [alt.id]            # laufende KW: auch überfällige


def test_kennzahlen_als_journal_momentaufnahme(db):
    m = _mandant(db)
    u = _benutzer(db)
    _budget_woche(db, m, "4920", 34, "10000")
    b = _bestellung(db, m, u, zahldatum=date(2026, 8, 19))
    r = _kz(db, m, kw=34, brutto=svc.brutto(b), ohne=b.id)
    json.dumps(r)                                     # JSON-tauglich
    svc.entscheiden(db, b, u, "FREIGEBEN", None, r)
    db.commit()
    db.expire_all()
    eintrag = db.query(models.BestellJournal).filter_by(aktion="FREIGEGEBEN").one()
    assert eintrag.kennzahlen == json.loads(json.dumps(r))
    assert svc.journal_liste(db, m, bestellung_id=b.id)[0]["kennzahlen"]["ampel"] == "gruen"
    # nach der Freigabe ist sie für andere Bestellungen gebunden
    k = _e(_kz(db, m, kw=34, brutto="100", basis="LIVE"))
    assert k["gebunden"]["bestellungen"] == 5950.0
