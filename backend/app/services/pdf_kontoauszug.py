"""Kontoumsätze aus PDF-Kontoauszügen (z. B. Sparkassen-Verfahrenskonto-Reports).

PDF-Auszüge sind layout- statt datenorientiert; die Erkennung arbeitet daher
auf dem extrahierten Text. Eine Umsatzzeile beginnt mit Auszugsnummer,
Auszugsdatum und Buchungsdatum, danach folgen Verwendungszweck-Zeilen; den
Abschluss bilden Empfänger/Auftraggeber und der Betrag (deutsches Format,
Minus für Ausgaben). Mehrzeilige Empfängernamen (Umbruch der Spalte) werden
über Heuristiken zusammengesetzt.
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


def parse_text(text: str) -> PdfErgebnis:
    """Extrahierten PDF-Text in Umsätze zerlegen (separat testbar)."""
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
    if not erg.umsaetze and not erg.warnungen:
        erg.warnungen.append(
            "Keine Umsätze erkannt – erwartet werden Zeilen mit Auszugsnummer, "
            "Auszugs- und Buchungsdatum (z. B. Sparkassen-Verfahrenskonto-Report)."
        )
    return erg


def parse_pdf(daten: bytes) -> PdfErgebnis:
    try:
        from pypdf import PdfReader

        leser = PdfReader(BytesIO(daten))
        text = "\n".join(seite.extract_text() or "" for seite in leser.pages)
    except Exception as e:  # defekte/verschlüsselte PDFs sauber melden
        erg = PdfErgebnis()
        erg.warnungen.append(f"PDF nicht lesbar: {e}")
        return erg
    return parse_text(text)
