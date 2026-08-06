from datetime import date
from decimal import Decimal

from app.services.zahlungskalender import steuertermine, sv_faelligkeit


def test_sv_faelligkeit_veroeffentlichte_termine():
    # Veröffentlichte SV-Fälligkeiten (drittletzter Bankarbeitstag)
    assert sv_faelligkeit(2025, 1, "NW") == date(2025, 1, 29)
    assert sv_faelligkeit(2025, 12, "NW") == date(2025, 12, 23)  # 24.+31.12. zählen nicht
    assert sv_faelligkeit(2026, 5, "NW") == date(2026, 5, 27)
    assert sv_faelligkeit(2026, 12, "NW") == date(2026, 12, 28)


def test_ust_va_monat_mit_dauerfrist():
    termine = steuertermine(date(2026, 8, 1), date(2026, 10, 31), "NW", "MONAT", dauerfrist=True)
    ust = [t for t in termine if t.typ == "UST_VA"]
    # Juli-VA mit Dauerfrist: 10.09.2026 (Donnerstag)
    assert any(t.datum == date(2026, 9, 10) and "07/2026" in t.beschreibung for t in ust)


def test_ust_va_quartal_ohne_dauerfrist():
    termine = steuertermine(date(2026, 10, 1), date(2026, 10, 31), "NW", "QUARTAL", dauerfrist=False)
    ust = [t for t in termine if t.typ == "UST_VA"]
    assert len(ust) == 1
    # Q3-VA: 10.10.2026 ist Samstag -> Montag 12.10.2026
    assert ust[0].datum == date(2026, 10, 12)


def test_lst_verschiebung_wochenende():
    termine = steuertermine(date(2026, 1, 1), date(2026, 1, 31), "NW", "MONAT", False)
    lst = [t for t in termine if t.typ == "LST"]
    # LSt Dezember 2025: 10.01.2026 ist Samstag -> 12.01.2026
    assert any(t.datum == date(2026, 1, 12) and "12/2025" in t.beschreibung for t in lst)


def test_ust_zahllast_aus_budget(db):
    from sqlalchemy import select

    from app import models
    from app.services.kontenrahmen import lege_kontenrahmen_an
    from app.services.zahlungskalender import (
        _periode_monate,
        generiere_termine,
        ust_zahllast_aus_budget,
    )

    assert _periode_monate("2026-05") == [(2026, 5)]
    assert _periode_monate("2026-Q3") == [(2026, 7), (2026, 8), (2026, 9)]

    m = models.Mandant(name="T", kurzname="t", kontenrahmen="SKR03", bundesland="NW")
    db.add(m)
    db.flush()
    lege_kontenrahmen_an(db, m)

    def konto(nr):
        return db.scalar(select(models.Konto).where(
            models.Konto.mandant_id == m.id, models.Konto.nummer == nr))

    # Erlöse 10.000 (19 %) -> USt 1.900; Wareneingang 4.000 (19 %) -> VSt 760;
    # Miete ohne USt-Satz bleibt außen vor -> Zahllast 1.140
    db.add(models.Budget(mandant_id=m.id, konto_id=konto("8400").id, jahr=2026,
                         monat=8, betrag_netto=Decimal("10000")))
    db.add(models.Budget(mandant_id=m.id, konto_id=konto("3400").id, jahr=2026,
                         monat=8, betrag_netto=Decimal("4000")))
    db.add(models.Budget(mandant_id=m.id, konto_id=konto("4210").id, jahr=2026,
                         monat=8, betrag_netto=Decimal("9000")))
    db.commit()

    assert ust_zahllast_aus_budget(db, m, [(2026, 8)]) == Decimal("1140.00")
    assert ust_zahllast_aus_budget(db, m, [(2026, 1)]) is None  # keine Budgets

    # Regel auf BUDGET -> generierter USt-Termin für August trägt die Zahllast
    regel = db.scalar(select(models.TerminRegel).where(
        models.TerminRegel.mandant_id == m.id, models.TerminRegel.typ == "UST_VA"))
    regel.betrag_modus = "BUDGET"
    regel.betrag_fix = Decimal("777")  # Fallback für Monate ohne Budget
    db.commit()
    generiere_termine(db, m, date(2026, 8, 3), date(2026, 11, 1), heute=date(2026, 8, 5))
    termine = {
        t.periode: t.betrag
        for t in db.scalars(select(models.Zahlungstermin).where(
            models.Zahlungstermin.mandant_id == m.id,
            models.Zahlungstermin.typ == "UST_VA"))
    }
    assert termine["2026-08"] == Decimal("1140.00")
    assert termine["2026-07"] == Decimal("777.00")  # kein Budget -> Fallback


def test_gewst_und_kst_termine():
    termine = steuertermine(date(2026, 8, 1), date(2026, 12, 31), "NW", "MONAT", False)
    gewst = [t.datum for t in termine if t.typ == "GEWST"]
    kst = [t.datum for t in termine if t.typ == "KST"]
    # 15.08.2026 ist Samstag -> 17.08.2026
    assert date(2026, 8, 17) in gewst
    assert date(2026, 11, 16) in gewst  # 15.11.2026 ist Sonntag -> Montag 16.11.
    assert date(2026, 9, 10) in kst
    assert date(2026, 12, 10) in kst
