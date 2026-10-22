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

PDFs ohne Textebene (Scans, „Microsoft Print to PDF“ mit Glyphen als
Vektorpfaden) werden per Texterkennung (tesseract) gelesen, sofern auf dem
Server installiert. Für das UC-eBanking-Layout werden dabei die Spalten über
die Wortpositionen getrennt und die Umsätze gegen Anfangs-/Endsaldo geprüft.

**ZIP-Pakete** bündeln einen Kontoauszug mit den UC-eBanking-Postenlisten
der Sammelüberweisungen („SEPA Zahlung (CCT)“, mit angehängten Belegen): Die
Auszugszeile einer Sammelüberweisung wird in die Einzelzahlungen je Empfänger
aufgelöst, damit jede für sich einem Konto zugeordnet werden kann. Bucht die
Bank die Posten bereits einzeln, werden die Auszugszeilen den Posten
zugeordnet und mit deren Empfänger/Verwendungszweck ergänzt.
"""

import os
import re
import shutil
import subprocess
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import PurePosixPath


@dataclass
class PdfUmsatz:
    auszug_nr: str
    auszugsdatum: date
    buchungstag: date
    verwendungszweck: str
    empfaenger: str
    betrag: Decimal
    # aus einer Sammelüberweisung aufgelöst: Auftragsnummer der Postenliste und
    # (Empfänger, Zweck) der ursprünglichen Auszugszeile
    sammelauftrag: str = ""
    ursprung: tuple[str, str] | None = None
    iban: str = ""          # Gegenkonto laut Auszug (für den Postenlisten-Abgleich)


@dataclass
class PdfErgebnis:
    umsaetze: list[PdfUmsatz] = field(default_factory=list)
    warnungen: list[str] = field(default_factory=list)
    hinweise: list[str] = field(default_factory=list)


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


def _uc_iban(zeile: str) -> str:
    """IBAN einer Zeile „DE55…“ bzw. „KTO: DE55…“, sonst leer."""
    wert = re.sub(r"^(KTO|IBAN)\s*:\s*", "", zeile).replace(" ", "")
    return wert if _UC_IBAN.match(wert) else ""


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
                    (i for i, z in enumerate(inhalt) if _uc_iban(z)), None
                )
                if iban_index is not None and iban_index > 0:
                    empfaenger = inhalt[iban_index - 1]
                    zweck_zeilen = inhalt[: iban_index - 1]
                else:
                    uebrig = [z for z in inhalt if not _uc_iban(z) and not _UC_BIC.match(z)
                              and not _PARTNER_FELD.match(z)]
                    empfaenger = uebrig[-1] if uebrig else ""
                    zweck_zeilen = uebrig[:-1]
                zweck = (_sepa_zweck([aktuell["art"]] + zweck_zeilen)
                         or " ".join([aktuell["art"]] + zweck_zeilen).strip())
                erg.umsaetze.append(
                    PdfUmsatz(
                        auszug_nr=auszug_nr,
                        auszugsdatum=auszugsdatum or aktuell["buchung"],
                        buchungstag=aktuell["buchung"],
                        verwendungszweck=zweck[:500],
                        empfaenger=empfaenger[:255],
                        betrag=betrag,
                        iban=_uc_iban(inhalt[iban_index]) if iban_index is not None else "",
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


def _pdf_leser(daten: bytes):
    import logging

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # Font-Hinweise unterdrücken
    from pypdf import PdfReader

    return PdfReader(BytesIO(daten))


def _seitentexte(daten: bytes) -> list[str]:
    """Text je Seite (pypdf); wirft bei defekten/verschlüsselten PDFs."""
    return [seite.extract_text() or "" for seite in _pdf_leser(daten).pages]


def _ohne_textebene(seiten: list[str]) -> bool:
    return len("".join(seiten).strip()) < 30


def parse_pdf(daten: bytes) -> PdfErgebnis:
    try:
        seiten = _seitentexte(daten)
    except Exception as e:  # defekte/verschlüsselte PDFs sauber melden
        erg = PdfErgebnis()
        erg.warnungen.append(f"PDF nicht lesbar: {e}")
        return erg
    if _ohne_textebene(seiten):
        return _auszug_per_ocr(daten)
    return parse_text("\n".join(seiten))


def parse_datei(daten: bytes) -> PdfErgebnis:
    """Einstieg für Uploads: ZIP-Paket oder einzelnes PDF."""
    if daten[:4] == b"PK\x03\x04":
        return parse_zip(daten)
    return parse_pdf(daten)


# ------------------------------------------------- Texterkennung (tesseract)

OCR_AUFLOESUNG = 300        # dpi beim Rendern
OCR_MAX_SEITEN = 30


@dataclass
class OcrWort:
    text: str
    links: float            # relativ zur Seitenbreite (0..1)
    rechts: float


def ocr_verfuegbar() -> bool:
    if shutil.which("tesseract") is None:
        return False
    try:
        import pypdfium2  # noqa: F401
    except ImportError:
        return False
    return True


def _ocr_sprache() -> str:
    try:
        sprachen = subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, timeout=30
        ).stdout.decode()
    except (OSError, subprocess.SubprocessError):
        return "eng"
    return "deu" if "deu" in sprachen.split() else "eng"


_PDFIUM_SPERRE = threading.Lock()     # pdfium ist nicht threadsicher
OCR_PARALLEL = max(1, min(4, os.cpu_count() or 1))


def _tesseract_tsv(png: bytes, sprache: str) -> str:
    return subprocess.run(
        ["tesseract", "stdin", "stdout", "-l", sprache, "--psm", "6", "tsv"],
        input=png, capture_output=True, timeout=180,
        env={**os.environ, "OMP_THREAD_LIMIT": "1"},
    ).stdout.decode("utf-8", "replace")


def _ocr_zeilen(daten: bytes, ab: int = 0, bis: int = OCR_MAX_SEITEN) -> list[list[list[OcrWort]]]:
    """Seiten → Zeilen → Wörter (links→rechts) mit relativen x-Positionen."""
    import pypdfium2 as pdfium

    with _PDFIUM_SPERRE:
        dokument = pdfium.PdfDocument(daten)
        try:
            bilder = [
                dokument[nr].render(scale=OCR_AUFLOESUNG / 72, grayscale=True).to_pil()
                for nr in range(ab, min(bis, len(dokument)))
            ]
        finally:
            dokument.close()

    sprache = _ocr_sprache()
    pngs = []
    for bild in bilder:
        png = BytesIO()
        bild.save(png, "PNG")
        pngs.append(png.getvalue())
    # Seiten parallel erkennen, je Prozess ein Thread: tesseracts eigenes
    # OpenMP-Threading überbucht sich bei parallelen Prozessen massiv
    with ThreadPoolExecutor(max_workers=max(1, min(OCR_PARALLEL, len(pngs)))) as pool:
        tsvs = list(pool.map(lambda png: _tesseract_tsv(png, sprache), pngs))

    seiten = []
    for bild, tsv in zip(bilder, tsvs):
        breite = bild.size[0] or 1
        zeilen: dict[tuple, list[OcrWort]] = {}
        for roh in tsv.splitlines()[1:]:
            t = roh.split("\t")
            if len(t) < 12 or t[0] != "5" or not t[11].strip():
                continue
            links = int(t[6]) / breite
            zeilen.setdefault((int(t[2]), int(t[3]), int(t[4])), []).append(
                OcrWort(t[11].strip(), links, links + int(t[8]) / breite)
            )
        seiten.append([sorted(w, key=lambda x: x.links) for w in zeilen.values()])
    return seiten


def _zeilentext(woerter: list[OcrWort]) -> str:
    return " ".join(w.text for w in woerter)


def _auszug_per_ocr(daten: bytes) -> PdfErgebnis:
    if not ocr_verfuegbar():
        erg = PdfErgebnis()
        erg.warnungen.append(
            "Das PDF enthält keinen auslesbaren Text (Scan bzw. Druck als Grafik). "
            "Die dafür nötige Texterkennung (tesseract) ist auf dem Server nicht "
            "installiert – siehe Installationshinweise."
        )
        return erg
    try:
        seiten = _ocr_zeilen(daten, bis=1)
        erste = "\n".join(_zeilentext(z) for s in seiten for z in s)
        if not re.search(r"saldo|auszug", erste, re.I):
            # z. B. eingescannte Rechnung: Rest gar nicht erst erkennen
            erg = PdfErgebnis()
            erg.warnungen.append("Per Texterkennung keine Kontoauszugsdaten gefunden.")
            return erg
        seiten += _ocr_zeilen(daten, ab=1)
    except Exception as e:
        erg = PdfErgebnis()
        erg.warnungen.append(f"Texterkennung fehlgeschlagen: {e}")
        return erg
    erg = _parse_uc_ocr(seiten)
    if not erg.umsaetze:
        # andere Layouts: erkannten Fließtext durch die Text-Parser schicken
        erg = parse_text("\n".join(_zeilentext(z) for s in seiten for z in s))
    if erg.umsaetze:
        erg.hinweise.insert(0, "Kontoauszug per Texterkennung (OCR) gelesen.")
    return erg


# UC-eBanking-„CAMT Auszüge“ aus OCR-Wörtern: Spalten Datum | Valuta |
# Buchungsinformationen (Zweck links, Empfänger/IBAN/BIC rechts) | Umsatz
_OCR_DATUM = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_OCR_BETRAG = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$")
# Tabellenende bzw. Seitenfuß („… UC eBanking prime Gedruckt durch Benutzer …“)
_OCR_ENDE = re.compile(r"^Endsaldo\b|Gedruckt durch|^UC eBanking prime")
_SALDO_MUSTER = {
    "anfang": re.compile(r"Anfangss?aldo\s+(-?\d{1,3}(?:\.\d{3})*,\d{2})"),
    "ende": re.compile(r"Endsaldo\s+(-?\d{1,3}(?:\.\d{3})*,\d{2})"),
}
# Empfängerspalte: nach dem Namen folgen Felder wie „KTO: DE…“, „BANK: …“, „BREF: …“
_PARTNER_FELD = re.compile(r"^(KTO|IBAN|BANK|BIC|KREF|BREF|EREF|MREF|CRED)\s*:", re.I)
# SEPA-Felder im Buchungstext; OCR liest „SVWZ+“ gelegentlich als „SVWZH+“
_SEPA_FELD = re.compile(r"^(EREF|KREF|MREF|CRED|PURP|ABWA|ABWE|SVW[Z2]\w?)\+")
_SVWZ = re.compile(r"^SVW[Z2]\w?\+")
_DTA_ZEILE = 27     # Banken brechen Zweckzeilen nach 27 Zeichen mitten im Text um


def _sepa_zweck(zeilen: list[str]) -> str | None:
    """Buchungsart + eigentlicher Verwendungszweck (SVWZ+) ohne Referenz-
    felder (EREF+, KREF+ …); None, wenn der Text keine SEPA-Felder hat."""
    if len(zeilen) < 2 or not any(_SVWZ.match(z) for z in zeilen[1:]):
        return None
    text, im_zweck, vorige = "", False, ""
    for z in zeilen[1:]:
        if _SVWZ.match(z):
            text, im_zweck = z.split("+", 1)[1], True
        elif _SEPA_FELD.match(z):
            if im_zweck:
                break
        elif im_zweck:
            text += ("" if len(vorige) >= _DTA_ZEILE else " ") + z
        vorige = z
    return f"{zeilen[0]} {text}".strip()


def _parse_uc_ocr(seiten: list[list[list[OcrWort]]]) -> PdfErgebnis:
    erg = PdfErgebnis()
    volltext = "\n".join(_zeilentext(z) for s in seiten for z in s)
    m = _UC_AUSZUG_NR.search(volltext)
    auszug_nr = m.group(1) if m else ""
    m = _UC_AUSZUGSDATUM.search(volltext)
    auszugsdatum = _datum(m.group(1)) if m else None

    bloecke: list[dict] = []
    for zeilen in seiten:
        spalten = None          # je Seite aus der Kopfzeile der Umsatztabelle
        aktuell = None
        for woerter in zeilen:
            texte = [w.text for w in woerter]
            if "Valuta" in texte and any(t.startswith("Buchungsinfo") for t in texte):
                info = next(w for w in woerter if w.text.startswith("Buchungsinfo"))
                rechts = max(w.rechts for w in woerter)
                spalten = {
                    "info": info.links,
                    "rechts": rechts,
                    # Empfängerspalte beginnt gut ein Drittel der Strecke
                    # Buchungsinformationen → rechter Rand der Umsatzspalte
                    "partner": info.links + 0.33 * (rechts - info.links),
                }
                aktuell = None
                continue
            if spalten is None:
                continue
            if _OCR_ENDE.search(_zeilentext(woerter)):
                aktuell = None
                continue
            rest = list(woerter)
            betrag = None
            if rest and _OCR_BETRAG.match(rest[-1].text) and rest[-1].rechts >= spalten["rechts"] - 0.03:
                betrag = _betrag_wert(rest.pop().text)
            if (len(rest) >= 2 and _OCR_DATUM.match(rest[0].text) and _OCR_DATUM.match(rest[1].text)
                    and rest[1].links < spalten["info"]):
                buchung = _datum(rest[0].text)
                if buchung is None or betrag is None:
                    aktuell = None
                    continue
                aktuell = {"buchung": buchung, "betrag": betrag, "zweck": [], "partner": []}
                bloecke.append(aktuell)
                rest = rest[2:]
            elif aktuell is None:
                continue
            # Wörter rechts einer deutlichen Lücke ab der Empfängerspalte → Empfänger
            zweck, partner = [], []
            for i, w in enumerate(rest):
                luecke = w.links - rest[i - 1].rechts if i else 1.0
                if partner or (w.links >= spalten["partner"] - 0.01 and luecke > 0.015):
                    partner.append(w.text)
                else:
                    zweck.append(w.text)
            if zweck:
                aktuell["zweck"].append(" ".join(zweck))
            if partner:
                aktuell["partner"].append(" ".join(partner))

    for b in bloecke:
        empfaenger_zeilen = []
        for z in b["partner"]:
            if _PARTNER_FELD.match(z) or _UC_IBAN.match(z.replace(" ", "")) or _UC_BIC.match(z):
                break
            empfaenger_zeilen.append(z)
        iban = _iban_finden(" ".join(b["partner"]))
        erg.umsaetze.append(PdfUmsatz(
            auszug_nr=auszug_nr,
            auszugsdatum=auszugsdatum or b["buchung"],
            buchungstag=b["buchung"],
            verwendungszweck=(_sepa_zweck(b["zweck"]) or " ".join(b["zweck"]))[:500],
            empfaenger=" ".join(empfaenger_zeilen)[:255],
            betrag=b["betrag"],
            iban=iban[2] if iban else "",
        ))
    if erg.umsaetze:
        _salden_pruefen(volltext, erg)
    return erg


def _salden_pruefen(text: str, erg: PdfErgebnis) -> None:
    """Gegen OCR-Lesefehler: Umsatzsumme muss der Saldoveränderung entsprechen."""
    werte = {}
    for name, muster in _SALDO_MUSTER.items():
        m = muster.search(text)
        if m:
            werte[name] = _betrag_wert(m.group(1))
    summe = sum((u.betrag for u in erg.umsaetze), Decimal(0))
    if werte.get("anfang") is not None and werte.get("ende") is not None:
        soll = werte["ende"] - werte["anfang"]
        if soll == summe:
            erg.hinweise.append(
                f"Prüfsumme stimmt: {len(erg.umsaetze)} Umsätze ergeben die Saldoveränderung "
                f"von {_eur(werte['anfang'])} auf {_eur(werte['ende'])} €."
            )
        else:
            erg.warnungen.append(
                f"Prüfsumme weicht ab: Umsätze ergeben {_eur(summe)} €, laut Anfangs-/Endsaldo "
                f"wären es {_eur(soll)} € – mögliche Lesefehler der Texterkennung, bitte "
                "Beträge mit dem Auszug abgleichen."
            )
    else:
        erg.warnungen.append(
            "Anfangs-/Endsaldo nicht erkannt – die per Texterkennung gelesenen Beträge "
            "bitte mit dem Auszug abgleichen."
        )


def _eur(wert: Decimal) -> str:
    return f"{wert:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ------------------------------- UC-eBanking-Postenliste (Sammelüberweisung)

_IBAN_LAENGEN = {
    "AT": 20, "BE": 16, "BG": 22, "CH": 21, "CY": 28, "CZ": 24, "DE": 22, "DK": 18,
    "EE": 20, "ES": 24, "FI": 18, "FR": 27, "GB": 22, "GR": 27, "HR": 21, "HU": 28,
    "IE": 22, "IS": 26, "IT": 27, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27,
    "MT": 31, "NL": 18, "NO": 15, "PL": 28, "PT": 25, "RO": 24, "SE": 24, "SI": 19,
    "SK": 24, "SM": 27,
}
_IBAN_ANFANG = re.compile(r"\b([A-Z]{2})(\d{2})")
_BIC_WORT = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?$")


def _iban_gueltig(iban: str) -> bool:
    umgestellt = iban[4:] + iban[:4]
    try:
        return int("".join(str(int(c, 36)) for c in umgestellt)) % 97 == 1
    except ValueError:
        return False


def _iban_finden(text: str) -> tuple[int, int, str] | None:
    """Erste gültige IBAN (Prüfziffer) – auch mit Leerzeichen und über einen
    Zeilenumbruch hinweg geschrieben („DE52 7001 … 8228\\n02“)."""
    for m in _IBAN_ANFANG.finditer(text):
        laenge = _IBAN_LAENGEN.get(m.group(1))
        if not laenge:
            continue
        zeichen = [m.group(1), m.group(2)]
        i = m.end()
        anzahl = 4
        while anzahl < laenge and i < len(text):
            c = text[i]
            if c in " \n":
                i += 1
            elif c.isascii() and (c.isdigit() or c.isupper()):
                zeichen.append(c)
                anzahl += 1
                i += 1
            else:
                break
        iban = "".join(zeichen)
        if anzahl == laenge and _iban_gueltig(iban):
            return m.start(), i, iban
    return None


@dataclass
class SammelPosten:
    empfaenger: str
    iban: str
    verwendungszweck: str
    betrag: Decimal


@dataclass
class Sammelauftrag:
    auftragsnummer: str
    ausfuehrung: date | None
    posten: list[SammelPosten]
    gesamt: Decimal | None
    anzahl: int | None
    dateiname: str = ""


_PL_KOPF = re.compile(r"Verwendungszweck\s+Betrag\s+Währung")
_PL_ENDE = re.compile(r"Gesamtbetrag|Seite \d+ von")
_PL_BETRAG = re.compile(r"(?P<betrag>\d{1,3}(?:\.\d{3})*,\d{2}) (?P<waehrung>[A-Z]{3})[ \t]*$", re.M)
_PL_SUMME = re.compile(r"Posten\s*:\s*(\d+)\s+(\d{1,3}(?:\.\d{3})*,\d{2})")


def ist_postenliste(text: str) -> bool:
    return "Postenliste" in text and "SEPA Zahlung" in text


def parse_postenliste(seiten: list[str], dateiname: str = "") -> tuple[Sammelauftrag | None, list[str]]:
    """UC-eBanking-Postenliste einer SEPA-Sammelüberweisung auswerten.

    Erste Seite(n) = Liste; folgende Belegseiten (Rechnungen, Mails, Scans)
    werden ignoriert."""
    warnungen: list[str] = []
    liste_seiten = []
    for text in seiten:
        if not _PL_KOPF.search(text):
            if liste_seiten:
                break
            continue
        liste_seiten.append(text)
        if "Gesamtbetrag" in text:
            break
    if not liste_seiten:
        return None, [f"{dateiname}: Postenliste ohne Detailtabelle – übersprungen."]
    kopf = liste_seiten[0]
    m = re.search(r"Auftragsnummer\s+(\S+)", kopf)
    nummer = m.group(1) if m else dateiname
    m = re.search(r"Ausführungsdatum\s+(\d{2}\.\d{2}\.\d{4})", kopf)
    ausfuehrung = _datum(m.group(1)) if m else None
    gesamt = anzahl = None
    m = _PL_SUMME.search("\n".join(liste_seiten))
    if m:
        anzahl, gesamt = int(m.group(1)), _betrag_wert(m.group(2))

    posten: list[SammelPosten] = []
    for text in liste_seiten:
        start = _PL_KOPF.search(text).end()
        ende = _PL_ENDE.search(text, start)
        tabelle = text[start: ende.start() if ende else len(text)]
        position = 0
        for mb in _PL_BETRAG.finditer(tabelle):
            stueck = tabelle[position: mb.start()]
            position = mb.end()
            fund = _iban_finden(stueck)
            if fund is None:
                warnungen.append(f"Sammelauftrag {nummer}: Posten ohne erkennbare IBAN "
                                 f"({' '.join(stueck.split())[:60]} …) – übersprungen.")
                continue
            vor = stueck[: fund[0]].split()
            if vor and _BIC_WORT.match(vor[-1]):
                vor = vor[:-1]          # BIC-Spalte steht zwischen Empfänger und IBAN
            posten.append(SammelPosten(
                empfaenger=" ".join(vor)[:255],
                iban=fund[2],
                verwendungszweck=" ".join(stueck[fund[1]:].split())[:500],
                betrag=_betrag_wert(mb.group("betrag")),
            ))

    summe = sum((p.betrag for p in posten), Decimal(0))
    if gesamt is not None and (summe != gesamt or anzahl != len(posten)):
        warnungen.append(
            f"Sammelauftrag {nummer}: {len(posten)} Posten über {_eur(summe)} € erkannt, laut "
            f"Liste {anzahl} Posten über {_eur(gesamt)} € – bitte prüfen."
        )
    return Sammelauftrag(nummer, ausfuehrung, posten, gesamt, anzahl, dateiname), warnungen


# ----------------------------------------------------------------- ZIP-Paket

ZIP_MAX_DATEIEN = 100
ZIP_MAX_GROESSE = 200 * 1024 * 1024     # entpackt, gegen ZIP-Bomben
_POSTEN_ANZAHL = re.compile(r"POSTEN\s+(\d+)")


def parse_zip(daten: bytes) -> PdfErgebnis:
    erg = PdfErgebnis()
    try:
        archiv = zipfile.ZipFile(BytesIO(daten))
        eintraege = [
            e for e in archiv.infolist()
            if not e.is_dir() and not e.filename.startswith("__MACOSX/")
            and not PurePosixPath(e.filename).name.startswith(".")
        ]
    except zipfile.BadZipFile as e:
        erg.warnungen.append(f"ZIP-Datei nicht lesbar: {e}")
        return erg
    if len(eintraege) > ZIP_MAX_DATEIEN or sum(e.file_size for e in eintraege) > ZIP_MAX_GROESSE:
        erg.warnungen.append("ZIP-Datei zu groß (max. 100 Dateien bzw. 200 MB entpackt).")
        return erg

    auszuege: list[tuple[str, PdfErgebnis]] = []
    auftraege: list[Sammelauftrag] = []
    uebersprungen: list[str] = []
    for eintrag in sorted(eintraege, key=lambda e: e.filename):
        name = PurePosixPath(eintrag.filename).name
        inhalt = archiv.read(eintrag)
        if not inhalt.startswith(b"%PDF"):
            uebersprungen.append(name)
            continue
        # Belege können sehr umfangreich sein: Text seitenweise nur so weit
        # lesen, wie es für die Einordnung nötig ist
        try:
            leser = _pdf_leser(inhalt)
            seiten = [leser.pages[0].extract_text() or ""] if leser.pages else [""]
        except Exception as e:
            erg.warnungen.append(f"{name}: PDF nicht lesbar ({e}).")
            continue
        if ist_postenliste(seiten[0]):
            for seite in leser.pages[1:]:
                if "Gesamtbetrag" in seiten[-1]:
                    break
                seiten.append(seite.extract_text() or "")
            auftrag, warn = parse_postenliste(seiten, name)
            erg.warnungen.extend(warn)
            if auftrag is not None:
                auftraege.append(auftrag)
        elif _ohne_textebene(seiten):
            teil = _auszug_per_ocr(inhalt)
            if teil.umsaetze:
                auszuege.append((name, teil))
            elif not ocr_verfuegbar():
                erg.warnungen.extend(f"{name}: {w}" for w in teil.warnungen)
            else:
                uebersprungen.append(name)
        elif re.search(r"saldo|auszug", seiten[0], re.I):
            seiten += [s.extract_text() or "" for s in leser.pages[1:]]
            teil = parse_text("\n".join(seiten))
            if teil.umsaetze:
                auszuege.append((name, teil))
            else:
                uebersprungen.append(name)
        else:
            uebersprungen.append(name)   # Rechnung o. ä.

    for name, teil in auszuege:
        erg.umsaetze.extend(teil.umsaetze)
        erg.warnungen.extend(f"{name}: {w}" for w in teil.warnungen)
        erg.hinweise.extend(f"{name}: {h}" for h in teil.hinweise)
    _sammelauftraege_aufloesen(erg, auftraege, mit_auszug=bool(auszuege))
    if uebersprungen:
        erg.hinweise.append(
            "Nicht als Kontoauszug oder Postenliste erkannt (z. B. Belege) und daher "
            "nicht ausgewertet: " + ", ".join(uebersprungen)
        )
    if not erg.umsaetze and not erg.warnungen:
        erg.warnungen.append("Im ZIP wurde weder ein Kontoauszug noch eine Postenliste erkannt.")
    return erg


def _im_zeitfenster(auftrag: Sammelauftrag, u: PdfUmsatz) -> bool:
    return auftrag.ausfuehrung is None or (
        auftrag.ausfuehrung - timedelta(days=3) <= u.buchungstag
        <= auftrag.ausfuehrung + timedelta(days=10)
    )


def _woerter(text: str) -> set[str]:
    return set(re.findall(r"[A-Z0-9]+", text.upper()))


def _einzelbuchungen_abgleichen(erg: PdfErgebnis, auftrag: Sammelauftrag) -> int:
    """Posten eines Sammelauftrags, die als einzelne Auszugszeilen gebucht
    sind, über Betrag, Empfänger-IBAN und Verwendungszweck zuordnen; die
    Zeilen übernehmen Empfänger und Zweck aus der (textbasierten, also
    verlässlicheren) Postenliste. Gibt die Zahl der Treffer zurück."""
    treffer: dict[int, tuple[SammelPosten, bool]] = {}
    for p in auftrag.posten:
        beste = None
        for idx, u in enumerate(erg.umsaetze):
            if (idx in treffer or u.sammelauftrag or u.betrag != -p.betrag
                    or not _im_zeitfenster(auftrag, u) or (u.iban and u.iban != p.iban)):
                continue
            # bei gleichem Betrag und Empfänger (z. B. zwei Rechnungen desselben
            # Lieferanten) entscheidet die Übereinstimmung im Zweck
            punkte = len(_woerter(p.verwendungszweck) & _woerter(u.verwendungszweck))
            if beste is None or punkte > beste[0]:
                beste = (punkte, idx)
        if beste is not None:
            treffer[beste[1]] = (p, erg.umsaetze[beste[1]].iban == p.iban)
    if len(treffer) < len(auftrag.posten):
        # unvollständig: Betragsgleichheit allein könnte Zufall sein
        treffer = {idx: t for idx, t in treffer.items() if t[1]}
    for idx, (p, _) in treffer.items():
        zeile = erg.umsaetze[idx]
        erg.umsaetze[idx] = PdfUmsatz(
            auszug_nr=zeile.auszug_nr,
            auszugsdatum=zeile.auszugsdatum,
            buchungstag=zeile.buchungstag,
            verwendungszweck=p.verwendungszweck,
            empfaenger=p.empfaenger,
            betrag=zeile.betrag,
            sammelauftrag=auftrag.auftragsnummer,
            ursprung=(zeile.empfaenger, zeile.verwendungszweck),
            iban=p.iban,
        )
    return len(treffer)


def _sammelauftraege_aufloesen(erg: PdfErgebnis, auftraege: list[Sammelauftrag],
                               mit_auszug: bool) -> None:
    """Auszugszeilen von Sammelüberweisungen durch ihre Einzelposten ersetzen.

    Zuordnung über den Gesamtbetrag (exakt), die Postenanzahl („POSTEN n“ im
    Buchungstext, falls vorhanden) und die Nähe zum Ausführungsdatum."""
    aufgeloest = abgeglichene_zeilen = 0
    abgeglichen: list[str] = []
    for auftrag in auftraege:
        if not auftrag.posten:
            continue
        gesamt = auftrag.gesamt if auftrag.gesamt is not None else sum(
            (p.betrag for p in auftrag.posten), Decimal(0))
        kandidaten = []
        for idx, u in enumerate(erg.umsaetze):
            if u.sammelauftrag or u.betrag != -gesamt or not _im_zeitfenster(auftrag, u):
                continue
            m = _POSTEN_ANZAHL.search(u.verwendungszweck)
            if m and int(m.group(1)) != len(auftrag.posten):
                continue
            kandidaten.append((0 if m else 1, abs((u.buchungstag - (auftrag.ausfuehrung or u.buchungstag)).days), idx))
        if kandidaten:
            _, _, idx = min(kandidaten)
            zeile = erg.umsaetze[idx]
            erg.umsaetze[idx: idx + 1] = [
                PdfUmsatz(
                    auszug_nr=zeile.auszug_nr,
                    auszugsdatum=zeile.auszugsdatum,
                    buchungstag=zeile.buchungstag,
                    verwendungszweck=p.verwendungszweck,
                    empfaenger=p.empfaenger,
                    betrag=-p.betrag,
                    sammelauftrag=auftrag.auftragsnummer,
                    ursprung=(zeile.empfaenger, zeile.verwendungszweck),
                    iban=p.iban,
                )
                for p in auftrag.posten
            ]
            aufgeloest += 1
        elif mit_auszug:
            # manche Banken buchen die Posten eines Sammelauftrags einzeln
            treffer = _einzelbuchungen_abgleichen(erg, auftrag)
            if treffer:
                abgeglichen.append(auftrag.auftragsnummer)
                abgeglichene_zeilen += treffer
            if treffer < len(auftrag.posten):
                fehlend = len(auftrag.posten) - treffer
                erg.warnungen.append(
                    f"Sammelauftrag {auftrag.auftragsnummer} ({len(auftrag.posten)} Posten, "
                    f"{_eur(gesamt)} €): " + (
                        "steht nicht im Kontoauszug – nicht übernommen." if not treffer else
                        f"{fehlend} Posten nicht im Kontoauszug gefunden (die übrigen "
                        "wurden den Auszugszeilen zugeordnet)."
                    )
                )
        elif auftrag.ausfuehrung:
            # ZIP ohne (lesbaren) Auszug: Einzelposten zum Ausführungsdatum
            erg.umsaetze.extend(
                PdfUmsatz(
                    auszug_nr="",
                    auszugsdatum=auftrag.ausfuehrung,
                    buchungstag=auftrag.ausfuehrung,
                    verwendungszweck=p.verwendungszweck,
                    empfaenger=p.empfaenger,
                    betrag=-p.betrag,
                    sammelauftrag=auftrag.auftragsnummer,
                )
                for p in auftrag.posten
            )
            aufgeloest += 1
    if aufgeloest:
        anzahl = sum(1 for u in erg.umsaetze if u.sammelauftrag) - abgeglichene_zeilen
        erg.hinweise.append(
            f"{aufgeloest} Sammelüberweisung(en) aus den Postenlisten in {anzahl} "
            "Einzelzahlungen aufgelöst."
        )
    if abgeglichen:
        erg.hinweise.append(
            (f"Sammelauftrag {abgeglichen[0]} wurde" if len(abgeglichen) == 1 else
             f"Sammelaufträge {', '.join(abgeglichen)} wurden")
            + f" von der Bank einzeln gebucht: {abgeglichene_zeilen} Auszugszeilen mit "
            "Empfänger und Verwendungszweck aus der Postenliste abgeglichen."
        )
    if auftraege and not mit_auszug and aufgeloest:
        erg.warnungen.append(
            "Kein lesbarer Kontoauszug im ZIP – die Einzelzahlungen wurden mit dem "
            "Ausführungsdatum der Postenlisten übernommen (Bankbuchung ggf. einen Tag später)."
        )
