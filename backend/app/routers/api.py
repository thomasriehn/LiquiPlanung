"""JSON-API. Alle Routen erfordern Login; Mandantenrouten prüfen die Berechtigung."""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import models
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
from ..services import datev
from ..services.feiertage import BUNDESLAENDER
from ..services.kontenrahmen import lege_kontenrahmen_an
from ..services.liquiditaet import (
    berechne_plan,
    erstelle_snapshot,
    soll_ist_vergleich,
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
    if daten.kontenrahmen not in ("SKR03", "SKR04", "EIGEN"):
        raise HTTPException(422, "Kontenrahmen muss SKR03, SKR04 oder EIGEN sein")
    if daten.bundesland not in BUNDESLAENDER:
        raise HTTPException(422, "Unbekanntes Bundesland")
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
    audit(db, benutzer, mandant.id, "MANDANT_ANGELEGT", mandant.name)
    db.commit()
    return {"id": mandant.id}


MANDANT_FELDER = {
    "name", "bundesland", "ust_zeitraum", "dauerfrist", "verfahrensstatus",
    "aktenzeichen", "aktiv",
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
        if feld == "insolvenz_stichtag":
            mandant.insolvenz_stichtag = _datum(wert, feld) if wert else None
        elif feld in MANDANT_FELDER:
            setattr(mandant, feld, wert)
    audit(db, benutzer, mandant.id, "MANDANT_GEAENDERT", str(sorted(daten.keys())))
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
            {"id": g.id, "code": g.code, "name": g.name, "richtung": g.richtung}
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
    if db.scalar(
        select(models.Konto.id).where(
            models.Konto.mandant_id == mandant.id, models.Konto.nummer == daten.nummer
        )
    ):
        raise HTTPException(409, "Kontonummer bereits vorhanden")
    if daten.gruppe_id is not None:
        gruppe = db.get(models.KontoGruppe, daten.gruppe_id)
        if gruppe is None or gruppe.mandant_id != mandant.id:
            raise HTTPException(422, "Ungültige Gruppe")
    konto = models.Konto(
        mandant_id=mandant.id,
        nummer=daten.nummer.strip(),
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
    db.execute(delete(models.Buchung).where(models.Buchung.batch_id == batch.id))
    audit(db, benutzer, batch.mandant_id, "IMPORT_GELOESCHT", batch.dateiname)
    db.delete(batch)
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
            "konto_id": p.konto_id,
            "status": p.status,
            "bezahlt_am": p.bezahlt_am.isoformat() if p.bezahlt_am else None,
            "forderungsklasse": p.forderungsklasse,
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
    notiz: str | None = None


def _konto_pruefen(db: Session, mandant_id: int, konto_id: int | None):
    if konto_id is None:
        return
    konto = db.get(models.Konto, konto_id)
    if konto is None or konto.mandant_id != mandant_id:
        raise HTTPException(422, "Ungültiges Konto")


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
    posten = models.OffenerPosten(
        mandant_id=mandant.id,
        art=daten.art,
        partner=daten.partner.strip(),
        belegnr=daten.belegnr,
        rechnungsdatum=_datum(daten.rechnungsdatum, "rechnungsdatum") if daten.rechnungsdatum else None,
        faellig_am=_datum(daten.faellig_am, "faellig_am"),
        zahlung_geplant_am=_datum(daten.zahlung_geplant_am, "zahlung_geplant_am")
        if daten.zahlung_geplant_am
        else None,
        betrag_brutto=_dec(daten.betrag_brutto),
        konto_id=daten.konto_id,
        forderungsklasse=daten.forderungsklasse,
        notiz=daten.notiz,
    )
    db.add(posten)
    db.commit()
    return {"id": posten.id}


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
        elif feld in POSTEN_FELDER:
            setattr(posten, feld, wert)
    db.commit()
    return {"ok": True}


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
    )
    db.add(d)
    db.commit()
    return {"id": d.id}


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
        elif feld in {"name", "art", "intervall", "stichtag", "aktiv"}:
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
        elif feld in {"kommentar", "status"}:
            setattr(t, feld, wert)
    if t.status != models.TerminStatus.ERLEDIGT.value:
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


# ---------------------------------------------------------------- Planung

@router.get("/mandanten/{mandant_id}/plan")
def plan_abrufen(
    mandant_id: int,
    start: str | None = None,
    wochen: int = 13,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    if not (1 <= wochen <= 26):
        raise HTTPException(422, "Wochen muss zwischen 1 und 26 liegen")
    return berechne_plan(
        db, mandant, start=_datum(start, "start") if start else None, wochen=wochen
    )


class SnapshotNeu(BaseModel):
    kommentar: str | None = None


@router.post("/mandanten/{mandant_id}/plan/snapshot", status_code=201)
def snapshot_anlegen(
    mandant_id: int,
    daten: SnapshotNeu,
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    nur_schreibend(benutzer)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    snap = erstelle_snapshot(db, mandant, kommentar=daten.kommentar)
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
        }
        for s in snaps
    ]


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
    if db.scalar(select(models.Benutzer.id).where(models.Benutzer.email == daten.email)):
        raise HTTPException(409, "E-Mail bereits vergeben")
    b = models.Benutzer(
        email=daten.email.strip().lower(),
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
        elif feld in {"name", "aktiv"}:
            setattr(b, feld, wert)
    audit(db, admin, None, "BENUTZER_GEAENDERT", b.email)
    db.commit()
    return {"ok": True}
