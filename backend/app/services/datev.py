"""Import von Buchungsstapeln.

Unterstützt:
- DATEV-Format (EXTF/DTVF, "Buchungsstapel"): Header mit Wirtschaftsjahresbeginn und
  Zeitraum, Spaltenzuordnung über Kopfzeile mit Positionsfallback, Belegdatum TTMM,
  Dezimalkomma, CP1252/UTF-8.
- Generisches CSV (z. B. aus Addison ableitbar): Kopfzeile mit den Spalten
  Datum;Konto;Gegenkonto;Betrag;SH;Belegfeld;Text (Reihenfolge frei, Namen tolerant).
- BWA-/Saldenliste: Konto;Jahr;Monat;Betrag
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation


@dataclass
class ImportBuchung:
    datum: date
    konto_nr: str
    gegenkonto_nr: str
    betrag: Decimal
    sh: str
    belegfeld: str | None = None
    text: str | None = None


@dataclass
class ImportErgebnis:
    format: str
    buchungen: list[ImportBuchung] = field(default_factory=list)
    bwa_werte: list[dict] = field(default_factory=list)
    posten: list[dict] = field(default_factory=list)
    warnungen: list[str] = field(default_factory=list)


def _dekodiere(daten: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return daten.decode(enc)
        except UnicodeDecodeError:
            continue
    return daten.decode("latin-1", errors="replace")


def _betrag(wert: str) -> Decimal:
    w = wert.strip().replace("\xa0", "").replace(" ", "")
    if not w:
        raise InvalidOperation("leer")
    # deutsches Format 1.234,56 und englisches Format 1,234.56:
    # das am weitesten rechts stehende Trennzeichen ist das Dezimaltrennzeichen
    if "," in w and "." in w:
        if w.rfind(",") > w.rfind("."):
            w = w.replace(".", "").replace(",", ".")
        else:
            w = w.replace(",", "")
    elif "," in w:
        w = w.replace(",", ".")
    return Decimal(w)


def _datum_flexibel(wert: str) -> date | None:
    w = wert.strip()
    try:
        m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})$", w)
        if m:
            t, mo, j = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if j < 100:
                j += 2000
            return date(j, mo, t)
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", w)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.match(r"^(\d{4})(\d{2})(\d{2})$", w)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return None


def _datev_belegdatum(wert: str, wj_beginn: date | None, zeitraum_von: date | None) -> date | None:
    """Belegdatum im Format TTMM (auch TMM); Jahr aus Zeitraum bzw. Wirtschaftsjahr."""
    w = re.sub(r"\D", "", wert.strip())
    if not w:
        return None
    if len(w) == 8:  # TTMMJJJJ kommt in manchen Exporten vor
        try:
            return date(int(w[4:8]), int(w[2:4]), int(w[0:2]))
        except ValueError:
            return None
    if len(w) == 3:
        w = "0" + w
    if len(w) != 4:
        return None
    tag, monat = int(w[:2]), int(w[2:])
    if not (1 <= monat <= 12 and 1 <= tag <= 31):
        return None
    # Jahr aus dem Wirtschaftsjahr (Headerfeld 13): TTMM liegt in [WJ-Beginn, +1 Jahr).
    # Der Exportzeitraum ist nur Rückfallebene (Monatsstapel enthalten regelmäßig
    # Belegdaten aus Vormonaten desselben Wirtschaftsjahres).
    anker = wj_beginn or zeitraum_von
    if anker is None:
        jahr = date.today().year
    elif monat >= anker.month:
        jahr = anker.year
    else:
        jahr = anker.year + 1
    try:
        return date(jahr, monat, tag)
    except ValueError:
        return None


def _yyyymmdd(wert: str) -> date | None:
    w = re.sub(r"\D", "", wert)
    if len(w) < 8:
        return None
    try:
        return date(int(w[:4]), int(w[4:6]), int(w[6:8]))
    except ValueError:
        return None


def _spalten_index(header: list[str], *muster: str) -> int | None:
    for i, name in enumerate(header):
        n = name.strip().strip('"').lower()
        for m in muster:
            if m in n:
                return i
    return None


def parse_datev(daten: bytes) -> ImportErgebnis:
    text = _dekodiere(daten)
    zeilen = [z for z in text.splitlines() if z.strip()]
    erg = ImportErgebnis(format="DATEV")
    if not zeilen:
        erg.warnungen.append("Leere Datei.")
        return erg

    meta = next(csv.reader([zeilen[0]], delimiter=";", quotechar='"'))
    wj_beginn = _yyyymmdd(meta[12]) if len(meta) > 12 else None
    zeitraum_von = _yyyymmdd(meta[14]) if len(meta) > 14 else None

    if len(zeilen) < 2:
        erg.warnungen.append("Kein Inhalt nach der Headerzeile.")
        return erg

    header = next(csv.reader([zeilen[1]], delimiter=";", quotechar='"'))
    i_umsatz = _spalten_index(header, "umsatz (ohne") or 0
    i_sh = _spalten_index(header, "soll/haben-kennzeichen")
    i_konto = None
    for i, name in enumerate(header):
        if name.strip().strip('"').lower() == "konto":
            i_konto = i
            break
    i_gegen = _spalten_index(header, "gegenkonto (ohne", "gegenkonto")
    i_datum = _spalten_index(header, "belegdatum")
    i_beleg1 = _spalten_index(header, "belegfeld 1")
    i_text = _spalten_index(header, "buchungstext")
    # Positionsfallback (DATEV-Format Buchungsstapel)
    if i_sh is None:
        i_sh = 1
    if i_konto is None:
        i_konto = 6
    if i_gegen is None:
        i_gegen = 7
    if i_datum is None:
        i_datum = 9
    if i_beleg1 is None:
        i_beleg1 = 10
    if i_text is None:
        i_text = 13

    for nr, zeile in enumerate(zeilen[2:], start=3):
        felder = next(csv.reader([zeile], delimiter=";", quotechar='"'))
        if len(felder) <= max(i_umsatz, i_sh, i_konto, i_gegen, i_datum):
            continue
        try:
            betrag = _betrag(felder[i_umsatz])
        except InvalidOperation:
            erg.warnungen.append(f"Zeile {nr}: Betrag nicht lesbar.")
            continue
        datum = _datev_belegdatum(felder[i_datum], wj_beginn, zeitraum_von)
        if datum is None:
            erg.warnungen.append(f"Zeile {nr}: Belegdatum nicht lesbar.")
            continue
        sh = felder[i_sh].strip().strip('"').upper() or "S"
        if sh not in ("S", "H"):
            sh = "S"
        konto = felder[i_konto].strip().strip('"')
        gegen = felder[i_gegen].strip().strip('"')
        if not konto or not gegen:
            erg.warnungen.append(f"Zeile {nr}: Konto/Gegenkonto fehlt.")
            continue
        erg.buchungen.append(
            ImportBuchung(
                datum=datum,
                konto_nr=konto,
                gegenkonto_nr=gegen,
                betrag=abs(betrag),
                sh=sh,
                belegfeld=(felder[i_beleg1].strip() or None) if len(felder) > i_beleg1 else None,
                text=(felder[i_text].strip() or None) if len(felder) > i_text else None,
            )
        )
    return erg


def parse_generisches_csv(daten: bytes) -> ImportErgebnis:
    text = _dekodiere(daten)
    erg = ImportErgebnis(format="CSV")
    delimiter = ";" if text.count(";") >= text.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, quotechar='"')
    zeilen = [z for z in reader if any(f.strip() for f in z)]
    if not zeilen:
        erg.warnungen.append("Leere Datei.")
        return erg
    header = [f.strip().lower() for f in zeilen[0]]
    i_datum = _spalten_index(header, "datum", "date")
    # "gegenkonto" enthält "konto" – Konto-Spalte gezielt ohne "gegen" suchen,
    # damit die Spaltenreihenfolge keine Rolle spielt
    i_konto = next(
        (i for i, name in enumerate(header)
         if ("konto" in name or "account" in name) and "gegen" not in name),
        None,
    )
    i_gegen = _spalten_index(header, "gegenkonto")
    i_betrag = _spalten_index(header, "betrag", "umsatz", "amount")
    i_sh = _spalten_index(header, "sh", "s/h", "soll/haben")
    i_beleg = next(
        (i for i, name in enumerate(header)
         if "beleg" in name and "datum" not in name),
        None,
    )
    i_text = _spalten_index(header, "text", "buchungstext", "verwendung")
    if i_datum is None or i_konto is None or i_betrag is None:
        erg.warnungen.append(
            "Kopfzeile nicht erkannt – benötigt mindestens: Datum, Konto, Betrag "
            "(optional Gegenkonto, SH, Belegfeld, Text)."
        )
        return erg
    for nr, felder in enumerate(zeilen[1:], start=2):
        if len(felder) <= max(i_datum, i_konto, i_betrag):
            continue
        datum = _datum_flexibel(felder[i_datum])
        if datum is None:
            erg.warnungen.append(f"Zeile {nr}: Datum nicht lesbar.")
            continue
        try:
            betrag = _betrag(felder[i_betrag])
        except InvalidOperation:
            erg.warnungen.append(f"Zeile {nr}: Betrag nicht lesbar.")
            continue
        sh = ""
        if i_sh is not None and len(felder) > i_sh:
            sh = felder[i_sh].strip().upper()
        if sh not in ("S", "H"):
            # Vorzeichenlogik: negativer Betrag = Haben auf dem Konto
            sh = "S" if betrag >= 0 else "H"
        konto = felder[i_konto].strip()
        gegen = felder[i_gegen].strip() if (i_gegen is not None and len(felder) > i_gegen) else ""
        if not konto:
            erg.warnungen.append(f"Zeile {nr}: Konto fehlt.")
            continue
        erg.buchungen.append(
            ImportBuchung(
                datum=datum,
                konto_nr=konto,
                gegenkonto_nr=gegen,
                betrag=abs(betrag),
                sh=sh,
                belegfeld=(felder[i_beleg].strip() or None) if (i_beleg is not None and len(felder) > i_beleg) else None,
                text=(felder[i_text].strip() or None) if (i_text is not None and len(felder) > i_text) else None,
            )
        )
    return erg


def parse_bwa_csv(daten: bytes) -> ImportErgebnis:
    text = _dekodiere(daten)
    erg = ImportErgebnis(format="BWA")
    delimiter = ";" if text.count(";") >= text.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, quotechar='"')
    zeilen = [z for z in reader if any(f.strip() for f in z)]
    if not zeilen:
        erg.warnungen.append("Leere Datei.")
        return erg
    header = [f.strip().lower() for f in zeilen[0]]
    i_konto = _spalten_index(header, "konto")
    i_jahr = _spalten_index(header, "jahr")
    i_monat = _spalten_index(header, "monat")
    i_betrag = _spalten_index(header, "betrag", "saldo", "wert")
    if None in (i_konto, i_jahr, i_monat, i_betrag):
        erg.warnungen.append("Kopfzeile nicht erkannt – benötigt: Konto, Jahr, Monat, Betrag.")
        return erg
    for nr, felder in enumerate(zeilen[1:], start=2):
        try:
            monat = int(felder[i_monat])
            jahr = int(felder[i_jahr])
            if not (1 <= monat <= 12) or not (1990 <= jahr <= 2100):
                raise ValueError
            erg.bwa_werte.append(
                {
                    "konto_nr": felder[i_konto].strip(),
                    "jahr": jahr,
                    "monat": monat,
                    "betrag": _betrag(felder[i_betrag]),
                }
            )
        except (ValueError, InvalidOperation, IndexError):
            erg.warnungen.append(f"Zeile {nr}: nicht lesbar.")
    return erg


_OP_ARTEN = {
    "ER": "KREDITOR", "KREDITOR": "KREDITOR", "EINGANGSRECHNUNG": "KREDITOR",
    "FO": "DEBITOR", "DEBITOR": "DEBITOR", "FORDERUNG": "DEBITOR",
    "AUSGANGSRECHNUNG": "DEBITOR",
}


def parse_op_csv(daten: bytes) -> ImportErgebnis:
    """OP-Liste: Art;Partner;Belegnummer;Rechnungsdatum;Faellig;Betrag;Konto;Notiz.

    Art: ER/Kreditor/Eingangsrechnung bzw. FO/Debitor/Forderung. Datumsformate
    TT.MM.JJJJ oder JJJJ-MM-TT; Beträge mit Dezimalkomma oder -punkt.
    """
    text = _dekodiere(daten)
    erg = ImportErgebnis(format="OP")
    delimiter = ";" if text.count(";") >= text.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, quotechar='"')
    zeilen = [z for z in reader if any(f.strip() for f in z)]
    if not zeilen:
        erg.warnungen.append("Leere Datei.")
        return erg
    header = [f.strip().lower() for f in zeilen[0]]
    i_art = _spalten_index(header, "art", "typ")
    i_partner = _spalten_index(header, "partner", "name", "lieferant", "kunde")
    i_beleg = next(
        (i for i, name in enumerate(header)
         if "beleg" in name and "datum" not in name),
        None,
    )
    i_rechnung = _spalten_index(header, "rechnungsdatum", "belegdatum")
    i_faellig = _spalten_index(header, "faellig", "fällig")
    i_betrag = _spalten_index(header, "betrag", "brutto")
    i_konto = next(
        (i for i, name in enumerate(header)
         if "konto" in name and "gegen" not in name),
        None,
    )
    i_notiz = _spalten_index(header, "notiz", "bemerkung", "kommentar")
    if None in (i_art, i_partner, i_faellig, i_betrag):
        erg.warnungen.append(
            "Kopfzeile nicht erkannt – benötigt mindestens: Art, Partner, Faellig, "
            "Betrag (optional Belegnummer, Rechnungsdatum, Konto, Notiz)."
        )
        return erg
    for nr, felder in enumerate(zeilen[1:], start=2):
        if len(felder) <= max(i_art, i_partner, i_faellig, i_betrag):
            erg.warnungen.append(f"Zeile {nr}: zu wenige Spalten.")
            continue
        art = _OP_ARTEN.get(felder[i_art].strip().upper())
        if art is None:
            erg.warnungen.append(f"Zeile {nr}: Art '{felder[i_art]}' unbekannt (ER/FO).")
            continue
        partner = felder[i_partner].strip()
        if not partner:
            erg.warnungen.append(f"Zeile {nr}: Partner fehlt.")
            continue
        faellig = _datum_flexibel(felder[i_faellig])
        if faellig is None:
            erg.warnungen.append(f"Zeile {nr}: Fälligkeit nicht lesbar.")
            continue
        try:
            betrag = abs(_betrag(felder[i_betrag]))
        except InvalidOperation:
            erg.warnungen.append(f"Zeile {nr}: Betrag nicht lesbar.")
            continue
        rechnungsdatum = None
        if i_rechnung is not None and len(felder) > i_rechnung and felder[i_rechnung].strip():
            rechnungsdatum = _datum_flexibel(felder[i_rechnung])
            if rechnungsdatum is None:
                erg.warnungen.append(f"Zeile {nr}: Rechnungsdatum nicht lesbar – ignoriert.")
        erg.posten.append(
            {
                "art": art,
                "partner": partner,
                "belegnr": (felder[i_beleg].strip() or None)
                if (i_beleg is not None and len(felder) > i_beleg) else None,
                "rechnungsdatum": rechnungsdatum,
                "faellig_am": faellig,
                "betrag_brutto": betrag,
                "konto_nr": (felder[i_konto].strip() or None)
                if (i_konto is not None and len(felder) > i_konto) else None,
                "notiz": (felder[i_notiz].strip() or None)
                if (i_notiz is not None and len(felder) > i_notiz) else None,
            }
        )
    return erg


def parse_automatisch(dateiname: str, daten: bytes) -> ImportErgebnis:
    kopf = _dekodiere(daten[:64]).strip().strip('"').upper()
    if kopf.startswith("EXTF") or kopf.startswith("DTVF"):
        return parse_datev(daten)
    return parse_generisches_csv(daten)
