#!/usr/bin/env python3
"""Erzeugt einen vollständigen, in sich konsistenten Testdatensatz für LiquiPlanung.

Fiktiver Mandant: Nordlicht Möbelwerk GmbH (SKR03, vorläufiges Insolvenzverfahren).
Erzeugt werden – relativ zu einem frei wählbaren Stichtag („heute“):

  01–03  DATEV-Buchungsstapel (EXTF) der letzten drei Monate mit Buchungsstoff
         auf allen liquiditätswirksamen SKR03-Konten der Vorlage
  04     BWA-/Saldenliste (CSV, 12 Monate) für die Historienschätzung
  05     OP-Liste (CSV) mit ~20 offenen Posten inkl. zweier Altverbindlichkeiten
         vor dem Insolvenz-Stichtag (automatische § 38-Einstufung beim Import)
  06     Kontoauszug MT940 (drei Banktage) – Beträge passen zu den offenen
         Posten: exakte Zahlung, Teilzahlung, Sammelüberweisung, Skonto
  07     Kontoauszug CAMT.053 (Folgetag) mit zwei weiteren Zahlungen
  UEBERSICHT.md mit allen Parametern, Salden und erwarteten Ergebnissen

Alle Salden sind durchgerechnet: Der Anfangssaldo des MT940-Auszugs ist genau
der Banksaldo nach allen Buchungen; die Kassen-/Warenbestände für die manuelle
Erfassung stehen in der UEBERSICHT. Nur Standardbibliothek, keine Abhängigkeiten.

Aufruf:  python testdaten/erzeuge_testdaten.py [--stichtag JJJJ-MM-TT] [--ziel PFAD]
"""

import argparse
import calendar
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

IBAN = "DE02120300000000202051"
BANK_START = Decimal("165000.00")   # Banksaldo zu Beginn des ersten Buchungsmonats
KASSE_START = Decimal("640.00")
WARENBESTAND = Decimal("68500.00")

D = Decimal


def g(betrag: Decimal) -> str:
    """Betrag im deutschen Format ohne Tausenderpunkt: 1234,56"""
    return f"{betrag:.2f}".replace(".", ",")


