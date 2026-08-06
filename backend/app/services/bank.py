"""Kontoauszugsimport: MT940 (SWIFT) und CAMT.053 (ISO 20022).

Liefert je Auszug die Kontokennung (IBAN bzw. Konto-Nr.), Anfangs-/Endsaldo und
die Einzelumsätze. Die Zuordnung zum Bankkonto des Mandanten erfolgt über die
IBAN am Konto (oder eine explizite Auswahl beim Import).
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation


@dataclass
class BankUmsatz:
    buchungstag: date
    valuta: date | None
    betrag: Decimal  # + Eingang / − Ausgang (Storni bereits vorzeichenrichtig)
    partner: str | None = None
    verwendungszweck: str | None = None
    referenz: str | None = None


@dataclass
class BankAuszug:
    konto_kennung: str
    auszug_nr: str | None = None
    anfangssaldo: Decimal | None = None
    anfangssaldo_datum: date | None = None
    endsaldo: Decimal | None = None
    endsaldo_datum: date | None = None
    umsaetze: list[BankUmsatz] = field(default_factory=list)


@dataclass
class BankImportErgebnis:
    format: str
    auszuege: list[BankAuszug] = field(default_factory=list)
    warnungen: list[str] = field(default_factory=list)


def _dekodiere(daten: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return daten.decode(enc)
        except UnicodeDecodeError:
            continue
    return daten.decode("latin-1", errors="replace")


def normalisiere_iban(wert: str) -> str:
    return re.sub(r"\s", "", wert or "").upper()


# ------------------------------------------------------------------- MT940

def _mt940_datum(jjmmtt: str) -> date | None:
    try:
        jahr = int(jjmmtt[0:2])
        jahr += 2000 if jahr < 80 else 1900
        return date(jahr, int(jjmmtt[2:4]), int(jjmmtt[4:6]))
    except (ValueError, IndexError):
        return None


def _mt940_saldo(inhalt: str) -> tuple[Decimal | None, date | None]:
    """':60F:'-/':62F:'-Inhalt: C/D + JJMMTT + Währung(3) + Betrag mit Komma."""
    m = re.match(r"^([CD])(\d{6})([A-Z]{3})([\d,\.]+)", inhalt.strip())
    if not m:
        return None, None
    try:
        betrag = Decimal(m.group(4).replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None, None
    if m.group(1) == "D":
        betrag = -betrag
    return betrag, _mt940_datum(m.group(2))


_MT940_UMSATZ = re.compile(
    r"^(?P<valuta>\d{6})(?P<buchung>\d{4})?(?P<cd>R?[CD])(?P<waehrung>[A-Z])?"
    r"(?P<betrag>\d+,\d*)(?P<code>[NFS][A-Z0-9]{3})(?P<referenz>.*)$"
)


def _mt940_86(inhalt: str) -> tuple[str | None, str | None]:
    """':86:'-Feld: strukturierte ?-Subfelder oder Freitext -> (partner, zweck)."""
    if "?" not in inhalt:
        text = inhalt.strip()
        return None, text or None
    felder: dict[str, str] = {}
    for teil in inhalt.split("?")[1:]:
        if len(teil) >= 2:
            felder[teil[:2]] = teil[2:]
    partner = ((felder.get("32", "") + " " + felder.get("33", "")).strip()) or None
    zweck_teile = [felder[k] for k in sorted(felder) if "20" <= k <= "29" and felder[k]]
    zweck = " ".join(zweck_teile).strip()
    zweck = re.sub(r"^(SVWZ\+|EREF\+[^ ]* ?)+", "", zweck).strip() or None
    return partner, zweck


def parse_mt940(daten: bytes) -> BankImportErgebnis:
    text = _dekodiere(daten)
    erg = BankImportErgebnis(format="MT940")

    # Tag-Strom aufbauen: Folgezeilen (ohne ':') gehören zum vorherigen Feld
    tags: list[tuple[str, str]] = []
    for zeile in text.splitlines():
        zeile = zeile.rstrip("\r")
        if not zeile.strip() or zeile.strip() == "-":
            continue
        m = re.match(r"^:(\d{2}[A-Z]?):(.*)$", zeile)
        if m:
            tags.append((m.group(1), m.group(2)))
        elif tags and not zeile.startswith("{"):
            tag, inhalt = tags[-1]
            tags[-1] = (tag, inhalt + "\n" + zeile)

    auszug: BankAuszug | None = None
    for tag, inhalt in tags:
        if tag == "20":
            if auszug is not None:
                erg.auszuege.append(auszug)
            auszug = BankAuszug(konto_kennung="")
        elif auszug is None:
            continue
        elif tag == "25":
            auszug.konto_kennung = inhalt.strip().split("\n")[0]
        elif tag == "28C" or tag == "28":
            auszug.auszug_nr = inhalt.strip()
        elif tag in ("60F", "60M"):
            saldo, datum = _mt940_saldo(inhalt)
            if auszug.anfangssaldo is None:
                auszug.anfangssaldo, auszug.anfangssaldo_datum = saldo, datum
        elif tag in ("62F", "62M"):
            auszug.endsaldo, auszug.endsaldo_datum = _mt940_saldo(inhalt)
        elif tag == "61":
            m = _MT940_UMSATZ.match(inhalt.split("\n")[0].strip())
            if not m:
                erg.warnungen.append(f"Umsatzzeile nicht lesbar: :61:{inhalt[:40]}")
                continue
            valuta = _mt940_datum(m.group("valuta"))
            buchungstag = valuta
            if m.group("buchung") and valuta:
                try:
                    buchungstag = date(
                        valuta.year, int(m.group("buchung")[:2]), int(m.group("buchung")[2:])
                    )
                    # Jahreswechsel: Buchung Dez, Valuta Jan (und umgekehrt)
                    if buchungstag - valuta > timedelta(days=180):
                        buchungstag = buchungstag.replace(year=buchungstag.year - 1)
                    elif valuta - buchungstag > timedelta(days=180):
                        buchungstag = buchungstag.replace(year=buchungstag.year + 1)
                except ValueError:
                    buchungstag = valuta
            if buchungstag is None:
                erg.warnungen.append(f"Umsatz ohne Datum: :61:{inhalt[:40]}")
                continue
            try:
                betrag = Decimal(m.group("betrag").replace(",", "."))
            except InvalidOperation:
                erg.warnungen.append(f"Betrag nicht lesbar: :61:{inhalt[:40]}")
                continue
            cd = m.group("cd")
            # C = Gutschrift, D = Lastschrift; RC/RD = Storno (Vorzeichen gedreht)
            if cd in ("D", "RC"):
                betrag = -betrag
            auszug.umsaetze.append(
                BankUmsatz(
                    buchungstag=buchungstag,
                    valuta=valuta,
                    betrag=betrag,
                    referenz=(m.group("referenz").strip().replace("NONREF", "") or None),
                )
            )
        elif tag == "86" and auszug.umsaetze:
            partner, zweck = _mt940_86(inhalt.replace("\n", ""))
            letzter = auszug.umsaetze[-1]
            if letzter.partner is None:
                letzter.partner = partner
            if letzter.verwendungszweck is None:
                letzter.verwendungszweck = zweck
    if auszug is not None:
        erg.auszuege.append(auszug)
    if not erg.auszuege:
        erg.warnungen.append("Keine MT940-Auszüge in der Datei gefunden.")
    return erg


# ------------------------------------------------------------------ CAMT.053

def _lokal(element: ET.Element) -> str:
    return element.tag.split("}")[-1]


def _finde(element: ET.Element, *pfad: str) -> ET.Element | None:
    aktuell = element
    for name in pfad:
        naechstes = None
        for kind in aktuell:
            if _lokal(kind) == name:
                naechstes = kind
                break
        if naechstes is None:
            return None
        aktuell = naechstes
    return aktuell


def _finde_alle(element: ET.Element, name: str) -> list[ET.Element]:
    return [kind for kind in element.iter() if _lokal(kind) == name]


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    return element.text.strip() or None


def _camt_datum(element: ET.Element | None) -> date | None:
    if element is None:
        return None
    wert = _text(_finde(element, "Dt")) or _text(_finde(element, "DtTm"))
    if not wert:
        return None
    try:
        return date.fromisoformat(wert[:10])
    except ValueError:
        return None


def parse_camt053(daten: bytes) -> BankImportErgebnis:
    erg = BankImportErgebnis(format="CAMT")
    try:
        wurzel = ET.fromstring(_dekodiere(daten))
    except ET.ParseError as e:
        erg.warnungen.append(f"XML nicht lesbar: {e}")
        return erg

    for stmt in _finde_alle(wurzel, "Stmt"):
        acct = _finde(stmt, "Acct", "Id")
        kennung = (
            _text(_finde(acct, "IBAN")) if acct is not None else None
        ) or (_text(_finde(acct, "Othr", "Id")) if acct is not None else None) or ""
        auszug = BankAuszug(
            konto_kennung=kennung,
            auszug_nr=_text(_finde(stmt, "ElctrncSeqNb")) or _text(_finde(stmt, "LglSeqNb")),
        )
        for bal in [k for k in stmt if _lokal(k) == "Bal"]:
            code = _text(_finde(bal, "Tp", "CdOrPrtry", "Cd"))
            betrag_el = _finde(bal, "Amt")
            try:
                betrag = Decimal(_text(betrag_el) or "")
            except InvalidOperation:
                continue
            if _text(_finde(bal, "CdtDbtInd")) == "DBIT":
                betrag = -betrag
            datum = _camt_datum(_finde(bal, "Dt"))
            if code == "OPBD":
                auszug.anfangssaldo, auszug.anfangssaldo_datum = betrag, datum
            elif code in ("CLBD", "CLAV") and (auszug.endsaldo is None or code == "CLBD"):
                auszug.endsaldo, auszug.endsaldo_datum = betrag, datum
        for ntry in [k for k in stmt if _lokal(k) == "Ntry"]:
            try:
                betrag = Decimal(_text(_finde(ntry, "Amt")) or "")
            except InvalidOperation:
                erg.warnungen.append("Umsatz ohne lesbaren Betrag übersprungen.")
                continue
            gutschrift = _text(_finde(ntry, "CdtDbtInd")) == "CRDT"
            if not gutschrift:
                betrag = -betrag
            if (_text(_finde(ntry, "RvslInd")) or "").lower() == "true":
                betrag = -betrag
            buchungstag = _camt_datum(_finde(ntry, "BookgDt"))
            valuta = _camt_datum(_finde(ntry, "ValDt"))
            if buchungstag is None and valuta is None:
                erg.warnungen.append("Umsatz ohne Datum übersprungen.")
                continue
            zweck_teile = [
                _text(el) for el in _finde_alle(ntry, "Ustrd") if _text(el)
            ]
            partei = "Dbtr" if gutschrift else "Cdtr"
            partner = None
            for tx in _finde_alle(ntry, "TxDtls"):
                partner = _text(_finde(tx, "RltdPties", partei, "Nm")) or _text(
                    _finde(tx, "RltdPties", partei, "Pty", "Nm")
                )
                if partner:
                    break
            auszug.umsaetze.append(
                BankUmsatz(
                    buchungstag=buchungstag or valuta,
                    valuta=valuta,
                    betrag=betrag,
                    partner=partner,
                    verwendungszweck=" ".join(zweck_teile) or _text(_finde(ntry, "AddtlNtryInf")),
                    referenz=_text(_finde(ntry, "AcctSvcrRef")),
                )
            )
        erg.auszuege.append(auszug)
    if not erg.auszuege:
        erg.warnungen.append("Keine CAMT.053-Auszüge (Stmt) in der Datei gefunden.")
    return erg


def parse_bank_automatisch(daten: bytes) -> BankImportErgebnis:
    kopf = _dekodiere(daten[:400]).lstrip()
    if kopf.startswith("<?xml") or "<Document" in kopf or "BkToCstmrStmt" in kopf:
        return parse_camt053(daten)
    return parse_mt940(daten)


def ist_bankformat(daten: bytes) -> bool:
    kopf = _dekodiere(daten[:400]).lstrip()
    if "BkToCstmrStmt" in kopf:
        return True
    return bool(re.search(r"^:2[05]:", kopf, re.M))
