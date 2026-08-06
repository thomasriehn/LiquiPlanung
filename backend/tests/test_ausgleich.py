from datetime import date, timedelta
from decimal import Decimal

from app import models
from app.services.ausgleich import bewerte, gleiche_aus, hebe_auf, vorschlaege
from app.services.kontenrahmen import lege_kontenrahmen_an

HEUTE = date(2026, 8, 5)


def _mandant(db) -> models.Mandant:
    m = models.Mandant(name="Test GmbH", kurzname="test", kontenrahmen="SKR03")
    db.add(m)
    db.flush()
    lege_kontenrahmen_an(db, m)
    return m


def _bank_konto(db, m):
    from sqlalchemy import select

    return db.scalar(select(models.Konto).where(
        models.Konto.mandant_id == m.id, models.Konto.nummer == "1200"))


def _umsatz(db, m, betrag, partner=None, zweck=None, tag=HEUTE):
    u = models.BankTransaktion(
        mandant_id=m.id, konto_id=_bank_konto(db, m).id, buchungstag=tag,
        betrag=Decimal(betrag), partner=partner, verwendungszweck=zweck,
    )
    db.add(u)
    return u


def _posten(db, m, art, partner, betrag, belegnr=None, faellig=None):
    p = models.OffenerPosten(
        mandant_id=m.id, art=art, partner=partner, belegnr=belegnr,
        faellig_am=faellig or HEUTE + timedelta(days=5),
        betrag_brutto=Decimal(betrag),
    )
    db.add(p)
    return p


def test_sicherer_vorschlag_mit_belegnummer(db):
    m = _mandant(db)
    _posten(db, m, "DEBITOR", "Kunde Albrecht AG", "1190.00", belegnr="RE-100")
    _umsatz(db, m, "1190.00", partner="ALBRECHT AG", zweck="SVWZ RE-100 Teilzahlung")
    db.commit()
    v = vorschlaege(db, m)
    assert len(v) == 1
    assert v[0]["konfidenz"] == "SICHER"
    assert "Belegnummer im Verwendungszweck" in v[0]["gruende"]


def test_richtung_und_betrag_als_torwaechter(db):
    m = _mandant(db)
    kreditor = _posten(db, m, "KREDITOR", "Lieferant X", "500.00")
    eingang = _umsatz(db, m, "500.00", partner="Lieferant X")   # Eingang passt nicht zu ER
    weit_weg = _umsatz(db, m, "-999.00", partner="Lieferant X")  # Betrag zu weit weg
    db.commit()
    assert bewerte(eingang, kreditor) is None
    assert bewerte(weit_weg, kreditor) is None
    assert vorschlaege(db, m) == []


def test_skonto_als_moeglich(db):
    m = _mandant(db)
    _posten(db, m, "KREDITOR", "Grosshandel Nord GmbH", "1000.00")
    _umsatz(db, m, "-970.00", partner="Grosshandel Nord", zweck="Zahlung abzgl. Skonto")
    db.commit()
    v = vorschlaege(db, m)
    assert len(v) == 1
    assert v[0]["konfidenz"] == "MOEGLICH"
    assert any("Skonto" in g for g in v[0]["gruende"])


def test_eindeutige_zuordnung(db):
    m = _mandant(db)
    _posten(db, m, "DEBITOR", "Kunde A", "800.00", belegnr="RE-2026-001")
    _posten(db, m, "DEBITOR", "Kunde A", "800.00", belegnr="RE-2026-002")
    _umsatz(db, m, "800.00", partner="Kunde A", zweck="RE-2026-002 Ausgleich")
    db.commit()
    v = vorschlaege(db, m)
    assert len(v) == 1  # ein Umsatz -> genau ein Vorschlag
    assert v[0]["posten"]["belegnr"] == "RE-2026-002"  # Belegnummer entscheidet


def test_teilzahlung_mit_belegnummer(db):
    m = _mandant(db)
    p = _posten(db, m, "KREDITOR", "Grosshandel Nord GmbH", "1000.00", belegnr="RE-2026-777")
    u1 = _umsatz(db, m, "-400.00", partner="Grosshandel Nord", zweck="RE-2026-777 Abschlag 1")
    db.commit()
    v = vorschlaege(db, m)
    assert len(v) == 1
    assert v[0]["art"] == "TEIL"
    assert v[0]["konfidenz"] == "MOEGLICH"  # Teilzahlungen nie vorausgewählt
    assert v[0]["posten"]["rest_nach_zahlung"] == 600.0

    gleiche_aus(db, m, [(u1.id, [p.id])])
    db.commit()
    assert p.status == "OFFEN"              # Teilzahlung: Posten bleibt offen
    assert p.bezahlt_betrag == Decimal("400.00")

    # zweite Zahlung über den Rest -> Vollausgleich gegen den Restbetrag
    u2 = _umsatz(db, m, "-600.00", partner="Grosshandel Nord", zweck="RE-2026-777 Rest")
    db.commit()
    v = vorschlaege(db, m)
    assert len(v) == 1 and v[0]["art"] == "VOLL" and v[0]["konfidenz"] == "SICHER"
    gleiche_aus(db, m, [(u2.id, [p.id])])
    db.commit()
    assert p.status == "BEZAHLT"
    assert p.bezahlt_betrag == Decimal("1000.00")

    # Aufhebung der zweiten Zahlung öffnet den Posten mit Rest 600
    hebe_auf(db, m, u2.id)
    db.commit()
    assert p.status == "OFFEN"
    assert p.bezahlt_betrag == Decimal("400.00")


