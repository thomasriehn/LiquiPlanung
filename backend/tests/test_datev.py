from datetime import date
from decimal import Decimal

from app.services.datev import parse_automatisch, parse_bwa_csv, parse_datev, parse_generisches_csv

EXTF_KOPF = (
    '"EXTF";700;21;"Buchungsstapel";12;20260801000000000;;"RE";"";"";1234567;40001;'
    "20260101;4;20260701;20260731;\"Juli 2026\";\"\";1;0;0;\"EUR\""
)
SPALTEN = (
    "Umsatz (ohne Soll/Haben-Kz);Soll/Haben-Kennzeichen;WKZ Umsatz;Kurs;Basisumsatz;"
    "WKZ Basisumsatz;Konto;Gegenkonto (ohne BU-Schlüssel);BU-Schlüssel;Belegdatum;"
    "Belegfeld 1;Belegfeld 2;Skonto;Buchungstext"
)


def _extf(zeilen: list[str]) -> bytes:
    return ("\n".join([EXTF_KOPF, SPALTEN] + zeilen)).encode("cp1252")


def test_datev_buchungsstapel():
    daten = _extf(
        [
            '1.190,00;"S";"EUR";;;;1200;8400;;1507;"RE-100";;;"Zahlungseingang Müller"',
            '595,00;"H";"EUR";;;;3400;1200;;3007;"ER-55";;;"Zahlung Großhandel"',
        ]
    )
    erg = parse_datev(daten)
    assert erg.format == "DATEV"
    assert len(erg.buchungen) == 2
    b1, b2 = erg.buchungen
    assert b1.datum == date(2026, 7, 15)
    assert b1.betrag == Decimal("1190.00")
    assert b1.sh == "S"
    assert b1.konto_nr == "1200"
    assert b1.gegenkonto_nr == "8400"
    assert b1.text == "Zahlungseingang Müller"
    assert b2.datum == date(2026, 7, 30)
    assert b2.sh == "H"


def test_datev_jahreswechsel_wirtschaftsjahr():
    # WJ-Beginn 01.01.2026, Zeitraum Dezember: Belegdatum 0501 im Januar -> 2027
    kopf = EXTF_KOPF.replace("20260701", "20261201").replace("20260731", "20270131")
    daten = ("\n".join([kopf, SPALTEN, '100,00;"S";;;;;4920;1200;;0501;;;;"Telefon"'])).encode("cp1252")
    erg = parse_datev(daten)
    assert erg.buchungen[0].datum == date(2027, 1, 5)


def test_datev_automatische_erkennung():
    erg = parse_automatisch("export.csv", _extf(['10,00;"S";;;;;1000;8400;;0107;;;;"Bar"']))
    assert erg.format == "DATEV"


def test_generisches_csv():
    text = (
        "Datum;Konto;Gegenkonto;Betrag;SH;Belegfeld;Text\n"
        "15.07.2026;1200;8400;1190,00;S;RE-1;Zahlungseingang\n"
        "2026-07-16;4210;1200;-8900,00;;;Miete\n"
    )
    erg = parse_generisches_csv(text.encode("utf-8"))
    assert not erg.warnungen
    assert len(erg.buchungen) == 2
    assert erg.buchungen[0].datum == date(2026, 7, 15)
    assert erg.buchungen[0].sh == "S"
    # negativer Betrag ohne SH-Spalte -> Haben auf dem Konto
    assert erg.buchungen[1].sh == "H"
    assert erg.buchungen[1].betrag == Decimal("8900.00")


def test_bwa_csv():
    text = "Konto;Jahr;Monat;Betrag\n8400;2026;6;98.500,00\n4110;2026;6;-27.300,50\n"
    erg = parse_bwa_csv(text.encode("utf-8"))
    assert len(erg.bwa_werte) == 2
    assert erg.bwa_werte[0]["betrag"] == Decimal("98500.00")
    assert erg.bwa_werte[1]["betrag"] == Decimal("-27300.50")