def werktag_nach(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def werktag_vor(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def drittletzter_werktag(jahr: int, monat: int) -> date:
    d = date(jahr, monat, calendar.monthrange(jahr, monat)[1])
    werktage = []
    while len(werktage) < 3:
        if d.weekday() < 5:
            werktage.append(d)
        d -= timedelta(days=1)
    return werktage[2]


def monatstag(jahr: int, monat: int, tag: int) -> date:
    """Kalendertag, auf den nächsten Werktag verschoben (Zahlungspraxis)."""
    tag = min(tag, calendar.monthrange(jahr, monat)[1])
    return werktag_nach(date(jahr, monat, tag))


def monat_zurueck(jahr: int, monat: int, schritte: int) -> tuple[int, int]:
    idx = jahr * 12 + (monat - 1) - schritte
    return idx // 12, idx % 12 + 1


MONATSNAMEN = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
               "August", "September", "Oktober", "November", "Dezember"]


# --------------------------------------------------------------- Buchungsstoff

class Kassenbuch:
    """Sammelt Buchungssätze und führt Bank- und Kassensaldo mit."""

    def __init__(self) -> None:
        self.zeilen: list[tuple[date, str, str, Decimal, str, str, str]] = []
        self.bank = BANK_START
        self.kasse = KASSE_START

    def _add(self, datum, konto, gegen, betrag, beleg, text):
        self.zeilen.append((datum, konto, gegen, betrag, "S", beleg, text))
        for seite, vorzeichen in ((konto, 1), (gegen, -1)):
            if seite == "1200":
                self.bank += vorzeichen * betrag
            elif seite == "1000":
                self.kasse += vorzeichen * betrag

    def bank_ein(self, datum, konto, betrag, text, beleg=""):
        self._add(datum, "1200", konto, D(betrag), beleg, text)

    def bank_aus(self, datum, konto, betrag, text, beleg=""):
        self._add(datum, konto, "1200", D(betrag), beleg, text)

    def kasse_ein(self, datum, konto, betrag, text):
        self._add(datum, "1000", konto, D(betrag), "", text)

    def kasse_aus(self, datum, konto, betrag, text):
        self._add(datum, konto, "1000", D(betrag), "", text)

    def transit(self, datum, betrag):
        self._add(datum, "1200", "1000", D(betrag), "", "Bareinzahlung Kasse")


# Zahlungseingänge Debitoren (Tag, Betrag, Kunde, Beleg) je Monatsindex 0..2
DEBITOREN = [
    [(4, "21420.00", "Hotel Seeblick GmbH", "RE-2026-0951"),
     (11, "17850.00", "Möbelhaus Kern GmbH", "RE-2026-0963"),
     (18, "14280.00", "Objekt Living AG", "RE-2026-0970"),
     (25, "11900.00", "Küchenstudio Lund", "RE-2026-0978")],
    [(4, "19040.00", "Hotel Seeblick GmbH", "RE-2026-0989"),
     (11, "16660.00", "Ferienpark Ostsee GmbH", "RE-2026-0994"),
     (18, "13090.00", "Möbelhaus Kern GmbH", "RE-2026-1003"),
     (25, "9520.00", "Objekt Living AG", "RE-2026-1011")],
    [(4, "16070.00", "Küchenstudio Lund", "RE-2026-1019"),
     (11, "13685.00", "Hotel Seeblick GmbH", "RE-2026-1024"),
     (18, "10710.00", "Tischlerei Matz", "RE-2026-1030"),
     (25, "8925.00", "Möbelhaus Kern GmbH", "RE-2026-1036")],
]

# Lieferantenzahlungen Wareneingang 19 % (Konto 3400)
LIEFERANTEN = [
    [(6, "12480.00", "Holz Petersen GmbH", "RE-P-77501"),
     (13, "9520.00", "Lackierwerk Brandt GmbH", "RE-L-4302"),
     (21, "7140.00", "Beschläge Nielsen KG", "RE-N-1188")],
    [(6, "10710.00", "Holz Petersen GmbH", "RE-P-77644"),
     (13, "8330.00", "Lackierwerk Brandt GmbH", "RE-L-4361"),
     (21, "5950.00", "Beschläge Nielsen KG", "RE-N-1240")],
    [(6, "8925.00", "Holz Petersen GmbH", "RE-P-77719"),
     (13, "7735.00", "Beschläge Nielsen KG", "RE-N-1301"),
     (21, "4760.00", "Furnierhandel Asmussen", "RE-A-2210")],
]

# je Monat gleichbleibende Zahlungen: (Tag, Konto, Betrag, Text)
MONATLICH = [
    (1, "4210", "8900.00", "Miete Werk und Buero Gewerbepark Flensburg"),
    (5, "4360", "1180.00", "Versicherungspaket Betrieb"),
    (5, "4530", "380.00", "Tankrechnung Fuhrpark"),
    (7, "4930", "290.00", "Buerobedarf Boie"),
    (8, "4230", "480.00", "Heizoel Abschlag"),
    (9, "3300", "2140.00", "Wareneingang 7 Prozent Stoffe"),
    (12, "4240", "1350.00", "Stadtwerke Flensburg Abschlag"),
    (14, "4250", "620.00", "Unterhaltsreinigung Blitz GmbH"),
    (15, "2100", "640.00", "Darlehenszinsen Nordbank"),
    (15, "0630", "2100.00", "Tilgung Darlehen Nordbank"),
    (16, "3100", "3570.00", "Fremdleistung Oberflaechenveredelung"),
    (17, "4900", "1240.00", "Frachten Spedition Voss"),
    (19, "4920", "410.00", "TeleNord Telefon und Internet"),
    (22, "4530", "380.00", "Tankrechnung Fuhrpark"),
    (23, "1600", "3800.00", "Zahlung Verbindlichkeitenkonto Sammler"),
    (24, "4955", "980.00", "Buchfuehrung Steuerberatung Petersen"),
]

# einmalige Zahlungen: (Monatsindex, Tag, Konto, Betrag, Text)
EINMALIG = [
    (0, 6, "0420", "1850.00", "Bueroeinrichtung Empfang"),
    (0, 12, "0320", "4500.00", "Anzahlung Transporter"),
    (0, 13, "4600", "750.00", "Anzeigen Fachzeitschrift"),
    (0, 21, "4670", "420.00", "Reisekosten Messe Hamburg"),
    (0, 26, "0210", "6900.00", "Ersatzspindel CNC-Fraese"),
    (0, 27, "4650", "240.00", "Bewirtung Kundentermin"),
    (1, 10, "4380", "340.00", "IHK-Beitrag Halbjahr"),
    (1, 16, "4800", "1450.00", "Reparatur Absauganlage"),
    (1, 21, "4670", "310.00", "Reisekosten Kundendienst"),
    (1, 24, "4130", "750.00", "Nachzahlung SV-Pruefung"),
    (1, 25, "4950", "2500.00", "Sanierungsberatung Kanzlei Feddersen"),
    (2, 9, "4950", "4800.00", "Sanierungsberatung Kanzlei Feddersen"),
    (2, 21, "4805", "380.00", "Reparatur Druckerstrasse"),
]

# einmalige Einzahlungen: (Monatsindex, Tag, Konto, Betrag, Text, Beleg)
EINMALIG_EIN = [
    (1, 16, "2700", "2400.00", "Versicherungserstattung Wasserschaden", ""),
    (1, 23, "8120", "6500.00", "ZE Moebler Aalborg Export Daenemark", "RE-2026-1008"),
    (2, 10, "8200", "1200.00", "Erloes Verkauf Altmaschine", ""),
    (2, 20, "8300", "1980.00", "ZE Tischlerei Matz Zuschnitt", "RE-2026-1032"),
]

# Steuern/Personal je Monatsindex: (USt-VA, LSt, SV, Löhne, Gehälter)
ABGABEN = [
    ("6240.00", "5120.00", "9860.00", "16500.00", "11200.00"),
    ("6980.00", "5080.00", "9740.00", "16500.00", "11200.00"),
    ("6510.00", "5190.00", "9812.00", "16500.00", "11200.00"),
]


def erzeuge_buchungen(monate: list[tuple[int, int]], buch_bis: date) -> Kassenbuch:
    kb = Kassenbuch()

    def add_aus(datum, konto, betrag, text, beleg=""):
        if datum <= buch_bis:
            kb.bank_aus(datum, konto, betrag, text, beleg)

    def add_ein(datum, konto, betrag, text, beleg=""):
        if datum <= buch_bis:
            kb.bank_ein(datum, konto, betrag, text, beleg)

    for idx, (jahr, monat) in enumerate(monate):
        for tag, betrag, kunde, beleg in DEBITOREN[idx]:
            add_ein(monatstag(jahr, monat, tag), "8400", betrag, f"ZE {kunde}", beleg)
        add_ein(monatstag(jahr, monat, 20), "1400",
                ["8500.00", "7200.00", "6100.00"][idx], "ZE Sammelkonto Forderungen")
        for tag, betrag, lieferant, beleg in LIEFERANTEN[idx]:
            add_aus(monatstag(jahr, monat, tag), "3400", betrag,
                    f"Zahlung {lieferant}", beleg)
        for tag, konto, betrag, text in MONATLICH:
            add_aus(monatstag(jahr, monat, tag), konto, betrag, text)

        ust, lst, sv, loehne, gehaelter = ABGABEN[idx]
        zehnter = monatstag(jahr, monat, 10)
        vj, vm = monat_zurueck(jahr, monat, 1)
        uj, um = monat_zurueck(jahr, monat, 2)   # Dauerfristverlängerung
        add_aus(zehnter, "1780", ust, f"USt-VA {um:02d}/{uj} Dauerfrist")
        add_aus(zehnter, "1741", lst, f"LSt {vm:02d}/{vj}")
        add_aus(drittletzter_werktag(jahr, monat), "1742", sv,
                f"SV-Beitraege {monat:02d}/{jahr}")
        lohnlauf = werktag_vor(date(jahr, monat, 28))
        add_aus(lohnlauf, "4110", loehne, f"Lohnlauf {monat:02d}/{jahr}")
        add_aus(lohnlauf, "4120", gehaelter, f"Gehaltslauf {monat:02d}/{jahr}")
        add_aus(werktag_vor(date(jahr, monat, 28)), "4970", "85.00",
                "Kontofuehrung Nordbank")

        # kalendergebundene Zahlungen (Quartals-/Jahrestermine)
        if monat in (2, 5, 8, 11):
            add_aus(monatstag(jahr, monat, 15), "4320", "2850.00",
                    f"GewSt-Vorauszahlung Q{(monat + 1) // 3}/{jahr}")
        if monat in (3, 6, 9, 12):
            add_aus(monatstag(jahr, monat, 10), "2200", "1600.00",
                    f"KSt-Vorauszahlung {jahr}")
        if monat == 5:
            add_aus(monatstag(jahr, monat, 20), "4138", "1900.00",
                    f"Berufsgenossenschaft Umlage {jahr - 1}")
        if monat in (1, 7):
            add_aus(monatstag(jahr, monat, 11), "4540", "890.00",
                    "Kfz-Versicherung Halbjahr")

        # Kasse: Barverkäufe freitags, Bareinzahlung zur Bank montags
        letzter = calendar.monthrange(jahr, monat)[1]
        for t in range(1, letzter + 1):
            d = date(jahr, monat, t)
            if d > buch_bis:
                break
            if d.weekday() == 4:
                kb.kasse_ein(d, "8300", D("760.00") + idx * 30 + (t % 3) * 15,
                             "Barverkauf Werksverkauf")
            if d.weekday() == 0 and kb.kasse >= D("1600.00"):
                kb.transit(d, "800.00")

    for idx, tag, konto, betrag, text in EINMALIG:
        jahr, monat = monate[idx]
        add_aus(monatstag(jahr, monat, tag), konto, betrag, text)
    for idx, tag, konto, betrag, text, beleg in EINMALIG_EIN:
        jahr, monat = monate[idx]
        add_ein(monatstag(jahr, monat, tag), konto, betrag, text, beleg)

    # kleine Barausgaben (Kassenbuchungen auf Aufwandskonten)
    j1, m1 = monate[1]
    if date(j1, m1, 11) <= buch_bis:
        kb.kasse_aus(monatstag(j1, m1, 11), "4930", "45.00", "Briefmarken bar")
    j2, m2 = monate[2]
    if date(j2, m2, 16) <= buch_bis:
        kb.kasse_aus(monatstag(j2, m2, 16), "4650", "96.50", "Bewirtung bar")

    kb.zeilen.sort(key=lambda z: z[0])
    return kb


def schreibe_extf(pfad: Path, zeilen, jahr: int, monat: int) -> int:
    """DATEV-Buchungsstapel (EXTF) für einen Monat."""
    von = date(jahr, monat, 1)
    bis = date(jahr, monat, calendar.monthrange(jahr, monat)[1])
    kopf = (
        f'"EXTF";700;21;"Buchungsstapel";12;;;"";"";"";1;1;{jahr}0101;4;'
        f'{von:%Y%m%d};{bis:%Y%m%d};"{MONATSNAMEN[monat - 1]} {jahr}";"";1;0;0;"EUR"'
    )
    spalten = (
        "Umsatz (ohne Soll/Haben-Kz);Soll/Haben-Kennzeichen;WKZ Umsatz;Kurs;"
        "Basisumsatz;WKZ Basisumsatz;Konto;Gegenkonto (ohne BU-Schlüssel);"
        "BU-Schlüssel;Belegdatum;Belegfeld 1;Belegfeld 2;Skonto;Buchungstext"
    )
    saetze = [z for z in zeilen if z[0].year == jahr and z[0].month == monat]
    inhalt = [kopf, spalten]
    for datum, konto, gegen, betrag, sh, beleg, text in saetze:
        inhalt.append(
            f'{g(betrag)};"{sh}";;;;;{konto};{gegen};;{datum.day:02d}{datum.month:02d};'
            f'"{beleg}";;;"{text}"'
        )
    pfad.write_bytes(("\n".join(inhalt) + "\n").encode("cp1252"))
    return len(saetze)


# ----------------------------------------------------------------- BWA / OP

BWA_KONTEN = [
    ("8400", D("98500")), ("8300", D("7400")), ("3400", D("-41200")),
    ("3300", D("-2600")), ("3100", D("-4900")), ("4110", D("-16500")),
    ("4120", D("-11200")), ("4130", D("-6300")), ("4210", D("-8900")),
    ("4240", D("-1350")), ("4920", D("-410")), ("4930", D("-290")),
]


def schreibe_bwa(pfad: Path, heute: date) -> int:
    """Saldenliste der letzten 12 abgeschlossenen Monate mit fallendem Umsatztrend."""
    zeilen = ["Konto;Jahr;Monat;Betrag"]
    for i in range(12, 0, -1):
        jahr, monat = monat_zurueck(heute.year, heute.month, i)
        faktor = D("1.06") - D("0.025") * (12 - i)      # Krise: rückläufig
        wobble = D("1.00") + D("0.01") * ((i % 3) - 1)
        for konto, basis in BWA_KONTEN:
            skaliert = basis * faktor * wobble if konto.startswith(("8", "3")) else basis
            zeilen.append(f"{konto};{jahr};{monat};{g(skaliert.quantize(D('0.01')))}")
    pfad.write_text("\n".join(zeilen) + "\n", encoding="utf-8")
    return len(zeilen) - 1


def op_liste(heute: date) -> list[tuple]:
    """(Art, Partner, Beleg, Rechnungsdatum-Offset, Fällig-Offset, Betrag, Konto, Notiz).

    Die beiden Altrechnungen (Offsets −35/−49) liegen vor dem Insolvenz-Stichtag
    (heute − 21) und werden beim Import automatisch als § 38-Forderung eingestuft."""
    return [
        ("ER", "Holz Petersen GmbH", "RE-P-77812", -14, 14, "4165.00", "3400",
         "Eiche Massivholz Lieferung KW 30"),
        ("ER", "Holz Petersen GmbH", "RE-P-77903", -9, 21, "3570.00", "3400",
         "Beschläge und Leimholz"),
        ("ER", "Callsen IT-Service", "RE-IT-2201", -12, 8, "3000.00", "4805",
         "Wartungsvertrag Q3 – 3 % Skonto bei Zahlung binnen 10 Tagen"),
        ("ER", "Stadtwerke Flensburg", "A-2026-08", -6, 6, "1350.00", "4240",
         "Abschlag August Strom/Gas"),
        ("ER", "Spedition Voss KG", "RE-V-5531", -8, 10, "1240.00", "4900",
         "Frachten KW 30/31"),
        ("ER", "Kanzlei Dr. Feddersen", "RE-2026-0712", -4, 18, "4800.00", "4950",
         "Sanierungsberatung Juli"),
        ("ER", "Lackierwerk Brandt GmbH", "RE-L-4418", -35, -5, "12680.00", "3400",
         "Lackierung Serie Polaris – Altverbindlichkeit"),
        ("ER", "Maschinenfabrik Otte KG", "RE-M-2288", -49, -10, "7140.00", "4800",
         "Reparatur Kantenanleimer – Altverbindlichkeit"),
        ("ER", "Reinigung Blitz GmbH", "RE-BL-889", -7, 12, "620.00", "4250",
         "Unterhaltsreinigung Juli"),
        ("ER", "TeleNord GmbH", "R-88123401", -5, 9, "410.00", "4920",
         "Telefon und Internet Juli"),
        ("ER", "Autohaus Clausen", "RE-AC-3315", -10, 16, "760.00", "4530",
         "Inspektion Transporter"),
        ("ER", "Büro Boie", "RE-BB-190", -3, 11, "290.00", "4930",
         "Druckerpapier und Toner"),
        ("FO", "Möbelhaus Kern GmbH", "RE-2026-1041", -17, -3, "11900.00", "8400",
         "Küchenserie Polaris"),
        ("FO", "Objekt Living AG", "RE-2026-1042", -15, 5, "12495.00", "8400",
         "Objektausstattung Hotelprojekt"),
        ("FO", "Küchenstudio Lund", "RE-2026-1043", -11, 9, "8330.00", "8400",
         "Fronten und Arbeitsplatten"),
        ("FO", "Hotel Seeblick GmbH", "RE-2026-1044", -9, 16, "21420.00", "8400",
         "Zimmereinrichtung Bauabschnitt 2"),
        ("FO", "Ferienpark Ostsee GmbH", "RE-2026-1045", -6, 23, "9520.00", "8400",
         "Apartmentmöbel Serie Düne"),
        ("FO", "Tischlerei Matz", "RE-2026-1046", -4, 30, "2140.00", "8300",
         "Zuschnitt und Furnier"),
        ("FO", "Møbler Aalborg ApS", "RE-2026-1047", -2, 27, "6500.00", "8120",
         "Export Dänemark – steuerfrei § 4 Nr. 1a"),
        ("FO", "Wohnwelt Harms", "RE-2026-0988", -60, -30, "5950.00", "8400",
         "Ausstellungsstücke – überfällig, mahnen"),
    ]


def schreibe_op(pfad: Path, heute: date) -> int:
    zeilen = ["Art;Partner;Belegnummer;Rechnungsdatum;Faellig;Betrag;Konto;Notiz"]
    for art, partner, beleg, r_off, f_off, betrag, konto, notiz in op_liste(heute):
        rechnung = heute + timedelta(days=r_off)
        faellig = heute + timedelta(days=f_off)
        zeilen.append(
            f"{art};{partner};{beleg};{rechnung:%d.%m.%Y};{faellig:%d.%m.%Y};"
            f"{g(D(betrag))};{konto};{notiz}"
        )
    pfad.write_text("\n".join(zeilen) + "\n", encoding="utf-8")
    return len(zeilen) - 1


# ------------------------------------------------------------- Kontoauszüge

def bank_umsaetze(bank_tage: list[date]) -> list[tuple]:
    """(Tag, Betrag, Partner, Verwendungszweck) – abgestimmt auf die OP-Liste."""
    t1, t2, t3 = bank_tage
    return [
        (t1, D("11900.00"), "MOEBELHAUS KERN GMBH",
         "RE-2026-1041 Kuechenserie Polaris"),
        (t1, D("-8900.00"), "GEWERBEPARK FLENSBURG GMBH",
         "Miete August Halle 4"),
        (t2, D("-7735.00"), "HOLZ PETERSEN GMBH",
         "RE-P-77812 RE-P-77903 Sammelueberweisung"),
        (t2, D("1250.00"), "EC KARTENABRECHNUNG",
         "Kartenumsaetze Werksverkauf KW 31"),
        (t3, D("5000.00"), "OBJEKT LIVING AG",
         "RE-2026-1042 Abschlagszahlung"),
        (t3, D("-2910.00"), "CALLSEN IT SERVICE",
         "RE-IT-2201 abzgl 3 Prozent Skonto"),
        (t3, D("-42.50"), "NORDBANK AG",
         "Kontofuehrungsentgelt Juli"),
    ]


def camt_umsaetze(tag: date) -> list[tuple]:
    return [
        (tag, D("8330.00"), "Küchenstudio Lund",
         "RE-2026-1043 Fronten und Arbeitsplatten"),
        (tag, D("-1350.00"), "Stadtwerke Flensburg",
         "Abschlag August Strom Gas"),
    ]


def _mt940_saldo(kennzeichen: str, saldo: Decimal, datum: date) -> str:
    cd = "C" if saldo >= 0 else "D"
    return f":{kennzeichen}:{cd}{datum:%y%m%d}EUR{g(abs(saldo))}"


def schreibe_mt940(pfad: Path, anfang: Decimal, anfang_datum: date,
                   umsaetze: list[tuple]) -> Decimal:
    zeilen = [":20:NORDLICHT-AUSZUG", f":25:{IBAN}", ":28C:31/1",
              _mt940_saldo("60F", anfang, anfang_datum)]
    saldo = anfang
    for tag, betrag, partner, zweck in umsaetze:
        cd = "C" if betrag > 0 else "D"
        zeilen.append(f":61:{tag:%y%m%d}{tag:%m%d}{cd}{g(abs(betrag))}NTRFNONREF")
        zeilen.append(f":86:166?00UEBERWEISUNG?20SVWZ+{zweck}?32{partner}")
        saldo += betrag
    zeilen.append(_mt940_saldo("62F", saldo, umsaetze[-1][0]))
    zeilen.append("-")
    pfad.write_bytes(("\r\n".join(zeilen) + "\r\n").encode("cp1252"))
    return saldo


def schreibe_camt(pfad: Path, anfang: Decimal, anfang_datum: date,
                  umsaetze: list[tuple]) -> Decimal:
    saldo = anfang + sum(u[1] for u in umsaetze)
    tag = umsaetze[-1][0]

    def bal(code: str, wert: Decimal, datum: date) -> str:
        ind = "CRDT" if wert >= 0 else "DBIT"
        return (
            f'      <Bal><Tp><CdOrPrtry><Cd>{code}</Cd></CdOrPrtry></Tp>'
            f'<Amt Ccy="EUR">{abs(wert):.2f}</Amt><CdtDbtInd>{ind}</CdtDbtInd>'
            f"<Dt><Dt>{datum.isoformat()}</Dt></Dt></Bal>"
        )

    eintraege = []
    for buchungstag, betrag, partner, zweck in umsaetze:
        ind = "CRDT" if betrag > 0 else "DBIT"
        partei = "Dbtr" if betrag > 0 else "Cdtr"
        eintraege.append(f"""      <Ntry>
        <Amt Ccy="EUR">{abs(betrag):.2f}</Amt>
        <CdtDbtInd>{ind}</CdtDbtInd>
        <Sts>BOOK</Sts>
        <BookgDt><Dt>{buchungstag.isoformat()}</Dt></BookgDt>
        <ValDt><Dt>{buchungstag.isoformat()}</Dt></ValDt>
        <NtryDtls><TxDtls>
          <RltdPties><{partei}><Nm>{partner}</Nm></{partei}></RltdPties>
          <RmtInf><Ustrd>{zweck}</Ustrd></RmtInf>
        </TxDtls></NtryDtls>
      </Ntry>""")
    inhalt = f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">
  <BkToCstmrStmt>
    <GrpHdr><MsgId>NORDLICHT-{tag:%Y%m%d}</MsgId></GrpHdr>
    <Stmt>
      <Id>NORDLICHT-{tag:%Y%m%d}-1</Id>
      <ElctrncSeqNb>32</ElctrncSeqNb>
      <Acct><Id><IBAN>{IBAN}</IBAN></Id></Acct>
{bal("OPBD", anfang, anfang_datum)}
{bal("CLBD", saldo, tag)}
{chr(10).join(eintraege)}
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""
    pfad.write_text(inhalt, encoding="utf-8")
    return saldo


# ----------------------------------------------------------------- Übersicht

def schreibe_uebersicht(pfad: Path, heute: date, antrag: date, buch_bis: date,
                        monate, kb: Kassenbuch, dateien: dict,
                        mt940_ende: Decimal, camt_ende: Decimal,
                        bank_tage: list[date], camt_tag: date) -> None:
    ig_bis = antrag + timedelta(days=76)
    ig_von = ig_bis - timedelta(days=90)
    posten = op_liste(heute)
    er_summe = sum(D(p[5]) for p in posten if p[0] == "ER")
    fo_summe = sum(D(p[5]) for p in posten if p[0] == "FO")
    inso_summe = sum(
        D(p[5]) for p in posten
        if p[0] == "ER" and heute + timedelta(days=p[3]) < antrag
    )
    inhalt = f"""# Testdatensatz „Nordlicht Möbelwerk GmbH“ – Übersicht

Erzeugt mit `erzeuge_testdaten.py --stichtag {heute.isoformat()}`. Alle Werte sind
in sich konsistent durchgerechnet; die Schritt-für-Schritt-Anleitung steht in
[ANLEITUNG.md](../ANLEITUNG.md).

## Mandant (bei „Neuer Mandant“ eintragen)

| Feld | Wert |
|---|---|
| Name | Nordlicht Möbelwerk GmbH |
| Kurzname | nordlicht |
| Kontenrahmen | SKR03 |
| Bundesland | Schleswig-Holstein (SH) |
| USt-Zeitraum | Monat, **mit Dauerfristverlängerung** |
| Verfahrensstatus | Vorläufiges Verfahren |
| Aktenzeichen | 58 IN 71/26 (AG Flensburg) |
| Insolvenz-Stichtag (Antrag) | **{antrag:%d.%m.%Y}** |
| Insolvenzgeld (optional) | {ig_von:%d.%m.%Y} – {ig_bis:%d.%m.%Y} (erwartete Eröffnung ≈ {(ig_bis + timedelta(days=1)):%d.%m.%Y}) |

Bankkonto 1200: IBAN **{IBAN}** (unter „Konten“ eintragen, sonst
findet der Kontoauszugsimport das Konto nicht automatisch).

## Bestände (unter „Einstellungen → Bestände“ manuell erfassen)

| Typ | Konto | Datum | Wert |
|---|---|---|---|
| Kasse | 1000 | {(heute - timedelta(days=1)):%d.%m.%Y} | **{g(kb.kasse)} €** |
| Waren (nachrichtlich) | – | {(heute - timedelta(days=1)):%d.%m.%Y} | {g(WARENBESTAND)} € |

Der Bankbestand kommt automatisch aus den Kontoauszügen (Endsaldo je Auszug).

## Dateien und Salden

| Datei | Inhalt | Sätze |
|---|---|---|
"""
    for name, (beschreibung, anzahl) in dateien.items():
        inhalt += f"| `{name}` | {beschreibung} | {anzahl} |\n"
    inhalt += f"""
Buchhaltung erfasst bis **{buch_bis:%d.%m.%Y}** (bewusst ~1 Woche Rückstand –
die aktuelle Woche kommt nur über die Kontoauszüge herein, wie in der Praxis).

| Saldo | Wert |
|---|---|
| Bank zu Beginn ({date(monate[0][0], monate[0][1], 1):%d.%m.%Y}) | {g(BANK_START)} € |
| Bank nach Buchhaltung (= Anfangssaldo MT940, {werktag_vor(bank_tage[0] - timedelta(days=1)):%d.%m.%Y}) | **{g(kb.bank)} €** |
| Endsaldo MT940 ({bank_tage[-1]:%d.%m.%Y}) | **{g(mt940_ende)} €** |
| Endsaldo CAMT.053 ({camt_tag:%d.%m.%Y}) | **{g(camt_ende)} €** |
| Kasse ({buch_bis:%d.%m.%Y}) | {g(kb.kasse)} € |

## Offene Posten (Import der OP-Liste)

- 20 Posten: 12 Eingangsrechnungen ({g(er_summe)} €), 8 Forderungen ({g(fo_summe)} €).
- **2 Posten** (Lackierwerk Brandt, Maschinenfabrik Otte; Rechnungsdatum vor dem
  {antrag:%d.%m.%Y}) werden automatisch als **Insolvenzforderung (§ 38)** eingestuft
  und gesperrt: zusammen **{g(inso_summe)} €**.
- Wohnwelt Harms ({heute + timedelta(days=-30):%d.%m.%Y}) ist überfällig – die Planung rollt
  den Eingang automatisch auf den nächsten Bankarbeitstag.

## Erwartete Abgleichvorschläge (Posten → Bankabgleich)

| Bankumsatz | Betrag | Vorschlag | Art | Konfidenz |
|---|---|---|---|---|
| Möbelhaus Kern ({bank_tage[0]:%d.%m.}) | +11.900,00 | RE-2026-1041 | exakt | sicher |
| Holz Petersen ({bank_tage[1]:%d.%m.}) | −7.735,00 | RE-P-77812 + RE-P-77903 | Sammelüberweisung (2 Posten) | sicher |
| Objekt Living ({bank_tage[2]:%d.%m.}) | +5.000,00 | RE-2026-1042 | Teilzahlung (Rest 7.495,00) | möglich, nie vorausgewählt |
| Callsen IT ({bank_tage[2]:%d.%m.}) | −2.910,00 | RE-IT-2201 (3 % Skonto) | voll mit Skonto | sicher |
| Küchenstudio Lund ({camt_tag:%d.%m.}, CAMT) | +8.330,00 | RE-2026-1043 | exakt | sicher |
| Stadtwerke Flensburg ({camt_tag:%d.%m.}, CAMT) | −1.350,00 | A-2026-08 | exakt (ohne Beleg im Zweck) | sicher |

Ohne Vorschlag bleiben: Miete −8.900,00 (Dauerbuchung, kein OP),
Kartenumsätze +1.250,00, Kontoführung −42,50 – so soll es sein.
"""
    pfad.write_text(inhalt, encoding="utf-8")


# ----------------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stichtag", default="2026-08-06",
                        help="Planungs-„heute“ (JJJJ-MM-TT), Standard: 2026-08-06")
    parser.add_argument("--ziel", default=None,
                        help="Zielverzeichnis (Standard: nordlicht/ neben dem Skript)")
    args = parser.parse_args()

    heute = date.fromisoformat(args.stichtag)
    ziel = Path(args.ziel) if args.ziel else Path(__file__).parent / "nordlicht"
    ziel.mkdir(parents=True, exist_ok=True)

    antrag = heute - timedelta(days=21)
    buch_bis = heute - timedelta(days=7)
    monate = [monat_zurueck(buch_bis.year, buch_bis.month, i) for i in (2, 1, 0)]

    kb = erzeuge_buchungen(monate, buch_bis)

    dateien: dict[str, tuple[str, int]] = {}
    for i, (jahr, monat) in enumerate(monate, start=1):
        name = f"{i:02d}_buchungen_{jahr}-{monat:02d}_datev.csv"
        anzahl = schreibe_extf(ziel / name, kb.zeilen, jahr, monat)
        dateien[name] = (f"DATEV-Buchungsstapel {MONATSNAMEN[monat - 1]} {jahr}", anzahl)

    anzahl = schreibe_bwa(ziel / "04_bwa_saldenliste_12monate.csv", heute)
    dateien["04_bwa_saldenliste_12monate.csv"] = ("BWA-/Saldenliste 12 Monate", anzahl)

    anzahl = schreibe_op(ziel / "05_op_liste.csv", heute)
    dateien["05_op_liste.csv"] = ("OP-Liste (offene Posten)", anzahl)

    bank_tage = []
    d = heute - timedelta(days=1)
    while len(bank_tage) < 3:
        if d.weekday() < 5:
            bank_tage.append(d)
        d -= timedelta(days=1)
    bank_tage.reverse()
    anfang_datum = werktag_vor(bank_tage[0] - timedelta(days=1))

    mt940 = bank_umsaetze(bank_tage)
    mt940_ende = schreibe_mt940(ziel / "06_kontoauszug_mt940.sta", kb.bank,
                                anfang_datum, mt940)
    dateien["06_kontoauszug_mt940.sta"] = (
        f"Kontoauszug MT940 ({bank_tage[0]:%d.%m.}–{bank_tage[-1]:%d.%m.})", len(mt940))

    camt_tag = heute if heute.weekday() < 5 else werktag_nach(heute)
    camt = camt_umsaetze(camt_tag)
    camt_ende = schreibe_camt(ziel / "07_kontoauszug_camt053.xml", mt940_ende,
                              bank_tage[-1], camt)
    dateien["07_kontoauszug_camt053.xml"] = (
        f"Kontoauszug CAMT.053 ({camt_tag:%d.%m.})", len(camt))

    schreibe_uebersicht(ziel / "UEBERSICHT.md", heute, antrag, buch_bis, monate,
                        kb, dateien, mt940_ende, camt_ende, bank_tage, camt_tag)

    print(f"Testdatensatz in {ziel}/ erzeugt (Stichtag {heute.isoformat()}):")
    for name, (beschreibung, anzahl) in dateien.items():
        print(f"  {name:42s} {beschreibung} ({anzahl} Sätze)")
    print(f"  Bank nach Buchhaltung: {g(kb.bank)} €, nach Auszügen: {g(camt_ende)} €, "
          f"Kasse: {g(kb.kasse)} €")


if __name__ == "__main__":
    main()
