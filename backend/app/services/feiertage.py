"""Gesetzliche Feiertage je Bundesland und Bankarbeitstage.

Bankarbeitstage: Montag bis Freitag ohne gesetzliche Feiertage; der 24.12. und
31.12. gelten für Zwecke der SV-Fälligkeit nicht als Bankarbeitstage.
"""

from datetime import date, timedelta
from functools import lru_cache

BUNDESLAENDER = {
    "BW": "Baden-Württemberg",
    "BY": "Bayern",
    "BE": "Berlin",
    "BB": "Brandenburg",
    "HB": "Bremen",
    "HH": "Hamburg",
    "HE": "Hessen",
    "MV": "Mecklenburg-Vorpommern",
    "NI": "Niedersachsen",
    "NW": "Nordrhein-Westfalen",
    "RP": "Rheinland-Pfalz",
    "SL": "Saarland",
    "SN": "Sachsen",
    "ST": "Sachsen-Anhalt",
    "SH": "Schleswig-Holstein",
    "TH": "Thüringen",
}


@lru_cache(maxsize=64)
def ostersonntag(jahr: int) -> date:
    """Gauß'sche Osterformel (erweitert nach Lichtenberg)."""
    k = jahr // 100
    m = 15 + (3 * k + 3) // 4 - (8 * k + 13) // 25
    s = 2 - (3 * k + 3) // 4
    a = jahr % 19
    d = (19 * a + m) % 30
    r = (d + a // 11) // 29
    og = 21 + d - r
    sz = 7 - (jahr + jahr // 4 + s) % 7
    oe = 7 - (og - sz) % 7
    tage_ab_maerz = og + oe  # Ostersonntag als "März-Datum"
    return date(jahr, 3, 1) + timedelta(days=tage_ab_maerz - 1)


@lru_cache(maxsize=256)
def feiertage(jahr: int, bundesland: str = "NW") -> frozenset[date]:
    os_ = ostersonntag(jahr)
    ft: set[date] = {
        date(jahr, 1, 1),                 # Neujahr
        os_ - timedelta(days=2),          # Karfreitag
        os_ + timedelta(days=1),          # Ostermontag
        date(jahr, 5, 1),                 # Tag der Arbeit
        os_ + timedelta(days=39),         # Christi Himmelfahrt
        os_ + timedelta(days=50),         # Pfingstmontag
        date(jahr, 10, 3),                # Tag der Deutschen Einheit
        date(jahr, 12, 25),
        date(jahr, 12, 26),
    }
    bl = bundesland.upper()
    if bl in {"BW", "BY", "ST"}:
        ft.add(date(jahr, 1, 6))          # Heilige Drei Könige
    if bl in {"BE"} or (bl == "MV" and jahr >= 2023):
        ft.add(date(jahr, 3, 8))          # Internationaler Frauentag
    if bl in {"BW", "BY", "HE", "NW", "RP", "SL"}:
        ft.add(os_ + timedelta(days=60))  # Fronleichnam
    if bl in {"SL", "BY"}:
        ft.add(date(jahr, 8, 15))         # Mariä Himmelfahrt (BY: nur kath. Gemeinden)
    if bl == "TH":
        ft.add(date(jahr, 9, 20))         # Weltkindertag
    if bl in {"BB", "MV", "SN", "ST", "TH"} or (
        bl in {"HB", "HH", "NI", "SH"} and jahr >= 2018
    ):
        ft.add(date(jahr, 10, 31))        # Reformationstag
    if bl in {"BW", "BY", "NW", "RP", "SL"}:
        ft.add(date(jahr, 11, 1))         # Allerheiligen
    if bl == "SN":
        # Buß- und Bettag: Mittwoch vor dem 23.11.
        d = date(jahr, 11, 22)
        while d.weekday() != 2:
            d -= timedelta(days=1)
        ft.add(d)
    return frozenset(ft)


def ist_feiertag(d: date, bundesland: str = "NW") -> bool:
    return d in feiertage(d.year, bundesland)


def ist_bankarbeitstag(d: date, bundesland: str = "NW") -> bool:
    if d.weekday() >= 5:
        return False
    if (d.month, d.day) in {(12, 24), (12, 31)}:
        return False
    return not ist_feiertag(d, bundesland)


def ist_werktag(d: date, bundesland: str = "NW") -> bool:
    """Werktag i. S. d. Fälligkeitsverschiebung: Mo-Fr, kein Feiertag."""
    return d.weekday() < 5 and not ist_feiertag(d, bundesland)


def naechster_werktag(d: date, bundesland: str = "NW") -> date:
    while not ist_werktag(d, bundesland):
        d += timedelta(days=1)
    return d


def naechster_bankarbeitstag(d: date, bundesland: str = "NW") -> date:
    while not ist_bankarbeitstag(d, bundesland):
        d += timedelta(days=1)
    return d


def bankarbeitstage_im_monat(jahr: int, monat: int, bundesland: str = "NW") -> list[date]:
    d = date(jahr, monat, 1)
    tage = []
    while d.month == monat:
        if ist_bankarbeitstag(d, bundesland):
            tage.append(d)
        d += timedelta(days=1)
    return tage
