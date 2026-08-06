"""JSON-API. Alle Routen erfordern Login; Mandantenrouten prüfen die Berechtigung."""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
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
from ..services import ausgleich, bank, datev, export, insolvenzgeld
from ..services.feiertage import BUNDESLAENDER
from ..services.kontenrahmen import lege_kontenrahmen_an, lege_standard_szenarien_an
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
    if daten.kontenrahmen not in ("SKR03", "SKR04", "EIGEN"):
        raise HTTPException(422, "Kontenrahmen muss SKR03, SKR04 oder EIGEN sein")
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
                "iban": k.iban,
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
    db.execute(delete(models.Buchung).where(models.Buchung.batch_id == batch.id))
    db.execute(
        delete(models.BankTransaktion).where(models.BankTransaktion.batch_id == batch.id)
    )
    audit(db, benutzer, batch.mandant_id, "IMPORT_GELOESCHT", batch.dateiname)
    db.delete(batch)
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
            "posten_id": u.posten_id,
        }
        for u in umsaetze
    ]


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
    posten_id: int


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
            db, mandant, [(p.transaktion_id, p.posten_id) for p in daten.paare]
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
    rechnungsdatum = (
        _datum(daten.rechnungsdatum, "rechnungsdatum") if daten.rechnungsdatum else None
    )
    klasse = daten.forderungsklasse
    if klasse:
        _enum_pruefen(klasse, models.Forderungsklasse, "forderungsklasse")
    elif (
        daten.art == "KREDITOR"
        and mandant.insolvenz_stichtag is not None
        and mandant.verfahrensstatus != "REGELMANDAT"
        and rechnungsdatum is not None
    ):
        # Vorklassifizierung: vor dem Stichtag begründet -> Insolvenzforderung (§ 38),
        # danach -> Masseverbindlichkeit (§ 55). Manuell übersteuerbar.
        klasse = (
            models.Forderungsklasse.INSOLVENZFORDERUNG.value
            if rechnungsdatum < mandant.insolvenz_stichtag
            else models.Forderungsklasse.MASSE.value
        )
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
        notiz=daten.notiz,
    )
    db.add(posten)
    db.commit()
    return {"id": posten.id, "forderungsklasse": posten.forderungsklasse}


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
            if feld == "status":
                _enum_pruefen(wert, models.PostenStatus, feld)
            if feld == "art":
                _enum_pruefen(wert, models.PostenArt, feld)
            if feld == "forderungsklasse" and wert is not None:
                _enum_pruefen(wert, models.Forderungsklasse, feld)
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
                _enum_pruefen(wert, ["FIX", "HISTORIE"], feld)
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
        }
        for s in szenarien
    ]


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


@router.get("/mandanten/{mandant_id}/export/plan.xlsx")
def export_plan_xlsx(
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
    plan = berechne_plan(
        db, mandant, start=_datum(start, "start") if start else None, wochen=wochen,
        szenario=_szenario_oder_none(db, mandant.id, szenario_id),
    )
    sollist = soll_ist_vergleich(db, mandant)
    daten = export.plan_xlsx(plan, sollist if sollist.get("snapshot") else None, mandant)
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
    benutzer=Depends(aktueller_benutzer),
    db: Session = Depends(get_db),
):
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    if not (1 <= wochen <= 26):
        raise HTTPException(422, "Wochen muss zwischen 1 und 26 liegen")
    plan = berechne_plan(
        db, mandant, start=_datum(start, "start") if start else None, wochen=wochen,
        szenario=_szenario_oder_none(db, mandant.id, szenario_id),
    )
    daten = export.plan_pdf(plan, mandant)
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
        elif feld in {"name", "aktiv"}:
            setattr(b, feld, wert)
    audit(db, admin, None, "BENUTZER_GEAENDERT", b.email)
    db.commit()
    return {"ok": True}