def test_teilzahlung_ohne_signal_kein_vorschlag(db):
    m = _mandant(db)
    _posten(db, m, "KREDITOR", "Grosshandel Nord GmbH", "1000.00")
    _umsatz(db, m, "-400.00", partner="Unbekannter Zahler")
    db.commit()
    assert vorschlaege(db, m) == []


def test_ueberzahlung_wird_abgelehnt(db):
    m = _mandant(db)
    p = _posten(db, m, "KREDITOR", "Lieferant X", "1000.00")
    u = _umsatz(db, m, "-1200.00", partner="Lieferant X")
    db.commit()
    assert bewerte(u, p) is None
    try:
        gleiche_aus(db, m, [(u.id, [p.id])])
        raise AssertionError("erwartete ValueError")
    except ValueError as e:
        assert "übersteigt" in str(e)
        db.rollback()


def test_ausgleich_und_aufhebung(db):
    m = _mandant(db)
    p = _posten(db, m, "DEBITOR", "Kunde B", "250.00")
    u = _umsatz(db, m, "250.00", partner="Kunde B")
    db.commit()
    anzahl = gleiche_aus(db, m, [(u.id, [p.id])])
    db.commit()
    assert anzahl == 1
    assert p.status == "BEZAHLT"
    assert p.bezahlt_am == u.buchungstag
    assert [z.posten_id for z in u.zuordnungen] == [p.id]
    # doppelter Ausgleich wird abgelehnt
    try:
        gleiche_aus(db, m, [(u.id, [p.id])])
        raise AssertionError("erwartete ValueError")
    except ValueError:
        db.rollback()
    hebe_auf(db, m, u.id)
    db.commit()
    assert p.status == "OFFEN"
    assert p.bezahlt_am is None
    assert u.zuordnungen == []


def test_sammelueberweisung_vorschlag_und_uebernahme(db):
    m = _mandant(db)
    p1 = _posten(db, m, "KREDITOR", "Spedition Weber", "2140.00", belegnr="FR-2026-11")
    p2 = _posten(db, m, "KREDITOR", "Spedition Weber", "1860.00", belegnr="FR-2026-12")
    # Störer: passt nicht in die Summe
    _posten(db, m, "KREDITOR", "Spedition Weber", "999.00")
    u = _umsatz(db, m, "-4000.00", partner="Spedition Weber",
                zweck="Sammelueberweisung FR-2026-11 FR-2026-12")
    db.commit()
    v = vorschlaege(db, m)
    sammel = [x for x in v if x["art"] == "SAMMEL"]
    assert len(sammel) == 1
    s = sammel[0]
    assert s["konfidenz"] == "SICHER"  # beide Belegnummern im Verwendungszweck
    assert sorted(p["id"] for p in s["posten_liste"]) == sorted([p1.id, p2.id])

    gleiche_aus(db, m, [(u.id, [p1.id, p2.id])])
    db.commit()
    assert p1.status == "BEZAHLT" and p2.status == "BEZAHLT"
    assert p1.bezahlt_betrag == Decimal("2140.00")
    assert p2.bezahlt_betrag == Decimal("1860.00")
    assert len(u.zuordnungen) == 2

    hebe_auf(db, m, u.id)
    db.commit()
    assert p1.status == "OFFEN" and p2.status == "OFFEN"
    assert p1.bezahlt_betrag == Decimal("0.00")


def test_sammelueberweisung_summe_muss_passen(db):
    m = _mandant(db)
    p1 = _posten(db, m, "KREDITOR", "Spedition Weber", "2140.00")
    p2 = _posten(db, m, "KREDITOR", "Spedition Weber", "1860.00")
    u = _umsatz(db, m, "-3500.00", partner="Spedition Weber")
    db.commit()
    assert [x for x in vorschlaege(db, m) if x["art"] == "SAMMEL"] == []
    try:
        gleiche_aus(db, m, [(u.id, [p1.id, p2.id])])
        raise AssertionError("erwartete ValueError")
    except ValueError as e:
        assert "ergeben nicht die Zahlung" in str(e)
        db.rollback()


def test_sammelueberweisung_nur_mit_signal(db):
    m = _mandant(db)
    _posten(db, m, "KREDITOR", "Alpha GmbH", "2140.00")
    _posten(db, m, "KREDITOR", "Beta GmbH", "1860.00")
    # Umsatz ohne Partner-/Belegbezug zu den Posten -> kein Sammel-Vorschlag
    _umsatz(db, m, "-4000.00", partner="Unbekannt", zweck="Zahlung")
    db.commit()
    assert vorschlaege(db, m) == []
