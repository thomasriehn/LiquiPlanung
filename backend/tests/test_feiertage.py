from datetime import date

from app.services.feiertage import (
    bankarbeitstage_im_monat,
    feiertage,
    ist_bankarbeitstag,
    naechster_werktag,
    ostersonntag,
)


def test_ostersonntag_bekannte_jahre():
    assert ostersonntag(2024) == date(2024, 3, 31)
    assert ostersonntag(2025) == date(2025, 4, 20)
    assert ostersonntag(2026) == date(2026, 4, 5)
    assert ostersonntag(2027) == date(2027, 3, 28)


def test_feiertage_nw_und_by():
    nw = feiertage(2026, "NW")
    assert date(2026, 6, 4) in nw          # Fronleichnam (Ostern + 60)
    assert date(2026, 11, 1) in nw         # Allerheiligen
    assert date(2026, 10, 31) not in nw    # Reformationstag nicht in NW
    by = feiertage(2026, "BY")
    assert date(2026, 1, 6) in by          # Heilige Drei Könige
    assert date(2026, 8, 15) in by         # Mariä Himmelfahrt
    sn = feiertage(2026, "SN")
    assert date(2026, 11, 18) in sn        # Buß- und Bettag (Mittwoch vor 23.11.)


def test_bankarbeitstage():
    assert not ist_bankarbeitstag(date(2026, 12, 24), "NW")  # Heiligabend
    assert not ist_bankarbeitstag(date(2026, 12, 31), "NW")  # Silvester
    assert not ist_bankarbeitstag(date(2026, 8, 8), "NW")    # Samstag
    assert not ist_bankarbeitstag(date(2026, 5, 1), "NW")    # Feiertag
    assert ist_bankarbeitstag(date(2026, 8, 5), "NW")        # Mittwoch
    august = bankarbeitstage_im_monat(2026, 8, "NW")
    assert august[0] == date(2026, 8, 3)
    assert august[-1] == date(2026, 8, 31)


def test_naechster_werktag_verschiebung():
    # 10.01.2026 ist ein Samstag -> Montag 12.01.
    assert naechster_werktag(date(2026, 1, 10), "NW") == date(2026, 1, 12)
    # Feiertag 01.05.2026 (Freitag) -> Montag 04.05.
    assert naechster_werktag(date(2026, 5, 1), "NW") == date(2026, 5, 4)
    # normaler Werktag bleibt
    assert naechster_werktag(date(2026, 8, 5), "NW") == date(2026, 8, 5)
