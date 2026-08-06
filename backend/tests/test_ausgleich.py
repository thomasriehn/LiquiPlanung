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


def test_ausgleich_und_aufhebung(db):
    m = _mandant(db)
    p = _posten(db, m, "DEBITOR", "Kunde B", "250.00")
    u = _umsatz(db, m, "250.00", partner="Kunde B")
    db.commit()
    anzahl = gleiche_aus(db, m, [(u.id, p.id)])
    db.commit()
    assert anzahl == 1
    assert p.status == "BEZAHLT"
    assert p.bezahlt_am == u.buchungstag
    assert u.posten_id == p.id
    # doppelter Ausgleich wird abgelehnt
    try:
        gleiche_aus(db, m, [(u.id, p.id)])
        raise AssertionError("erwartete ValueError")
    except ValueError:
        db.rollback()
    hebe_auf(db, m, u.id)
    db.commit()
    assert p.status == "OFFEN"
    assert p.bezahlt_am is None
    assert u.posten_id is None
