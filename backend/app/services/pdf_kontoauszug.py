"""Kontoumsätze aus PDF-Kontoauszügen.

PDF-Auszüge sind layout- statt datenorientiert; die Erkennung arbeitet daher
auf dem extrahierten Text. Unterstützte Formate (automatische Erkennung –
es gewinnt der Format-Parser mit den meisten erkannten Umsätzen):

- **Sparkassen-Verfahrenskonto-Report**: Blockbeginn mit Auszugsnummer,
  Auszugs- und Buchungsdatum; Empfänger und Betrag am Blockende.
- **Deutsche Bank Kontoauszug**: Blockbeginn mit Vorzeichen + Betrag (mit dem
  Vorgangstext verklebt), danach Empfänger, Buchungs-/Valutadatum (über zwei
  Zeilen umbrochen) und Verwendungszweck nach der Markerzeile.
- **UC eBanking prime „CAMT Auszüge“ (HypoVereinsbank/UniCredit)**:
  Blockbeginn mit Buchungs- und Valutadatum plus Buchungsart, danach
  Verwendungszweck, Empfänger, IBAN/BIC; der Betrag steht allein am Blockende.

Mehrzeilige Empfängernamen und Rauschen (Seitenköpfe, Adressblöcke,
Saldozeilen) werden über Heuristiken behandelt.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO


@dataclass
class PdfUmsatz:
    auszug_nr: str
    auszugsdatum: date
    buchungstag: date
    verwendungszweck: str
    empfaenger: str
    betrag: Decimal


@dataclass
class PdfErgebnis:
    umsaetze: list[PdfUmsatz] = field(default_factory=list)
    warnungen: list[str] = field(default_factory=list)


# Blockbeginn: Auszugsnummer + Auszugsdatum + Buchungsdatum (+ Beginn Zweck)
_START = re.compile(
    r"^(?P<nr>\d{3,10})\s+(?P<auszug>\d{2}\.\d{2}\.\d{4})\s+"
    r"(?P<buchung>\d{2}\.\d{2}\.\d{4})\s*(?P<rest>.*)$"
)
# Betrag am Zeilenende (Saldozeilen wie „38.228,56 +“ enden nicht auf den Betrag)
_BETRAG = re.compile(r"^(?P<text>.*?)\s*(?P<betrag>-?\d{1,3}(?:\.\d{3})*,\d{2})$")
_KOPFZEILEN = re.compile(
    r"^(Auszug-Nr\.|Seite \d|Erstellungsdatum|Abfragezeitraum|Anzahl gebuchte|"
    r"Startsaldo|Schlusssaldo|IBAN\b|Verfahrenskonto|Amtsgericht\b)"
)


def _datum(wert: str) -> date | None:
    try:
        return date(int(wert[6:10]), int(wert[3:5]), int(wert[0:2]))
    except ValueError:
        return None


def _betrag_wert(wert: str) -> Decimal | None:
    try:
        return Decimal(wert.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None


def _parse_verfahrenskonto(text: str) -> PdfErgebnis:
    """Sparkassen-Verfahrenskonto-Report."""
    erg = PdfErgebnis()
    bloecke: list[tuple[re.Match, list[str]]] = []
    zeilen_aktuell: list[str] | None = None
    for roh in text.splitlines():
        zeile = roh.strip()
        if not zeile:
            continue
        m = _START.match(zeile)
        if m:
            zeilen_aktuell = []
            bloecke.append((m, zeilen_aktuell))
            continue
        if zeilen_aktuell is not None and not _KOPFZEILEN.match(zeile):
            zeilen_aktuell.append(zeile)

    for m, zeilen in bloecke:
        auszugsdatum = _datum(m.group("auszug"))
        buchungstag = _datum(m.group("buchung"))
        if auszugsdatum is None or buchungstag is None:
            erg.warnungen.append(f"Umsatz {m.group('nr')}: Datum nicht lesbar – übersprungen.")
            continue
        # Betrag von hinten suchen: die letzte Zeile des Blocks trägt ihn
        betrag = None
        betrag_index = None
        betrag_text = ""
        for i in range(len(zeilen) - 1, -1, -1):
            mb = _BETRAG.match(zeilen[i])
            if mb:
                betrag = _betrag_wert(mb.group("betrag"))
                betrag_index = i
                betrag_text = mb.group("text").strip()
                break
        if betrag is None:
            erg.warnungen.append(
                f"Umsatz {m.group('nr')} vom {m.group('buchung')}: kein Betrag gefunden – übersprungen."
            )
            continue

        if betrag_text:
            empfaenger = betrag_text
            zweck_ende = betrag_index
        else:
            # Betrag steht allein: Empfänger ist die Zeile davor; bei umbrochenen
            # Namen (Einzelwort bzw. Vorzeile endet auf „-“) auch die Vorzeile
            empfaenger = zeilen[betrag_index - 1] if betrag_index >= 1 else ""
            zweck_ende = max(betrag_index - 1, 0)
            if betrag_index >= 2 and empfaenger and (
                " " not in empfaenger or zeilen[betrag_index - 2].endswith("-")
            ):
                empfaenger = f"{zeilen[betrag_index - 2]} {empfaenger}"
                zweck_ende = betrag_index - 2

        zweck_teile = [m.group("rest").strip()] + zeilen[:zweck_ende]
        zweck = " ".join(t for t in zweck_teile if t).strip()
        erg.umsaetze.append(
            PdfUmsatz(
                auszug_nr=m.group("nr"),
                auszugsdatum=auszugsdatum,
                buchungstag=buchungstag,
                verwendungszweck=zweck[:500],
                empfaenger=empfaenger[:255],
                betrag=betrag,
            )
        )
    return erg


# ------------------------------------------------ Deutsche Bank Kontoauszug

# Blockbeginn: Vorzeichen + Betrag, direkt verklebt mit dem Vorgangstext
# („+ 204,37SEPA Überweisung von“); Saldozeilen kleben am „EUR“ und sind keine
# Umsätze („+ 178.228,05EURDE62 …“)
_DB_START = re.compile(
    r"^(?P<vz>[+-])\s?(?P<betrag>\d{1,3}(?:\.\d{3})*,\d{2})(?P<vorgang>(?!EUR).*)$"
)
_DB_DATUM_TEIL = re.compile(r"^\d{2}\.\d{2}\.$")        # „08.07.“ (Jahr folgt separat)
_DB_DATUM_VOLL = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_DB_ZEITRAUM = re.compile(
    r"Kontoauszug vom \d{2}\.\d{2}\.\d{4} bis (\d{2}\.\d{2}\.\d{4})"
)
_DB_ZWECK_MARKER = "Verwendungszweck/ Kundenreferenz"
_DB_RAUSCHEN = re.compile(
    r"^(HabenSollVorgangValutaBuchung|IBANvonSeiteAuszug|Alter Saldo|Neuer Saldo|"
    r"Deutsche Bank AG|Service Center|Treuhandkonten|24h-Kundenservice|"
    r"Telefon \(|Herrn?$|Die abgerechneten|ausgewiesen ist|Umsatzsteuer ID|"
    r"Guthaben sind als|für den Einleger|Filiale\b|"
    r"[A-Z]{2}\d{2} \d{4} |\d+ / \d+ / \d+$)"
)
_IBAN_BIC_ZEILE = re.compile(r"^(IBAN [A-Z]{2}\d{2}|BIC [A-Z0-9]{8})")


def _parse_deutsche_bank(text: str) -> PdfErgebnis:
    erg = PdfErgebnis()
    auszugsdatum = None
    m = _DB_ZEITRAUM.search(text)
    if m:
        auszugsdatum = _datum(m.group(1))

    aktuell: dict | None = None

    def abschliessen() -> None:
        nonlocal aktuell
        if aktuell is None:
            return
        if aktuell["buchung"] is None:
            aktuell = None
            return
        zweck = " ".join(t for t in [aktuell["vorgang"]] + aktuell["zweck"] if t).strip()
        erg.umsaetze.append(
            PdfUmsatz(
                auszug_nr=aktuell["nr"],
                auszugsdatum=auszugsdatum or aktuell["buchung"],
                buchungstag=aktuell["buchung"],
                verwendungszweck=zweck[:500],
                empfaenger=" ".join(aktuell["partner"]).strip()[:255],
                betrag=aktuell["betrag"],
            )
        )
        aktuell = None

    zeilen = [z.strip() for z in text.splitlines() if z.strip()]
    i = 0
    while i < len(zeilen):
        zeile = zeilen[i]
        m = _DB_START.match(zeile)
        if m:
            abschliessen()
            betrag = _betrag_wert(m.group("betrag"))
            if betrag is None:
                i += 1
                continue
            if m.group("vz") == "-":
                betrag = -betrag
            aktuell = {
                "nr": "", "betrag": betrag, "vorgang": m.group("vorgang").strip(),
                "partner": [], "buchung": None, "zweck": [],
                "phase": "partner", "datumspaare": 0,
            }
            i += 1
            continue
        if aktuell is None or _DB_RAUSCHEN.match(zeile):
            if aktuell is not None and _DB_RAUSCHEN.match(zeile):
                # Seitenende/Adressblock: Rest des Blocks nicht mehr einsammeln
                aktuell["phase"] = "ende"
            i += 1
            continue
        # Datumsangaben: „08.07.“ + Folgezeile „2026“ oder komplett in einer Zeile
        datum = None
        if _DB_DATUM_TEIL.match(zeile) and i + 1 < len(zeilen) and re.match(r"^\d{4}$", zeilen[i + 1]):
            datum = _datum(zeile + zeilen[i + 1])
            i += 2
        elif _DB_DATUM_VOLL.match(zeile):
            datum = _datum(zeile)
            i += 1
        if datum is not None:
            if aktuell["buchung"] is None:
                aktuell["buchung"] = datum
            aktuell["datumspaare"] += 1
            aktuell["phase"] = "nach_datum"
            continue
        if zeile == _DB_ZWECK_MARKER:
            aktuell["phase"] = "zweck"
        elif aktuell["phase"] == "partner":
            aktuell["partner"].append(zeile)
        elif aktuell["phase"] == "zweck":
            aktuell["zweck"].append(zeile)
        elif aktuell["phase"] == "nach_datum" and not _IBAN_BIC_ZEILE.match(zeile):
            # z. B. Kartenumsätze ohne Verwendungszweck-Marker
            aktuell["zweck"].append(zeile)
        i += 1
    abschliessen()
    return erg


# --------------------------- UC eBanking prime „CAMT Auszüge“ (HVB/UniCredit)

# Blockbeginn: Buchungsdatum + Valuta + Buchungsart in einer Zeile
_UC_START = re.compile(
    r"^(?P<buchung>\d{2}\.\d{2}\.\d{4})\s+(?P<valuta>\d{2}\.\d{2}\.\d{4})\s+(?P<art>\D.*)$"
)
_UC_BETRAG = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$")   # Betrag allein auf der Zeile
_UC_IBAN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$")
_UC_BIC = re.compile(r"^[A-Z]{4}[A-Z0-9]{4}(?:[A-Z0-9]{3})?$")
_UC_AUSZUG_NR = re.compile(r"Auszug Nr\.\s*(\d+)")
_UC_AUSZUGSDATUM = re.compile(r"Kontoauszugsdatum\s*(\d{2}\.\d{2}\.\d{4})")


def _parse_uc_ebanking(text: str) -> PdfErgebnis:
    erg = PdfErgebnis()
    auszug_nr = ""
    auszugsdatum = None
    m = _UC_AUSZUG_NR.search(text)
    if m:
        auszug_nr = m.group(1)
    m = _UC_AUSZUGSDATUM.search(text)
    if m:
        auszugsdatum = _datum(m.group(1))

    aktuell: dict | None = None
    for roh in text.splitlines():
        zeile = roh.strip()
        if not zeile:
            continue
        m = _UC_START.match(zeile)
        if m:
            buchung = _datum(m.group("buchung"))
            if buchung is None:
                continue
            aktuell = {"buchung": buchung, "art": m.group("art").strip(), "zeilen": []}
            continue
        if aktuell is None:
            continue
        if _UC_BETRAG.match(zeile):
            betrag = _betrag_wert(zeile)
            if betrag is not None:
                inhalt = aktuell["zeilen"]
                # Empfänger steht vor der IBAN-Zeile; IBAN/BIC gehören nicht
                # in den Verwendungszweck
                iban_index = next(
                    (i for i, z in enumerate(inhalt) if _UC_IBAN.match(z)), None
                )
                if iban_index is not None and iban_index > 0:
                    empfaenger = inhalt[iban_index - 1]
                    zweck_zeilen = inhalt[: iban_index - 1]
                else:
                    uebrig = [z for z in inhalt if not _UC_IBAN.match(z) and not _UC_BIC.match(z)]
                    empfaenger = uebrig[-1] if uebrig else ""
                    zweck_zeilen = uebrig[:-1]
                zweck = " ".join([aktuell["art"]] + zweck_zeilen).strip()
                erg.umsaetze.append(
                    PdfUmsatz(
                        auszug_nr=auszug_nr,
                        auszugsdatum=auszugsdatum or aktuell["buchung"],
                        buchungstag=aktuell["buchung"],
                        verwendungszweck=zweck[:500],
                        empfaenger=empfaenger[:255],
                        betrag=betrag,
                    )
                )
            aktuell = None
            continue
        aktuell["zeilen"].append(zeile)
    return erg


# ----------------------------------------------------------------- Einstieg

def parse_text(text: str) -> PdfErgebnis:
    """Extrahierten PDF-Text in Umsätze zerlegen (separat testbar).

    Alle Format-Parser laufen über den Text; es gewinnt das Ergebnis mit den
    meisten erkannten Umsätzen."""
    kandidaten = [
        _parse_verfahrenskonto(text),
        _parse_deutsche_bank(text),
        _parse_uc_ebanking(text),
    ]
    beste = max(kandidaten, key=lambda e: len(e.umsaetze))
    if beste.umsaetze:
        return beste
    erg = PdfErgebnis()
    erg.warnungen.append(
        "Keine Umsätze erkannt – unterstützt werden Sparkassen-Verfahrenskonto-"
        "Reports, Deutsche-Bank-Kontoauszüge und UC-eBanking-prime-Auszüge "
        "(HypoVereinsbank/UniCredit). Für weitere Formate bitte ein Beispiel-PDF "
        "bereitstellen."
    )
    return erg


def parse_pdf(daten: bytes) -> PdfErgebnis:
    import logging

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # Font-Hinweise unterdrücken
    try:
        from pypdf import PdfReader

        leser = PdfReader(BytesIO(daten))
        text = "\n".join(seite.extract_text() or "" for seite in leser.pages)
    except Exception as e:  # defekte/verschlüsselte PDFs sauber melden
        erg = PdfErgebnis()
        erg.warnungen.append(f"PDF nicht lesbar: {e}")
        return erg
    return parse_text(text)
