"""Vortrag nicht (vollständig) gebuchter Zahlungen aus Dauerschuldverhältnissen."""

from datetime import date
from decimal import Decimal

from app import models
from app.services.liquiditaet import berechne_plan

from test_liquiditaet import _konto, _mandant

HEUTE = date(2026, 8, 5)        # Mittwoch
ZIEL = "2026-08-06"             # nächster Bankarbeitstag ab morgen


def _miete(db, m, betrag="1500", name="Miete", vortragen=True, konto="4210"):
    db.add(models.Dauerbuchung(
        mandant_id=m.id, name=name, art="KREDITOR", konto_id=_konto(db, m, konto).id,
        betrag_brutto=Decimal(betrag), intervall="MONATLICH", stichtag=1,
        gueltig_von=date(2026, 1, 1), vortragen=vortragen,
    ))


def _zahlung(db, m, tag: date, betrag: str, konto="4210"):
    """Auszahlung: Aufwandskonto an Bank."""
    db.add(models.Buchung(mandant_id=m.id, datum=tag, konto_nr=konto, gegenkonto_nr="1200",
                          betrag=Decimal(betrag), sh="S"))


def _plan(db, m, start=date(2026, 7, 6), wochen=6):
    db.commit()
    return berechne_plan(db, m, start=start, wochen=wochen, heute=HEUTE)


def _zeile(plan, nummer="4210"):
    return next(k for g in plan["zeilen"] for k in g["kinder"] if k["nummer"] == nummer)


def test_nicht_gebuchte_rate_wird_vorgetragen(db):
    m = _mandant(db)
    _miete(db, m)            # fällig 01.08. (Samstag) -> 03.08.
    bank = _konto(db, m, "1200")
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=bank.id,
                          datum=date(2026, 7, 5), wert=Decimal("10000")))
    plan = _plan(db, m)
    assert _zeile(plan)["vortrag"] == {ZIEL: -1500.0}
    assert plan["summen"]["auszahlungen"]["vortrag"] == {ZIEL: -1500.0}
    v = plan["vortraege"][0]
    assert (v["name"], v["faellig"], v["ziel"], v["soll"], v["gezahlt"], v["betrag"]) == (
        "Miete", "2026-08-03", ZIEL, -1500.0, 0.0, -1500.0)
    # fortgeschriebene Liquidität ab dem Vortragstag um den Betrag niedriger
    b = plan["bestaende"]
    assert b["liquiditaet_vortrag"][ZIEL] == b["liquiditaet"][ZIEL] - 1500
    assert b["liquiditaet_vortrag"]["2026-08-05"] == b["liquiditaet"]["2026-08-05"]


def test_teilzahlung_mindert_vortrag(db):
    m = _mandant(db)
    _miete(db, m)
    _zahlung(db, m, date(2026, 8, 4), "700")
    plan = _plan(db, m)
    assert _zeile(plan)["vortrag"] == {ZIEL: -800.0}
    assert plan["vortraege"][0]["gezahlt"] == 700.0


def test_gebuchte_rate_kein_vortrag(db):
    m = _mandant(db)
    _miete(db, m)
    _zahlung(db, m, date(2026, 7, 31), "1500")     # vor Fälligkeit gezahlt
    plan = _plan(db, m)
    assert plan["vortraege"] == []
    assert _zeile(plan)["vortrag"] == {}
    assert "liquiditaet_vortrag" not in plan["bestaende"]


def test_ohne_vortrag_kennzeichen(db):
    m = _mandant(db)
    _miete(db, m, vortragen=False)
    assert _plan(db, m)["vortraege"] == []


def test_zwei_raten_am_selben_tag_betragsgleich_getrennt(db):
    """Zwei Raten auf demselben Konto: die betragsgleiche Zahlung deckt
    ihre eigene Rate, vorgetragen wird genau die andere."""
    m = _mandant(db)
    _miete(db, m, "300", name="Versicherung A", konto="4540")
    _miete(db, m, "500", name="Versicherung B", konto="4540")
    _zahlung(db, m, date(2026, 8, 3), "500", konto="4540")
    plan = _plan(db, m)
    assert [(v["name"], v["betrag"]) for v in plan["vortraege"]] == [("Versicherung A", -300.0)]


def test_mehrere_teilzahlungen_decken_eine_rate(db):
    m = _mandant(db)
    _miete(db, m)            # Fälligkeiten 01.06., 01.07., 03.08.
    _zahlung(db, m, date(2026, 6, 1), "1000")
    _zahlung(db, m, date(2026, 6, 10), "500")      # Juni in zwei Teilen
    _zahlung(db, m, date(2026, 7, 1), "1500")
    plan = _plan(db, m, start=date(2026, 6, 1), wochen=12)
    assert [(v["faellig"], v["betrag"]) for v in plan["vortraege"]] == [("2026-08-03", -1500.0)]


def test_vorauszahlung_deckt_folgerate(db):
    m = _mandant(db)
    _miete(db, m)
    _zahlung(db, m, date(2026, 6, 1), "3000")      # Juni + Juli im Voraus
    plan = _plan(db, m, start=date(2026, 6, 1), wochen=12)
    assert [(v["faellig"], v["betrag"]) for v in plan["vortraege"]] == [("2026-08-03", -1500.0)]


def test_ausgefallene_rate_bleibt_ihrer_faelligkeit_zugeordnet(db):
    """Juni nicht gezahlt, Juli pünktlich: vorgetragen wird die Juni-Rate
    (die Juli-Zahlung gehört betragsgleich zu ihrer eigenen Fälligkeit)."""
    m = _mandant(db)
    _miete(db, m)
    _zahlung(db, m, date(2026, 7, 1), "1500")
    _zahlung(db, m, date(2026, 8, 3), "1500")
    plan = _plan(db, m, start=date(2026, 6, 1), wochen=12)
    assert [(v["faellig"], v["betrag"]) for v in plan["vortraege"]] == [("2026-06-01", -1500.0)]


def test_vortrag_trifft_auf_planbuchung_desselben_tags(db):
    m = _mandant(db)
    _miete(db, m)
    db.add(models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="Hausverwaltung",
                                faellig_am=date(2026, 8, 6), betrag_brutto=Decimal("200"),
                                konto_id=_konto(db, m, "4210").id))
    zeile = _zeile(_plan(db, m))
    # Plan und Vortrag bleiben getrennt ausgewiesen; die Oberfläche addiert
    # und schraffiert die Zelle
    assert zeile["plan"][ZIEL] == -200.0
    assert zeile["vortrag"][ZIEL] == -1500.0


def test_kein_vortrag_ausserhalb_des_fensters(db):
    m = _mandant(db)
    _miete(db, m)
    # Fenster liegt komplett in der Zukunft: keine fälligen Raten im Fenster
    assert berechne_plan(db, m, start=date(2026, 9, 7), wochen=4, heute=HEUTE)["vortraege"] == []


def test_export_gruppenzeile_enthaelt_vortrag(db):
    from app.services.export import _zeilen_struktur, _zellwert

    m = _mandant(db)
    _miete(db, m)
    plan = _plan(db, m)
    gruppe = next(d for typ, name, d in _zeilen_struktur(plan) if name == "Raumkosten")
    assert _zellwert(gruppe, [ZIEL], plan["heute"], "vortrag") == (-1500.0, True, False)
    assert _zellwert(gruppe, [ZIEL], plan["heute"], "auto") == (None, False, False)
