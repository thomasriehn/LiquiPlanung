from datetime import date

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


def test_gewst_und_kst_termine():
    termine = steuertermine(date(2026, 8, 1), date(2026, 12, 31), "NW", "MONAT", False)
    gewst = [t.datum for t in termine if t.typ == "GEWST"]
    kst = [t.datum for t in termine if t.typ == "KST"]
    # 15.08.2026 ist Samstag -> 17.08.2026
    assert date(2026, 8, 17) in gewst
    assert date(2026, 11, 16) in gewst  # 15.11.2026 ist Sonntag -> Montag 16.11.
    assert date(2026, 9, 10) in kst
    assert date(2026, 12, 10) in kst
