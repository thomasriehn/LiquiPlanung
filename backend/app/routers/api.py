"""JSON-API. Alle Routen erfordern Login; Mandantenrouten prüfen die Berechtigung."""

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .. import database, models
from ..database import get_db
from ..security import (
    aktueller_benutzer,
    audit,
    hash_passwort,
    mandant_oder_403,
    nur_admin,
    nur_schreibend,
    sichere_mandanten_liste,
)
from ..config import get_settings
from ..services import ausgleich, backup, bank, datev, export, insolvenzgeld
from ..services.feiertage import BUNDESLAENDER
from ..services.kontenrahmen import (
    eingebaute_definition,
    lege_kontenrahmen_an,
    lege_standard_szenarien_an,
)
from ..services.feiertage import naechster_bankarbeitstag
from ..services.liquiditaet import (
    berechne_plan,
    erstelle_snapshot,
    soll_ist_vergleich,
    szenarien_vergleich,
    wochen_start,
)
from ..services.zahlungskalender import generiere_termine

router = APIRouter(prefix="/api")


def _dec(wert, feld: str = "betrag") -> Decimal:
    try:
        return Decimal(str(wert)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        raise HTTPException(422, f"Ungültiger Betrag für '{feld}'")


def _datum(wert, feld: str = "datum") -> date:
    try:
        return date.fromisoformat(str(wert))
    except (ValueError, TypeError):
        raise HTTPException(422, f"Ungültiges Datum für '{feld}' (erwartet JJJJ-MM-TT)")


def _enum_pruefen(wert, erlaubt, feld: str):
    werte = [e.value for e in erlaubt] if hasattr(next(iter(erlaubt)), "value") else list(erlaubt)
    if wert not in werte:
        raise HTTPException(422, f"Ungültiger Wert für '{feld}' (erlaubt: {', '.join(werte)})")


VERFAHRENSSTATUS = ["REGELMANDAT", "VORLAEUFIG", "EROEFFNET", "EIGENVERWALTUNG"]


# ---------------------------------------------------------------- Mandanten

class MandantNeu(BaseModel):
    name: str
    kurzname: str
    kontenrahmen: str = "SKR03"
    bundesland: str = "NW"
    ust_zeitraum: str = "MONAT"
    dauerfrist: bool = False
    verfahrensstatus: str = "REGELMANDAT"
    aktenzeichen: str | None = None
    insolvenz_stichtag: str | None = None


@router.get("/mandanten")
def mandanten_liste(
    benutzer=Depends(aktueller_benutzer), db: Session = Depends(get_db)
):
    return [
        {
            "id": m.id,
            "name": m.name,
            "kurzname": m.kurzname,
            "kontenrahmen": m.kontenrahmen,
            "bundesland": m.bundesland,
            "verfahrensstatus": m.verfahrensstatus,
            "aktiv": m.aktiv,
        }
        for m in sichere_mandanten_liste(db, benutzer)
    ]


@router.post("/mandanten", status_code=201)
def mandant_anlegen(
    daten: MandantNeu,
    benutzer=Depends(nur_admin),
    db: Session = Depends(get_db),
):
    if daten.kontenrahmen not in EINGEBAUTE_KONTENRAHMEN and not db.scalar(
        select(models.KontenrahmenVorlage.id).where(
            models.KontenrahmenVorlage.code == daten.kontenrahmen
        )
    ):
        raise HTTPException(
            422, "Unbekannter Kontenrahmen (SKR03, SKR04, EIGEN oder eigene Vorlage)"
        )
    if daten.bundesland not in BUNDESLAENDER:
        raise HTTPException(422, "Unbekanntes Bundesland")
    _enum_pruefen(daten.ust_zeitraum, ["MONAT", "QUARTAL"], "ust_zeitraum")
    _enum_pruefen(daten.verfahrensstatus, VERFAHRENSSTATUS, "verfahrensstatus")
    if db.scalar(select(models.Mandant.id).where(models.Mandant.kurzname == daten.kurzname)):
        raise HTTPException(409, "Kurzname bereits vergeben")
    mandant = models.Mandant(
        name=daten.name,
        kurzname=daten.kurzname,
        kontenrahmen=daten.kontenrahmen,
        bundesland=daten.bundesland,
        ust_zeitraum=daten.ust_zeitraum,
        dauerfrist=daten.dauerfrist,
        verfahrensstatus=daten.verfahrensstatus,
        aktenzeichen=daten.aktenzeichen,
        insolvenz_stichtag=_datum(daten.insolvenz_stichtag, "insolvenz_stichtag")
        if daten.insolvenz_stichtag
        else None,
    )
    db.add(mandant)
    db.flush()
    lege_kontenrahmen_an(db, mandant)
    lege_standard_szenarien_an(db, mandant)
    audit(db, benutzer, mandant.id, "MANDANT_ANGELEGT", mandant.name)
    db.commit()
    return {"id": mandant.id}


MANDANT_FELDER = {
    "name", "bundesland", "ust_zeitraum", "dauerfrist", "verfahrensstatus",
    "aktenzeichen", "aktiv", "insolvenzgeld_aktiv",
}


@router.patch("/mandanten/{mandant_id}")
def mandant_aendern(
    mandant_id: int,
    daten: dict,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    for feld, wert in daten.items():
        if feld in ("insolvenz_stichtag", "insolvenzgeld_von", "insolvenzgeld_bis"):
            setattr(mandant, feld, _datum(wert, feld) if wert else None)
        elif feld in MANDANT_FELDER:
            if feld == "bundesland" and wert not in BUNDESLAENDER:
                raise HTTPException(422, "Unbekanntes Bundesland")
            if feld == "ust_zeitraum":
                _enum_pruefen(wert, ["MONAT", "QUARTAL"], feld)
            if feld == "verfahrensstatus":
                _enum_pruefen(wert, VERFAHRENSSTATUS, feld)
            setattr(mandant, feld, wert)
    audit(db, benutzer, mandant.id, "MANDANT_GEAENDERT", str(sorted(daten.keys())))
    db.commit()
    return {"ok": True}


@router.delete("/mandanten/{mandant_id}")
def mandant_loeschen(
    mandant_id: int,
    bestaetigung: str = "",
    benutzer=Depends(nur_admin),
    db: Session = Depends(get_db),
):
    """Mandanten mit sämtlichen Daten endgültig löschen (nur Admin).

    Zur Bestätigung muss der Kurzname mitgegeben werden
    (`?bestaetigung=<kurzname>`). Das Audit-Log bleibt erhalten, verliert aber
    den Mandantenbezug. Nicht umkehrbar – vorher ggf. Sicherung erstellen.
    """
    mandant = db.get(models.Mandant, mandant_id)
    if mandant is None:
        raise HTTPException(404, "Mandant nicht gefunden")
    if bestaetigung.strip() != mandant.kurzname:
        raise HTTPException(
            422,
            f"Zur Bestätigung den Kurznamen '{mandant.kurzname}' mitgeben "
            "(?bestaetigung=…)",
        )
    mid = mandant.id
    name = f"{mandant.name} ({mandant.kurzname})"

    # Kinder in Fremdschlüssel-Reihenfolge löschen – bewusst ohne DB-Kaskaden,
    # damit es auf PostgreSQL und SQLite gleichermaßen funktioniert.
    fuer_mandant = [
        models.AusgleichZuordnung,
        models.BankTransaktion,
        models.Buchung,
        models.BWAWert,
        models.OffenerPosten,
        models.Dauerbuchung,
        models.BudgetWoche,
        models.Budget,
        models.Zahlungstermin,
        models.TerminRegel,
        models.Bestand,
        models.ImportBatch,
        models.PlanVerschiebung,
    ]
    for modell in fuer_mandant:
        db.execute(delete(modell).where(modell.mandant_id == mid))
    snapshot_ids = select(models.PlanSnapshot.id).where(
        models.PlanSnapshot.mandant_id == mid
    )
    db.execute(delete(models.PlanSnapshotWert).where(
        models.PlanSnapshotWert.snapshot_id.in_(snapshot_ids)))
    db.execute(delete(models.PlanSnapshot).where(models.PlanSnapshot.mandant_id == mid))
    szenario_ids = select(models.Szenario.id).where(models.Szenario.mandant_id == mid)
    db.execute(delete(models.SzenarioRegel).where(
        models.SzenarioRegel.szenario_id.in_(szenario_ids)))
    db.execute(delete(models.Szenario).where(models.Szenario.mandant_id == mid))
    db.execute(delete(models.benutzer_mandanten).where(
        models.benutzer_mandanten.c.mandant_id == mid))
    db.execute(delete(models.Konto).where(models.Konto.mandant_id == mid))
    db.execute(delete(models.KontoGruppe).where(models.KontoGruppe.mandant_id == mid))
    # Audit-Historie behalten, nur den Mandantenverweis lösen
    db.execute(update(models.AuditLog).where(models.AuditLog.mandant_id == mid)
               .values(mandant_id=None))
    db.execute(delete(models.Mandant).where(models.Mandant.id == mid))
    audit(db, benutzer, None, "MANDANT_GELOESCHT", name)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------ Kontenrahmen-Vorlagen

EINGEBAUTE_KONTENRAHMEN = ("SKR03", "SKR04", "EIGEN")
VORLAGE_CODE_MUSTER = r"^[A-Za-z0-9_\-]{2,16}$"


class VorlageNeu(BaseModel):
    code: str
    name: str
    basis: str | None = None  # SKR03/SKR04/EIGEN oder Code einer eigenen Vorlage


class VorlageDefinition(BaseModel):
    name: str | None = None
    gruppen: list[dict] = []
    konten: list[dict] = []


def _vorlage_oder_404(db: Session, code: str) -> models.KontenrahmenVorlage:
    vorlage = db.scalar(
        select(models.KontenrahmenVorlage).where(models.KontenrahmenVorlage.code == code)
    )
    if vorlage is None:
        raise HTTPException(404, "Kontenrahmen-Vorlage nicht gefunden")
    return vorlage


def _vorlage_json(v: models.KontenrahmenVorlage) -> dict:
    return {
        "code": v.code,
        "name": v.name,
        "fest": False,
        "gruppen": [
            {"code": g.code, "name": g.name, "richtung": g.richtung,
             "sortierung": g.sortierung}
            for g in v.gruppen
        ],
        "konten": [
            {
                "nummer": k.nummer, "bezeichnung": k.bezeichnung, "typ": k.typ,
                "ust_satz": float(k.ust_satz) if k.ust_satz is not None else None,
                "gruppe": k.gruppe_code,
                "liquiditaetswirksam": k.liquiditaetswirksam,
            }
            for k in v.konten
        ],
    }


def _vorlage_inhalt_pruefen(gruppen: list[dict], konten: list[dict]) -> None:
    gruppen_codes: set[str] = set()
    for g in gruppen:
        code = str(g.get("code") or "").strip()
        if not code:
            raise HTTPException(422, "Gruppe ohne Code")
        if code in gruppen_codes:
            raise HTTPException(422, f"Gruppencode '{code}' doppelt")
        gruppen_codes.add(code)
        if not str(g.get("name") or "").strip():
            raise HTTPException(422, f"Gruppe '{code}' ohne Namen")
        _enum_pruefen(g.get("richtung", "AUS"), models.Richtung, "richtung")
    nummern: set[str] = set()
    for k in konten:
        nummer = str(k.get("nummer") or "").strip()
        if not nummer:
            raise HTTPException(422, "Konto ohne Nummer")
        if nummer in nummern:
            raise HTTPException(422, f"Kontonummer '{nummer}' doppelt")
        nummern.add(nummer)
        if not str(k.get("bezeichnung") or "").strip():
            raise HTTPException(422, f"Konto {nummer} ohne Bezeichnung")
        _enum_pruefen(k.get("typ", "AUFWAND"), models.KontoTyp, "typ")
        if k.get("gruppe") and k["gruppe"] not in gruppen_codes:
            raise HTTPException(422, f"Konto {nummer}: Gruppe '{k['gruppe']}' nicht definiert")


def _vorlage_inhalt_setzen(
    vorlage: models.KontenrahmenVorlage, gruppen: list[dict], konten: list[dict]
) -> None:
    vorlage.gruppen = [
        models.VorlageGruppe(
            code=str(g["code"]).strip(),
            name=str(g["name"]).strip(),
            richtung=g.get("richtung", "AUS"),
            sortierung=int(g.get("sortierung") or 0),
        )
        for g in gruppen
    ]
    vorlage.konten = [
        models.VorlageKonto(
            nummer=str(k["nummer"]).strip(),
            bezeichnung=str(k["bezeichnung"]).strip(),
            typ=k.get("typ", "AUFWAND"),
            ust_satz=_dec(k["ust_satz"], "ust_satz")
            if k.get("ust_satz") is not None else None,
            gruppe_code=k.get("gruppe") or None,
            liquiditaetswirksam=bool(k.get("liquiditaetswirksam", True)),
        )
        for k in konten
    ]


@router.get("/kontenrahmen")
def kontenrahmen_liste(benutzer=Depends(nur_admin), db: Session = Depends(get_db)):
    eigene = db.scalars(
        select(models.KontenrahmenVorlage).order_by(models.KontenrahmenVorlage.code)
    )
    ergebnis = [
        {"code": code, "name": eingebaute_definition(code)["name"], "fest": True}
        for code in EINGEBAUTE_KONTENRAHMEN
    ]
    ergebnis += [
        {"code": v.code, "name": v.name, "fest": False,
         "gruppen_anzahl": len(v.gruppen), "konten_anzahl": len(v.konten)}
        for v in eigene
    ]
    return ergebnis


@router.get("/kontenrahmen/{code}")
def kontenrahmen_lesen(
    code: str, benutzer=Depends(nur_admin), db: Session = Depends(get_db)
):
    eingebaut = eingebaute_definition(code)
    if eingebaut is not None:
        return eingebaut
    return _vorlage_json(_vorlage_oder_404(db, code))


@router.post("/kontenrahmen", status_code=201)
def kontenrahmen_anlegen(
    daten: VorlageNeu, benutzer=Depends(nur_admin), db: Session = Depends(get_db)
):
    code = daten.code.strip()
    if not re.match(VORLAGE_CODE_MUSTER, code):
        raise HTTPException(
            422, "Code: 2–16 Zeichen, nur Buchstaben, Ziffern, '-' und '_'"
        )
    if code in EINGEBAUTE_KONTENRAHMEN:
        raise HTTPException(409, "Code ist für die eingebauten Vorlagen reserviert")
    if not daten.name.strip():
        raise HTTPException(422, "Name fehlt")
    if db.scalar(
        select(models.KontenrahmenVorlage.id).where(
            models.KontenrahmenVorlage.code == code
        )
    ):
        raise HTTPException(409, "Code bereits vergeben")

    basis = daten.basis or "EIGEN"
    definition = eingebaute_definition(basis)
    if definition is None:
        definition = _vorlage_json(_vorlage_oder_404(db, basis))
    vorlage = models.KontenrahmenVorlage(code=code, name=daten.name.strip())
    _vorlage_inhalt_setzen(vorlage, definition["gruppen"], definition["konten"])
    db.add(vorlage)
    audit(db, benutzer, None, "KONTENRAHMEN_ANGELEGT", f"{code} (Basis {basis})")
    db.commit()
    return {"code": vorlage.code}


@router.put("/kontenrahmen/{code}")
def kontenrahmen_speichern(
    code: str,
    daten: VorlageDefinition,
    benutzer=Depends(nur_admin),
    db: Session = Depends(get_db),
):
    """Ersetzt Gruppen und Konten der Vorlage vollständig. Wirkt nur auf danach
    angelegte Mandanten – bestehende behalten ihre Kopie des Kontenrahmens."""
    if code in EINGEBAUTE_KONTENRAHMEN:
        raise HTTPException(409, "Eingebaute Kontenrahmen sind nicht änderbar")
    vorlage = _vorlage_oder_404(db, code)
    _vorlage_inhalt_pruefen(daten.gruppen, daten.konten)
    if daten.name is not None and daten.name.strip():
        vorlage.name = daten.name.strip()
    # alte Zeilen erst entfernen (Flush), sonst kollidieren die Unique-Constraints
    vorlage.gruppen.clear()
    vorlage.konten.clear()
    db.flush()
    _vorlage_inhalt_setzen(vorlage, daten.gruppen, daten.konten)
    audit(db, benutzer, None, "KONTENRAHMEN_GEAENDERT",
          f"{code}: {len(daten.gruppen)} Gruppen, {len(daten.konten)} Konten")
    db.commit()
    return {"ok": True}


@router.delete("/kontenrahmen/{code}")
def kontenrahmen_loeschen(
    code: str, benutzer=Depends(nur_admin), db: Session = Depends(get_db)
):
    if code in EINGEBAUTE_KONTENRAHMEN:
        raise HTTPException(409, "Eingebaute Kontenrahmen sind nicht löschbar")
    vorlage = _vorlage_oder_404(db, code)
    db.delete(vorlage)
    audit(db, benutzer, None, "KONTENRAHMEN_GELOESCHT", code)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Konten

@router.get("/mandanten/{mandant_id}/konten")
def konten_liste(
    mandant_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    gruppen = list(
        db.scalars(
            select(models.KontoGruppe)
            .where(models.KontoGruppe.mandant_id == mandant.id)
            .order_by(models.KontoGruppe.sortierung)
        )
    )
    konten = list(
        db.scalars(
            select(models.Konto)
            .where(models.Konto.mandant_id == mandant.id)
            .order_by(models.Konto.nummer)
        )
    )
    return {
        "gruppen": [
            {"id": g.id, "code": g.code, "name": g.name, "richtung": g.richtung,
             "sortierung": g.sortierung, "sondereffekt": g.sondereffekt}
            for g in gruppen
        ],
        "konten": [
            {
                "id": k.id,
                "nummer": k.nummer,
                "bezeichnung": k.bezeichnung,
                "typ": k.typ,
                "ust_satz": float(k.ust_satz) if k.ust_satz is not None else None,
                "gruppe_id": k.gruppe_id,
                "kreditlinie": float(k.kreditlinie or 0),
                "iban": k.iban,
                "verteilung": k.verteilung,
                "liquiditaetswirksam": k.liquiditaetswirksam,
                "aktiv": k.aktiv,
            }
            for k in konten
        ],
    }


class KontoNeu(BaseModel):
    nummer: str
    bezeichnung: str
    typ: str = "AUFWAND"
    ust_satz: float | None = None
    gruppe_id: int | None = None
    kreditlinie: float = 0
    liquiditaetswirksam: bool = True


@router.post("/mandanten/{mandant_id}/konten", status_code=201)
def konto_anlegen(
    mandant_id: int,
    daten: KontoNeu,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    if daten.typ not in [t.value for t in models.KontoTyp]:
        raise HTTPException(422, "Unbekannter Kontotyp")
    nummer = daten.nummer.strip()
    if not nummer:
        raise HTTPException(422, "Kontonummer fehlt")
    if db.scalar(
        select(models.Konto.id).where(
            models.Konto.mandant_id == mandant.id, models.Konto.nummer == nummer
        )
    ):
        raise HTTPException(409, "Kontonummer bereits vorhanden")
    if daten.gruppe_id is not None:
        gruppe = db.get(models.KontoGruppe, daten.gruppe_id)
        if gruppe is None or gruppe.mandant_id != mandant.id:
            raise HTTPException(422, "Ungültige Gruppe")
    konto = models.Konto(
        mandant_id=mandant.id,
        nummer=nummer,
        bezeichnung=daten.bezeichnung.strip(),
        typ=daten.typ,
        ust_satz=_dec(daten.ust_satz, "ust_satz") if daten.ust_satz is not None else None,
        gruppe_id=daten.gruppe_id,
        kreditlinie=_dec(daten.kreditlinie, "kreditlinie"),
        liquiditaetswirksam=daten.liquiditaetswirksam,
    )
    db.add(konto)
    audit(db, benutzer, mandant.id, "KONTO_ANGELEGT", daten.nummer)
    db.commit()
    return {"id": konto.id}


KONTO_FELDER = {"bezeichnung", "typ", "gruppe_id", "liquiditaetswirksam", "aktiv"}


@router.patch("/konten/{konto_id}")
def konto_aendern(
    konto_id: int,
    daten: dict,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    konto = db.get(models.Konto, konto_id)
    if konto is None:
        raise HTTPException(404, "Konto nicht gefunden")
    mandant_oder_403(db, benutzer, konto.mandant_id)
    for feld, wert in daten.items():
        if feld == "ust_satz":
            konto.ust_satz = _dec(wert, feld) if wert is not None and wert != "" else None
        elif feld == "kreditlinie":
            konto.kreditlinie = _dec(wert, feld)
        elif feld == "iban":
            konto.iban = bank.normalisiere_iban(str(wert)) or None if wert else None
        elif feld == "verteilung":
            from ..services.liquiditaet import VERTEILUNGEN

            _enum_pruefen(wert, VERTEILUNGEN, feld)
            konto.verteilung = wert
        elif feld == "typ":
            if wert not in [t.value for t in models.KontoTyp]:
                raise HTTPException(422, "Unbekannter Kontotyp")
            konto.typ = wert
        elif feld == "gruppe_id":
            if wert is not None:
                gruppe = db.get(models.KontoGruppe, wert)
                if gruppe is None or gruppe.mandant_id != konto.mandant_id:
                    raise HTTPException(422, "Ungültige Gruppe")
            konto.gruppe_id = wert
        elif feld in KONTO_FELDER:
            setattr(konto, feld, wert)
    audit(db, benutzer, konto.mandant_id, "KONTO_GEAENDERT", konto.nummer)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- BWA-Gruppen

class GruppeNeu(BaseModel):
    code: str
    name: str
    richtung: str = "AUS"
    sortierung: int = 0
    sondereffekt: bool = False


def _gruppe_oder_404(db: Session, gruppe_id: int) -> models.KontoGruppe:
    gruppe = db.get(models.KontoGruppe, gruppe_id)
    if gruppe is None:
        raise HTTPException(404, "Gruppe nicht gefunden")
    return gruppe


@router.post("/mandanten/{mandant_id}/gruppen", status_code=201)
def gruppe_anlegen(
    mandant_id: int,
    daten: GruppeNeu,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    code = daten.code.strip()
    if not code:
        raise HTTPException(422, "Code fehlt")
    _enum_pruefen(daten.richtung, models.Richtung, "richtung")
    if db.scalar(
        select(models.KontoGruppe.id).where(
            models.KontoGruppe.mandant_id == mandant.id,
            models.KontoGruppe.code == code,
        )
    ):
        raise HTTPException(409, "Gruppencode bereits vorhanden")
    gruppe = models.KontoGruppe(
        mandant_id=mandant.id,
        code=code,
        name=daten.name.strip(),
        richtung=daten.richtung,
        sortierung=daten.sortierung,
        sondereffekt=daten.sondereffekt,
    )
    db.add(gruppe)
    audit(db, benutzer, mandant.id, "GRUPPE_ANGELEGT", code)
    db.commit()
    return {"id": gruppe.id}


@router.patch("/gruppen/{gruppe_id}")
def gruppe_aendern(
    gruppe_id: int,
    daten: dict,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    gruppe = _gruppe_oder_404(db, gruppe_id)
    mandant_oder_403(db, benutzer, gruppe.mandant_id)
    for feld, wert in daten.items():
        if feld == "richtung":
            _enum_pruefen(wert, models.Richtung, feld)
            gruppe.richtung = wert
        elif feld == "sortierung":
            gruppe.sortierung = int(wert or 0)
        elif feld == "sondereffekt":
            gruppe.sondereffekt = bool(wert)
        elif feld == "code":
            code = str(wert).strip()
            if not code:
                raise HTTPException(422, "Code fehlt")
            doppelt = db.scalar(
                select(models.KontoGruppe.id).where(
                    models.KontoGruppe.mandant_id == gruppe.mandant_id,
                    models.KontoGruppe.code == code,
                    models.KontoGruppe.id != gruppe.id,
                )
            )
            if doppelt:
                raise HTTPException(409, "Gruppencode bereits vorhanden")
            gruppe.code = code
        elif feld == "name":
            if not str(wert or "").strip():
                raise HTTPException(422, "Name fehlt")
            gruppe.name = str(wert).strip()
    db.commit()
    return {"ok": True}


@router.delete("/gruppen/{gruppe_id}")
def gruppe_loeschen(
    gruppe_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Gruppe entfernen: zugeordnete Konten bleiben (ohne Gruppe) erhalten,
    Szenario-Detailregeln auf die Gruppe werden mitgelöscht."""
    nur_schreibend(benutzer)
    gruppe = _gruppe_oder_404(db, gruppe_id)
    mandant_oder_403(db, benutzer, gruppe.mandant_id)
    for konto in db.scalars(
        select(models.Konto).where(models.Konto.gruppe_id == gruppe.id)
    ):
        konto.gruppe_id = None
    for kind in db.scalars(
        select(models.KontoGruppe).where(models.KontoGruppe.parent_id == gruppe.id)
    ):
        kind.parent_id = None
    db.execute(
        delete(models.SzenarioRegel).where(models.SzenarioRegel.gruppe_id == gruppe.id)
    )
    audit(db, benutzer, gruppe.mandant_id, "GRUPPE_GELOESCHT", gruppe.code)
    db.delete(gruppe)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Import

@router.post("/mandanten/{mandant_id}/import")
async def import_datei(
    mandant_id: int,
    datei: UploadFile,
    format: str = "auto",
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    daten = await datei.read()
    name = datei.filename or "import.csv"
    if format == "auto" and bank.ist_bankformat(daten):
        raise HTTPException(
            422,
            "Die Datei ist ein Kontoauszug (MT940/CAMT) – bitte den Bereich "
            "„Kontoauszug importieren“ verwenden.",
        )
    if format == "datev":
        erg = datev.parse_datev(daten)
    elif format == "csv":
        erg = datev.parse_generisches_csv(daten)
    elif format == "bwa":
        erg = datev.parse_bwa_csv(daten)
    else:
        erg = datev.parse_automatisch(name, daten)

    batch = models.ImportBatch(
        mandant_id=mandant.id,
        dateiname=name,
        format=erg.format,
        anzahl=len(erg.buchungen) + len(erg.bwa_werte),
        warnungen="\n".join(erg.warnungen[:200]) or None,
        benutzer_id=benutzer.id,
    )
    db.add(batch)
    db.flush()

    bekannte = {
        k.nummer
        for k in db.scalars(select(models.Konto).where(models.Konto.mandant_id == mandant.id))
    }
    unbekannt: set[str] = set()
    for b in erg.buchungen:
        db.add(
            models.Buchung(
                mandant_id=mandant.id,
                batch_id=batch.id,
                datum=b.datum,
                konto_nr=b.konto_nr,
                gegenkonto_nr=b.gegenkonto_nr,
                betrag=b.betrag,
                sh=b.sh,
                belegfeld=b.belegfeld,
                text=b.text,
            )
        )
        for nr in (b.konto_nr, b.gegenkonto_nr):
            if nr and nr not in bekannte:
                unbekannt.add(nr)
    for w in erg.bwa_werte:
        db.execute(
            delete(models.BWAWert).where(
                models.BWAWert.mandant_id == mandant.id,
                models.BWAWert.konto_nr == w["konto_nr"],
                models.BWAWert.jahr == w["jahr"],
                models.BWAWert.monat == w["monat"],
            )
        )
        db.add(models.BWAWert(mandant_id=mandant.id, **w))
        if w["konto_nr"] not in bekannte:
            unbekannt.add(w["konto_nr"])

    audit(db, benutzer, mandant.id, "IMPORT", f"{name} ({batch.anzahl} Sätze)")
    db.commit()
    return {
        "batch_id": batch.id,
        "format": erg.format,
        "anzahl": batch.anzahl,
        "warnungen": erg.warnungen[:50],
        "unbekannte_konten": sorted(unbekannt),
    }


@router.get("/mandanten/{mandant_id}/importe")
def import_liste(
    mandant_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    batches = db.scalars(
        select(models.ImportBatch)
        .where(models.ImportBatch.mandant_id == mandant.id)
        .order_by(models.ImportBatch.erstellt_am.desc())
    )
    return [
        {
            "id": b.id,
            "dateiname": b.dateiname,
            "format": b.format,
            "anzahl": b.anzahl,
            "warnungen": b.warnungen,
            "erstellt_am": b.erstellt_am.isoformat(),
        }
        for b in batches
    ]


@router.delete("/importe/{batch_id}")
def import_loeschen(
    batch_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    batch = db.get(models.ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Import nicht gefunden")
    mandant_oder_403(db, benutzer, batch.mandant_id)
    # Zahlungsbuchungen mit Posten-Referenz: Anrechnung zurücknehmen
    for b in db.scalars(
        select(models.Buchung).where(
            models.Buchung.batch_id == batch.id,
            models.Buchung.posten_id.is_not(None),
        )
    ):
        _zahlungs_anteil_zuruecknehmen(db, b)
    db.execute(delete(models.Buchung).where(models.Buchung.batch_id == batch.id))
    db.execute(
        delete(models.BankTransaktion).where(models.BankTransaktion.batch_id == batch.id)
    )
    audit(db, benutzer, batch.mandant_id, "IMPORT_GELOESCHT", batch.dateiname)
    db.delete(batch)
    db.commit()
    return {"ok": True}


@router.get("/importe/{batch_id}/saetze")
def import_saetze(
    batch_id: int,
    limit: int = 1000,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Die Sätze eines Imports: Buchungen (DATEV/CSV/PDF) bzw. Bankumsätze
    (MT940/CAMT). BWA-Werte werden je Konto/Monat fortgeschrieben und hängen
    an keinem Import."""
    batch = db.get(models.ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Import nicht gefunden")
    mandant_oder_403(db, benutzer, batch.mandant_id)
    limit = max(1, min(limit, 5000))
    if batch.format in ("MT940", "CAMT"):
        umsaetze = list(db.scalars(
            select(models.BankTransaktion)
            .where(models.BankTransaktion.batch_id == batch.id)
            .order_by(models.BankTransaktion.buchungstag, models.BankTransaktion.id)
            .limit(limit + 1)
        ))
        return {
            "format": batch.format,
            "mehr": len(umsaetze) > limit,
            "bank_umsaetze": [
                {
                    "id": u.id,
                    "buchungstag": u.buchungstag.isoformat(),
                    "betrag": float(u.betrag),
                    "partner": u.partner,
                    "verwendungszweck": u.verwendungszweck,
                }
                for u in umsaetze[:limit]
            ],
        }
    if batch.format == "BWA":
        return {"format": "BWA", "mehr": False, "hinweis": (
            "BWA-Werte werden je Konto und Monat fortgeschrieben und sind "
            "keinem Import zugeordnet.")}
    buchungen = list(db.scalars(
        select(models.Buchung)
        .where(models.Buchung.batch_id == batch.id)
        .order_by(models.Buchung.datum, models.Buchung.id)
        .limit(limit + 1)
    ))
    return {
        "format": batch.format,
        "mehr": len(buchungen) > limit,
        "buchungen": [
            {
                "id": b.id,
                "datum": b.datum.isoformat(),
                "konto_nr": b.konto_nr,
                "gegenkonto_nr": b.gegenkonto_nr,
                "betrag": float(b.betrag),
                "sh": b.sh,
                "belegfeld": b.belegfeld,
                "text": b.text,
                # Zahlungsbuchung zu einem offenen Posten: Betrag nicht änderbar
                "posten_id": b.posten_id,
            }
            for b in buchungen[:limit]
        ],
    }


@router.get("/importe/{batch_id}/pdf-ansicht")
def import_pdf_ansicht(
    batch_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Zuordnungsansicht eines PDF-Imports wiederherstellen („ansehen“).

    Die gespeicherte PDF-Datei wird erneut ausgewertet; zu jedem Umsatz werden
    die in diesem Import getroffenen Zuordnungen (Konto bzw. offener Posten)
    mitgeliefert, damit sie angezeigt und per ✕ aufgehoben oder nachträglich
    geändert werden können. In anderen Importen verbuchte Umsätze bleiben
    gesperrt."""
    batch = db.get(models.ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Import nicht gefunden")
    mandant_oder_403(db, benutzer, batch.mandant_id)
    if batch.format != "PDF":
        raise HTTPException(422, "Die Zuordnungsansicht gibt es nur für PDF-Importe")
    if not batch.datei:
        raise HTTPException(
            422, "Für diesen älteren Import ist die PDF-Datei nicht gespeichert – "
            "den Auszug bitte einmal neu hochladen",
        )
    from ..services import pdf_kontoauszug

    erg = pdf_kontoauszug.parse_datei(batch.datei)
    eigene: dict[tuple, list[models.Buchung]] = {}
    for b in db.scalars(
        select(models.Buchung).where(models.Buchung.batch_id == batch.id)
    ):
        eigene.setdefault((b.datum, b.text), []).append(b)
    verbucht_alle = _pdf_verbuchte_schluessel(db, batch.mandant_id)
    konten_namen = {
        k.nummer: f"{k.nummer} {k.bezeichnung}"
        for k in db.scalars(
            select(models.Konto).where(models.Konto.mandant_id == batch.mandant_id)
        )
    }

    umsaetze = []
    for u in erg.umsaetze:
        schluessel = (u.buchungstag, _pdf_umsatz_text(u.empfaenger, u.verwendungszweck))
        zugeordnet = eigene.get(schluessel, [])
        zuordnung = None
        if zugeordnet:
            posten_anteile = []
            konto_teile = []
            for b in zugeordnet:
                if b.posten_id is not None:
                    p = db.get(models.OffenerPosten, b.posten_id)
                    posten_anteile.append({
                        "partner": p.partner if p else f"Posten #{b.posten_id}",
                        "status": p.status if p else "?",
                        "betrag": float(b.betrag),
                    })
                else:
                    sachkonto_nr = b.konto_nr if u.betrag < 0 else b.gegenkonto_nr
                    name = konten_namen.get(sachkonto_nr, sachkonto_nr)
                    # bei Aufteilung auf mehrere Konten: Anteil mit ausweisen
                    if len(zugeordnet) > 1:
                        name += f" ({b.betrag:.2f} €)".replace(".", ",")
                    konto_teile.append(name)
            zuordnung = {
                "buchungen": [b.id for b in zugeordnet],
                "konto": ", ".join(konto_teile) or None,
                "posten": posten_anteile,
            }
        umsaetze.append({
            "auszug_nr": u.auszug_nr,
            "auszugsdatum": u.auszugsdatum.isoformat(),
            "buchungstag": u.buchungstag.isoformat(),
            "verwendungszweck": u.verwendungszweck,
            "empfaenger": u.empfaenger,
            "betrag": float(u.betrag),
            "sammelauftrag": u.sammelauftrag or None,
            "zuordnung": zuordnung,
            # in einem anderen Import verbucht -> gesperrt
            "bereits_verbucht": zuordnung is None and _pdf_bereits_verbucht(u, verbucht_alle),
        })
    return {
        "batch_id": batch.id,
        "dateiname": batch.dateiname,
        "warnungen": erg.warnungen[:20],
        "hinweise": erg.hinweise[:20],
        "umsaetze": umsaetze,
    }


def _zahlungs_anteil_zuruecknehmen(db: Session, buchung: models.Buchung) -> None:
    """Anrechnung einer Zahlungsbuchung vom Posten abziehen (vor dem Löschen)."""
    if buchung.posten_id is None:
        return
    posten = db.get(models.OffenerPosten, buchung.posten_id)
    if posten is None:
        return
    posten.bezahlt_betrag = max(
        Decimal("0"), (posten.bezahlt_betrag or Decimal("0")) - buchung.betrag
    )
    posten.status = models.PostenStatus.OFFEN.value
    posten.bezahlt_am = None


BUCHUNG_FELDER = {"konto_nr", "gegenkonto_nr", "belegfeld", "text"}


@router.patch("/buchungen/{buchung_id}")
def buchung_aendern(
    buchung_id: int,
    daten: dict,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Einzelnen importierten Buchungssatz korrigieren (Datum, Konten, Betrag,
    S/H, Belegfeld, Text). Bei Zahlungsbuchungen zu einem offenen Posten ist
    der Betrag gesperrt – dort Zuordnung aufheben und neu erfassen."""
    nur_schreibend(benutzer)
    buchung = db.get(models.Buchung, buchung_id)
    if buchung is None:
        raise HTTPException(404, "Buchung nicht gefunden")
    mandant_oder_403(db, benutzer, buchung.mandant_id)
    for feld, wert in daten.items():
        if feld == "datum":
            buchung.datum = _datum(wert, feld)
        elif feld == "betrag":
            if buchung.posten_id is not None:
                raise HTTPException(
                    422, "Betrag einer Posten-Zahlung ist gesperrt – Zuordnung "
                    "aufheben und neu erfassen",
                )
            betrag = _dec(wert)
            if betrag <= 0:
                raise HTTPException(422, "Betrag muss größer 0 sein")
            buchung.betrag = betrag
        elif feld == "sh":
            if wert not in ("S", "H"):
                raise HTTPException(422, "S/H muss S oder H sein")
            buchung.sh = wert
        elif feld in BUCHUNG_FELDER:
            wert = (str(wert).strip() if wert is not None else "")
            if feld in ("konto_nr", "gegenkonto_nr") and not wert:
                raise HTTPException(422, "Konto/Gegenkonto darf nicht leer sein")
            setattr(buchung, feld, wert or None)
    audit(db, benutzer, buchung.mandant_id, "BUCHUNG_GEAENDERT",
          f"Buchung {buchung.id}: {sorted(daten.keys())}")
    db.commit()
    return {"ok": True}


@router.get("/mandanten/{mandant_id}/plan/ist-buchungen")
def plan_ist_buchungen(
    mandant_id: int,
    key: str,
    von: str,
    bis: str,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Buchungen hinter einer Ist-Zelle des 13-Wochen-Plans: Zahlungsflüsse
    (genau eine Seite Bank/Kasse) auf dem Sachkonto der Planzeile im Zeitraum."""
    from ..services.liquiditaet import _sachfluss

    mandant = mandant_oder_403(db, benutzer, mandant_id)
    konten = list(db.scalars(select(models.Konto).where(models.Konto.mandant_id == mandant.id)))
    if key.startswith("NR:"):
        nummer = key[3:]
    else:
        konto = next((k for k in konten if str(k.id) == key), None)
        if konto is None:
            raise HTTPException(404, "Zeile ohne Konto – keine Ist-Buchungen zuordenbar")
        nummer = konto.nummer
    finanz_nrn = {k.nummer for k in konten
                  if k.typ in (models.KontoTyp.BANK.value, models.KontoTyp.KASSE.value)}
    namen = {k.nummer: k.bezeichnung for k in konten}
    batches = {b.id: b for b in db.scalars(
        select(models.ImportBatch).where(models.ImportBatch.mandant_id == mandant.id))}
    ergebnis = []
    for b in db.scalars(
        select(models.Buchung)
        .where(
            models.Buchung.mandant_id == mandant.id,
            models.Buchung.datum >= _datum(von, "von"),
            models.Buchung.datum <= _datum(bis, "bis"),
            (models.Buchung.konto_nr == nummer) | (models.Buchung.gegenkonto_nr == nummer),
        )
        .order_by(models.Buchung.datum, models.Buchung.id)
    ):
        fluss = _sachfluss(b, finanz_nrn)
        if fluss is None or fluss[0] != nummer:
            continue
        finanz = b.gegenkonto_nr if b.konto_nr == nummer else b.konto_nr
        batch = batches.get(b.batch_id)
        ergebnis.append({
            "id": b.id,
            "datum": b.datum.isoformat(),
            "fluss": float(fluss[1]),
            "text": b.text,
            "belegfeld": b.belegfeld,
            "sachkonto": f"{nummer} {namen.get(nummer, '(nicht angelegt)')}",
            "finanzkonto": f"{finanz} {namen.get(finanz, '')}".strip(),
            "posten_id": b.posten_id,
            "import": f"{batch.dateiname} ({batch.format})" if batch else None,
        })
    return ergebnis


class SachkontoWahl(BaseModel):
    konto_id: int


@router.post("/buchungen/{buchung_id}/sachkonto")
def buchung_sachkonto_setzen(
    buchung_id: int,
    daten: SachkontoWahl,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Kontoverknüpfung einer Zahlungsbuchung neu festlegen: die Sachkonto-
    Seite (nicht Bank/Kasse) wird auf das gewählte Konto umgebucht."""
    from ..services.liquiditaet import _sachfluss

    nur_schreibend(benutzer)
    buchung = db.get(models.Buchung, buchung_id)
    if buchung is None:
        raise HTTPException(404, "Buchung nicht gefunden")
    mandant_oder_403(db, benutzer, buchung.mandant_id)
    konto = db.get(models.Konto, daten.konto_id)
    if konto is None or konto.mandant_id != buchung.mandant_id:
        raise HTTPException(404, "Konto nicht gefunden")
    finanz_typen = (models.KontoTyp.BANK.value, models.KontoTyp.KASSE.value)
    if konto.typ in finanz_typen:
        raise HTTPException(422, "Bitte ein Sachkonto wählen (nicht Bank/Kasse)")
    finanz_nrn = {k.nummer for k in db.scalars(select(models.Konto).where(
        models.Konto.mandant_id == buchung.mandant_id, models.Konto.typ.in_(finanz_typen)))}
    fluss = _sachfluss(buchung, finanz_nrn)
    if fluss is None:
        raise HTTPException(422, "Keine Zahlungsbuchung (genau eine Seite Bank/Kasse)")
    alt = fluss[0]
    if buchung.konto_nr == alt:
        buchung.konto_nr = konto.nummer
    else:
        buchung.gegenkonto_nr = konto.nummer
    audit(db, benutzer, buchung.mandant_id, "BUCHUNG_UMGEBUCHT",
          f"Buchung {buchung.id}: Sachkonto {alt} → {konto.nummer}")
    db.commit()
    return {"ok": True, "konto": f"{konto.nummer} {konto.bezeichnung}"}


@router.delete("/buchungen/{buchung_id}")
def buchung_loeschen(
    buchung_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Einzelnen Buchungssatz löschen. Zahlungsbuchungen geben ihre Anrechnung
    an den offenen Posten zurück; der Import-Zähler wird mitgeführt."""
    nur_schreibend(benutzer)
    buchung = db.get(models.Buchung, buchung_id)
    if buchung is None:
        raise HTTPException(404, "Buchung nicht gefunden")
    mandant_oder_403(db, benutzer, buchung.mandant_id)
    _zahlungs_anteil_zuruecknehmen(db, buchung)
    if buchung.batch_id:
        batch = db.get(models.ImportBatch, buchung.batch_id)
        if batch is not None and batch.anzahl > 0:
            batch.anzahl -= 1
    audit(db, benutzer, buchung.mandant_id, "BUCHUNG_GELOESCHT",
          f"{buchung.datum} {buchung.konto_nr}/{buchung.gegenkonto_nr} "
          f"{buchung.betrag:.2f} €")
    db.delete(buchung)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Bankimport

@router.post("/mandanten/{mandant_id}/bank-import")
async def bank_import(
    mandant_id: int,
    datei: UploadFile,
    konto_id: int | None = None,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Kontoauszugsimport (MT940/CAMT.053): Umsätze speichern, Endsalden als
    Bestandsanker übernehmen. Zuordnung über IBAN am Bankkonto oder konto_id."""
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    _konto_pruefen(db, mandant.id, konto_id)
    daten = await datei.read()
    erg = bank.parse_bank_automatisch(daten)
    if not erg.auszuege:
        raise HTTPException(422, "; ".join(erg.warnungen) or "Keine Auszüge gefunden")

    bankkonten = [
        k
        for k in db.scalars(
            select(models.Konto).where(
                models.Konto.mandant_id == mandant.id,
                models.Konto.typ.in_([models.KontoTyp.BANK.value, models.KontoTyp.KASSE.value]),
                models.Konto.aktiv.is_(True),
            )
        )
    ]
    nach_iban = {bank.normalisiere_iban(k.iban): k for k in bankkonten if k.iban}
    gewaehlt = db.get(models.Konto, konto_id) if konto_id else None

    batch = models.ImportBatch(
        mandant_id=mandant.id,
        dateiname=datei.filename or "kontoauszug",
        format=erg.format,
        anzahl=0,
        benutzer_id=benutzer.id,
    )
    db.add(batch)
    db.flush()

    warnungen = list(erg.warnungen)
    anker: list[dict] = []
    anzahl = 0
    for auszug in erg.auszuege:
        kennung = bank.normalisiere_iban(auszug.konto_kennung)
        konto = nach_iban.get(kennung)
        if konto is None and gewaehlt is not None:
            konto = gewaehlt
        if konto is None and len(bankkonten) == 1 and not nach_iban:
            konto = bankkonten[0]
            warnungen.append(
                f"Auszug '{auszug.konto_kennung}' automatisch dem einzigen Bankkonto "
                f"{konto.nummer} zugeordnet – IBAN am Konto hinterlegen."
            )
        if konto is None:
            warnungen.append(
                f"Auszug '{auszug.konto_kennung}' übersprungen: kein Bankkonto mit dieser "
                "IBAN – IBAN unter „Konten“ hinterlegen oder Konto beim Import wählen."
            )
            continue
        if auszug.umsaetze:
            von = min(u.buchungstag for u in auszug.umsaetze)
            bis = max(u.buchungstag for u in auszug.umsaetze)
            vorhandene = db.scalar(
                select(models.BankTransaktion.id)
                .where(
                    models.BankTransaktion.mandant_id == mandant.id,
                    models.BankTransaktion.konto_id == konto.id,
                    models.BankTransaktion.buchungstag >= von,
                    models.BankTransaktion.buchungstag <= bis,
                )
                .limit(1)
            )
            if vorhandene:
                warnungen.append(
                    f"Konto {konto.nummer}: Zeitraum {von} – {bis} enthält bereits "
                    "Bankumsätze – mögliche Dubletten (ggf. alten Import löschen)."
                )
        for u in auszug.umsaetze:
            db.add(
                models.BankTransaktion(
                    mandant_id=mandant.id,
                    batch_id=batch.id,
                    konto_id=konto.id,
                    buchungstag=u.buchungstag,
                    valuta=u.valuta,
                    betrag=u.betrag,
                    partner=u.partner,
                    verwendungszweck=u.verwendungszweck,
                    referenz=u.referenz,
                )
            )
            anzahl += 1
        # Endsaldo als Bestandsanker übernehmen (Kontoauszug ist maßgeblich)
        if auszug.endsaldo is not None and auszug.endsaldo_datum is not None:
            bestand = db.scalar(
                select(models.Bestand).where(
                    models.Bestand.mandant_id == mandant.id,
                    models.Bestand.konto_id == konto.id,
                    models.Bestand.datum == auszug.endsaldo_datum,
                )
            )
            if bestand is None:
                db.add(
                    models.Bestand(
                        mandant_id=mandant.id,
                        typ=konto.typ,
                        konto_id=konto.id,
                        datum=auszug.endsaldo_datum,
                        wert=auszug.endsaldo,
                    )
                )
            else:
                bestand.wert = auszug.endsaldo
            anker.append(
                {
                    "konto": f"{konto.nummer} {konto.bezeichnung}",
                    "datum": auszug.endsaldo_datum.isoformat(),
                    "wert": float(auszug.endsaldo),
                }
            )

    batch.anzahl = anzahl
    batch.warnungen = "\n".join(warnungen[:200]) or None
    audit(db, benutzer, mandant.id, "BANK_IMPORT",
          f"{batch.dateiname} ({anzahl} Umsätze, {len(anker)} Anker)")
    db.commit()
    return {
        "batch_id": batch.id,
        "format": erg.format,
        "anzahl": anzahl,
        "bestandsanker": anker,
        "warnungen": warnungen[:50],
    }


@router.get("/mandanten/{mandant_id}/bank-umsaetze")
def bank_umsaetze(
    mandant_id: int,
    von: str | None = None,
    bis: str | None = None,
    konto_id: int | None = None,
    limit: int = 100,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    abfrage = select(models.BankTransaktion).where(
        models.BankTransaktion.mandant_id == mandant.id
    )
    if von:
        abfrage = abfrage.where(models.BankTransaktion.buchungstag >= _datum(von, "von"))
    if bis:
        abfrage = abfrage.where(models.BankTransaktion.buchungstag <= _datum(bis, "bis"))
    if konto_id:
        abfrage = abfrage.where(models.BankTransaktion.konto_id == konto_id)
    umsaetze = db.scalars(
        abfrage.order_by(
            models.BankTransaktion.buchungstag.desc(), models.BankTransaktion.id.desc()
        ).limit(max(1, min(limit, 500)))
    )
    return [
        {
            "id": u.id,
            "konto_id": u.konto_id,
            "buchungstag": u.buchungstag.isoformat(),
            "valuta": u.valuta.isoformat() if u.valuta else None,
            "betrag": float(u.betrag),
            "partner": u.partner,
            "verwendungszweck": u.verwendungszweck,
            "referenz": u.referenz,
            "zuordnungen": [
                {"posten_id": z.posten_id, "betrag": float(z.betrag)}
                for z in u.zuordnungen
            ],
        }
        for u in umsaetze
    ]


# ------------------------------------------------- PDF-Kontoumsätze (Ist)

def _pdf_umsatz_text(empfaenger: str, zweck: str) -> str | None:
    """Kanonischer Buchungstext eines PDF-Umsatzes.

    Muss zur Textbildung der Oberfläche passen – darüber werden bei
    überschneidenden Auszugszeiträumen bereits verbuchte Umsätze erkannt."""
    return f"{empfaenger} – {zweck}".strip()[:255] or None


def _pdf_verbuchte_schluessel(db: Session, mandant_id: int) -> set[tuple[date, str | None]]:
    """(Datum, Text) aller Ist-Buchungen aus PDF-Importen des Mandanten."""
    pdf_batches = select(models.ImportBatch.id).where(
        models.ImportBatch.mandant_id == mandant_id,
        models.ImportBatch.format == "PDF",
    )
    zeilen = db.execute(
        select(models.Buchung.datum, models.Buchung.text).where(
            models.Buchung.mandant_id == mandant_id,
            models.Buchung.batch_id.in_(pdf_batches),
        )
    ).all()
    return {(datum, text) for datum, text in zeilen}


def _pdf_bereits_verbucht(u, verbucht: set[tuple[date, str | None]]) -> bool:
    """Umsatz schon in einem PDF-Import verbucht? Aus Sammelüberweisungen
    aufgelöste Einzelzahlungen gelten auch dann als verbucht, wenn die
    ursprüngliche Auszugszeile früher als Ganzes verbucht wurde."""
    if (u.buchungstag, _pdf_umsatz_text(u.empfaenger, u.verwendungszweck)) in verbucht:
        return True
    return u.ursprung is not None and (
        u.buchungstag, _pdf_umsatz_text(*u.ursprung)
    ) in verbucht


@router.post("/mandanten/{mandant_id}/pdf-umsaetze")
async def pdf_umsaetze_hochladen(
    mandant_id: int,
    datei: UploadFile,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """PDF-Kontoauszug bzw. ZIP-Paket (Auszug + Postenlisten) auswerten.

    Liefert die erkannten Umsätze zurück; verbucht wird einzeln über
    /pdf-umsaetze/verbuchen. Der angelegte Import-Batch bündelt die daraus
    entstehenden Ist-Buchungen (gesammelt löschbar unter „Bisherige Importe“)."""
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    from ..services import pdf_kontoauszug

    daten_roh = await datei.read()
    # Texterkennung dauert Sekunden – nicht im Event-Loop ausführen
    erg = await run_in_threadpool(pdf_kontoauszug.parse_datei, daten_roh)
    if not erg.umsaetze:
        raise HTTPException(422, "; ".join(erg.warnungen) or "Keine Umsätze im PDF gefunden")
    batch = models.ImportBatch(
        mandant_id=mandant.id,
        dateiname=datei.filename or "kontoauszug.pdf",
        format="PDF",
        anzahl=0,
        warnungen="\n".join(erg.warnungen) or None,
        benutzer_id=benutzer.id,
        datei=daten_roh,   # für die spätere Zuordnungsansicht („ansehen“)
    )
    db.add(batch)
    audit(db, benutzer, mandant.id, "PDF_UMSAETZE",
          f"{datei.filename} ({len(erg.umsaetze)} Umsätze erkannt)")
    db.commit()
    # Überschneidende Auszugszeiträume: schon verbuchte Umsätze markieren
    verbucht = _pdf_verbuchte_schluessel(db, mandant.id)
    return {
        "batch_id": batch.id,
        "warnungen": erg.warnungen[:20],
        "hinweise": erg.hinweise[:20],
        "umsaetze": [
            {
                "auszug_nr": u.auszug_nr,
                "auszugsdatum": u.auszugsdatum.isoformat(),
                "buchungstag": u.buchungstag.isoformat(),
                "verwendungszweck": u.verwendungszweck,
                "empfaenger": u.empfaenger,
                "betrag": float(u.betrag),
                "sammelauftrag": u.sammelauftrag or None,
                "bereits_verbucht": _pdf_bereits_verbucht(u, verbucht),
            }
            for u in erg.umsaetze
        ],
    }


class PdfVerbuchungAnteil(BaseModel):
    konto_id: int
    betrag: float        # Anteil (positiv), Summe muss den Umsatzbetrag ergeben


class PdfVerbuchung(BaseModel):
    batch_id: int
    konto_id: int | None = None   # Sachkonto (einfacher Fall)
    bank_konto_id: int            # Bank-/Kassenkonto des Auszugs (Gegenkonto)
    datum: str                    # Buchungsdatum aus dem Auszug
    betrag: float                 # mit Vorzeichen: negativ = Auszahlung
    text: str | None = None
    # optional: Aufteilung auf mehrere Konten (statt konto_id)
    aufteilung: list[PdfVerbuchungAnteil] | None = None


@router.post("/mandanten/{mandant_id}/pdf-umsaetze/verbuchen")
def pdf_umsatz_verbuchen(
    mandant_id: int,
    daten: PdfVerbuchung,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Einzelnen PDF-Umsatz als Ist-Buchung übernehmen.

    Negativer Betrag: Auszahlung (Sachkonto an Bank), positiver Betrag:
    Einzahlung (Bank an Sachkonto) – DATEV-Logik mit Betrag positiv und
    S/H auf dem ersten Konto. Optional lässt sich der Umsatz über
    `aufteilung` auf mehrere Konten verteilen (die Anteile müssen den
    Umsatzbetrag exakt ergeben); je Anteil entsteht eine Buchung.
    Doppelte Übernahme desselben Umsatzes wird abgelehnt."""
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    batch = db.get(models.ImportBatch, daten.batch_id)
    if batch is None or batch.mandant_id != mandant.id or batch.format != "PDF":
        raise HTTPException(404, "PDF-Import nicht gefunden")
    bank = _bankkonto_oder_422(db, mandant.id, daten.bank_konto_id)
    betrag = _dec(daten.betrag)
    if betrag == 0:
        raise HTTPException(422, "Betrag 0 kann nicht verbucht werden")
    datum = _datum(daten.datum)
    text = (daten.text or "").strip()[:255] or None

    # Anteile bestimmen: einfacher Fall (ein Konto) oder Aufteilung
    if daten.aufteilung:
        anteile_roh = [(a.konto_id, _dec(a.betrag)) for a in daten.aufteilung]
        if any(b <= 0 for _, b in anteile_roh):
            raise HTTPException(422, "Alle Teilbeträge müssen größer 0 sein")
        summe = sum(b for _, b in anteile_roh)
        if abs(summe - abs(betrag)) > Decimal("0.01"):
            raise HTTPException(
                422,
                f"Die Teilbeträge ({summe:.2f} €) müssen den Umsatzbetrag "
                f"({abs(betrag):.2f} €) ergeben",
            )
    elif daten.konto_id is not None:
        anteile_roh = [(daten.konto_id, abs(betrag))]
    else:
        raise HTTPException(422, "Konto oder Aufteilung angeben")

    anteile: list[tuple[models.Konto, Decimal]] = []
    for konto_id, anteil in anteile_roh:
        konto = db.get(models.Konto, konto_id)
        if konto is None or konto.mandant_id != mandant.id:
            raise HTTPException(422, "Ungültiges Konto")
        if konto.id == bank.id:
            raise HTTPException(422, "Konto und Gegenkonto dürfen nicht identisch sein")
        anteile.append((konto, anteil))

    # Dublettenschutz über alle PDF-Importe hinweg (überschneidende Zeiträume)
    if (datum, text) in _pdf_verbuchte_schluessel(db, mandant.id):
        raise HTTPException(
            409, "Dieser Umsatz ist bereits verbucht (ggf. aus einem früheren, "
            "sich überschneidenden Kontoauszug)"
        )
    buchungen: list[models.Buchung] = []
    for konto, anteil in anteile:
        if betrag < 0:  # Auszahlung: Sachkonto (Soll) an Bank
            konto_nr, gegen_nr = konto.nummer, bank.nummer
        else:           # Einzahlung: Bank (Soll) an Sachkonto
            konto_nr, gegen_nr = bank.nummer, konto.nummer
        buchung = models.Buchung(
            mandant_id=mandant.id,
            batch_id=batch.id,
            datum=datum,
            konto_nr=konto_nr,
            gegenkonto_nr=gegen_nr,
            betrag=anteil,
            sh="S",
            text=text,
        )
        db.add(buchung)
        buchungen.append(buchung)
    batch.anzahl += len(buchungen)
    konto_text = ", ".join(f"{k.nummer} {k.bezeichnung}" for k, _ in anteile)
    audit(db, benutzer, mandant.id, "PDF_UMSATZ_VERBUCHT",
          f"{datum} {betrag:.2f} € auf {konto_text}")
    db.commit()
    return {
        "id": buchungen[0].id,
        "ids": [b.id for b in buchungen],
        "konto": konto_text,
    }


class PdfZuweisung(BaseModel):
    batch_id: int
    posten_id: int       # offener Posten, auf den der Eingang angerechnet wird
    bank_konto_id: int   # Bank-/Kassenkonto des Auszugs
    datum: str           # Buchungsdatum aus dem Auszug
    betrag: float        # mit Vorzeichen: positiv = Eingang (Debitor)
    text: str | None = None


@router.post("/mandanten/{mandant_id}/pdf-umsaetze/zuweisen")
def pdf_umsatz_zuweisen(
    mandant_id: int,
    daten: PdfZuweisung,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """PDF-Umsatz einem offenen Posten zuweisen: Anrechnung auf den Posten
    (inkl. Überzahlungs-Vortrag bei Sammelposten) und Ist-Buchung je Anteil.

    Eingänge (positiv) gehören auf Debitoren-Posten, Ausgaben (negativ) auf
    Kreditoren-Posten."""
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    batch = db.get(models.ImportBatch, daten.batch_id)
    if batch is None or batch.mandant_id != mandant.id or batch.format != "PDF":
        raise HTTPException(404, "PDF-Import nicht gefunden")
    posten = db.get(models.OffenerPosten, daten.posten_id)
    if posten is None or posten.mandant_id != mandant.id:
        raise HTTPException(422, "Ungültiger Posten")
    bank = _bankkonto_oder_422(db, mandant.id, daten.bank_konto_id)
    betrag = _dec(daten.betrag)
    if betrag == 0:
        raise HTTPException(422, "Betrag 0 kann nicht zugewiesen werden")
    erwartet = models.PostenArt.DEBITOR.value if betrag > 0 else models.PostenArt.KREDITOR.value
    if posten.art != erwartet:
        raise HTTPException(
            422,
            "Eingänge gehören auf Forderungen (Debitoren), Ausgaben auf "
            "Eingangsrechnungen (Kreditoren)",
        )
    zahltag = _datum(daten.datum, "datum")
    text = (daten.text or "").strip()[:255] or None
    # Dublettenschutz über alle PDF-Importe hinweg (überschneidende Zeiträume)
    if (zahltag, text) in _pdf_verbuchte_schluessel(db, mandant.id):
        raise HTTPException(
            409, "Dieser Umsatz ist bereits verbucht (ggf. aus einem früheren, "
            "sich überschneidenden Kontoauszug)"
        )

    anteile, unverteilt = _zahlung_anwenden(db, posten, abs(betrag), zahltag, False)
    buchungen, hinweise = _zahlungs_buchungen(
        db, mandant.id, anteile, bank, zahltag, text, batch_id=batch.id
    )
    batch.anzahl += len(buchungen)
    audit(db, benutzer, mandant.id, "PDF_UMSATZ_ZUGEWIESEN",
          f"{zahltag} {betrag:.2f} € auf Posten {posten.id} ({posten.partner})")
    db.commit()
    return {
        "posten": {
            "id": posten.id,
            "partner": posten.partner,
            "status": posten.status,
            "restbetrag": float(posten.betrag_brutto - posten.bezahlt_betrag),
        },
        "verrechnet": [
            {"posten_id": p.id, "partner": p.partner, "betrag": float(anteil),
             "status": p.status}
            for p, anteil in anteile[1:]
        ],
        "unverteilt": float(unverteilt),
        "buchungen": [b.id for b in buchungen],
        "hinweise": hinweise,
    }


class PdfAufhebung(BaseModel):
    buchung_id: int


@router.post("/mandanten/{mandant_id}/pdf-umsaetze/aufheben")
def pdf_umsatz_aufheben(
    mandant_id: int,
    daten: PdfAufhebung,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Verbuchung/Zuweisung rückgängig machen: die Ist-Buchung wird gelöscht;
    hängt sie an einem Posten, wird der Zahlungsanteil wieder abgezogen und
    der Posten erneut geöffnet. Nur für Buchungen aus PDF-Importen und
    Posten-Zahlungen (Fibu-Importbuchungen bleiben unantastbar)."""
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    buchung = db.get(models.Buchung, daten.buchung_id)
    if buchung is None or buchung.mandant_id != mandant.id:
        raise HTTPException(404, "Buchung nicht gefunden")
    batch = db.get(models.ImportBatch, buchung.batch_id) if buchung.batch_id else None
    if buchung.posten_id is None and (batch is None or batch.format != "PDF"):
        raise HTTPException(
            422, "Nur PDF-Verbuchungen und Posten-Zahlungen können aufgehoben werden"
        )
    if buchung.posten_id is not None:
        posten = db.get(models.OffenerPosten, buchung.posten_id)
        if posten is not None:
            posten.bezahlt_betrag = max(
                Decimal("0"), (posten.bezahlt_betrag or Decimal("0")) - buchung.betrag
            )
            posten.status = models.PostenStatus.OFFEN.value
            posten.bezahlt_am = None
    if batch is not None and batch.anzahl > 0:
        batch.anzahl -= 1
    audit(db, benutzer, mandant.id, "PDF_UMSATZ_AUFGEHOBEN",
          f"Buchung {buchung.id}: {buchung.datum} {buchung.betrag:.2f} €")
    db.delete(buchung)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- OP-Ausgleich

@router.get("/mandanten/{mandant_id}/op-ausgleich/vorschlaege")
def ausgleich_vorschlaege(
    mandant_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    return ausgleich.vorschlaege(db, mandant)


class AusgleichPaar(BaseModel):
    transaktion_id: int
    posten_id: int | None = None          # 1:1 (abwärtskompatibel)
    posten_ids: list[int] | None = None   # Sammelüberweisung (1:n)

    def alle_posten(self) -> list[int]:
        if self.posten_ids:
            return self.posten_ids
        if self.posten_id is not None:
            return [self.posten_id]
        return []


class AusgleichUebernahme(BaseModel):
    paare: list[AusgleichPaar]


@router.post("/mandanten/{mandant_id}/op-ausgleich")
def ausgleich_uebernehmen(
    mandant_id: int,
    daten: AusgleichUebernahme,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    if not daten.paare:
        raise HTTPException(422, "Keine Zuordnungen übergeben")
    try:
        anzahl = ausgleich.gleiche_aus(
            db, mandant, [(p.transaktion_id, p.alle_posten()) for p in daten.paare]
        )
    except ValueError as e:
        db.rollback()
        raise HTTPException(409, str(e))
    audit(db, benutzer, mandant.id, "OP_AUSGLEICH", f"{anzahl} Zuordnungen")
    db.commit()
    return {"ausgeglichen": anzahl}


class AusgleichAufhebung(BaseModel):
    transaktion_id: int


@router.post("/mandanten/{mandant_id}/op-ausgleich/aufheben")
def ausgleich_aufheben(
    mandant_id: int,
    daten: AusgleichAufhebung,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    try:
        ausgleich.hebe_auf(db, mandant, daten.transaktion_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    audit(db, benutzer, mandant.id, "OP_AUSGLEICH_AUFGEHOBEN", str(daten.transaktion_id))
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Offene Posten

@router.get("/mandanten/{mandant_id}/posten")
def posten_liste(
    mandant_id: int,
    status: str | None = None,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    abfrage = select(models.OffenerPosten).where(
        models.OffenerPosten.mandant_id == mandant.id
    )
    if status:
        abfrage = abfrage.where(models.OffenerPosten.status == status)
    posten = db.scalars(abfrage.order_by(models.OffenerPosten.faellig_am))
    return [
        {
            "id": p.id,
            "art": p.art,
            "partner": p.partner,
            "belegnr": p.belegnr,
            "rechnungsdatum": p.rechnungsdatum.isoformat() if p.rechnungsdatum else None,
            "faellig_am": p.faellig_am.isoformat(),
            "zahlung_geplant_am": p.zahlung_geplant_am.isoformat() if p.zahlung_geplant_am else None,
            "betrag_brutto": float(p.betrag_brutto),
            "bezahlt_betrag": float(p.bezahlt_betrag or 0),
            "restbetrag": float(p.betrag_brutto - (p.bezahlt_betrag or 0)),
            "konto_id": p.konto_id,
            "status": p.status,
            "bezahlt_am": p.bezahlt_am.isoformat() if p.bezahlt_am else None,
            "forderungsklasse": p.forderungsklasse,
            "sammel": p.sammel,
            "notiz": p.notiz,
        }
        for p in posten
    ]


class PostenNeu(BaseModel):
    art: str = "KREDITOR"
    partner: str
    belegnr: str | None = None
    rechnungsdatum: str | None = None
    faellig_am: str
    zahlung_geplant_am: str | None = None
    betrag_brutto: float
    konto_id: int | None = None
    forderungsklasse: str | None = None
    sammel: bool = False
    notiz: str | None = None


def _konto_pruefen(db: Session, mandant_id: int, konto_id: int | None):
    if konto_id is None:
        return
    konto = db.get(models.Konto, konto_id)
    if konto is None or konto.mandant_id != mandant_id:
        raise HTTPException(422, "Ungültiges Konto")


def _forderungsklasse_automatisch(
    mandant: models.Mandant, art: str, rechnungsdatum: date | None
) -> str | None:
    """Vorklassifizierung: vor dem Stichtag begründet -> Insolvenzforderung (§ 38),
    danach -> Masseverbindlichkeit (§ 55). Nur für Kreditoren im Verfahren."""
    if (
        art != "KREDITOR"
        or mandant.insolvenz_stichtag is None
        or mandant.verfahrensstatus == "REGELMANDAT"
        or rechnungsdatum is None
    ):
        return None
    if rechnungsdatum < mandant.insolvenz_stichtag:
        return models.Forderungsklasse.INSOLVENZFORDERUNG.value
    return models.Forderungsklasse.MASSE.value


@router.post("/mandanten/{mandant_id}/posten", status_code=201)
def posten_anlegen(
    mandant_id: int,
    daten: PostenNeu,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    if daten.art not in ("KREDITOR", "DEBITOR"):
        raise HTTPException(422, "Art muss KREDITOR oder DEBITOR sein")
    _konto_pruefen(db, mandant.id, daten.konto_id)
    rechnungsdatum = (
        _datum(daten.rechnungsdatum, "rechnungsdatum") if daten.rechnungsdatum else None
    )
    klasse = daten.forderungsklasse
    if klasse:
        _enum_pruefen(klasse, models.Forderungsklasse, "forderungsklasse")
    else:
        klasse = _forderungsklasse_automatisch(mandant, daten.art, rechnungsdatum)
    posten = models.OffenerPosten(
        mandant_id=mandant.id,
        art=daten.art,
        partner=daten.partner.strip(),
        belegnr=daten.belegnr,
        rechnungsdatum=rechnungsdatum,
        faellig_am=_datum(daten.faellig_am, "faellig_am"),
        zahlung_geplant_am=_datum(daten.zahlung_geplant_am, "zahlung_geplant_am")
        if daten.zahlung_geplant_am
        else None,
        betrag_brutto=_dec(daten.betrag_brutto),
        konto_id=daten.konto_id,
        forderungsklasse=klasse,
        sammel=daten.sammel,
        notiz=daten.notiz,
    )
    db.add(posten)
    db.commit()
    return {"id": posten.id, "forderungsklasse": posten.forderungsklasse}


@router.post("/mandanten/{mandant_id}/posten/import")
async def posten_import(
    mandant_id: int,
    datei: UploadFile,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """OP-Listen-Import (CSV): Art;Partner;Belegnummer;Rechnungsdatum;Faellig;Betrag;Konto;Notiz.

    Konten werden über die Kontonummer aufgelöst; die Forderungsklasse wird wie
    bei der Einzelanlage automatisch vorbelegt. Bereits vorhandene Posten
    (gleicher Partner, Belegnummer und Betrag) werden übersprungen."""
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    erg = datev.parse_op_csv(await datei.read())
    konten_nach_nr = {
        k.nummer: k
        for k in db.scalars(
            select(models.Konto).where(models.Konto.mandant_id == mandant.id)
        )
    }
    vorhandene = {
        ((p.partner or "").strip().lower(), p.belegnr or "", p.betrag_brutto)
        for p in db.scalars(
            select(models.OffenerPosten).where(
                models.OffenerPosten.mandant_id == mandant.id
            )
        )
    }
    warnungen = list(erg.warnungen)
    unbekannte_konten: set[str] = set()
    angelegt, uebersprungen, gesperrt = 0, 0, 0
    for p in erg.posten:
        schluessel = (p["partner"].strip().lower(), p["belegnr"] or "", p["betrag_brutto"])
        if schluessel in vorhandene:
            uebersprungen += 1
            continue
        vorhandene.add(schluessel)
        konto = konten_nach_nr.get(p["konto_nr"]) if p["konto_nr"] else None
        if p["konto_nr"] and konto is None:
            unbekannte_konten.add(p["konto_nr"])
        klasse = _forderungsklasse_automatisch(mandant, p["art"], p["rechnungsdatum"])
        if klasse == models.Forderungsklasse.INSOLVENZFORDERUNG.value:
            gesperrt += 1
        db.add(
            models.OffenerPosten(
                mandant_id=mandant.id,
                art=p["art"],
                partner=p["partner"],
                belegnr=p["belegnr"],
                rechnungsdatum=p["rechnungsdatum"],
                faellig_am=p["faellig_am"],
                betrag_brutto=p["betrag_brutto"],
                konto_id=konto.id if konto else None,
                forderungsklasse=klasse,
                sammel=p["sammel"],
                notiz=p["notiz"],
            )
        )
        angelegt += 1
    if unbekannte_konten:
        warnungen.append(
            "Konten nicht angelegt (Posten ohne Kontozuordnung übernommen): "
            + ", ".join(sorted(unbekannte_konten))
        )
    audit(db, benutzer, mandant.id, "OP_IMPORT",
          f"{datei.filename} ({angelegt} angelegt, {uebersprungen} übersprungen)")
    db.commit()
    return {
        "angelegt": angelegt,
        "uebersprungen": uebersprungen,
        "insolvenzforderungen": gesperrt,
        "warnungen": warnungen[:50],
    }


POSTEN_FELDER = {"partner", "belegnr", "status", "forderungsklasse", "notiz", "art"}


@router.patch("/posten/{posten_id}")
def posten_aendern(
    posten_id: int,
    daten: dict,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    posten = db.get(models.OffenerPosten, posten_id)
    if posten is None:
        raise HTTPException(404, "Posten nicht gefunden")
    mandant_oder_403(db, benutzer, posten.mandant_id)
    for feld, wert in daten.items():
        if feld in ("faellig_am",):
            posten.faellig_am = _datum(wert, feld)
        elif feld in ("zahlung_geplant_am", "bezahlt_am", "rechnungsdatum"):
            setattr(posten, feld, _datum(wert, feld) if wert else None)
        elif feld == "betrag_brutto":
            posten.betrag_brutto = _dec(wert)
        elif feld == "konto_id":
            _konto_pruefen(db, posten.mandant_id, wert)
            posten.konto_id = wert
        elif feld == "sammel":
            posten.sammel = bool(wert)
        elif feld in POSTEN_FELDER:
            if feld == "status":
                _enum_pruefen(wert, models.PostenStatus, feld)
            if feld == "art":
                _enum_pruefen(wert, models.PostenArt, feld)
            if feld == "forderungsklasse" and wert is not None:
                _enum_pruefen(wert, models.Forderungsklasse, feld)
            setattr(posten, feld, wert)
    db.commit()
    return {"ok": True}


class ZahlungNeu(BaseModel):
    betrag: float
    datum: str | None = None
    # Rest ausbuchen (z. B. Skonto/Erlass): Posten gilt trotz Restbetrag als bezahlt
    ausbuchen: bool = False
    # Zahlungsmittelkonto: wenn gesetzt, wird die Zahlung zusätzlich im Ist
    # gebucht (Debitor: Bank an Konto des Postens, Kreditor: Konto an Bank)
    bank_konto_id: int | None = None


def _zahlung_anwenden(
    db: Session,
    posten: models.OffenerPosten,
    betrag: Decimal,
    zahltag: date,
    ausbuchen: bool,
) -> tuple[list[tuple[models.OffenerPosten, Decimal]], Decimal]:
    """Zahlung auf den Posten anrechnen, Überhang auf Sammelposten vortragen.

    Liefert die Anteile je Posten (erster Eintrag = Hauptposten) und den nicht
    verteilbaren Rest."""
    if posten.status != models.PostenStatus.OFFEN.value:
        raise HTTPException(422, "Zahlungen sind nur auf offene Posten möglich")
    rest = posten.betrag_brutto - (posten.bezahlt_betrag or Decimal("0"))
    uebertrag = betrag - rest
    if uebertrag > Decimal("0.01") and not posten.sammel:
        raise HTTPException(
            422,
            f"Zahlung ({betrag:.2f} €) übersteigt den Restbetrag ({rest:.2f} €) – "
            "Überzahlungen werden nur bei Sammelposten auf spätere Posten vorgetragen",
        )
    anteile: list[tuple[models.OffenerPosten, Decimal]] = [(posten, min(betrag, rest))]
    posten.bezahlt_betrag = (posten.bezahlt_betrag or Decimal("0")) + min(betrag, rest)
    if ausbuchen or posten.betrag_brutto - posten.bezahlt_betrag <= Decimal("0.01"):
        posten.status = models.PostenStatus.BEZAHLT.value
        posten.bezahlt_am = zahltag

    if uebertrag > Decimal("0.01"):
        folgende = db.scalars(
            select(models.OffenerPosten)
            .where(
                models.OffenerPosten.mandant_id == posten.mandant_id,
                models.OffenerPosten.sammel.is_(True),
                models.OffenerPosten.status == models.PostenStatus.OFFEN.value,
                models.OffenerPosten.art == posten.art,
                models.OffenerPosten.id != posten.id,
            )
            .order_by(models.OffenerPosten.faellig_am, models.OffenerPosten.id)
        )
        for f in folgende:
            if uebertrag <= Decimal("0.01"):
                break
            f_rest = f.betrag_brutto - (f.bezahlt_betrag or Decimal("0"))
            if f_rest <= 0:
                continue
            anteil = min(uebertrag, f_rest)
            f.bezahlt_betrag = (f.bezahlt_betrag or Decimal("0")) + anteil
            if f.betrag_brutto - f.bezahlt_betrag <= Decimal("0.01"):
                f.status = models.PostenStatus.BEZAHLT.value
                f.bezahlt_am = zahltag
            anteile.append((f, anteil))
            uebertrag -= anteil
    return anteile, (uebertrag if uebertrag > Decimal("0.01") else Decimal("0"))


def _zahlungs_buchungen(
    db: Session,
    mandant_id: int,
    anteile: list[tuple[models.OffenerPosten, Decimal]],
    bank: models.Konto,
    zahltag: date,
    text: str | None,
    batch_id: int | None = None,
) -> tuple[list[models.Buchung], list[str]]:
    """Ist-Buchungen je Zahlungsanteil (DATEV-Logik, mit Posten-Referenz)."""
    buchungen: list[models.Buchung] = []
    hinweise: list[str] = []
    for p, anteil in anteile:
        if p.konto is None:
            hinweise.append(
                f"Posten „{p.partner}“ hat kein Konto – Anteil {anteil:.2f} € "
                "wurde nicht im Ist gebucht."
            )
            continue
        if p.art == models.PostenArt.DEBITOR.value:
            konto_nr, gegen_nr = bank.nummer, p.konto.nummer
        else:
            konto_nr, gegen_nr = p.konto.nummer, bank.nummer
        buchung = models.Buchung(
            mandant_id=mandant_id,
            batch_id=batch_id,
            posten_id=p.id,
            datum=zahltag,
            konto_nr=konto_nr,
            gegenkonto_nr=gegen_nr,
            betrag=anteil,
            sh="S",
            text=(text or f"Zahlung {p.partner}")[:255],
        )
        db.add(buchung)
        buchungen.append(buchung)
    return buchungen, hinweise


def _bankkonto_oder_422(db: Session, mandant_id: int, konto_id: int) -> models.Konto:
    bank = db.get(models.Konto, konto_id)
    if (
        bank is None
        or bank.mandant_id != mandant_id
        or bank.typ not in (models.KontoTyp.BANK.value, models.KontoTyp.KASSE.value)
    ):
        raise HTTPException(422, "Ungültiges Bank-/Kassenkonto")
    return bank


@router.post("/posten/{posten_id}/zahlungen")
def zahlung_erfassen(
    posten_id: int,
    daten: ZahlungNeu,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Manuelle (Teil-)Zahlung auf einen offenen Posten erfassen.

    Deckt die Zahlung den Restbetrag (oder ist `ausbuchen` gesetzt), wird der
    Posten BEZAHLT; sonst bleibt er mit Restbetrag offen und die Planung
    rechnet mit dem Rest. Auf Sammelposten sind Überzahlungen erlaubt: der
    Überhang wird auf die nächsten offenen Sammelposten gleicher Art (nach
    Fälligkeit) vorgetragen. Mit `bank_konto_id` wird die Zahlung zusätzlich
    als Ist-Buchung übernommen (je Anteil, mit Posten-Referenz) – nicht
    verwenden, wenn dieselbe Zahlung ohnehin über den Buchhaltungsimport kommt
    (Doppelzählung).
    """
    nur_schreibend(benutzer)
    posten = db.get(models.OffenerPosten, posten_id)
    if posten is None:
        raise HTTPException(404, "Posten nicht gefunden")
    mandant_oder_403(db, benutzer, posten.mandant_id)
    betrag = _dec(daten.betrag)
    if betrag <= 0:
        raise HTTPException(422, "Zahlungsbetrag muss größer 0 sein")
    zahltag = _datum(daten.datum, "datum") if daten.datum else date.today()

    anteile, unverteilt = _zahlung_anwenden(db, posten, betrag, zahltag, daten.ausbuchen)
    buchungen: list[models.Buchung] = []
    hinweise: list[str] = []
    if daten.bank_konto_id is not None:
        bank = _bankkonto_oder_422(db, posten.mandant_id, daten.bank_konto_id)
        buchungen, hinweise = _zahlungs_buchungen(
            db, posten.mandant_id, anteile, bank, zahltag,
            f"Zahlung {posten.partner}" + (f" ({posten.belegnr})" if posten.belegnr else ""),
        )
    audit(db, benutzer, posten.mandant_id, "ZAHLUNG_ERFASST",
          f"Posten {posten.id} ({posten.partner}): {betrag:.2f} €")
    db.commit()
    return {
        "status": posten.status,
        "bezahlt_betrag": float(posten.bezahlt_betrag),
        "restbetrag": float(posten.betrag_brutto - posten.bezahlt_betrag),
        "verrechnet": [
            {
                "posten_id": p.id,
                "partner": p.partner,
                "faellig_am": p.faellig_am.isoformat(),
                "betrag": float(anteil),
                "status": p.status,
            }
            for p, anteil in anteile[1:]
        ],
        "unverteilt": float(unverteilt),
        "buchungen": [b.id for b in buchungen],
        "hinweise": hinweise,
    }


@router.delete("/posten/{posten_id}")
def posten_loeschen(
    posten_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    posten = db.get(models.OffenerPosten, posten_id)
    if posten is None:
        raise HTTPException(404, "Posten nicht gefunden")
    mandant_oder_403(db, benutzer, posten.mandant_id)
    db.delete(posten)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Dauerbuchungen

@router.get("/mandanten/{mandant_id}/dauerbuchungen")
def dauer_liste(
    mandant_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    dauer = db.scalars(
        select(models.Dauerbuchung)
        .where(models.Dauerbuchung.mandant_id == mandant.id)
        .order_by(models.Dauerbuchung.name)
    )
    return [
        {
            "id": d.id,
            "name": d.name,
            "art": d.art,
            "konto_id": d.konto_id,
            "betrag_brutto": float(d.betrag_brutto),
            "intervall": d.intervall,
            "stichtag": d.stichtag,
            "gueltig_von": d.gueltig_von.isoformat(),
            "gueltig_bis": d.gueltig_bis.isoformat() if d.gueltig_bis else None,
            "aktiv": d.aktiv,
            "vortragen": d.vortragen,
        }
        for d in dauer
    ]


class DauerNeu(BaseModel):
    name: str
    art: str = "KREDITOR"
    konto_id: int | None = None
    betrag_brutto: float
    intervall: str = "MONATLICH"
    stichtag: int = 1
    gueltig_von: str
    gueltig_bis: str | None = None
    vortragen: bool = True


@router.post("/mandanten/{mandant_id}/dauerbuchungen", status_code=201)
def dauer_anlegen(
    mandant_id: int,
    daten: DauerNeu,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    if daten.intervall not in [i.value for i in models.Intervall]:
        raise HTTPException(422, "Unbekanntes Intervall")
    if daten.art not in ("KREDITOR", "DEBITOR"):
        raise HTTPException(422, "Art muss KREDITOR oder DEBITOR sein")
    _konto_pruefen(db, mandant.id, daten.konto_id)
    d = models.Dauerbuchung(
        mandant_id=mandant.id,
        name=daten.name.strip(),
        art=daten.art,
        konto_id=daten.konto_id,
        betrag_brutto=_dec(daten.betrag_brutto),
        intervall=daten.intervall,
        stichtag=daten.stichtag,
        gueltig_von=_datum(daten.gueltig_von, "gueltig_von"),
        gueltig_bis=_datum(daten.gueltig_bis, "gueltig_bis") if daten.gueltig_bis else None,
        vortragen=daten.vortragen,
    )
    db.add(d)
    db.commit()
    return {"id": d.id}


@router.post("/mandanten/{mandant_id}/dauerbuchungen/import")
async def dauer_import(
    mandant_id: int,
    datei: UploadFile,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Dauerbuchungs-Import (CSV): Name;Art;Betrag;Intervall;Stichtag;GueltigVon;GueltigBis;Konto
    (optional Vortrag: ja/nein, Standard ja).

    Konten werden über die Kontonummer aufgelöst. Bereits vorhandene
    Dauerbuchungen (gleicher Name, Art, Betrag, Intervall und Stichtag)
    werden übersprungen."""
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    erg = datev.parse_dauer_csv(await datei.read())
    konten_nach_nr = {
        k.nummer: k
        for k in db.scalars(
            select(models.Konto).where(models.Konto.mandant_id == mandant.id)
        )
    }
    vorhandene = {
        (d.name.strip().lower(), d.art, d.betrag_brutto, d.intervall, d.stichtag)
        for d in db.scalars(
            select(models.Dauerbuchung).where(
                models.Dauerbuchung.mandant_id == mandant.id
            )
        )
    }
    warnungen = list(erg.warnungen)
    unbekannte_konten: set[str] = set()
    angelegt, uebersprungen = 0, 0
    for d in erg.dauerbuchungen:
        schluessel = (d["name"].strip().lower(), d["art"], d["betrag_brutto"],
                      d["intervall"], d["stichtag"])
        if schluessel in vorhandene:
            uebersprungen += 1
            continue
        vorhandene.add(schluessel)
        konto = konten_nach_nr.get(d["konto_nr"]) if d["konto_nr"] else None
        if d["konto_nr"] and konto is None:
            unbekannte_konten.add(d["konto_nr"])
        db.add(
            models.Dauerbuchung(
                mandant_id=mandant.id,
                name=d["name"],
                art=d["art"],
                konto_id=konto.id if konto else None,
                betrag_brutto=d["betrag_brutto"],
                intervall=d["intervall"],
                stichtag=d["stichtag"],
                gueltig_von=d["gueltig_von"],
                gueltig_bis=d["gueltig_bis"],
                vortragen=d["vortragen"],
            )
        )
        angelegt += 1
    if unbekannte_konten:
        warnungen.append(
            "Konten nicht angelegt (Dauerbuchungen ohne Kontozuordnung übernommen): "
            + ", ".join(sorted(unbekannte_konten))
        )
    audit(db, benutzer, mandant.id, "DAUER_IMPORT",
          f"{datei.filename} ({angelegt} angelegt, {uebersprungen} übersprungen)")
    db.commit()
    return {
        "angelegt": angelegt,
        "uebersprungen": uebersprungen,
        "warnungen": warnungen[:50],
    }


@router.patch("/dauerbuchungen/{dauer_id}")
def dauer_aendern(
    dauer_id: int,
    daten: dict,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    d = db.get(models.Dauerbuchung, dauer_id)
    if d is None:
        raise HTTPException(404, "Dauerbuchung nicht gefunden")
    mandant_oder_403(db, benutzer, d.mandant_id)
    for feld, wert in daten.items():
        if feld == "betrag_brutto":
            d.betrag_brutto = _dec(wert)
        elif feld == "gueltig_von":
            d.gueltig_von = _datum(wert, feld)
        elif feld == "gueltig_bis":
            d.gueltig_bis = _datum(wert, feld) if wert else None
        elif feld == "konto_id":
            _konto_pruefen(db, d.mandant_id, wert)
            d.konto_id = wert
        elif feld == "vortragen":
            d.vortragen = bool(wert)
        elif feld in {"name", "art", "intervall", "stichtag", "aktiv"}:
            if feld == "art":
                _enum_pruefen(wert, models.PostenArt, feld)
            if feld == "intervall":
                _enum_pruefen(wert, models.Intervall, feld)
            setattr(d, feld, wert)
    db.commit()
    return {"ok": True}


@router.delete("/dauerbuchungen/{dauer_id}")
def dauer_loeschen(
    dauer_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    d = db.get(models.Dauerbuchung, dauer_id)
    if d is None:
        raise HTTPException(404, "Dauerbuchung nicht gefunden")
    mandant_oder_403(db, benutzer, d.mandant_id)
    db.delete(d)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Budget

@router.get("/mandanten/{mandant_id}/budgets")
def budget_liste(
    mandant_id: int,
    jahr: int | None = None,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    abfrage = select(models.Budget).where(models.Budget.mandant_id == mandant.id)
    if jahr:
        abfrage = abfrage.where(models.Budget.jahr == jahr)
    return [
        {
            "konto_id": b.konto_id,
            "jahr": b.jahr,
            "monat": b.monat,
            "betrag_netto": float(b.betrag_netto),
        }
        for b in db.scalars(abfrage)
    ]


class BudgetZelle(BaseModel):
    konto_id: int
    jahr: int
    monat: int
    betrag_netto: float | None = None


@router.put("/mandanten/{mandant_id}/budgets")
def budget_speichern(
    mandant_id: int,
    zellen: list[BudgetZelle],
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    # doppelte Zellen im Payload: letzte gewinnt (verhindert IntegrityError)
    eindeutig: dict[tuple[int, int, int], BudgetZelle] = {}
    for z in zellen:
        eindeutig[(z.konto_id, z.jahr, z.monat)] = z
    zellen = list(eindeutig.values())
    for z in zellen:
        if not (1 <= z.monat <= 12):
            raise HTTPException(422, "Monat muss 1..12 sein")
        _konto_pruefen(db, mandant.id, z.konto_id)
        vorhanden = db.scalar(
            select(models.Budget).where(
                models.Budget.mandant_id == mandant.id,
                models.Budget.konto_id == z.konto_id,
                models.Budget.jahr == z.jahr,
                models.Budget.monat == z.monat,
            )
        )
        if z.betrag_netto is None or z.betrag_netto == 0:
            if vorhanden:
                db.delete(vorhanden)
        elif vorhanden:
            vorhanden.betrag_netto = _dec(z.betrag_netto)
        else:
            db.add(
                models.Budget(
                    mandant_id=mandant.id,
                    konto_id=z.konto_id,
                    jahr=z.jahr,
                    monat=z.monat,
                    betrag_netto=_dec(z.betrag_netto),
                )
            )
    audit(db, benutzer, mandant.id, "BUDGET_GESPEICHERT", f"{len(zellen)} Zellen")
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Zahlungskalender

@router.get("/mandanten/{mandant_id}/termine")
def termine_liste(
    mandant_id: int,
    von: str | None = None,
    bis: str | None = None,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    v = _datum(von) if von else wochen_start(date.today())
    b = _datum(bis) if bis else v + timedelta(days=13 * 7 - 1)
    termine = db.scalars(
        select(models.Zahlungstermin)
        .where(
            models.Zahlungstermin.mandant_id == mandant.id,
            models.Zahlungstermin.datum >= v,
            models.Zahlungstermin.datum <= b,
        )
        .order_by(models.Zahlungstermin.datum)
    )
    return [
        {
            "id": t.id,
            "typ": t.typ,
            "periode": t.periode,
            "datum": t.datum.isoformat(),
            "betrag": float(t.betrag),
            "status": t.status,
            "konto_id": t.konto_id,
            "kommentar": t.kommentar,
            "generiert": t.generiert,
        }
        for t in termine
    ]


class TermineGenerieren(BaseModel):
    von: str | None = None
    bis: str | None = None


@router.post("/mandanten/{mandant_id}/termine/generieren")
def termine_generieren(
    mandant_id: int,
    daten: TermineGenerieren,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    von = _datum(daten.von, "von") if daten.von else wochen_start(date.today())
    bis = _datum(daten.bis, "bis") if daten.bis else von + timedelta(days=13 * 7 - 1)
    erg = generiere_termine(db, mandant, von, bis)
    audit(db, benutzer, mandant.id, "TERMINE_GENERIERT", str(erg))
    db.commit()
    return erg


class TerminNeu(BaseModel):
    typ: str = "SONSTIG"
    datum: str
    betrag: float
    konto_id: int | None = None
    kommentar: str | None = None


@router.post("/mandanten/{mandant_id}/termine", status_code=201)
def termin_anlegen(
    mandant_id: int,
    daten: TerminNeu,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    if daten.typ not in [t.value for t in models.TerminTyp]:
        raise HTTPException(422, "Unbekannter Termintyp")
    _konto_pruefen(db, mandant.id, daten.konto_id)
    t = models.Zahlungstermin(
        mandant_id=mandant.id,
        typ=daten.typ,
        datum=_datum(daten.datum),
        betrag=_dec(daten.betrag),
        konto_id=daten.konto_id,
        kommentar=daten.kommentar,
        status=models.TerminStatus.ANGEPASST.value,
        generiert=False,
    )
    db.add(t)
    db.commit()
    return {"id": t.id}


@router.patch("/termine/{termin_id}")
def termin_aendern(
    termin_id: int,
    daten: dict,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    t = db.get(models.Zahlungstermin, termin_id)
    if t is None:
        raise HTTPException(404, "Termin nicht gefunden")
    mandant_oder_403(db, benutzer, t.mandant_id)
    for feld, wert in daten.items():
        if feld == "betrag":
            t.betrag = _dec(wert)
        elif feld == "datum":
            t.datum = _datum(wert)
        elif feld == "konto_id":
            _konto_pruefen(db, t.mandant_id, wert)
            t.konto_id = wert
        elif feld == "status":
            _enum_pruefen(wert, models.TerminStatus, feld)
            t.status = wert
        elif feld == "kommentar":
            t.kommentar = wert
    # implizite Änderungen als "angepasst" markieren, expliziten Status respektieren
    if "status" not in daten and t.status == models.TerminStatus.GEPLANT.value:
        t.status = models.TerminStatus.ANGEPASST.value
    db.commit()
    return {"ok": True}


@router.delete("/termine/{termin_id}")
def termin_loeschen(
    termin_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    t = db.get(models.Zahlungstermin, termin_id)
    if t is None:
        raise HTTPException(404, "Termin nicht gefunden")
    mandant_oder_403(db, benutzer, t.mandant_id)
    db.delete(t)
    db.commit()
    return {"ok": True}


@router.get("/mandanten/{mandant_id}/termin-regeln")
def regeln_liste(
    mandant_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    regeln = db.scalars(
        select(models.TerminRegel).where(models.TerminRegel.mandant_id == mandant.id)
    )
    return [
        {
            "id": r.id,
            "typ": r.typ,
            "aktiv": r.aktiv,
            "betrag_modus": r.betrag_modus,
            "betrag_fix": float(r.betrag_fix or 0),
            "konto_id": r.konto_id,
        }
        for r in regeln
    ]


@router.patch("/termin-regeln/{regel_id}")
def regel_aendern(
    regel_id: int,
    daten: dict,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    r = db.get(models.TerminRegel, regel_id)
    if r is None:
        raise HTTPException(404, "Regel nicht gefunden")
    mandant_oder_403(db, benutzer, r.mandant_id)
    for feld, wert in daten.items():
        if feld == "betrag_fix":
            r.betrag_fix = _dec(wert)
        elif feld == "konto_id":
            _konto_pruefen(db, r.mandant_id, wert)
            r.konto_id = wert
        elif feld in {"aktiv", "betrag_modus"}:
            if feld == "betrag_modus":
                _enum_pruefen(wert, ["FIX", "HISTORIE", "BUDGET"], feld)
            setattr(r, feld, wert)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Bestände

@router.get("/mandanten/{mandant_id}/bestaende")
def bestaende_liste(
    mandant_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    bestaende = db.scalars(
        select(models.Bestand)
        .where(models.Bestand.mandant_id == mandant.id)
        .order_by(models.Bestand.datum.desc())
    )
    return [
        {
            "id": b.id,
            "typ": b.typ,
            "konto_id": b.konto_id,
            "datum": b.datum.isoformat(),
            "wert": float(b.wert),
        }
        for b in bestaende
    ]


class BestandNeu(BaseModel):
    typ: str
    konto_id: int | None = None
    datum: str
    wert: float


@router.post("/mandanten/{mandant_id}/bestaende", status_code=201)
def bestand_anlegen(
    mandant_id: int,
    daten: BestandNeu,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    if daten.typ not in [t.value for t in models.BestandTyp]:
        raise HTTPException(422, "Typ muss BANK, KASSE oder WAREN sein")
    if daten.typ != models.BestandTyp.WAREN.value and daten.konto_id is None:
        raise HTTPException(422, "Bank-/Kassenbestand benötigt ein Konto")
    _konto_pruefen(db, mandant.id, daten.konto_id)
    b = models.Bestand(
        mandant_id=mandant.id,
        typ=daten.typ,
        konto_id=daten.konto_id,
        datum=_datum(daten.datum),
        wert=_dec(daten.wert, "wert"),
    )
    db.add(b)
    audit(db, benutzer, mandant.id, "BESTAND_ERFASST", f"{daten.typ} {daten.datum}")
    db.commit()
    return {"id": b.id}


@router.delete("/bestaende/{bestand_id}")
def bestand_loeschen(
    bestand_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    b = db.get(models.Bestand, bestand_id)
    if b is None:
        raise HTTPException(404, "Bestand nicht gefunden")
    mandant_oder_403(db, benutzer, b.mandant_id)
    db.delete(b)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Szenarien

def _szenario_oder_none(
    db: Session, mandant_id: int, szenario_id: int | None
) -> models.Szenario | None:
    if szenario_id is None:
        return None
    szenario = db.get(models.Szenario, szenario_id)
    if szenario is None or szenario.mandant_id != mandant_id:
        raise HTTPException(404, "Szenario nicht gefunden")
    return szenario


@router.get("/mandanten/{mandant_id}/szenarien")
def szenarien_liste(
    mandant_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    szenarien = db.scalars(
        select(models.Szenario)
        .where(models.Szenario.mandant_id == mandant.id)
        .order_by(models.Szenario.name)
    )
    return [
        {
            "id": s.id,
            "name": s.name,
            "kommentar": s.kommentar,
            "ein_faktor": float(s.ein_faktor),
            "aus_faktor": float(s.aus_faktor),
            "debitoren_verzoegerung_tage": s.debitoren_verzoegerung_tage,
            "regeln_anzahl": len(s.regeln),
        }
        for s in szenarien
    ]


@router.get("/szenarien/{szenario_id}/regeln")
def szenario_regeln_liste(
    szenario_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    s = db.get(models.Szenario, szenario_id)
    if s is None:
        raise HTTPException(404, "Szenario nicht gefunden")
    mandant_oder_403(db, benutzer, s.mandant_id)
    return [
        {
            "id": r.id,
            "konto_id": r.konto_id,
            "gruppe_id": r.gruppe_id,
            "faktor": float(r.faktor),
        }
        for r in s.regeln
    ]


class SzenarioRegelNeu(BaseModel):
    konto_id: int | None = None
    gruppe_id: int | None = None
    faktor: float = 100


@router.put("/szenarien/{szenario_id}/regeln")
def szenario_regeln_speichern(
    szenario_id: int,
    regeln: list[SzenarioRegelNeu],
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Ersetzt alle Detailregeln des Szenarios (leere Liste löscht alle)."""
    nur_schreibend(benutzer)
    s = db.get(models.Szenario, szenario_id)
    if s is None:
        raise HTTPException(404, "Szenario nicht gefunden")
    mandant_oder_403(db, benutzer, s.mandant_id)
    gesehen: set[tuple] = set()
    neue: list[models.SzenarioRegel] = []
    for r in regeln:
        if (r.konto_id is None) == (r.gruppe_id is None):
            raise HTTPException(422, "Je Regel genau ein Konto ODER eine Gruppe angeben")
        if not (0 <= r.faktor <= 500):
            raise HTTPException(422, "Faktor muss zwischen 0 und 500 Prozent liegen")
        if r.konto_id is not None:
            _konto_pruefen(db, s.mandant_id, r.konto_id)
        else:
            gruppe = db.get(models.KontoGruppe, r.gruppe_id)
            if gruppe is None or gruppe.mandant_id != s.mandant_id:
                raise HTTPException(422, "Ungültige Gruppe")
        schluessel = ("K", r.konto_id) if r.konto_id else ("G", r.gruppe_id)
        if schluessel in gesehen:
            raise HTTPException(422, "Doppelte Regel für dasselbe Konto/dieselbe Gruppe")
        gesehen.add(schluessel)
        neue.append(models.SzenarioRegel(
            konto_id=r.konto_id, gruppe_id=r.gruppe_id,
            faktor=_dec(r.faktor, "faktor"),
        ))
    s.regeln = neue
    audit(db, benutzer, s.mandant_id, "SZENARIO_REGELN", f"{s.name}: {len(neue)} Regeln")
    db.commit()
    return {"anzahl": len(neue)}


class SzenarioNeu(BaseModel):
    name: str
    kommentar: str | None = None
    ein_faktor: float = 100
    aus_faktor: float = 100
    debitoren_verzoegerung_tage: int = 0


def _szenario_werte_pruefen(ein: float, aus: float, tage: int) -> None:
    if not (0 <= ein <= 500 and 0 <= aus <= 500):
        raise HTTPException(422, "Faktoren müssen zwischen 0 und 500 Prozent liegen")
    if not (0 <= tage <= 180):
        raise HTTPException(422, "Verzögerung muss zwischen 0 und 180 Tagen liegen")


@router.post("/mandanten/{mandant_id}/szenarien", status_code=201)
def szenario_anlegen(
    mandant_id: int,
    daten: SzenarioNeu,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    _szenario_werte_pruefen(daten.ein_faktor, daten.aus_faktor, daten.debitoren_verzoegerung_tage)
    s = models.Szenario(
        mandant_id=mandant.id,
        name=daten.name.strip(),
        kommentar=daten.kommentar,
        ein_faktor=_dec(daten.ein_faktor, "ein_faktor"),
        aus_faktor=_dec(daten.aus_faktor, "aus_faktor"),
        debitoren_verzoegerung_tage=daten.debitoren_verzoegerung_tage,
    )
    db.add(s)
    audit(db, benutzer, mandant.id, "SZENARIO_ANGELEGT", daten.name)
    db.commit()
    return {"id": s.id}


@router.patch("/szenarien/{szenario_id}")
def szenario_aendern(
    szenario_id: int,
    daten: dict,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    s = db.get(models.Szenario, szenario_id)
    if s is None:
        raise HTTPException(404, "Szenario nicht gefunden")
    mandant_oder_403(db, benutzer, s.mandant_id)
    for feld, wert in daten.items():
        if feld in ("ein_faktor", "aus_faktor"):
            setattr(s, feld, _dec(wert, feld))
        elif feld == "debitoren_verzoegerung_tage":
            s.debitoren_verzoegerung_tage = int(wert)
        elif feld in {"name", "kommentar"}:
            setattr(s, feld, wert)
    _szenario_werte_pruefen(
        float(s.ein_faktor), float(s.aus_faktor), s.debitoren_verzoegerung_tage
    )
    db.commit()
    return {"ok": True}


@router.delete("/szenarien/{szenario_id}")
def szenario_loeschen(
    szenario_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    s = db.get(models.Szenario, szenario_id)
    if s is None:
        raise HTTPException(404, "Szenario nicht gefunden")
    mandant_oder_403(db, benutzer, s.mandant_id)
    db.delete(s)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Insolvenzgeld

@router.get("/mandanten/{mandant_id}/insolvenzgeld/vorschau")
def insolvenzgeld_vorschau(
    mandant_id: int,
    von: str | None = None,
    bis: str | None = None,
    start: str | None = None,
    wochen: int = 13,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    if not (1 <= wochen <= 26):
        raise HTTPException(422, "Wochen muss zwischen 1 und 26 liegen")
    fenster_start = wochen_start(_datum(start, "start") if start else date.today())
    fenster_ende = fenster_start + timedelta(days=wochen * 7 - 1)
    return insolvenzgeld.vorschau(
        db,
        mandant,
        fenster_start,
        fenster_ende,
        von=_datum(von, "von") if von else None,
        bis=_datum(bis, "bis") if bis else None,
    )


# ---------------------------------------------------------------- Planung

@router.get("/mandanten/{mandant_id}/plan")
def plan_abrufen(
    mandant_id: int,
    start: str | None = None,
    wochen: int = 13,
    szenario_id: int | None = None,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    if not (1 <= wochen <= 26):
        raise HTTPException(422, "Wochen muss zwischen 1 und 26 liegen")
    szenario = _szenario_oder_none(db, mandant.id, szenario_id)
    return berechne_plan(
        db, mandant, start=_datum(start, "start") if start else None, wochen=wochen,
        szenario=szenario,
    )


class SnapshotNeu(BaseModel):
    kommentar: str | None = None
    # gewählter Fensterbeginn der Plan-Seite (wird auf den Wochenmontag gerundet);
    # ohne Angabe: Beginn der aktuellen Woche
    start: str | None = None
    wochen: int = 13


@router.post("/mandanten/{mandant_id}/plan/snapshot", status_code=201)
def snapshot_anlegen(
    mandant_id: int,
    daten: SnapshotNeu,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    if not 1 <= daten.wochen <= 52:
        raise HTTPException(422, "Wochen müssen zwischen 1 und 52 liegen")
    snap = erstelle_snapshot(
        db, mandant,
        start=_datum(daten.start, "start") if daten.start else None,
        wochen=daten.wochen,
        kommentar=daten.kommentar,
    )
    audit(db, benutzer, mandant.id, "SNAPSHOT_ERSTELLT", snap.stichtag.isoformat())
    db.commit()
    return {"id": snap.id, "stichtag": snap.stichtag.isoformat()}


@router.get("/mandanten/{mandant_id}/plan/snapshots")
def snapshot_liste(
    mandant_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    snaps = db.scalars(
        select(models.PlanSnapshot)
        .where(models.PlanSnapshot.mandant_id == mandant.id)
        .order_by(models.PlanSnapshot.stichtag.desc(), models.PlanSnapshot.id.desc())
    )
    return [
        {
            "id": s.id,
            "stichtag": s.stichtag.isoformat(),
            "wochen": s.wochen,
            "kommentar": s.kommentar,
            "erstellt_am": s.erstellt_am.isoformat(),
            "referenz": s.referenz,
        }
        for s in snaps
    ]


@router.delete("/mandanten/{mandant_id}/plan/snapshots/{snapshot_id}")
def snapshot_loeschen(
    mandant_id: int,
    snapshot_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Eingefrorenen Planstand löschen (war er Soll-Referenz, gilt danach
    wieder der jüngste passende Snapshot)."""
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    snap = db.get(models.PlanSnapshot, snapshot_id)
    if snap is None or snap.mandant_id != mandant.id:
        raise HTTPException(404, "Snapshot nicht gefunden")
    beschreibung = f"Stichtag {snap.stichtag.isoformat()}" + (" (Soll-Referenz)" if snap.referenz else "")
    db.execute(delete(models.PlanSnapshotWert).where(
        models.PlanSnapshotWert.snapshot_id == snap.id))
    db.delete(snap)
    audit(db, benutzer, mandant.id, "SNAPSHOT_GELOESCHT", beschreibung)
    db.commit()
    return {"ok": True}


class ReferenzWahl(BaseModel):
    snapshot_id: int | None = None      # None = automatisch (jüngster passender)


@router.put("/mandanten/{mandant_id}/plan/referenz")
def referenz_setzen(
    mandant_id: int,
    daten: ReferenzWahl,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Soll-Referenz für Plan (Abweichung), Soll/Ist und Excel-Export wählen."""
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    if daten.snapshot_id is not None:
        snap = db.get(models.PlanSnapshot, daten.snapshot_id)
        if snap is None or snap.mandant_id != mandant.id:
            raise HTTPException(404, "Snapshot nicht gefunden")
    db.execute(
        update(models.PlanSnapshot)
        .where(models.PlanSnapshot.mandant_id == mandant.id)
        .values(referenz=models.PlanSnapshot.id == daten.snapshot_id
                if daten.snapshot_id is not None else False)
    )
    audit(db, benutzer, mandant.id, "SOLL_REFERENZ",
          f"Snapshot {daten.snapshot_id}" if daten.snapshot_id else "automatisch")
    db.commit()
    return {"ok": True, "snapshot_id": daten.snapshot_id}


# ------------------------------------------------- Verschiebungen im Plan

class VerschiebungNeu(BaseModel):
    key: str                      # Planzeile: Konto-ID oder "TERMIN:<Typ>"
    quelle: str = "PLAN"          # PLAN (geplanter Wert) | VORTRAG
    von: str                      # angeklickte Zelle: erster Tag …
    bis: str                      # … und letzter Tag
    nach: str                     # Zieltag bzw. ein Tag des Zielzeitraums
    ansicht: str = "tage"         # tage | wochen | monate (Ziel: erster Bankarbeitstag)
    betrag: float                 # zu verschiebender Betrag (positiv)
    start: str | None = None      # Fenster der Plan-Seite
    wochen: int = 13
    szenario_id: int | None = None
    kommentar: str | None = None


def _eur_text(betrag: Decimal) -> str:
    return f"{betrag:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _verschiebung_dict(v: models.PlanVerschiebung, heute: date) -> dict:
    return {
        "id": v.id,
        "key": str(v.konto_id) if v.konto_id is not None else f"TERMIN:{v.termin_typ}",
        "quelle": v.quelle,
        "von": v.von.isoformat(),
        "nach": v.nach.isoformat(),
        "betrag": float(v.betrag),
        "kommentar": v.kommentar,
        "erstellt_am": v.erstellt_am.isoformat() if v.erstellt_am else None,
        "benutzer": v.benutzer.name if v.benutzer else None,
        # Vortragsverschiebungen enden mit ihrem Zieltag, Planverschiebungen wirken dauerhaft
        "abgelaufen": v.quelle == "VORTRAG" and v.nach <= heute,
    }


def _plan_zeile(plan: dict, key: str) -> dict | None:
    for gruppe in plan["zeilen"]:
        for kind in gruppe["kinder"]:
            if kind["key"] == key:
                return kind
    return None


@router.get("/mandanten/{mandant_id}/plan/verschiebungen")
def verschiebungen_liste(
    mandant_id: int,
    key: str | None = None,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    q = select(models.PlanVerschiebung).where(models.PlanVerschiebung.mandant_id == mandant.id)
    if key is not None:
        if key.isdigit():
            q = q.where(models.PlanVerschiebung.konto_id == int(key))
        elif key.startswith("TERMIN:"):
            q = q.where(models.PlanVerschiebung.konto_id.is_(None),
                        models.PlanVerschiebung.termin_typ == key[7:])
        else:
            return []
    heute = date.today()
    return [_verschiebung_dict(v, heute)
            for v in db.scalars(q.order_by(models.PlanVerschiebung.von, models.PlanVerschiebung.id))]


@router.post("/mandanten/{mandant_id}/plan/verschiebungen", status_code=201)
def verschiebung_anlegen(
    mandant_id: int,
    daten: VerschiebungNeu,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Geplanten Wert oder Vortrag einer Planzelle (ganz oder teilweise) in
    einen anderen Zeitraum verschieben. Ziel ist der gewählte Tag bzw. der
    erste Bankarbeitstag der gewählten Woche/des Monats, frühestens morgen.
    Entnommen wird chronologisch aus den künftigen Tagen der Zelle."""
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    heute = date.today()
    if daten.quelle not in ("PLAN", "VORTRAG"):
        raise HTTPException(422, "Quelle: PLAN oder VORTRAG")
    if daten.ansicht not in EXPORT_ANSICHTEN:
        raise HTTPException(422, "Spalten: tage, wochen oder monate")
    if not (1 <= daten.wochen <= 26):
        raise HTTPException(422, "Wochen muss zwischen 1 und 26 liegen")
    von, bis, nach = _datum(daten.von, "von"), _datum(daten.bis, "bis"), _datum(daten.nach, "nach")
    if bis < von:
        raise HTTPException(422, "Zeitraum: bis liegt vor von")
    konto_id, termin_typ = None, None
    if daten.key.isdigit():
        konto = db.get(models.Konto, int(daten.key))
        if konto is None or konto.mandant_id != mandant.id:
            raise HTTPException(404, "Konto nicht gefunden")
        konto_id = konto.id
    elif daten.key.startswith("TERMIN:") and len(daten.key) > 7:
        termin_typ = daten.key[7:]
    else:
        raise HTTPException(422, "Nur Planzeilen eines Kontos oder Zahlungstermins sind verschiebbar")

    if daten.ansicht == "wochen":
        ziel_basis = wochen_start(nach)
    elif daten.ansicht == "monate":
        ziel_basis = nach.replace(day=1)
    else:
        ziel_basis = nach
    ziel = naechster_bankarbeitstag(max(ziel_basis, heute + timedelta(days=1)), mandant.bundesland)
    if von <= ziel <= bis:
        raise HTTPException(422, "Der Zielzeitraum entspricht dem Ausgangszeitraum")

    szenario = _szenario_oder_none(db, mandant.id, daten.szenario_id)
    plan = berechne_plan(
        db, mandant, start=_datum(daten.start, "start") if daten.start else None,
        wochen=daten.wochen, szenario=szenario, heute=heute,
    )
    zeile = _plan_zeile(plan, daten.key)
    feld = "plan" if daten.quelle == "PLAN" else "vortrag"
    werte = sorted(
        (date.fromisoformat(t), Decimal(str(w)))
        for t, w in (zeile or {}).get(feld, {}).items()
        if von <= date.fromisoformat(t) <= bis and date.fromisoformat(t) > heute
    )
    netto = sum((w for _, w in werte), Decimal("0"))
    if not netto:
        raise HTTPException(
            422, "Kein verschiebbarer " + ("Planwert" if feld == "plan" else "Vortrag")
            + " ab morgen in diesem Zeitraum")
    vz = Decimal(1) if netto > 0 else Decimal(-1)
    verfuegbar = sum((abs(w) for _, w in werte if w * vz > 0), Decimal("0"))
    try:
        betrag = Decimal(str(daten.betrag)).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise HTTPException(422, "Ungültiger Betrag")
    if betrag <= 0:
        raise HTTPException(422, "Der Betrag muss größer als 0 sein")
    if betrag > verfuegbar:
        raise HTTPException(422, f"Höchstens {_eur_text(verfuegbar)} € verschiebbar")

    rest, angelegt = betrag, []
    for tag, w in werte:
        if w * vz <= 0 or rest <= 0:
            continue
        anteil = min(rest, abs(w))
        v = models.PlanVerschiebung(
            mandant_id=mandant.id, konto_id=konto_id, termin_typ=termin_typ,
            quelle=daten.quelle, von=tag, nach=ziel, betrag=vz * anteil,
            kommentar=(daten.kommentar or "").strip()[:255] or None, benutzer_id=benutzer.id,
        )
        db.add(v)
        angelegt.append(v)
        rest -= anteil
    audit(db, benutzer, mandant.id, "PLAN_VERSCHOBEN",
          f"{zeile['nummer']} {zeile['name']}: {_eur_text(betrag)} € "
          f"({'Vortrag' if feld == 'vortrag' else 'Plan'}) {von.isoformat()}–{bis.isoformat()} "
          f"→ {ziel.isoformat()}")
    db.commit()
    return {"ziel": ziel.isoformat(), "betrag": float(betrag), "anzahl": len(angelegt),
            "ids": [v.id for v in angelegt]}


@router.delete("/mandanten/{mandant_id}/plan/verschiebungen/{verschiebung_id}")
def verschiebung_loeschen(
    mandant_id: int,
    verschiebung_id: int,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    v = db.get(models.PlanVerschiebung, verschiebung_id)
    if v is None or v.mandant_id != mandant.id:
        raise HTTPException(404, "Verschiebung nicht gefunden")
    audit(db, benutzer, mandant.id, "PLAN_VERSCHIEBUNG_GELOESCHT",
          f"{v.quelle} {v.von.isoformat()} → {v.nach.isoformat()}: {_eur_text(abs(v.betrag))} €")
    db.delete(v)
    db.commit()
    return {"ok": True}


EXPORT_ANSICHTEN = ("tage", "wochen", "monate")
EXPORT_METRIKEN = ("auto", "vortrag", "plan", "ist", "delta")


def _export_parameter_pruefen(wochen: int, ansicht: str, metrik: str) -> None:
    if not (1 <= wochen <= 26):
        raise HTTPException(422, "Wochen muss zwischen 1 und 26 liegen")
    if ansicht not in EXPORT_ANSICHTEN:
        raise HTTPException(422, "Spalten: tage, wochen oder monate")
    if metrik not in EXPORT_METRIKEN:
        raise HTTPException(422, "Werte: " + ", ".join(EXPORT_METRIKEN))


@router.get("/mandanten/{mandant_id}/export/plan.xlsx")
def export_plan_xlsx(
    mandant_id: int,
    start: str | None = None,
    wochen: int = 13,
    szenario_id: int | None = None,
    metrik: str = "auto",
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Excel-Arbeitsmappe; Soll-Basis (Δ) und Blatt „Soll-Ist“ beziehen sich
    auf die gewählte Soll-Referenz (sonst den jüngsten Snapshot)."""
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    _export_parameter_pruefen(wochen, "tage", metrik)
    plan = berechne_plan(
        db, mandant, start=_datum(start, "start") if start else None, wochen=wochen,
        szenario=_szenario_oder_none(db, mandant.id, szenario_id),
    )
    sollist = soll_ist_vergleich(db, mandant)
    daten = export.plan_xlsx(plan, sollist if sollist.get("snapshot") else None, mandant,
                             metrik=metrik)
    audit(db, benutzer, mandant.id, "EXPORT_XLSX", plan["start"])
    db.commit()
    return Response(
        content=daten,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition":
                f'attachment; filename="liquiplan_{mandant.kurzname}_{plan["start"]}.xlsx"'
        },
    )


@router.get("/mandanten/{mandant_id}/export/plan.pdf")
def export_plan_pdf(
    mandant_id: int,
    start: str | None = None,
    wochen: int = 13,
    szenario_id: int | None = None,
    ansicht: str = "wochen",
    metrik: str = "auto",
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """PDF-Bericht in der Spaltenansicht der Plan-Seite; breite Pläne werden
    nach rechts auf Folgeblätter fortgesetzt."""
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    _export_parameter_pruefen(wochen, ansicht, metrik)
    plan = berechne_plan(
        db, mandant, start=_datum(start, "start") if start else None, wochen=wochen,
        szenario=_szenario_oder_none(db, mandant.id, szenario_id),
    )
    daten = export.plan_pdf(plan, mandant, ansicht=ansicht, metrik=metrik)
    audit(db, benutzer, mandant.id, "EXPORT_PDF", plan["start"])
    db.commit()
    return Response(
        content=daten,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f'inline; filename="liquiplan_{mandant.kurzname}_{plan["start"]}.pdf"'
        },
    )


@router.get("/mandanten/{mandant_id}/szenarien-vergleich")
def szenarien_vergleich_abrufen(
    mandant_id: int,
    start: str | None = None,
    wochen: int = 13,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    if not (1 <= wochen <= 26):
        raise HTTPException(422, "Wochen muss zwischen 1 und 26 liegen")
    return szenarien_vergleich(
        db, mandant, start=_datum(start, "start") if start else None, wochen=wochen
    )


@router.get("/mandanten/{mandant_id}/kpi")
def kpi_abrufen(
    mandant_id: int,
    start: str | None = None,
    wochen: int = 13,
    vergleich_id: int | None = None,
    vorwoche_id: int | None = None,
    antrag_id: int | None = None,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """Kennzahlen für die KPI-Seite (Fact Sheet). Vergleich ohne Angabe:
    Soll-Referenz bzw. jüngster passender Planstand; Vorwoche ohne Angabe:
    jüngster vor der laufenden Woche eingefrorener Planstand; Antrag ohne
    Angabe: Planstand mit „Antrag“ im Kommentar, sonst der älteste."""
    from ..services.kpi import kpi_daten

    mandant = mandant_oder_403(db, benutzer, mandant_id)
    if not (1 <= wochen <= 26):
        raise HTTPException(422, "Wochen muss zwischen 1 und 26 liegen")
    try:
        return kpi_daten(db, mandant, start=_datum(start, "start") if start else None,
                         wochen=wochen, vergleich_id=vergleich_id, vorwoche_id=vorwoche_id,
                         antrag_id=antrag_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/mandanten/{mandant_id}/export/kpi.pdf")
def export_kpi_pdf(
    mandant_id: int,
    start: str | None = None,
    wochen: int = 13,
    vergleich_id: int | None = None,
    vorwoche_id: int | None = None,
    antrag_id: int | None = None,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    """KPI-Übersicht als PDF (Kacheln, Diagramme, Wertetabellen) mit
    denselben Einstellungen wie die KPI-Seite."""
    from ..services.kpi import kpi_daten
    from ..services.kpi_pdf import kpi_pdf

    mandant = mandant_oder_403(db, benutzer, mandant_id)
    if not (1 <= wochen <= 26):
        raise HTTPException(422, "Wochen muss zwischen 1 und 26 liegen")
    try:
        kpi = kpi_daten(db, mandant, start=_datum(start, "start") if start else None,
                        wochen=wochen, vergleich_id=vergleich_id, vorwoche_id=vorwoche_id,
                        antrag_id=antrag_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    daten = kpi_pdf(kpi, mandant)
    audit(db, benutzer, mandant.id, "EXPORT_KPI_PDF", kpi["start"])
    db.commit()
    return Response(
        content=daten,
        media_type="application/pdf",
        headers={"Content-Disposition":
                 f'inline; filename="kpi_{mandant.kurzname}_{kpi["start"]}.pdf"'},
    )


@router.get("/mandanten/{mandant_id}/sollist")
def sollist_abrufen(
    mandant_id: int,
    snapshot_id: int | None = None,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    try:
        return soll_ist_vergleich(db, mandant, snapshot_id=snapshot_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


# ---------------------------------------------------------------- Profil / 2FA

class PasswortWechsel(BaseModel):
    aktuelles_passwort: str
    neues_passwort: str


@router.post("/profil/passwort")
def eigenes_passwort_aendern(
    daten: PasswortWechsel,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    from ..security import pruefe_passwort

    if not pruefe_passwort(daten.aktuelles_passwort, benutzer.passwort_hash):
        raise HTTPException(403, "Aktuelles Passwort falsch")
    if len(daten.neues_passwort) < 8:
        raise HTTPException(422, "Passwort muss mindestens 8 Zeichen haben")
    benutzer.passwort_hash = hash_passwort(daten.neues_passwort)
    audit(db, benutzer, None, "PASSWORT_GEAENDERT")
    db.commit()
    return {"ok": True}


@router.get("/profil")
def profil_abrufen(benutzer=Depends(aktueller_benutzer)):
    return {
        "email": benutzer.email,
        "name": benutzer.name,
        "rolle": benutzer.rolle,
        "totp_aktiv": benutzer.totp_aktiv,
    }


@router.post("/profil/2fa/einrichten")
def zwei_faktor_einrichten(
    benutzer=Depends(aktueller_benutzer), db: Session = Depends(get_db)
):
    """Erzeugt ein neues TOTP-Geheimnis (aktiv erst nach Bestätigung)."""
    import segno

    from ..services import totp

    if benutzer.totp_aktiv:
        raise HTTPException(409, "2FA ist bereits aktiv – zuerst deaktivieren")
    geheimnis = totp.neues_geheimnis()
    benutzer.totp_geheimnis = geheimnis
    benutzer.totp_letzter_schritt = 0
    db.commit()
    uri = totp.otpauth_uri(geheimnis, benutzer.email)
    return {
        "geheimnis": geheimnis,
        "uri": uri,
        "qr_svg": segno.make(uri).svg_data_uri(scale=4),
    }


class ZweiFaktorCode(BaseModel):
    code: str


@router.post("/profil/2fa/bestaetigen")
def zwei_faktor_bestaetigen(
    daten: ZweiFaktorCode,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    from ..services import totp

    if not benutzer.totp_geheimnis:
        raise HTTPException(409, "Zuerst 2FA einrichten")
    schritt = totp.pruefe_code(benutzer.totp_geheimnis, daten.code)
    if schritt is None:
        raise HTTPException(422, "Code ungültig – bitte erneut versuchen")
    benutzer.totp_aktiv = True
    benutzer.totp_letzter_schritt = schritt
    audit(db, benutzer, None, "2FA_AKTIVIERT")
    db.commit()
    return {"ok": True}


class ZweiFaktorDeaktivierung(BaseModel):
    passwort: str


@router.post("/profil/2fa/deaktivieren")
def zwei_faktor_deaktivieren(
    daten: ZweiFaktorDeaktivierung,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    from ..security import pruefe_passwort

    if not pruefe_passwort(daten.passwort, benutzer.passwort_hash):
        raise HTTPException(403, "Passwort falsch")
    benutzer.totp_aktiv = False
    benutzer.totp_geheimnis = None
    benutzer.totp_letzter_schritt = 0
    audit(db, benutzer, None, "2FA_DEAKTIVIERT")
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Sicherungen (Admin)

@router.get("/backups")
def backups_liste(admin=Depends(nur_admin)):
    einstellungen = get_settings()
    return {
        "verzeichnis": str(einstellungen.backup_verzeichnis),
        "intervall_stunden": einstellungen.backup_intervall_stunden,
        "aufbewahrung_tage": einstellungen.backup_aufbewahrung_tage,
        "dateien": backup.liste(einstellungen.backup_verzeichnis),
    }


@router.post("/backups", status_code=201)
def backup_erstellen(admin=Depends(nur_admin), db: Session = Depends(get_db)):
    einstellungen = get_settings()
    try:
        pfad = backup.erstelle_backup(einstellungen.backup_verzeichnis)
    except OSError as e:
        raise HTTPException(500, f"Sicherung fehlgeschlagen: {e}")
    backup.raeume_auf(einstellungen.backup_verzeichnis, einstellungen.backup_aufbewahrung_tage)
    audit(db, admin, None, "BACKUP_ERSTELLT", pfad.name)
    db.commit()
    return {"name": pfad.name, "groesse": pfad.stat().st_size}


def _backup_pfad(name: str):
    if not backup.NAME_MUSTER.match(name):
        raise HTTPException(422, "Ungültiger Sicherungsname")
    pfad = Path(get_settings().backup_verzeichnis) / name
    if not pfad.is_file():
        raise HTTPException(404, "Sicherung nicht gefunden")
    return pfad


@router.get("/backups/{name}")
def backup_herunterladen(name: str, admin=Depends(nur_admin)):
    pfad = _backup_pfad(name)
    return Response(
        content=pfad.read_bytes(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{pfad.name}"'},
    )


@router.delete("/backups/{name}")
def backup_loeschen(name: str, admin=Depends(nur_admin), db: Session = Depends(get_db)):
    pfad = _backup_pfad(name)
    pfad.unlink()
    audit(db, admin, None, "BACKUP_GELOESCHT", name)
    db.commit()
    return {"ok": True}


@router.post("/backups/wiederherstellen")
async def backup_wiederherstellen(
    datei: UploadFile,
    bestaetigung: str = Form(""),
    admin=Depends(nur_admin),
):
    """Ersetzt den KOMPLETTEN Datenbestand durch die hochgeladene Sicherung."""
    if bestaetigung != "WIEDERHERSTELLEN":
        raise HTTPException(
            422, "Bestätigung fehlt: bitte WIEDERHERSTELLEN eingeben"
        )
    daten = await datei.read()
    # Sicherheitsnetz: vor dem Ersetzen automatisch den Ist-Zustand sichern
    try:
        vorher = backup.erstelle_backup(get_settings().backup_verzeichnis)
    except OSError as e:
        raise HTTPException(500, f"Vorab-Sicherung fehlgeschlagen: {e}")
    try:
        ergebnis = backup.stelle_wieder_her(daten)
    except ValueError as e:
        raise HTTPException(409, str(e))
    with database.SessionLocal() as audit_db:
        audit(audit_db, None, None, "BACKUP_WIEDERHERGESTELLT",
              f"{datei.filename} (Vorab-Sicherung: {vorher.name})")
        audit_db.commit()
    return {"ok": True, "vorab_sicherung": vorher.name, **ergebnis}


# ---------------------------------------------------------------- Benutzer (Admin)

class BenutzerNeu(BaseModel):
    email: str
    name: str
    passwort: str
    rolle: str = "BEARBEITER"
    mandanten_ids: list[int] = []


@router.get("/benutzer")
def benutzer_liste(admin=Depends(nur_admin), db: Session = Depends(get_db)):
    benutzer = db.scalars(select(models.Benutzer).order_by(models.Benutzer.email))
    return [
        {
            "id": b.id,
            "email": b.email,
            "name": b.name,
            "rolle": b.rolle,
            "aktiv": b.aktiv,
            "totp_aktiv": b.totp_aktiv,
            "mandanten_ids": [m.id for m in b.mandanten],
        }
        for b in benutzer
    ]


@router.post("/benutzer", status_code=201)
def benutzer_anlegen(
    daten: BenutzerNeu, admin=Depends(nur_admin), db: Session = Depends(get_db)
):
    if daten.rolle not in [r.value for r in models.Rolle]:
        raise HTTPException(422, "Unbekannte Rolle")
    if len(daten.passwort) < 8:
        raise HTTPException(422, "Passwort muss mindestens 8 Zeichen haben")
    email = daten.email.strip().lower()
    if db.scalar(select(models.Benutzer.id).where(models.Benutzer.email == email)):
        raise HTTPException(409, "E-Mail bereits vergeben")
    b = models.Benutzer(
        email=email,
        name=daten.name.strip(),
        passwort_hash=hash_passwort(daten.passwort),
        rolle=daten.rolle,
    )
    for mid in daten.mandanten_ids:
        m = db.get(models.Mandant, mid)
        if m:
            b.mandanten.append(m)
    db.add(b)
    audit(db, admin, None, "BENUTZER_ANGELEGT", daten.email)
    db.commit()
    return {"id": b.id}


@router.patch("/benutzer/{benutzer_id}")
def benutzer_aendern(
    benutzer_id: int,
    daten: dict,
    admin=Depends(nur_admin),
    db: Session = Depends(get_db),
):
    b = db.get(models.Benutzer, benutzer_id)
    if b is None:
        raise HTTPException(404, "Benutzer nicht gefunden")
    for feld, wert in daten.items():
        if feld == "passwort":
            if len(str(wert)) < 8:
                raise HTTPException(422, "Passwort muss mindestens 8 Zeichen haben")
            b.passwort_hash = hash_passwort(str(wert))
        elif feld == "rolle":
            if wert not in [r.value for r in models.Rolle]:
                raise HTTPException(422, "Unbekannte Rolle")
            b.rolle = wert
        elif feld == "mandanten_ids":
            b.mandanten = [
                m for mid in wert if (m := db.get(models.Mandant, mid)) is not None
            ]
        elif feld == "totp_zuruecksetzen" and wert:
            # Geräteverlust: Admin setzt 2FA zurück, Benutzer richtet neu ein
            b.totp_aktiv = False
            b.totp_geheimnis = None
            b.totp_letzter_schritt = 0
        elif feld in {"name", "aktiv"}:
            setattr(b, feld, wert)
    audit(db, admin, None, "BENUTZER_GEAENDERT", b.email)
    db.commit()
    return {"ok": True}
