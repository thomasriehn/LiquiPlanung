"""Bestellungen und Bestellfreigabe (Sachwalter / Insolvenzverwalter).

Eine Bestellung wird netto mit USt-Satz erfasst und fließt brutto zum
erwarteten Zahldatum als Auszahlung in den Plan (liquiditaet.plan_fluesse,
Schritt 3b), bis sie durch die Ausgangszahlung, eine Eingangsrechnung (offener
Posten) oder manuell abgelöst wird. Freigabe/Ablehnung erfolgt je KW anhand
der Kennzahlen aus `freigabe_kennzahlen`; jede Aktion steht unveränderlich im
Journal (BestellJournal), bei Entscheidungen mit den gezeigten Kennzahlen.

Konventionen: Service-Funktionen committen nicht und melden fachliche Fehler
als ValueError. Beträge intern Decimal (Flussvorzeichen: Auszahlung −), Ausgabe
als positive Auszahlungsbeträge (float, 2 Nachkommastellen).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from .. import models
from ..models import AbloeseArt, BestellStatus, KontoTyp, PostenArt, PostenStatus
from .ausgleich import _norm, _tokens
from .feiertage import naechster_bankarbeitstag
from .insolvenzgeld import igeld_fenster, termin_entlastung
from .liquiditaet import (
    BESTELL_KEY,
    CENT,
    Key,
    _brutto,
    _extern_plan,
    _f,
    _lade_konten,
    _sachfluss,
    berechne_plan,
    bestell_plan_status,
    bestellung_rest,
    dauer_vortraege,
    gruppen_aufloeser,
    ist_zahlungsfluesse,
    liquiditaets_richtung,
    plan_fluesse,
    plan_verschiebungen,
    referenz_snapshot,
    snapshot_fluesse,
    verschiebungen_anwenden,
    wochen_start,
)
from .zugaenge import DateiRef

STATUS_TEXT = {
    "OFFEN": "zur Freigabe",
    "FREIGEGEBEN": "freigegeben",
    "ABGELEHNT": "abgelehnt",
    "ABGELOEST": "abgelöst",
    "STORNIERT": "storniert",
}
AKTION_TEXT = {
    "ERFASST": "erfasst",
    "GEAENDERT": "geändert",
    "FREIGEGEBEN": "freigegeben",
    "ABGELEHNT": "abgelehnt",
    "ZURUECKGESETZT": "Entscheidung zurückgenommen",
    "ABGELOEST": "abgelöst",
    "TEILZAHLUNG": "Teilzahlung zugeordnet",
    "ABLOESUNG_AUFGEHOBEN": "Ablösung aufgehoben",
    "STORNIERT": "storniert",
    "GELOESCHT": "gelöscht",
    "DATEI": "Datei geändert",
}
ABLOESE_ART_TEXT = {"ZAHLUNG": "Zahlung", "RECHNUNG": "Rechnung", "MANUELL": "manuell"}

# Felder, die erfasst/geändert werden können (Anzeigename fürs Journal)
FELDER = {
    "kreditor": "Kreditor",
    "bestellnummer": "Bestellnummer",
    "beschreibung": "Beschreibung",
    "bestelldatum": "Bestelldatum",
    "lieferdatum": "Lieferdatum",
    "zahldatum": "Zahldatum",
    "betrag_netto": "Betrag netto",
    "ust_satz": "USt-Satz",
    "konto_id": "Konto",
}
# Änderungen daran erfordern bei freigegebenen Bestellungen eine erneute Freigabe
# (Kreditor nicht bei bloßer Schreibkorrektur, Zahldatum nur bei Wechsel der KW –
# siehe _freigabe_relevant); dazu das Ersetzen der Datei (datei_setzen)
FREIGABE_RELEVANT = ("kreditor", "betrag_netto", "ust_satz", "konto_id", "zahldatum")

SCHWELLE_VORSCHLAG = 60   # Ablösevorschläge ab diesem Score
SCHWELLE_SICHER = 85
MAX_KANDIDATEN = 30
NULL = Decimal("0")

_OFFEN = BestellStatus.OFFEN.value
_FREIGEGEBEN = BestellStatus.FREIGEGEBEN.value
_ABGELEHNT = BestellStatus.ABGELEHNT.value
_ABGELOEST = BestellStatus.ABGELOEST.value
_STORNIERT = BestellStatus.STORNIERT.value
_FINANZ_TYPEN = (KontoTyp.BANK.value, KontoTyp.KASSE.value)


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------

def _eur(betrag: Decimal | float | None) -> str:
    """Deutsches Zahlenformat: 1.234,56"""
    s = f"{Decimal(str(betrag or 0)).quantize(CENT):,.2f}"
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def _datum_de(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


def _iso(d: date | datetime | None) -> str | None:
    return d.isoformat() if d else None


def _kw_info(d: date) -> dict:
    iso = d.isocalendar()
    return {"jahr": iso.year, "kw": iso.week, "label": f"KW {iso.week}/{iso.year}"}


def _text(wert, feld: str, max_laenge: int | None = None, pflicht: bool = False) -> str | None:
    if wert is None:
        text = ""
    elif isinstance(wert, str):
        text = wert.strip()
    else:
        text = str(wert).strip()
    if not text:
        if pflicht:
            raise ValueError(f"{FELDER.get(feld, feld)} fehlt")
        return None
    if max_laenge and len(text) > max_laenge:
        raise ValueError(f"{FELDER.get(feld, feld)} ist zu lang (höchstens {max_laenge} Zeichen)")
    return text


def _datum(wert, feld: str, pflicht: bool = False) -> date | None:
    if wert is None or wert == "":
        if pflicht:
            raise ValueError(f"{FELDER.get(feld, feld)} fehlt")
        return None
    if isinstance(wert, datetime):
        return wert.date()
    if isinstance(wert, date):
        return wert
    try:
        return date.fromisoformat(str(wert).strip()[:10])
    except ValueError:
        raise ValueError(f"{FELDER.get(feld, feld)}: ungültiges Datum") from None


def _dezimal(wert, feld: str) -> Decimal | None:
    if wert is None or wert == "":
        return None
    if isinstance(wert, bool):
        raise ValueError(f"{FELDER.get(feld, feld)}: ungültiger Betrag")
    try:
        d = Decimal(str(wert).strip().replace(",", ".")) if isinstance(wert, str) else Decimal(str(wert))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{FELDER.get(feld, feld)}: ungültiger Betrag") from None
    if not d.is_finite():
        raise ValueError(f"{FELDER.get(feld, feld)}: ungültiger Betrag")
    return d


def _betrag_netto(wert) -> Decimal:
    d = _dezimal(wert, "betrag_netto")
    if d is None:
        raise ValueError("Betrag netto fehlt")
    d = d.quantize(CENT)
    if d <= 0:
        raise ValueError("Der Nettobetrag muss größer als 0 sein")
    if d >= Decimal("1000000000000"):
        raise ValueError("Der Nettobetrag ist zu groß")
    return d


def _ust_satz(wert) -> Decimal | None:
    d = _dezimal(wert, "ust_satz")
    if d is None:
        return None
    if d < 0 or d > 100:
        raise ValueError("Der USt-Satz muss zwischen 0 und 100 % liegen")
    return d.quantize(CENT)


def _konto_pruefen(db: Session, mandant: models.Mandant, konto_id) -> models.Konto:
    if konto_id in (None, ""):
        raise ValueError("Bitte ein Konto wählen")
    try:
        kid = int(konto_id)
    except (TypeError, ValueError):
        raise ValueError("Konto nicht gefunden") from None
    k = db.get(models.Konto, kid)
    if k is None or k.mandant_id != mandant.id:
        raise ValueError("Konto nicht gefunden")
    if k.typ in _FINANZ_TYPEN:
        raise ValueError("Bank- und Kassenkonten sind für Bestellungen nicht zulässig")
    return k


def _benutzer_name(db: Session, benutzer_id: int | None) -> str | None:
    if benutzer_id is None:
        return None
    b = db.get(models.Benutzer, benutzer_id)
    return b.name if b else None


def _finanz_nrn(konten: list[models.Konto]) -> set[str]:
    return {k.nummer for k in konten if k.typ in _FINANZ_TYPEN}


def _mandant(db: Session, b: models.Bestellung) -> models.Mandant | None:
    return db.get(models.Mandant, b.mandant_id)


def _status_setzen(db: Session, b: models.Bestellung, nachher: str) -> None:
    """Status setzen – nur, wenn die Bestellung in der Datenbank noch den geprüften
    Status hat (bedingtes UPDATE). So scheitert bei gleichzeitigen Anfragen die
    zweite (ValueError → 409), statt die erste still zu überschreiben."""
    vorher = b.status
    jetzt = models.utcnow()
    if b.id is not None:
        db.flush()
        erg = db.execute(
            update(models.Bestellung)
            .where(models.Bestellung.id == b.id, models.Bestellung.status == vorher)
            .values(status=nachher, geaendert_am=jetzt)
            .execution_options(synchronize_session=False)
        )
        if erg.rowcount != 1:
            raise ValueError(
                "Die Bestellung wurde bereits von anderer Seite geändert – bitte neu laden")
    b.status = nachher
    b.geaendert_am = jetzt


def _freigabe_relevant(feld: str, alt, neu) -> bool:
    """Wesentliche Änderung, die eine erneute Freigabe erfordert? Kreditor: nicht bei
    bloßer Schreibkorrektur (Groß-/Kleinschreibung, Leer-/Satzzeichen, Umlaute);
    Zahldatum: nur beim Wechsel der Kalenderwoche (die Freigabe gilt je KW)."""
    if feld not in FREIGABE_RELEVANT:
        return False
    if feld == "kreditor":
        return _norm(alt) != _norm(neu)
    if feld == "zahldatum":
        return alt is None or neu is None or alt.isocalendar()[:2] != neu.isocalendar()[:2]
    return True


def bestellung_im_plan(b: models.Bestellung, mandant: models.Mandant | None) -> bool:
    """Steht die Bestellung mit ihrem offenen Rest im Plan (Mandantenstandard)?"""
    if mandant is None:
        return b.status == _FREIGEGEBEN
    status = bestell_plan_status(mandant, None) or set()
    return b.status in status


def brutto(b: models.Bestellung) -> Decimal:
    return _brutto(b.betrag_netto, b.ust_satz)


def bestellung_text(b: models.Bestellung) -> str:
    """Kurzbeschreibung fürs Journal: „Kreditor · Nr. 4711 · 1.234,56 € netto“."""
    teile = [b.kreditor or "?"]
    if b.bestellnummer:
        teile.append(f"Nr. {b.bestellnummer}")
    teile.append(f"{_eur(b.betrag_netto)} € netto")
    return " · ".join(teile)[:255]


# ---------------------------------------------------------------------------
# Journal und Darstellung
# ---------------------------------------------------------------------------

def journal(
    db: Session,
    mandant_id: int,
    b: models.Bestellung | None,
    benutzer: models.Benutzer | None,
    aktion: str,
    *,
    status_vorher: str | None = None,
    status_nachher: str | None = None,
    begruendung: str | None = None,
    kennzahlen: dict | None = None,
) -> models.BestellJournal:
    """Journaleintrag anlegen (nur anfügen, nie ändern); Benutzername und
    Bestelltext werden eingefroren. Ohne Benutzer (Systemhaken): „System“."""
    if aktion not in AKTION_TEXT:
        raise ValueError(f"Unbekannte Journalaktion: {aktion}")
    if b is not None and b.id is None:
        db.flush()
    eintrag = models.BestellJournal(
        mandant_id=mandant_id,
        bestellung_id=b.id if b is not None else None,
        benutzer_id=benutzer.id if benutzer is not None else None,
        benutzer_name=(benutzer.name if benutzer is not None else "System")[:255],
        aktion=aktion,
        status_vorher=status_vorher,
        status_nachher=status_nachher,
        begruendung=begruendung,
        bestellung_text=bestellung_text(b) if b is not None else None,
        kennzahlen=kennzahlen,
    )
    db.add(eintrag)
    return eintrag


def journal_als_dict(j: models.BestellJournal) -> dict:
    return {
        "id": j.id,
        "zeit": _iso(j.zeit),
        "benutzer": j.benutzer_name,
        "aktion": j.aktion,
        "aktion_text": AKTION_TEXT.get(j.aktion, j.aktion),
        "status_vorher": j.status_vorher,
        "status_nachher": j.status_nachher,
        "begruendung": j.begruendung,
        "bestellung_id": j.bestellung_id,
        "bestellung_text": j.bestellung_text,
        "kennzahlen": j.kennzahlen,
    }


def journal_liste(db: Session, mandant: models.Mandant, *, bestellung_id: int | None = None,
                  limit: int = 200) -> list[dict]:
    """Journaleinträge des Mandanten, neueste zuerst."""
    q = select(models.BestellJournal).where(models.BestellJournal.mandant_id == mandant.id)
    if bestellung_id is not None:
        q = q.where(models.BestellJournal.bestellung_id == bestellung_id)
    q = q.order_by(models.BestellJournal.zeit.desc(), models.BestellJournal.id.desc())
    return [journal_als_dict(j) for j in db.scalars(q.limit(max(1, min(int(limit), 5000))))]


def _beleg_text(db: Session, a: models.BestellAbloesung) -> str | None:
    """Lesbare Beschreibung der Ablösequelle."""
    if a.buchung_id is not None:
        bu = db.get(models.Buchung, a.buchung_id)
        if bu is None:
            return "Buchung (gelöscht)"
        text = bu.text or bu.belegfeld or ""
        return f"Buchung {_datum_de(bu.datum)} · {bu.konto_nr}/{bu.gegenkonto_nr}" + (f" · {text}" if text else "")
    if a.transaktion_id is not None:
        t = db.get(models.BankTransaktion, a.transaktion_id)
        if t is None:
            return "Bankumsatz (gelöscht)"
        text = " · ".join(x for x in (t.partner, t.verwendungszweck) if x)
        return f"Bankumsatz {_datum_de(t.buchungstag)}" + (f" · {text}" if text else "")
    if a.posten_id is not None:
        p = db.get(models.OffenerPosten, a.posten_id)
        if p is None:
            return "Rechnung (gelöscht)"
        return f"Rechnung {p.partner}" + (f" · {p.belegnr}" if p.belegnr else "") + \
            f" · {_eur(p.betrag_brutto)} €"
    if a.art == AbloeseArt.MANUELL.value:
        return "manuell"
    return None


def als_dict(db: Session, b: models.Bestellung) -> dict:
    m = _mandant(db, b)
    br = brutto(b)
    abgeloest_betrag = sum((a.betrag for a in b.abloesungen), NULL)
    im_plan = bestellung_im_plan(b, m)
    rest = bestellung_rest(b) if b.status in (_OFFEN, _FREIGEGEBEN) else NULL
    konto = b.konto
    datei = None
    if b.datei_name:
        datei = {
            "name": b.datei_name,
            "mime": b.datei_mime,
            "groesse": b.datei_groesse,
            "ablage": b.datei_ablage,
            "pfad": b.datei_pfad,
            "web_url": b.datei_web_url,
            "url": f"/mandanten/{b.mandant_id}/bestellungen/{b.id}/datei",
        }
    return {
        "id": b.id,
        "mandant_id": b.mandant_id,
        "kreditor": b.kreditor,
        "bestellnummer": b.bestellnummer,
        "beschreibung": b.beschreibung,
        "bestelldatum": _iso(b.bestelldatum),
        "lieferdatum": _iso(b.lieferdatum),
        "zahldatum": _iso(b.zahldatum),
        "kw": _kw_info(b.zahldatum),
        "betrag_netto": _f(b.betrag_netto),
        "ust_satz": _f(b.ust_satz) if b.ust_satz is not None else None,
        "betrag_brutto": _f(br),
        "konto": (
            {"id": konto.id, "nummer": konto.nummer, "bezeichnung": konto.bezeichnung}
            if konto is not None else None
        ),
        "status": b.status,
        "status_text": STATUS_TEXT.get(b.status, b.status),
        "entschieden_am": _iso(b.entschieden_am),
        "entschieden_von": _benutzer_name(db, b.entschieden_von),
        "abgeloest_am": _iso(b.abgeloest_am),
        "abgeloest_durch": b.abgeloest_durch,
        "abgeloest_betrag": _f(abgeloest_betrag),
        "abweichung": _f(abgeloest_betrag - br) if b.status == _ABGELOEST else None,
        "rest_geplant": _f(max(rest, NULL)) if im_plan else 0.0,
        "rest_offen": _f(max(rest, NULL)),
        "im_plan": im_plan,
        "datei": datei,
        "ki": bool(b.ki_daten),
        "erstellt_am": _iso(b.erstellt_am),
        "erstellt_von": _benutzer_name(db, b.erstellt_von),
        "abloesungen": [
            {
                "id": a.id,
                "art": a.art,
                "art_text": ABLOESE_ART_TEXT.get(a.art, a.art),
                "betrag": _f(a.betrag),
                "datum": _iso(a.datum),
                "abschliessend": bool(a.abschliessend),
                "notiz": a.notiz,
                "buchung_id": a.buchung_id,
                "transaktion_id": a.transaktion_id,
                "posten_id": a.posten_id,
                "beleg": _beleg_text(db, a),
            }
            for a in b.abloesungen
        ],
    }


def _status_filter(status: str | None) -> list[str] | None:
    if status is None or not str(status).strip() or str(status).strip().upper() == "ALLE":
        return None
    werte = [s.strip().upper() for s in str(status).split(",") if s.strip()]
    for s in werte:
        if s not in STATUS_TEXT:
            raise ValueError(f"Unbekannter Status: {s}")
    return werte


def liste(db: Session, mandant: models.Mandant, *, status: str | None = None,
          suche: str | None = None) -> list[dict]:
    """Bestellungen des Mandanten (Status auch kommagetrennt, „ALLE“), sortiert
    nach Zahldatum."""
    q = (
        select(models.Bestellung)
        .options(selectinload(models.Bestellung.abloesungen), selectinload(models.Bestellung.konto))
        .where(models.Bestellung.mandant_id == mandant.id)
    )
    werte = _status_filter(status)
    if werte:
        q = q.where(models.Bestellung.status.in_(werte))
    q = q.order_by(models.Bestellung.zahldatum, models.Bestellung.id)
    bestellungen = list(db.scalars(q))
    if suche and suche.strip():
        s_fold = suche.strip().casefold()
        s_norm = _norm(suche)

        def passt(b: models.Bestellung) -> bool:
            felder = " ".join(x for x in (b.kreditor, b.bestellnummer, b.beschreibung) if x)
            return s_fold in felder.casefold() or (bool(s_norm) and s_norm in _norm(felder))

        bestellungen = [b for b in bestellungen if passt(b)]
    return [als_dict(db, b) for b in bestellungen]


def kreditoren(db: Session, mandant: models.Mandant) -> list[str]:
    """Bekannte Kreditoren (Bestellungen und Kreditoren-Posten), eindeutig, sortiert."""
    namen: dict[str, str] = {}
    quellen = list(db.scalars(
        select(models.Bestellung.kreditor).where(models.Bestellung.mandant_id == mandant.id)
        .order_by(models.Bestellung.id.desc())
    )) + list(db.scalars(
        select(models.OffenerPosten.partner).where(
            models.OffenerPosten.mandant_id == mandant.id,
            models.OffenerPosten.art == PostenArt.KREDITOR.value,
        ).order_by(models.OffenerPosten.id.desc())
    ))
    for name in quellen:
        n = (name or "").strip()
        if n:
            namen.setdefault(n.casefold(), n)
    return sorted(namen.values(), key=lambda n: (n.casefold(), n))


# ---------------------------------------------------------------------------
# Erfassen, Ändern, Status-Workflow
# ---------------------------------------------------------------------------

def _datei_uebernehmen(b: models.Bestellung, datei: DateiRef) -> None:
    b.datei_name = datei.name[:255] if datei.name else None
    b.datei_mime = datei.mime
    b.datei_groesse = datei.groesse
    b.datei_ablage = datei.ablage
    b.datei_pfad = datei.pfad
    b.datei_drive_id = datei.drive_id
    b.datei_item_id = datei.item_id
    b.datei_web_url = datei.web_url


def anlegen(
    db: Session,
    mandant: models.Mandant,
    benutzer: models.Benutzer | None,
    daten: dict,
    datei: DateiRef | None = None,
    ki_daten: dict | None = None,
) -> models.Bestellung:
    """Neue Bestellung „zur Freigabe“ (Status OFFEN) mit Journal ERFASST.

    ust_satz: fehlender Schlüssel → Steuersatz des Kontos; ausdrücklich None →
    ohne USt. Andere Schlüssel (z. B. token, ablage_element) werden ignoriert."""
    konto = _konto_pruefen(db, mandant, daten.get("konto_id"))
    ust = _ust_satz(daten["ust_satz"]) if "ust_satz" in daten else konto.ust_satz
    b = models.Bestellung(
        mandant_id=mandant.id,
        kreditor=_text(daten.get("kreditor"), "kreditor", 255, pflicht=True),
        bestellnummer=_text(daten.get("bestellnummer"), "bestellnummer", 64),
        beschreibung=_text(daten.get("beschreibung"), "beschreibung"),
        bestelldatum=_datum(daten.get("bestelldatum"), "bestelldatum", pflicht=True),
        lieferdatum=_datum(daten.get("lieferdatum"), "lieferdatum"),
        zahldatum=_datum(daten.get("zahldatum"), "zahldatum", pflicht=True),
        betrag_netto=_betrag_netto(daten.get("betrag_netto")),
        ust_satz=ust,
        konto_id=konto.id,
        status=_OFFEN,
        ki_daten=ki_daten or None,
        erstellt_von=benutzer.id if benutzer is not None else None,
    )
    b.konto = konto
    if datei is not None:
        _datei_uebernehmen(b, datei)
    db.add(b)
    db.flush()
    journal(db, mandant.id, b, benutzer, "ERFASST", status_nachher=_OFFEN,
            begruendung=f"{_eur(brutto(b))} € brutto, Zahlung erwartet {_datum_de(b.zahldatum)}")
    return b


def datei_setzen(db: Session, b: models.Bestellung, benutzer: models.Benutzer | None,
                 datei: DateiRef) -> None:
    """Datei nachreichen bzw. ersetzen; Ersetzen bei einer freigegebenen Bestellung
    → wieder OFFEN (erneute Freigabe)."""
    alt = b.datei_name
    vorher = b.status
    text = f"{alt} → {datei.name}" if alt else datei.name
    _datei_uebernehmen(b, datei)
    if vorher == _FREIGEGEBEN and alt:
        _status_setzen(db, b, _OFFEN)
        b.entschieden_am = None
        b.entschieden_von = None
        text += " – erneute Freigabe erforderlich"
    b.geaendert_am = models.utcnow()
    journal(db, b.mandant_id, b, benutzer, "DATEI", status_vorher=vorher,
            status_nachher=b.status, begruendung=text)


def _feld_anzeige(db: Session, feld: str, wert) -> str:
    if wert is None:
        return "–"
    if feld == "betrag_netto":
        return f"{_eur(wert)} €"
    if feld == "ust_satz":
        return f"{_eur(wert)} %"
    if feld == "konto_id":
        k = db.get(models.Konto, wert)
        return f"{k.nummer} {k.bezeichnung}" if k else str(wert)
    if isinstance(wert, date):
        return _datum_de(wert)
    text = str(wert)
    return text if len(text) <= 60 else text[:57] + "…"


def aendern(db: Session, b: models.Bestellung, benutzer: models.Benutzer | None,
            daten: dict) -> list[str]:
    """Felder ändern (nur OFFEN/FREIGEGEBEN/ABGELEHNT). Freigegeben + wesentliche
    Änderung (Kreditor, Betrag, USt, Konto, Zahldatum in anderer KW) → wieder OFFEN
    (erneute Freigabe); abgelehnt + jede Änderung → OFFEN (Wiedervorlage). Liefert
    die geänderten Felder."""
    if b.status not in (_OFFEN, _FREIGEGEBEN, _ABGELEHNT):
        raise ValueError(
            f"Eine Bestellung im Status „{STATUS_TEXT.get(b.status, b.status)}“ kann nicht geändert werden")
    unbekannt = set(daten) - set(FELDER)
    if unbekannt:
        raise ValueError(f"Unbekannte Felder: {', '.join(sorted(unbekannt))}")
    m = _mandant(db, b)
    neu: dict = {}
    for feld, wert in daten.items():
        if feld == "kreditor":
            neu[feld] = _text(wert, feld, 255, pflicht=True)
        elif feld == "bestellnummer":
            neu[feld] = _text(wert, feld, 64)
        elif feld == "beschreibung":
            neu[feld] = _text(wert, feld)
        elif feld in ("bestelldatum", "zahldatum"):
            neu[feld] = _datum(wert, feld, pflicht=True)
        elif feld == "lieferdatum":
            neu[feld] = _datum(wert, feld)
        elif feld == "betrag_netto":
            neu[feld] = _betrag_netto(wert)
        elif feld == "ust_satz":
            neu[feld] = _ust_satz(wert)
        elif feld == "konto_id":
            neu[feld] = _konto_pruefen(db, m, wert).id
    geaendert = [f for f in FELDER if f in neu and getattr(b, f) != neu[f]]
    if not geaendert:
        return []
    beschreibung = "; ".join(
        f"{FELDER[f]}: {_feld_anzeige(db, f, getattr(b, f))} → {_feld_anzeige(db, f, neu[f])}"
        for f in geaendert
    )
    wesentlich = any(_freigabe_relevant(f, getattr(b, f), neu[f]) for f in geaendert)
    for f in geaendert:
        setattr(b, f, neu[f])
    if "konto_id" in geaendert:
        b.konto = db.get(models.Konto, b.konto_id)
    vorher = nachher = b.status
    if vorher == _FREIGEGEBEN and wesentlich:
        nachher = _OFFEN
        beschreibung += " – erneute Freigabe erforderlich"
    elif vorher == _ABGELEHNT:
        nachher = _OFFEN
        beschreibung += " – zur erneuten Freigabe vorgelegt"
    _status_setzen(db, b, nachher)
    if nachher != vorher:
        b.entschieden_am = None
        b.entschieden_von = None
    journal(db, b.mandant_id, b, benutzer, "GEAENDERT", status_vorher=vorher,
            status_nachher=b.status, begruendung=beschreibung)
    return geaendert


def entscheiden(db: Session, b: models.Bestellung, benutzer: models.Benutzer | None,
                entscheidung: str, begruendung: str | None, kennzahlen: dict) -> None:
    """Freigeben oder ablehnen (nur aus OFFEN); die Kennzahlen werden als
    Momentaufnahme im Journal gespeichert. Ablehnung nur mit Begründung."""
    if b.status != _OFFEN:
        raise ValueError(
            f"Nur Bestellungen „zur Freigabe“ können entschieden werden "
            f"(Status: {STATUS_TEXT.get(b.status, b.status)})")
    art = (entscheidung or "").strip().upper()
    text = _text(begruendung, "begruendung")
    if art == "FREIGEBEN":
        nachher, aktion = _FREIGEGEBEN, "FREIGEGEBEN"
    elif art == "ABLEHNEN":
        if not text:
            raise ValueError("Bitte die Ablehnung begründen")
        nachher, aktion = _ABGELEHNT, "ABGELEHNT"
    else:
        raise ValueError("Unbekannte Entscheidung (FREIGEBEN oder ABLEHNEN)")
    vorher = b.status
    _status_setzen(db, b, nachher)
    b.entschieden_am = models.utcnow()
    b.entschieden_von = benutzer.id if benutzer is not None else None
    journal(db, b.mandant_id, b, benutzer, aktion, status_vorher=vorher, status_nachher=nachher,
            begruendung=text, kennzahlen=kennzahlen)


def zuruecksetzen(db: Session, b: models.Bestellung, benutzer: models.Benutzer | None,
                  begruendung: str) -> None:
    """Entscheidung zurücknehmen: FREIGEGEBEN/ABGELEHNT → OFFEN (mit Begründung)."""
    if b.status not in (_FREIGEGEBEN, _ABGELEHNT):
        raise ValueError("Nur freigegebene oder abgelehnte Bestellungen können zurückgesetzt werden")
    text = _text(begruendung, "begruendung")
    if not text:
        raise ValueError("Bitte das Zurücknehmen der Entscheidung begründen")
    vorher = b.status
    _status_setzen(db, b, _OFFEN)
    b.entschieden_am = None
    b.entschieden_von = None
    journal(db, b.mandant_id, b, benutzer, "ZURUECKGESETZT", status_vorher=vorher,
            status_nachher=_OFFEN, begruendung=text)


def stornieren(db: Session, b: models.Bestellung, benutzer: models.Benutzer | None,
               begruendung: str | None) -> None:
    """Bestellung zurückziehen (OFFEN/FREIGEGEBEN/ABGELEHNT → STORNIERT)."""
    if b.status not in (_OFFEN, _FREIGEGEBEN, _ABGELEHNT):
        raise ValueError(
            f"Eine Bestellung im Status „{STATUS_TEXT.get(b.status, b.status)}“ kann nicht storniert werden")
    vorher = b.status
    _status_setzen(db, b, _STORNIERT)
    journal(db, b.mandant_id, b, benutzer, "STORNIERT", status_vorher=vorher,
            status_nachher=_STORNIERT, begruendung=_text(begruendung, "begruendung"))


def loeschen(db: Session, b: models.Bestellung, benutzer: models.Benutzer | None) -> None:
    """Löschen nur, solange nie entschieden wurde und keine Ablösung besteht;
    das Journal bleibt erhalten (Bezug auf die Bestellung wird gelöst)."""
    if b.abloesungen:
        raise ValueError("Die Bestellung hat Ablösungen und kann nicht gelöscht werden")
    db.flush()
    entschieden = db.scalar(
        select(models.BestellJournal.id).where(
            models.BestellJournal.bestellung_id == b.id,
            models.BestellJournal.aktion.in_(("FREIGEGEBEN", "ABGELEHNT")),
        ).limit(1)
    )
    if entschieden is not None or b.entschieden_am is not None:
        raise ValueError(
            "Über die Bestellung wurde bereits entschieden – bitte stornieren statt löschen")
    db.execute(
        update(models.BestellJournal)
        .where(models.BestellJournal.bestellung_id == b.id)
        .values(bestellung_id=None)
    )
    eintrag = journal(db, b.mandant_id, b, benutzer, "GELOESCHT", status_vorher=b.status)
    eintrag.bestellung_id = None
    db.delete(b)
    db.flush()


# ---------------------------------------------------------------------------
# Ablösung
# ---------------------------------------------------------------------------

def _belegt(db: Session, spalte, wert: int, ausser_id: int | None = None) -> bool:
    q = select(models.BestellAbloesung.id).where(spalte == wert)
    if ausser_id is not None:
        q = q.where(models.BestellAbloesung.id != ausser_id)
    return db.scalar(q.limit(1)) is not None


def abloesen(
    db: Session,
    b: models.Bestellung,
    benutzer: models.Benutzer | None,
    *,
    art: str,
    betrag: Decimal | None = None,
    datum: date | None = None,
    abschliessend: bool = True,
    buchung_id: int | None = None,
    transaktion_id: int | None = None,
    posten_id: int | None = None,
    notiz: str | None = None,
) -> models.BestellAbloesung:
    """Bestellung ablösen bzw. Teilzahlung zuordnen (aus OFFEN oder FREIGEGEBEN).

    ZAHLUNG: genau eine Quelle (Ist-Buchung mit Auszahlung oder Bankumsatz < 0),
             noch keiner anderen Ablösung bzw. keinem OP-Ausgleich zugeordnet.
    RECHNUNG: offener Posten (Kreditor) übernimmt die Planung – immer abschließend.
    MANUELL: Betrag und Datum von Hand.
    Abschließend (oder alle Ablösungen decken das Brutto bis auf 0,01 €) → Status
    ABGELOEST, auch bei abweichendem Betrag (Minderlieferung); sonst Teilzahlung –
    der Rest bleibt geplant. Gespeichert wird nur die ausdrückliche Wahl."""
    if b.status not in (_OFFEN, _FREIGEGEBEN):
        raise ValueError(
            f"Eine Bestellung im Status „{STATUS_TEXT.get(b.status, b.status)}“ kann nicht abgelöst werden")
    art = (art or "").strip().upper()
    if art not in ABLOESE_ART_TEXT:
        raise ValueError("Unbekannte Ablöseart (ZAHLUNG, RECHNUNG oder MANUELL)")
    betrag_d = _dezimal(betrag, "betrag")
    datum_d = _datum(datum, "datum")
    quellen = [q for q in (buchung_id, transaktion_id, posten_id) if q is not None]

    if art == AbloeseArt.ZAHLUNG.value:
        if posten_id is not None or len(quellen) != 1:
            raise ValueError("Bitte genau eine Zahlung (Buchung oder Bankumsatz) angeben")
        if buchung_id is not None:
            bu = db.get(models.Buchung, buchung_id)
            if bu is None or bu.mandant_id != b.mandant_id:
                raise ValueError("Buchung nicht gefunden")
            fluss = _sachfluss(bu, _finanz_nrn(_lade_konten(db, b.mandant_id)))
            if fluss is None or fluss[1] >= 0:
                raise ValueError("Die Buchung ist keine Auszahlung über Bank oder Kasse")
            if bu.posten_id is not None:
                raise ValueError("Die Buchung ist bereits einem offenen Posten zugeordnet")
            if _belegt(db, models.BestellAbloesung.buchung_id, bu.id):
                raise ValueError("Die Buchung ist bereits einer Bestellung zugeordnet")
            standard_betrag, standard_datum = -fluss[1], bu.datum
        else:
            t = db.get(models.BankTransaktion, transaktion_id)
            if t is None or t.mandant_id != b.mandant_id:
                raise ValueError("Bankumsatz nicht gefunden")
            if t.betrag >= 0:
                raise ValueError("Der Bankumsatz ist keine Auszahlung")
            if db.scalar(select(models.AusgleichZuordnung.id).where(
                    models.AusgleichZuordnung.transaktion_id == t.id).limit(1)) is not None:
                raise ValueError("Der Bankumsatz ist bereits mit offenen Posten abgeglichen")
            if _belegt(db, models.BestellAbloesung.transaktion_id, t.id):
                raise ValueError("Der Bankumsatz ist bereits einer Bestellung zugeordnet")
            standard_betrag, standard_datum = -t.betrag, t.buchungstag
    elif art == AbloeseArt.RECHNUNG.value:
        if posten_id is None or buchung_id is not None or transaktion_id is not None:
            raise ValueError("Bitte die Rechnung (offenen Posten) angeben")
        p = db.get(models.OffenerPosten, posten_id)
        if p is None or p.mandant_id != b.mandant_id:
            raise ValueError("Offener Posten nicht gefunden")
        if p.art != PostenArt.KREDITOR.value:
            raise ValueError("Nur Eingangsrechnungen (Kreditoren-Posten) können eine Bestellung ablösen")
        if p.status == PostenStatus.STORNIERT.value:
            raise ValueError("Der Posten ist storniert")
        if p.forderungsklasse == models.Forderungsklasse.INSOLVENZFORDERUNG.value:
            # Zahlungssperre: die Engine plant den Posten nie – die Bestellung fiele
            # ersatzlos aus dem Plan
            raise ValueError("Der Posten ist eine Insolvenzforderung (Zahlungssperre) und "
                             "kann die Bestellung nicht ablösen")
        abschliessend = True   # der Posten trägt die Zahlung
        standard_betrag, standard_datum = p.betrag_brutto, p.rechnungsdatum or date.today()
    else:
        if quellen:
            raise ValueError("Eine manuelle Ablösung hat keinen Beleg")
        if betrag_d is None or datum_d is None:
            raise ValueError("Für eine manuelle Ablösung sind Betrag und Datum erforderlich")
        standard_betrag, standard_datum = betrag_d, datum_d

    betrag_d = (betrag_d if betrag_d is not None else standard_betrag).quantize(CENT)
    datum_d = datum_d or standard_datum
    if betrag_d <= 0:
        raise ValueError("Der Betrag muss größer als 0 sein")

    br = brutto(b)
    # gespeichert wird nur die ausdrückliche Wahl; ob die Bestellung erledigt ist,
    # entscheidet _abschluss (auch, wenn die Summe den Bruttobetrag deckt)
    abschliessend = bool(abschliessend)
    a = models.BestellAbloesung(
        mandant_id=b.mandant_id,
        art=art,
        buchung_id=buchung_id,
        transaktion_id=transaktion_id,
        posten_id=posten_id,
        betrag=betrag_d,
        datum=datum_d,
        abschliessend=abschliessend,
        notiz=_text(notiz, "notiz", 255),
        benutzer_id=benutzer.id if benutzer is not None else None,
    )
    b.abloesungen.append(a)
    db.flush()
    vorher = b.status
    quelle = ABLOESE_ART_TEXT[art]
    if _abschluss(b) is not None:
        summe = sum((x.betrag for x in b.abloesungen), NULL)
        abw = summe - br
        _status_setzen(db, b, _ABGELOEST)
        b.abgeloest_am = datum_d
        b.abgeloest_durch = art
        text = f"Abgelöst durch {quelle} vom {_datum_de(datum_d)}: {_eur(betrag_d)} €"
        if summe != betrag_d:
            text += f" (insgesamt {_eur(summe)} €)"
        text += (f"; Abweichung zum Bestellbrutto {_eur(br)} €: "
                 f"{'+' if abw > 0 else ''}{_eur(abw)} €")
        journal(db, b.mandant_id, b, benutzer, "ABGELOEST", status_vorher=vorher,
                status_nachher=_ABGELOEST, begruendung=text)
    else:
        _status_setzen(db, b, vorher)   # Bestellung inzwischen anderweitig geändert?
        journal(db, b.mandant_id, b, benutzer, "TEILZAHLUNG", status_vorher=vorher,
                status_nachher=vorher,
                begruendung=(f"Teilzahlung ({quelle}) vom {_datum_de(datum_d)}: {_eur(betrag_d)} €; "
                             f"weiter geplant: {_eur(bestellung_rest(b))} €"))
    return a


def _abschluss(b: models.Bestellung) -> models.BestellAbloesung | None:
    """Ablösung, mit der die Bestellung erledigt ist: die letzte ausdrücklich
    abschließende, sonst die letzte, wenn die Summe aller Ablösungen den
    Bruttobetrag (bis auf 1 Cent) deckt; None = noch offen."""
    if not b.abloesungen:
        return None
    abschliessende = [a for a in b.abloesungen if a.abschliessend]
    if abschliessende:
        return abschliessende[-1]
    summe = sum((a.betrag for a in b.abloesungen), NULL)
    return b.abloesungen[-1] if summe >= brutto(b) - CENT else None


def abloesung_aufheben(db: Session, abloesung: models.BestellAbloesung,
                       benutzer: models.Benutzer | None, grund: str | None = None) -> None:
    """Ablösung entfernen und den Status neu bewerten: abgelöst bleibt die
    Bestellung nur, solange eine ausdrücklich abschließende Ablösung übrig ist oder
    die übrigen den Bruttobetrag decken – sonst wieder FREIGEGEBEN (wenn
    entschieden) bzw. OFFEN."""
    b = abloesung.bestellung
    beschreibung = (f"{ABLOESE_ART_TEXT.get(abloesung.art, abloesung.art)} vom "
                    f"{_datum_de(abloesung.datum)} über {_eur(abloesung.betrag)} € aufgehoben")
    if grund:
        beschreibung += f" – {grund}"
    if abloesung in b.abloesungen:
        b.abloesungen.remove(abloesung)
    db.delete(abloesung)
    db.flush()
    vorher = nachher = b.status
    if b.status == _ABGELOEST:
        abschluss = _abschluss(b)
        if abschluss is not None:
            b.abgeloest_am, b.abgeloest_durch = abschluss.datum, abschluss.art
        else:
            nachher = _FREIGEGEBEN if b.entschieden_am is not None else _OFFEN
            b.abgeloest_am = None
            b.abgeloest_durch = None
    _status_setzen(db, b, nachher)
    journal(db, b.mandant_id, b, benutzer, "ABLOESUNG_AUFGEHOBEN", status_vorher=vorher,
            status_nachher=b.status, begruendung=beschreibung)


def _hook(db: Session, spalte, ids: list[int], benutzer, grund: str) -> int:
    ids = [i for i in ids or [] if i is not None]
    if not ids:
        return 0
    anzahl = 0
    for a in list(db.scalars(select(models.BestellAbloesung).where(spalte.in_(ids)))):
        abloesung_aufheben(db, a, benutzer, grund)
        anzahl += 1
    db.flush()
    return anzahl


def buchungen_entfernt(db: Session, buchung_ids: list[int], benutzer=None) -> int:
    """Vor dem Löschen von Buchungen aufrufen: zugehörige Ablösungen aufheben."""
    return _hook(db, models.BestellAbloesung.buchung_id, buchung_ids, benutzer, "Buchung gelöscht")


def transaktionen_entfernt(db: Session, transaktion_ids: list[int], benutzer=None) -> int:
    """Vor dem Löschen von Bankumsätzen aufrufen: zugehörige Ablösungen aufheben."""
    return _hook(db, models.BestellAbloesung.transaktion_id, transaktion_ids, benutzer,
                 "Bankumsatz gelöscht")


def posten_entfernt(db: Session, posten_ids: list[int], benutzer=None) -> int:
    """Vor dem Löschen offener Posten aufrufen: zugehörige Ablösungen aufheben."""
    return _hook(db, models.BestellAbloesung.posten_id, posten_ids, benutzer,
                 "Rechnung (offener Posten) gelöscht")


def posten_geaendert(db: Session, posten: models.OffenerPosten, benutzer=None) -> int:
    """Nach dem Ändern eines offenen Postens aufrufen: trägt er die Zahlung nicht
    mehr (storniert, keine Eingangsrechnung, Insolvenzforderung mit Zahlungssperre),
    werden Ablösungen durch ihn aufgehoben – die Bestellung steht wieder im Plan."""
    if posten.status == PostenStatus.STORNIERT.value:
        grund = "Rechnung storniert"
    elif posten.art != PostenArt.KREDITOR.value:
        grund = "Posten ist keine Eingangsrechnung mehr"
    elif posten.forderungsklasse == models.Forderungsklasse.INSOLVENZFORDERUNG.value:
        grund = "Rechnung ist Insolvenzforderung (Zahlungssperre)"
    else:
        return 0
    return _hook(db, models.BestellAbloesung.posten_id, [posten.id], benutzer, grund)


# ---------------------------------------------------------------------------
# Ablösekandidaten und Abgleichsvorschläge
# ---------------------------------------------------------------------------

_TYP_RANG = {"POSTEN": 0, "BUCHUNG": 1, "TRANSAKTION": 2}


def _kandidaten_pool(db: Session, mandant: models.Mandant, ab: date | None) -> list[dict]:
    """Mögliche Ablösebelege: Auszahlungsbuchungen, Bankumsätze < 0 und offene
    Kreditoren-Posten, die noch frei sind (vorberechnete Normalformen)."""
    konten = _lade_konten(db, mandant.id)
    finanz_nrn = _finanz_nrn(konten)
    konto_by_nr = {k.nummer: k for k in konten}
    konto_by_id = {k.id: k for k in konten}
    belegt = list(db.execute(
        select(models.BestellAbloesung.buchung_id, models.BestellAbloesung.transaktion_id,
               models.BestellAbloesung.posten_id)
        .where(models.BestellAbloesung.mandant_id == mandant.id)
    ))
    belegt_b = {r[0] for r in belegt if r[0] is not None}
    belegt_t = {r[1] for r in belegt if r[1] is not None}
    belegt_p = {r[2] for r in belegt if r[2] is not None}
    abgeglichen = set(db.scalars(
        select(models.AusgleichZuordnung.transaktion_id)
        .where(models.AusgleichZuordnung.mandant_id == mandant.id)
    ))
    pool: list[dict] = []

    q = select(models.Buchung).where(models.Buchung.mandant_id == mandant.id,
                                     models.Buchung.posten_id.is_(None))
    if ab is not None:
        q = q.where(models.Buchung.datum >= ab)
    for bu in db.scalars(q):
        if bu.id in belegt_b:
            continue
        fluss = _sachfluss(bu, finanz_nrn)
        if fluss is None or fluss[1] >= 0:
            continue
        k = konto_by_nr.get(fluss[0])
        text = " ".join(x for x in (bu.text, bu.belegfeld) if x)
        pool.append({
            "typ": "BUCHUNG", "id": bu.id, "datum": bu.datum, "betrag": -fluss[1],
            "text": bu.text or bu.belegfeld or "",
            "konto": f"{k.nummer} {k.bezeichnung}" if k else f"{fluss[0]} (nicht angelegt)",
            "konto_id": k.id if k else None,
            "_norm": _norm(text), "_tokens": _tokens(text),
        })

    q = select(models.BankTransaktion).where(models.BankTransaktion.mandant_id == mandant.id,
                                             models.BankTransaktion.betrag < 0)
    if ab is not None:
        q = q.where(models.BankTransaktion.buchungstag >= ab)
    for t in db.scalars(q):
        if t.id in belegt_t or t.id in abgeglichen:
            continue
        bank = konto_by_id.get(t.konto_id)
        pool.append({
            "typ": "TRANSAKTION", "id": t.id, "datum": t.buchungstag, "betrag": -t.betrag,
            "text": " · ".join(x for x in (t.partner, t.verwendungszweck) if x),
            "konto": f"{bank.nummer} {bank.bezeichnung}" if bank else None,
            "konto_id": None,
            "_norm": _norm(" ".join(x for x in (t.partner, t.verwendungszweck, t.referenz) if x)),
            "_tokens": _tokens(" ".join(x for x in (t.partner, t.verwendungszweck) if x)),
        })

    for p in db.scalars(select(models.OffenerPosten).where(
            models.OffenerPosten.mandant_id == mandant.id,
            models.OffenerPosten.art == PostenArt.KREDITOR.value,
            models.OffenerPosten.status == PostenStatus.OFFEN.value)):
        # Insolvenzforderungen plant die Engine nie (Zahlungssperre)
        if p.id in belegt_p or p.forderungsklasse == models.Forderungsklasse.INSOLVENZFORDERUNG.value:
            continue
        k = konto_by_id.get(p.konto_id) if p.konto_id else None
        pool.append({
            "typ": "POSTEN", "id": p.id, "datum": p.zahlung_geplant_am or p.faellig_am,
            "beleg_datum": p.rechnungsdatum or p.faellig_am,
            "betrag": p.betrag_brutto,
            "text": " · ".join(x for x in (p.partner, p.belegnr and f"Beleg {p.belegnr}") if x),
            "konto": f"{k.nummer} {k.bezeichnung}" if k else None,
            "konto_id": p.konto_id,
            "_norm": _norm(" ".join(x for x in (p.partner, p.belegnr, p.notiz) if x)),
            "_tokens": _tokens(p.partner),
        })
    return pool


def _bewerte(b: models.Bestellung, rest: Decimal, k: dict) -> tuple[int, list[str]]:
    score = 0
    gruende: list[str] = []
    eigene = _tokens(b.kreditor)
    if eigene:
        gemeinsam = eigene & k["_tokens"]
        if len(gemeinsam) / len(eigene) >= 0.5:
            score += 40
            gruende.append("Kreditor passt")
        elif gemeinsam:
            score += 20
            gruende.append("Kreditor passt teilweise")
    nr = _norm(b.bestellnummer)
    if len(nr) >= 3 and nr in k["_norm"]:
        score += 40
        gruende.append("Bestellnummer im Text")
    if b.konto_id is not None and k["konto_id"] == b.konto_id:
        score += 25
        gruende.append("gleiches Konto")
    betrag = k["betrag"]
    if rest > 0:
        diff = abs(betrag - rest)
        if diff <= CENT:
            score += 35
            gruende.append("Betrag exakt")
        elif diff <= rest * Decimal("0.03"):
            score += 25
            gruende.append(f"Betrag ähnlich (Differenz {_eur(betrag - rest)} €)")
        elif diff <= rest * Decimal("0.15"):
            score += 10
            gruende.append(f"Betrag abweichend (Differenz {_eur(betrag - rest)} €)")
        elif betrag > rest * Decimal("1.2"):
            score -= 20
            gruende.append("Betrag deutlich höher als die Bestellung")
    if abs((k["datum"] - b.zahldatum).days) <= 14:
        score += 10
        gruende.append("Datum nahe am erwarteten Zahldatum")
    return score, gruende


def _kandidat_dict(k: dict, score: int, gruende: list[str]) -> dict:
    return {
        "typ": k["typ"],
        "art": AbloeseArt.RECHNUNG.value if k["typ"] == "POSTEN" else AbloeseArt.ZAHLUNG.value,
        "id": k["id"],
        "datum": _iso(k["datum"]),
        "betrag": _f(k["betrag"]),
        "text": k["text"],
        "konto": k["konto"],
        "score": score,
        "gruende": gruende,
    }


def _bewertete(b: models.Bestellung, pool: list[dict]) -> list[tuple[int, list[str], dict]]:
    ab = b.bestelldatum - timedelta(days=7)
    rest = bestellung_rest(b)
    ergebnis = []
    for k in pool:
        # Zahlungen ab Bestelldatum − 7; Rechnungen nach Rechnungs- bzw. Fälligkeitsdatum
        if k.get("beleg_datum", k["datum"]) < ab:
            continue
        score, gruende = _bewerte(b, rest, k)
        if score > 0:
            ergebnis.append((score, gruende, k))
    ergebnis.sort(key=lambda x: (-x[0], _TYP_RANG[x[2]["typ"]], x[2]["datum"], x[2]["id"]))
    return ergebnis


def abloese_kandidaten(db: Session, mandant: models.Mandant, b: models.Bestellung, *,
                       heute: date | None = None) -> list[dict]:
    """Mögliche Belege zum Ablösen einer Bestellung, bester zuerst (max. 30)."""
    pool = _kandidaten_pool(db, mandant, b.bestelldatum - timedelta(days=7))
    return [_kandidat_dict(k, s, g) for s, g, k in _bewertete(b, pool)[:MAX_KANDIDATEN]]


def abloese_vorschlaege(db: Session, mandant: models.Mandant) -> list[dict]:
    """Je offener/freigegebener Bestellung der beste Beleg ab Score 60 – gierig
    eindeutig (jeder Beleg und jede Bestellung höchstens einmal)."""
    offene = list(db.scalars(
        select(models.Bestellung)
        .options(selectinload(models.Bestellung.abloesungen))
        .where(models.Bestellung.mandant_id == mandant.id,
               models.Bestellung.status.in_((_OFFEN, _FREIGEGEBEN)))
        .order_by(models.Bestellung.zahldatum, models.Bestellung.id)
    ))
    if not offene:
        return []
    pool = _kandidaten_pool(db, mandant, min(b.bestelldatum for b in offene) - timedelta(days=7))
    paare = []
    for b in offene:
        for score, gruende, k in _bewertete(b, pool):
            if score < SCHWELLE_VORSCHLAG:
                break
            paare.append((score, b, k, gruende))
    paare.sort(key=lambda x: (-x[0], _TYP_RANG[x[2]["typ"]], x[1].zahldatum, x[1].id, x[2]["id"]))
    belegt_b: set[int] = set()
    belegt_k: set[tuple[str, int]] = set()
    ergebnis = []
    for score, b, k, gruende in paare:
        if b.id in belegt_b or (k["typ"], k["id"]) in belegt_k:
            continue
        belegt_b.add(b.id)
        belegt_k.add((k["typ"], k["id"]))
        ergebnis.append({
            "bestellung": als_dict(db, b),
            "kandidat": _kandidat_dict(k, score, gruende),
            "score": score,
            "konfidenz": "SICHER" if score >= SCHWELLE_SICHER else "MOEGLICH",
        })
    return ergebnis


# ---------------------------------------------------------------------------
# Bestellungen im Plan (Zellendialog)
# ---------------------------------------------------------------------------

def plan_bestellungen(db: Session, mandant: models.Mandant, *, konto_id: int | None,
                      von: date, bis: date, heute: date | None = None) -> list[dict]:
    """Bestellungen, die im Plan (Mandantenstandard) im Zeitraum stehen – offene
    mit ihrem Rest am Zahldatum bzw. überfällige am Rolltag, abgelöste (außer
    durch Rechnung) wie bezahlte Posten am Zahldatum. konto_id None = Bestellungen
    ohne Konto (Planzeile „Bestellungen (ohne Konto)“)."""
    heute = heute or date.today()
    status = bestell_plan_status(mandant, None) or set()
    roll_ziel = naechster_bankarbeitstag(heute, mandant.bundesland)
    q = (
        select(models.Bestellung)
        .options(selectinload(models.Bestellung.abloesungen), selectinload(models.Bestellung.konto))
        .where(models.Bestellung.mandant_id == mandant.id,
               models.Bestellung.status.in_(sorted(status | {_ABGELOEST})))
    )
    q = q.where(models.Bestellung.konto_id == konto_id) if konto_id is not None \
        else q.where(models.Bestellung.konto_id.is_(None))
    ergebnis = []
    for b in db.scalars(q.order_by(models.Bestellung.zahldatum, models.Bestellung.id)):
        gerollt = False
        if b.status == _ABGELOEST:
            if b.abgeloest_durch == AbloeseArt.RECHNUNG.value or b.abgeloest_am is None:
                continue
            tag, betrag = b.zahldatum, brutto(b)
        else:
            betrag = bestellung_rest(b)
            if betrag <= 0:
                continue
            tag = b.zahldatum
            if tag < heute:
                tag, gerollt = roll_ziel, True
        if von <= tag <= bis:
            eintrag = als_dict(db, b)
            eintrag.update({"plan_tag": tag.isoformat(), "plan_betrag": _f(betrag),
                            "gerollt": gerollt})
            ergebnis.append(eintrag)
    return ergebnis


# ---------------------------------------------------------------------------
# Freigabe-Kennzahlen
# ---------------------------------------------------------------------------

def _snap_info(s: models.PlanSnapshot) -> dict:
    return {"id": s.id, "stichtag": s.stichtag.isoformat(), "wochen": s.wochen,
            "kommentar": s.kommentar, "referenz": bool(s.referenz)}


def _basis_waehlen(db: Session, mandant: models.Mandant, basis, w_von: date, w_bis: date,
                   heute: date) -> tuple[models.PlanSnapshot | None, str | None]:
    """Soll-Basis: (Snapshot | None = LIVE, Hinweis)."""
    def deckt(s: models.PlanSnapshot) -> bool:
        return (s.stichtag <= w_von and s.stichtag + timedelta(days=7 * s.wochen - 1) >= w_bis
                and s.stichtag <= heute)

    if isinstance(basis, str):
        roh = basis.strip()
        if roh.isdigit():
            basis = int(roh)
        elif roh.upper() in ("", "AUTO"):
            basis = None
        elif roh.upper() == "LIVE":
            basis = "LIVE"
        else:
            raise ValueError(f"Unbekannte Basis: {roh}")
    if basis == "LIVE":
        return None, ("Basis ist der aktuelle Plan – die bisherige Abweichung umfasst nur "
                      "die laufende Woche.")
    if isinstance(basis, int) and not isinstance(basis, bool):
        snap = db.get(models.PlanSnapshot, basis)
        if snap is None or snap.mandant_id != mandant.id:
            raise ValueError("Planstand nicht gefunden")
        if deckt(snap):
            return snap, None
        return snap, (f"Der gewählte Planstand vom {_datum_de(snap.stichtag)} deckt die KW "
                      f"nicht (vollständig) ab – Planwerte fehlen dort.")
    if basis is not None:
        raise ValueError("Unbekannte Basis")
    ref = referenz_snapshot(db, mandant.id)
    if ref is not None and deckt(ref):
        return ref, None
    for s in db.scalars(
        select(models.PlanSnapshot)
        .where(models.PlanSnapshot.mandant_id == mandant.id,
               models.PlanSnapshot.stichtag <= heute)
        .order_by(models.PlanSnapshot.stichtag.desc(), models.PlanSnapshot.id.desc())
    ):
        if deckt(s):
            vorwort = (f"Die Soll-Referenz (Planstand vom {_datum_de(ref.stichtag)}) deckt die KW "
                       f"nicht ab" if ref is not None else "Keine Soll-Referenz gewählt")
            return s, (f"{vorwort} – verwendet wird der jüngste passende Planstand vom "
                       f"{_datum_de(s.stichtag)}.")
    return None, ("Kein Planstand deckt die KW ab – Basis ist der aktuelle Plan; die bisherige "
                  "Abweichung umfasst nur die laufende Woche.")


def _extern_explizit(db: Session, mandant: models.Mandant, ist_bis: date,
                     ohne_bestellung_id: int | None):
    """Noch offene explizite Planbeträge (ohne Budget) eines Tages außerhalb des
    Fensters – Sicht wie „gebunden“: Bezahltes, Abgelöstes und Erledigtes ist nicht
    mehr verschiebbar; an vergangenen Tagen steht nichts mehr (Überfälliges liegt auf
    dem Rolltag, von dort bewegt die Verschiebung den gerollten Anteil)."""
    cache: dict[date, dict] = {}

    def verfuegbar(key: Key, tag: date) -> Decimal:
        w = wochen_start(tag)
        if w not in cache:
            cache[w] = plan_fluesse(db, mandant, w, w + timedelta(days=6), heute=ist_bis,
                                    nur_offen=True, mit_budget=False,
                                    bestellungen="FREIGEGEBEN",
                                    ohne_bestellung_id=ohne_bestellung_id)
        return cache[w].get(key, {}).get(tag, NULL)

    return verfuegbar


def _stand_gestern(
    db: Session, mandant: models.Mandant, heute: date, ende: date, roll_tag: date,
    ohne_bestellung_id: int | None, fluesse, gerollt: dict, mehrausgabe: dict[Key, Decimal],
) -> list[tuple[Key, date, Decimal]]:
    """Ist zählt nur bis gestern: Zahlungen und Ablösungen ab heute sind darin noch
    nicht enthalten, die zugehörigen Posten/Bestellungen gelten für die Freigabe
    weiter als offen (Stand Ende gestern). Erledigte Zahlungstermine (ohne
    Erledigungsdatum) bleiben offen, soweit das Ist seit der Basis ihre Zahlung nicht
    als Mehrausgabe des Terminkontos zeigt (`mehrausgabe`: Soll − Ist je Key).
    Ergänzt `fluesse`/`gerollt` und liefert die nicht überfälligen Bestellanteile
    (key, Zahldatum, Betrag)."""
    bestell_anteile: list[tuple[Key, date, Decimal]] = []

    def eintragen(key: Key, zahltag: date, betrag: Decimal) -> date | None:
        if betrag <= 0:
            return None
        d = zahltag if zahltag >= heute else roll_tag
        if d > ende:
            return None
        fluesse[key][d] -= betrag
        if d == roll_tag and zahltag < d:
            gerollt[key] = gerollt.get(key, NULL) - betrag
        return d

    # Kreditoren-Posten: heute gezahlte Anteile (Buchung zum Posten, OP-Ausgleich)
    # und beim heutigen Ausgleich ausgebuchte Reste
    heute_bezahlt: dict[int, Decimal] = defaultdict(Decimal)
    for pid, betrag in db.execute(
        select(models.Buchung.posten_id, models.Buchung.betrag).where(
            models.Buchung.mandant_id == mandant.id, models.Buchung.posten_id.is_not(None),
            models.Buchung.datum >= heute)
    ):
        heute_bezahlt[pid] += betrag
    for pid, betrag in db.execute(
        select(models.AusgleichZuordnung.posten_id, models.AusgleichZuordnung.betrag)
        .join(models.BankTransaktion,
              models.BankTransaktion.id == models.AusgleichZuordnung.transaktion_id)
        .where(models.AusgleichZuordnung.mandant_id == mandant.id,
               models.BankTransaktion.buchungstag >= heute)
    ):
        heute_bezahlt[pid] += betrag
    kandidaten = set(heute_bezahlt) | set(db.scalars(
        select(models.OffenerPosten.id).where(
            models.OffenerPosten.mandant_id == mandant.id,
            models.OffenerPosten.status == PostenStatus.BEZAHLT.value,
            models.OffenerPosten.bezahlt_am >= heute)
    ))
    for pid in sorted(kandidaten):
        p = db.get(models.OffenerPosten, pid)
        if (p is None or p.mandant_id != mandant.id or p.art != PostenArt.KREDITOR.value
                or p.status == PostenStatus.STORNIERT.value
                or p.forderungsklasse == models.Forderungsklasse.INSOLVENZFORDERUNG.value):
            continue
        betrag = heute_bezahlt.get(pid, NULL)
        if p.status == PostenStatus.BEZAHLT.value and p.bezahlt_am and p.bezahlt_am >= heute:
            betrag += max(NULL, p.betrag_brutto - (p.bezahlt_betrag or NULL))
        key = p.konto_id if p.konto_id is not None else f"TERMIN:OP_{p.art}"
        eintragen(key, p.zahlung_geplant_am or p.faellig_am, betrag)

    # freigegebene Bestellungen: Teilzahlungen ab heute, heute abgelöste (außer durch
    # Rechnung – dann trägt der offene Posten die Zahlung)
    for b in db.scalars(
        select(models.Bestellung)
        .options(selectinload(models.Bestellung.abloesungen))
        .where(models.Bestellung.mandant_id == mandant.id,
               models.Bestellung.status.in_((_FREIGEGEBEN, _ABGELOEST)))
    ):
        if b.id == ohne_bestellung_id:
            continue
        key = b.konto_id if b.konto_id is not None else BESTELL_KEY
        if b.status == _FREIGEGEBEN:
            betrag = sum((a.betrag for a in b.abloesungen
                          if not a.abschliessend and a.datum >= heute), NULL)
        elif (b.entschieden_am is not None and b.abgeloest_am is not None
              and b.abgeloest_am >= heute and b.abgeloest_durch != AbloeseArt.RECHNUNG.value):
            betrag = brutto(b) - sum((a.betrag for a in b.abloesungen
                                      if not a.abschliessend and a.datum < heute), NULL)
        else:
            continue
        d = eintragen(key, b.zahldatum, betrag)
        if d is not None and d == b.zahldatum:
            bestell_anteile.append((key, b.zahldatum, betrag))

    # erledigte Zahlungstermine ab heute (nur mit Konto – sonst fehlt der Ist-Bezug)
    igeld = igeld_fenster(mandant)
    gedeckt = {k: max(v, NULL) for k, v in mehrausgabe.items()}
    for t in db.scalars(
        select(models.Zahlungstermin).where(
            models.Zahlungstermin.mandant_id == mandant.id,
            models.Zahlungstermin.status == models.TerminStatus.ERLEDIGT.value,
            models.Zahlungstermin.konto_id.is_not(None),
            models.Zahlungstermin.datum >= heute, models.Zahlungstermin.datum <= ende)
        .order_by(models.Zahlungstermin.datum, models.Zahlungstermin.id)
    ):
        betrag = t.betrag - (termin_entlastung(t, igeld) if igeld else NULL)
        im_ist = min(max(betrag, NULL), gedeckt.get(t.konto_id, NULL))
        gedeckt[t.konto_id] = gedeckt.get(t.konto_id, NULL) - im_ist
        eintragen(t.konto_id, t.datum, betrag - im_ist)
    return bestell_anteile


def _abgeloest_vor_basis(db: Session, mandant: models.Mandant, s0: date, ende: date,
                         ohne_bestellung_id: int | None,
                         nur_vor_basis_abgeloest: bool) -> list[tuple[Key, date, Decimal]]:
    """Abgelöste Bestellungen stehen im Soll mit dem vollen Betrag am Zahldatum (vor s0:
    erster Bankarbeitstag ab s0). Ihre Zahlungen vor s0 enthält das Ist der Prüfung
    nicht – ohne Nachtrag gäbe die früh bezahlte Bestellung ihren Planbetrag wieder
    frei. Ein Planstand führt Bestellungen, die zum Stichtag noch nicht abgelöst
    waren, nur mit ihrem Rest (`nur_vor_basis_abgeloest`). Liefert (key, Plantag, Betrag)."""
    ergebnis: list[tuple[Key, date, Decimal]] = []
    for b in db.scalars(
        select(models.Bestellung)
        .options(selectinload(models.Bestellung.abloesungen))
        .where(models.Bestellung.mandant_id == mandant.id, models.Bestellung.status == _ABGELOEST,
               models.Bestellung.zahldatum <= ende)
    ):
        if (b.id == ohne_bestellung_id or b.abgeloest_am is None
                or b.abgeloest_durch == AbloeseArt.RECHNUNG.value
                or (b.abgeloest_am < s0 and b.zahldatum < s0)
                or (nur_vor_basis_abgeloest and b.abgeloest_am >= s0)):
            continue
        betrag = sum((a.betrag for a in b.abloesungen if a.datum < s0), NULL)
        tag = b.zahldatum if b.zahldatum >= s0 else naechster_bankarbeitstag(s0, mandant.bundesland)
        if betrag > 0 and tag <= ende:
            ergebnis.append((b.konto_id if b.konto_id is not None else BESTELL_KEY, tag, betrag))
    return ergebnis


def _kennzahlen(
    db: Session, mandant: models.Mandant, *, konto_id: int | None, betrag_brutto: Decimal,
    jahr: int, kw: int, basis, ohne_bestellung_id: int | None, heute: date | None,
    im_plan: bool | None, bestellung_brutto: Decimal | None = None,
    bereits_gezahlt: Decimal | None = None,
) -> dict:
    heute = heute or date.today()
    bl = mandant.bundesland
    betrag_brutto = (_dezimal(betrag_brutto, "betrag_brutto") or NULL).quantize(CENT)
    bereits_gezahlt = Decimal(bereits_gezahlt or 0).quantize(CENT)
    bestellung_brutto = (betrag_brutto if bestellung_brutto is None
                         else Decimal(bestellung_brutto).quantize(CENT))
    # 1) Zeitraum
    try:
        w_von = date.fromisocalendar(int(jahr), int(kw), 1)
    except (TypeError, ValueError):
        raise ValueError("Ungültige Kalenderwoche") from None
    w_bis = w_von + timedelta(days=6)
    if w_bis < heute:
        raise ValueError("Die KW liegt in der Vergangenheit")
    ist_bis = heute - timedelta(days=1)
    ab = max(w_von, heute)
    roll_tag = naechster_bankarbeitstag(heute, bl)
    hinweise: list[str] = []

    # 2) Ebenen
    konten = _lade_konten(db, mandant.id)
    konto_by_id = {k.id: k for k in konten}
    konto_by_nr = {k.nummer: k for k in konten}
    finanz_nrn = _finanz_nrn(konten)
    gruppen = list(db.scalars(select(models.KontoGruppe).where(
        models.KontoGruppe.mandant_id == mandant.id)))
    gruppe_fuer_key = gruppen_aufloeser(konto_by_id, gruppen)
    richtung_cache: dict[Key, str | None] = {}

    def ist_aus(key: Key) -> bool:
        if key not in richtung_cache:
            richtung_cache[key] = liquiditaets_richtung(key, konto_by_id, gruppe_fuer_key)
        return richtung_cache[key] == models.Richtung.AUS.value

    ebenen_def: list[tuple[str, str, object]] = []
    if konto_id is not None:
        konto = konto_by_id.get(int(konto_id))
        if konto is None:
            raise ValueError("Konto nicht gefunden")
        ebenen_def.append(("konto", f"{konto.nummer} {konto.bezeichnung}",
                           lambda k, kid=konto.id: k == kid and ist_aus(k)))
        if not ist_aus(konto.id):
            hinweise.append(f"Konto {konto.nummer} ist kein liquiditätswirksames Auszahlungskonto.")
        gruppe = gruppe_fuer_key(konto.id)
        if gruppe is not None:
            ebenen_def.append((
                "gruppe", gruppe.name,
                lambda k, code=gruppe.code: ist_aus(k) and (g := gruppe_fuer_key(k)) is not None
                and g.code == code,
            ))
    ebenen_def.append(("gesamt", "Auszahlungen gesamt", ist_aus))

    # PLAN-Verschiebungen wirken wie im Plan (Snapshots enthalten sie bereits)
    verschiebungen = plan_verschiebungen(db, mandant.id, "PLAN")

    # 3) Basis
    snap, basis_hinweis = _basis_waehlen(db, mandant, basis, w_von, w_bis, heute)
    if basis_hinweis:
        hinweise.append(basis_hinweis)
    # LIVE: Vergangenheit ungerollt wie ein Planstand zum Wochenbeginn, damit sich
    # Überfälliges in C1 (Minderausgabe) und C3 aufhebt
    soll_gerollt: dict[Key, Decimal] = {}
    if snap is not None:
        soll = snapshot_fluesse(snap)
        s0 = snap.stichtag
    else:
        s0 = wochen_start(heute)
        soll = plan_fluesse(db, mandant, s0, w_bis, heute=s0, bestellungen="FREIGEGEBEN",
                            ohne_bestellung_id=ohne_bestellung_id, gerollt=soll_gerollt)
        if verschiebungen:
            for key, _, _, betrag, art, _ in verschiebungen_anwenden(
                soll, verschiebungen, s0, w_bis, s0, naechster_bankarbeitstag(s0, bl),
                dict(soll_gerollt),
                _extern_plan(db, mandant, s0, bestellungen="FREIGEGEBEN",
                             ohne_bestellung_id=ohne_bestellung_id),
            ):
                if art == "rollend":
                    soll_gerollt[key] = soll_gerollt.get(key, NULL) - betrag
    # vor s0 Überfälliges steht im LIVE-Soll am ersten Bankarbeitstag ab s0; ist der
    # noch nicht vorbei und liegt außerhalb der KW, fehlt C3 der Gegenposten
    s0_roll = naechster_bankarbeitstag(s0, bl)
    c3_ohne_gegenposten = snap is None and s0_roll > ist_bis and not (ab <= s0_roll <= w_bis)

    # 6) Ist bis gestern (Schlüssel wie im Soll/Ist-Vergleich)
    ist: dict[Key, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    unbekannt: set[str] = set()
    if s0 <= ist_bis:
        sach, _ = ist_zahlungsfluesse(db, mandant.id, finanz_nrn, s0, ist_bis)
        for nr, tageswerte in sach.items():
            k = konto_by_nr.get(nr)
            if k is not None and (not k.liquiditaetswirksam or k.typ in _FINANZ_TYPEN):
                continue
            key = k.id if k is not None else f"NR:{nr}"
            for d, v in tageswerte.items():
                if k is None:
                    # nicht angelegtes Konto: ohne Gruppe nur Abflüsse als Auszahlung werten
                    v = min(v, NULL)
                    if v:
                        unbekannt.add(nr)
                ist[key][d] += v
    if unbekannt:
        hinweise.append("Ist-Auszahlungen auf nicht angelegten Konten ("
                        + ", ".join(sorted(unbekannt)) + ") zählen nur in der Gesamtebene.")
    vortraege = dauer_vortraege(
        db, mandant, s0, max(w_bis, roll_tag), ist_bis, finanz_nrn,
        verschiebungen=[(v.konto_id, v.von, v.nach, v.betrag) for v in verschiebungen
                        if v.konto_id is not None],
    )

    # 5) gebunden: explizite offene Zahlungen ab heute (ohne Budget)
    e_ende = max(w_bis, roll_tag)
    g: dict[Key, Decimal] = {}
    e_fluesse = plan_fluesse(db, mandant, heute, e_ende, heute=ist_bis, nur_offen=True,
                             mit_budget=False, gerollt=g, bestellungen="FREIGEGEBEN",
                             ohne_bestellung_id=ohne_bestellung_id)
    # vor s0 gezahlte abgelöste Bestellungen am Plantag nachtragen (Ist bzw. gebunden)
    vor_basis: list[tuple[Key, date, Decimal]] = []
    for key, tag, betrag in _abgeloest_vor_basis(db, mandant, s0, e_ende, ohne_bestellung_id,
                                                 nur_vor_basis_abgeloest=snap is not None):
        if tag <= ist_bis:
            ist[key][tag] -= betrag
        elif tag >= heute:
            e_fluesse[key][tag] -= betrag
            vor_basis.append((key, tag, betrag))
    mehrausgabe: dict[Key, Decimal] = defaultdict(Decimal)   # Soll − Ist seit s0
    for quelle, vz in ((soll, 1), (ist, -1)):
        for key, tageswerte in quelle.items():
            for d, v in tageswerte.items():
                if s0 <= d <= ist_bis:
                    mehrausgabe[key] += vz * v
    heute_anteile = _stand_gestern(db, mandant, heute, e_ende, roll_tag, ohne_bestellung_id,
                                   e_fluesse, g, mehrausgabe)
    bewegungen: list[tuple] = []
    if verschiebungen:
        bewegungen = verschiebungen_anwenden(
            e_fluesse, verschiebungen, heute, e_ende, ist_bis, roll_tag, g,
            _extern_explizit(db, mandant, ist_bis, ohne_bestellung_id))
        for key, _, _, betrag, art, _ in bewegungen:
            if art == "rollend":
                g[key] = g.get(key, NULL) - betrag
    # Überfälliges (auf den Rolltag gewandert) zählt in C, nicht in B
    for key, betrag in g.items():
        if betrag:
            e_fluesse[key][roll_tag] -= betrag

    # Aufschlüsselung: nicht überfällige Bestellanteile je Konto/Tag; Verschiebungen
    # nehmen sie mit, begrenzt auf den dort noch gebundenen Betrag
    anteile: dict[tuple[Key, date], Decimal] = defaultdict(Decimal)
    for key, tag, betrag in heute_anteile + vor_basis:
        anteile[(key, tag)] += betrag
    for b in db.scalars(
        select(models.Bestellung)
        .options(selectinload(models.Bestellung.abloesungen))
        .where(models.Bestellung.mandant_id == mandant.id,
               models.Bestellung.status == _FREIGEGEBEN, models.Bestellung.zahldatum >= heute)
    ):
        if b.id == ohne_bestellung_id:
            continue
        rest = bestellung_rest(b)
        if rest > 0:
            anteile[(b.konto_id if b.konto_id is not None else BESTELL_KEY, b.zahldatum)] += rest
    for key, von_tag, nach_tag, betrag, art, _ in bewegungen:
        if art == "fest" and betrag < 0 and anteile.get((key, von_tag), NULL) > 0:
            mit = min(anteile[(key, von_tag)], -betrag)
            anteile[(key, von_tag)] -= mit
            anteile[(key, nach_tag)] += mit
    bestell_anteile: list[tuple[Key, Decimal]] = [
        (key, min(betrag, max(NULL, -e_fluesse.get(key, {}).get(tag, NULL))))
        for (key, tag), betrag in anteile.items() if betrag > 0 and ab <= tag <= w_bis]

    def summe(fluesse, pruefe, von: date, bis: date) -> Decimal:
        s = NULL
        for key, tageswerte in fluesse.items():
            if not pruefe(key):
                continue
            for d, v in tageswerte.items():
                if von <= d <= bis:
                    s += v
        return s

    # 4)–7) je Ebene (positive Auszahlungsbeträge)
    ebenen = []
    werte: dict[str, dict[str, Decimal]] = {}
    for name, bezeichnung, pruefe in ebenen_def:
        soll_kw = -summe(soll, pruefe, w_von, w_bis)
        soll_rest = -summe(soll, pruefe, ab, w_bis)
        gebunden = -summe(e_fluesse, pruefe, ab, w_bis)
        gebunden_best = sum((betrag for key, betrag in bestell_anteile if pruefe(key)), NULL)
        c1 = summe(soll, pruefe, s0, ist_bis) - summe(ist, pruefe, s0, ist_bis) \
            if s0 <= ist_bis else NULL
        c2 = sum((-v["betrag"] for v in vortraege if pruefe(v["konto_id"])), NULL)
        c3 = sum((-betrag for key, betrag in g.items() if pruefe(key)), NULL)
        if c3_ohne_gegenposten:
            c3 -= sum((-betrag for key, betrag in soll_gerollt.items() if pruefe(key)), NULL)
        abweichung = c1 + c2 + c3
        verfuegbar = soll_rest - gebunden - abweichung
        nach = verfuegbar - betrag_brutto
        werte[name] = {"soll_kw": soll_kw, "nach_freigabe": nach}
        ebenen.append({
            "ebene": name,
            "name": bezeichnung,
            "soll_kw": _f(soll_kw),
            "soll_kw_rest": _f(soll_rest),
            "soll_vergangen": _f(soll_kw - soll_rest),
            "gebunden": {"bestellungen": _f(gebunden_best),
                         "sonstige": _f(gebunden - gebunden_best),
                         "summe": _f(gebunden)},
            "abweichung": {"ist_minus_plan": _f(c1), "vortraege": _f(c2),
                           "ueberfaellig": _f(c3), "summe": _f(abweichung)},
            "verfuegbar": _f(verfuegbar),
            "nach_freigabe": _f(nach),
        })
    if "konto" in werte:
        massgeblich = "gruppe" if werte["konto"]["soll_kw"] == 0 and "gruppe" in werte else "konto"
    else:
        massgeblich = "gesamt"
    ampel = "gruen" if werte[massgeblich]["nach_freigabe"] >= 0 else "rot"

    # Bestellungen der KW (laufende KW: auch überfällige)
    def bestell_liste(status: str) -> list[dict]:
        q = select(models.Bestellung).where(
            models.Bestellung.mandant_id == mandant.id, models.Bestellung.status == status,
            models.Bestellung.zahldatum <= w_bis)
        if w_von > heute:
            q = q.where(models.Bestellung.zahldatum >= w_von)
        if konto_id is not None:
            q = q.where(models.Bestellung.konto_id == int(konto_id))
        return [
            {"id": b.id, "kreditor": b.kreditor, "betrag_brutto": _f(brutto(b)),
             "zahldatum": b.zahldatum.isoformat()}
            for b in db.scalars(q.order_by(models.Bestellung.zahldatum, models.Bestellung.id))
            if b.id != ohne_bestellung_id
        ]

    # 8) Liquidität zur Gegenprobe
    if im_plan is None:
        kandidat = db.get(models.Bestellung, ohne_bestellung_id) if ohne_bestellung_id else None
        im_plan = kandidat is not None and bestellung_im_plan(kandidat, mandant)
    start = wochen_start(heute)
    wochen = (w_von - start).days // 7 + 1
    ende_kw = verfuegbar_kw = None
    if wochen <= 26:
        bestaende = berechne_plan(db, mandant, start=start, wochen=wochen, heute=heute)["bestaende"]
        liq = bestaende.get("liquiditaet_vortrag") or bestaende["liquiditaet"]
        verf = bestaende.get("verfuegbar_vortrag") or bestaende["verfuegbar"]
        ende_kw = liq.get(w_bis.isoformat())
        verfuegbar_kw = verf.get(w_bis.isoformat())
    else:
        hinweise.append("Die Liquiditätsvorschau reicht nur 26 Wochen weit.")
    nach_liq = None
    if ende_kw is not None:
        nach_liq = round(ende_kw - (0 if im_plan else float(betrag_brutto)), 2)

    return {
        "kw": {**_kw_info(w_von), "von": w_von.isoformat(), "bis": w_bis.isoformat()},
        "heute": heute.isoformat(),
        "ist_bis": ist_bis.isoformat(),
        "ab": ab.isoformat(),
        "basis": {
            "typ": "SNAPSHOT" if snap is not None else "LIVE",
            "snapshot": _snap_info(snap) if snap is not None else None,
            "s0": s0.isoformat(),
            "hinweis": basis_hinweis,
        },
        "betrag_brutto": _f(betrag_brutto),           # Prüfbetrag (offener Rest)
        "bestellung_brutto": _f(bestellung_brutto),
        "bereits_gezahlt": _f(bereits_gezahlt),
        "ebenen": ebenen,
        "massgeblich": massgeblich,
        "ampel": ampel,
        "freigegebene_bestellungen": bestell_liste(_FREIGEGEBEN),
        "offene_bestellungen": bestell_liste(_OFFEN),
        "liquiditaet": {
            "ende_kw": ende_kw,
            "verfuegbar_ende_kw": verfuegbar_kw,
            "nach_freigabe": nach_liq,
            "bestellung_im_plan": bool(im_plan),
        },
        "hinweise": hinweise,
    }


def freigabe_kennzahlen(
    db: Session,
    mandant: models.Mandant,
    *,
    konto_id: int | None,
    betrag_brutto: Decimal,
    jahr: int,
    kw: int,
    basis: str | int | None = None,
    ohne_bestellung_id: int | None = None,
    heute: date | None = None,
) -> dict:
    """Spielraum einer KW für eine Bestellung (Ebenen Konto/Gruppe/Gesamt):
    verfügbar = Plan der KW ab heute − gebunden (offene explizite Zahlungen der KW)
    − bisherige Abweichung (Plan − Ist bis gestern, Vorträge, Überfälliges)."""
    return _kennzahlen(db, mandant, konto_id=konto_id, betrag_brutto=betrag_brutto, jahr=jahr,
                       kw=kw, basis=basis, ohne_bestellung_id=ohne_bestellung_id, heute=heute,
                       im_plan=None)


def pruefbetrag(b: models.Bestellung, heute: date) -> tuple[Decimal, Decimal]:
    """(Prüfbetrag, bereits gezahlt) der Freigabe: Brutto abzüglich der Teilzahlungen
    vor heute – die stehen schon im Ist bis gestern (bzw. vor der Basis); heutige
    zählen erst morgen im Ist und bleiben im Prüfbetrag."""
    gezahlt = sum((a.betrag for a in b.abloesungen if not a.abschliessend and a.datum < heute),
                  NULL)
    return max(brutto(b) - gezahlt, NULL), gezahlt


def bestellung_kennzahlen(db: Session, mandant: models.Mandant, b: models.Bestellung, *,
                          jahr: int, kw: int, basis: str | int | None = None,
                          heute: date | None = None) -> dict:
    """Freigabe-Kennzahlen einer Bestellung: geprüft wird der offene Rest
    (`betrag_brutto`), dazu `bestellung_brutto` und `bereits_gezahlt`."""
    heute = heute or date.today()
    pruef, gezahlt = pruefbetrag(b, heute)
    return _kennzahlen(db, mandant, konto_id=b.konto_id, betrag_brutto=pruef, jahr=jahr, kw=kw,
                       basis=basis, ohne_bestellung_id=b.id, heute=heute, im_plan=None,
                       bestellung_brutto=brutto(b), bereits_gezahlt=gezahlt)


def _kw_bestellungen(db: Session, mandant: models.Mandant, status: str, w_von: date,
                     w_bis: date, heute: date) -> list[models.Bestellung]:
    q = (
        select(models.Bestellung)
        .options(selectinload(models.Bestellung.abloesungen), selectinload(models.Bestellung.konto))
        .where(models.Bestellung.mandant_id == mandant.id, models.Bestellung.status == status,
               models.Bestellung.zahldatum <= w_bis)
    )
    if w_von > heute:
        q = q.where(models.Bestellung.zahldatum >= w_von)
    return list(db.scalars(q.order_by(models.Bestellung.zahldatum, models.Bestellung.id)))


def freigabe_uebersicht(db: Session, mandant: models.Mandant, *, jahr: int, kw: int,
                        basis: str | int | None = None, heute: date | None = None) -> dict:
    """Rahmen der KW (Ebene gesamt) mit allen noch nicht entschiedenen
    Bestellungen der KW (laufende KW: auch überfällige)."""
    heute = heute or date.today()
    try:
        w_von = date.fromisocalendar(int(jahr), int(kw), 1)
    except (TypeError, ValueError):
        raise ValueError("Ungültige Kalenderwoche") from None
    w_bis = w_von + timedelta(days=6)
    offen = _kw_bestellungen(db, mandant, _OFFEN, w_von, w_bis, heute)
    freigegeben = _kw_bestellungen(db, mandant, _FREIGEGEBEN, w_von, w_bis, heute)
    summe_offen = sum((pruefbetrag(b, heute)[0] for b in offen), NULL)   # offener Rest
    k = _kennzahlen(db, mandant, konto_id=None, betrag_brutto=summe_offen, jahr=jahr, kw=kw,
                    basis=basis, ohne_bestellung_id=None, heute=heute,
                    im_plan=bool(offen) and (bestell_plan_status(mandant, None) or set()) >= {_OFFEN})
    return {
        "kw": k["kw"],
        "heute": k["heute"],
        "ist_bis": k["ist_bis"],
        "basis": k["basis"],
        "gesamt": k["ebenen"][-1],
        "ampel": k["ampel"],
        "liquiditaet": k["liquiditaet"],
        "hinweise": k["hinweise"],
        "offen": [als_dict(db, b) for b in offen],
        "freigegeben": [als_dict(db, b) for b in freigegeben],
        "summe_offen": _f(summe_offen),
    }
