"""JSON-API Bestellungen & Bestellfreigabe, Systemeinstellungen (Microsoft Graph,
KI-Zugang), Belegablage sowie der Datei-Proxy für Bestelldateien.

- `router` (Präfix /api): alle JSON-Endpunkte; Graph/KI in synchronen Routen
  (Threadpool), Uploads `async` mit `run_in_threadpool`.
- `seiten`: Datei-Proxy außerhalb von /api – bei abgelaufener Sitzung leitet er
  auf die Anmeldung um, statt JSON zu zeigen.

Fehler der Dienste: ValueError → 422 (Eingabe) bzw. 409 (Zustand),
AblageFehler → `.status`, GraphFehler → 502 (403/404/413/422 durchgereicht),
KiFehler → 502. Jede Änderung: audit + commit.
"""

import json
import logging
import re
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Body, Depends, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..database import get_db
from ..models import AblageTyp, BestellStatus
from ..security import (
    aktueller_benutzer,
    audit,
    darf_bestellfreigabe,
    mandant_oder_403,
    nur_admin,
    nur_freigabe,
    nur_schreibend,
)
from ..services import ablage, bestellungen, ki, sharepoint, systemeinstellungen
from ..services.liquiditaet import BESTELL_KEY
from ..services.zugaenge import DateiRef

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")
seiten = APIRouter()

KI_DATEN_MAX = 200_000          # Zeichen (JSON) – Nachvollziehbarkeit, kein Ablageort
_VORSCHLAG_FELDER = (
    "kreditor", "bestellnummer", "beschreibung", "bestelldatum", "lieferdatum", "zahldatum",
    "zahlungsziel_tage", "betrag_netto", "ust_satz", "betrag_brutto", "konto_id",
    "konto_quelle", "iban",
)
# Zustandsfehler der Dienste (→ 409); alles andere sind Eingabefehler (→ 422)
_ZUSTAND = re.compile(
    r"im Status|\(Status:|kann nicht|können\b.*\bwerden|\bbereits\b|ist storniert"
)
# „Name (1).pdf“ – Zähler der Ablage bei Namensgleichheit
_ZAEHLER = re.compile(r" \(\d+\)(?=\.[^.]*$|$)")


# ---------------------------------------------------------------- Hilfen

@contextmanager
def _fehler(db: Session):
    """Dienstfehler in HTTP-Fehler übersetzen (Transaktion zurückrollen)."""
    try:
        yield
    except HTTPException:
        db.rollback()
        raise
    except ValueError as e:
        db.rollback()
        text = str(e) or "Ungültige Eingabe"
        raise HTTPException(409 if _ZUSTAND.search(text) else 422, text) from e
    except ablage.AblageFehler as e:
        db.rollback()
        raise HTTPException(e.status, e.nachricht) from e
    except sharepoint.GraphFehler as e:
        db.rollback()
        # nie 401 an den Browser (würde zur Anmeldung umleiten)
        status = e.status if e.status in (403, 404, 413, 422) else 502
        raise HTTPException(status, str(e)) from e
    except ki.KiFehler as e:
        db.rollback()
        raise HTTPException(502, str(e)) from e


def _eur(betrag) -> str:
    """Deutsches Zahlenformat: 1.234,56"""
    s = f"{float(betrag or 0):,.2f}"
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def _ganzzahl(wert, feld: str) -> int | None:
    if wert is None or wert == "":
        return None
    if isinstance(wert, bool):
        raise HTTPException(422, f"Ungültiger Wert für '{feld}'")
    try:
        return int(wert)
    except (TypeError, ValueError):
        raise HTTPException(422, f"Ungültiger Wert für '{feld}'") from None


def _wahr(wert, standard: bool = True) -> bool:
    if wert is None:
        return standard
    if isinstance(wert, str):
        return wert.strip().lower() not in ("", "0", "false", "nein", "off", "no")
    return bool(wert)


def _datum_param(wert: str | None, feld: str) -> date:
    try:
        return date.fromisoformat(str(wert or "").strip())
    except ValueError:
        raise HTTPException(422, f"Ungültiges Datum für '{feld}' (erwartet JJJJ-MM-TT)") from None


def _bestellung(db: Session, benutzer: models.Benutzer,
                bestellung_id: int) -> tuple[models.Bestellung, models.Mandant]:
    b = db.get(models.Bestellung, bestellung_id)
    if b is None:
        raise HTTPException(404, "Bestellung nicht gefunden")
    return b, mandant_oder_403(db, benutzer, b.mandant_id)


def _kw(jahr, kw, standard: date | None = None) -> tuple[int, int]:
    """(Jahr, KW) aus der Anfrage; ohne Angabe die KW von `standard`, mindestens
    die laufende KW."""
    if jahr in (None, "") and kw in (None, ""):
        heute = date.today()
        iso = max(standard or heute, heute).isocalendar()
        return iso.year, iso.week
    if jahr in (None, "") or kw in (None, ""):
        raise HTTPException(422, "Bitte Jahr und Kalenderwoche angeben")
    try:
        j, w = int(jahr), int(kw)
        date.fromisocalendar(j, w, 1)
    except (TypeError, ValueError):
        raise HTTPException(422, "Ungültige Kalenderwoche") from None
    return j, w


def _kennzahlen(db: Session, m: models.Mandant, b: models.Bestellung, jahr: int, kw: int,
                basis) -> dict:
    """Freigabe-Kennzahlen der Bestellung – immer serverseitig berechnet; geprüft wird
    der offene Rest (Teilzahlungen bis gestern stehen schon im Ist)."""
    return bestellungen.bestellung_kennzahlen(
        db, m, b, jahr=jahr, kw=kw, basis=basis if basis not in ("", None) else None,
    )


def _graph(db: Session) -> sharepoint.GraphClient:
    zugang = systemeinstellungen.graph_zugang(db)
    if zugang is None:
        raise HTTPException(
            409,
            "Microsoft Graph (SharePoint) ist nicht eingerichtet – bitte unter „System“ "
            "Tenant-ID, Anwendungs-(Client-)ID und Client-Secret hinterlegen",
        )
    return sharepoint.GraphClient(zugang)


def _originalname(dateiname: str | None) -> str:
    name = str(dateiname or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:255] or "bestellung"


def _ablage_name(b: models.Bestellung, originalname: str, mime: str) -> str:
    """Dateiname in der Ablage: „{bestelldatum}_{kreditor}_{originalname}“ –
    Endung aus dem erkannten Dateityp, nie aus dem Client. Kreditor einzeln
    bereinigt: „/“ bzw. „\\“ (z. B. „Hansen A/S“) gilt sonst als Pfadtrenner."""
    kreditor = ablage._sicherer_name(b.kreditor, 60) or "Kreditor"
    return ablage.sicherer_dateiname(
        f"{b.bestelldatum.isoformat()}_{kreditor}_{_originalname(originalname)}",
        ablage.ERLAUBTE_ENDUNGEN.get(mime),
    )


def _datei_eintragen(b: models.Bestellung, ref: DateiRef) -> None:
    """Dateireferenz an der Bestellung (ohne Journal – Teil der Erfassung)."""
    b.datei_name = ref.name[:255] if ref.name else None
    b.datei_mime = ref.mime
    b.datei_groesse = ref.groesse
    b.datei_ablage = ref.ablage
    b.datei_pfad = ref.pfad
    b.datei_drive_id = ref.drive_id
    b.datei_item_id = ref.item_id
    b.datei_web_url = ref.web_url


async def _upload_lesen(datei: UploadFile) -> tuple[bytes, str, str]:
    """Upload mit Größengrenze lesen und nach Inhalt prüfen → (daten, name, mime)."""
    max_mb = int(get_settings().ablage_max_mb)
    grenze = max_mb * 1024 * 1024
    zu_gross = HTTPException(413, f"Die Datei ist zu groß (max. {max_mb} MB)")
    if datei.size is not None and datei.size > grenze:
        raise zu_gross
    teile, n = [], 0
    while stueck := await datei.read(1024 * 1024):
        n += len(stueck)
        if n > grenze:
            raise zu_gross
        teile.append(stueck)
    daten = b"".join(teile)
    name = _originalname(datei.filename)
    try:
        mime = ablage.pruefe_upload(daten, name)
    except ablage.AblageFehler as e:
        raise HTTPException(e.status, e.nachricht) from e
    return daten, name, mime


def _zwischen_eigen(meta: dict, benutzer: models.Benutzer) -> None:
    """Zwischenablage nur für den hochladenden Benutzer."""
    if meta.get("benutzer_id") not in (None, benutzer.id):
        raise ablage.AblageFehler(
            404, "Die hochgeladene Datei ist nicht mehr vorhanden – bitte erneut hochladen")


def _duplikat(db: Session, m: models.Mandant, name: str, groesse: int, mime: str,
              ref: DateiRef | None = None) -> dict | None:
    """Bestehende Bestellung mit derselben Datei (gleicher Name und gleiche Größe;
    aus der Ablage übernommen: gleiches Element)."""
    sicher = ablage.sicherer_dateiname(name, ablage.ERLAUBTE_ENDUNGEN.get(mime)).casefold()
    q = (
        select(models.Bestellung)
        .where(models.Bestellung.mandant_id == m.id, models.Bestellung.datei_groesse == groesse)
        .order_by(models.Bestellung.id.desc())
    )
    for b in db.scalars(q):
        gespeichert = _ZAEHLER.sub("", b.datei_name or "").casefold()
        ki_datei = (b.ki_daten or {}).get("datei") if isinstance(b.ki_daten, dict) else None
        ki_name = ki_datei.get("name") if isinstance(ki_datei, dict) else None
        gleich = (
            b.datei_name == name
            or ki_name == name
            or gespeichert == sicher
            or gespeichert.endswith("_" + sicher)
        )
        if ref is not None and b.datei_ablage == ref.ablage:
            if ref.ablage == AblageTyp.SHAREPOINT.value:
                gleich = gleich or bool(ref.item_id and b.datei_item_id == ref.item_id)
            else:
                gleich = gleich or bool(ref.pfad and b.datei_pfad == ref.pfad)
        if gleich:
            return {
                "id": b.id,
                "kreditor": b.kreditor,
                "erstellt_am": b.erstellt_am.isoformat() if b.erstellt_am else None,
                "status": b.status,
                "status_text": bestellungen.STATUS_TEXT.get(b.status, b.status),
            }
    return None


def _ki_auslesen(db: Session, m: models.Mandant, benutzer: models.Benutzer, daten: bytes,
                 name: str, mime: str) -> dict:
    """Dokument auslesen (KI); Fehler landen in `ki.fehler` – erfassen lässt sich
    die Bestellung immer, notfalls manuell."""
    try:
        erg = ki.bestellung_auslesen(db, m, daten, name, mime)
    except Exception as e:  # noqa: BLE001 – laut Vertrag wirft das Auslesen nicht
        log.exception("Auslesen der Bestellung fehlgeschlagen")
        db.rollback()
        erg = {
            "vorschlag": {f: None for f in _VORSCHLAG_FELDER},
            "ki": {"aktiv": False, "modell": None, "quelle": None, "felder": [], "hinweise": [],
                   "fehler": f"Auslesen fehlgeschlagen: {e}"},
            "text_quelle": "LEER",
            "text_auszug": "",
        }
    info = erg.get("ki") or {}
    if info.get("aktiv"):
        # Datenschutz: Übermittlung an den KI-Dienst nachvollziehbar machen
        audit(db, benutzer, m.id, "BESTELLUNG_AUSGELESEN", f"{name} → KI ({info.get('modell')})")
        db.commit()
    return {
        "vorschlag": erg.get("vorschlag") or {},
        "ki": info,
        "text_quelle": erg.get("text_quelle"),
        "text_auszug": erg.get("text_auszug") or "",
    }


# ---------------------------------------------------------------- Systemeinstellungen (Admin)

@router.get("/system/einstellungen")
def system_einstellungen(admin=Depends(nur_admin), db: Session = Depends(get_db)):
    return systemeinstellungen.oeffentlich(db)


@router.put("/system/einstellungen")
def system_einstellungen_aendern(
    daten: dict, admin=Depends(nur_admin), db: Session = Depends(get_db)
):
    """Nur gesendete Felder ändern; Geheimnis: fehlt/null = unverändert, "" = löschen."""
    with _fehler(db):
        geaendert = systemeinstellungen.setze(db, admin, daten)
        if geaendert:
            # nur Schlüsselnamen – nie Werte (Geheimnisse!)
            audit(db, admin, None, "SYSTEMEINSTELLUNGEN_GEAENDERT", ", ".join(geaendert))
        db.commit()
    return systemeinstellungen.oeffentlich(db)


@router.post("/system/graph/test")
def system_graph_test(admin=Depends(nur_admin), db: Session = Depends(get_db)):
    with _fehler(db), _graph(db) as gc:
        return gc.verbindung_testen()


@router.post("/system/ki/test")
def system_ki_test(admin=Depends(nur_admin), db: Session = Depends(get_db)):
    zugang = systemeinstellungen.ki_zugang(db)
    hinweis = systemeinstellungen.ki_key_hinweis(db)
    if zugang is None:
        raise HTTPException(
            409, "Der KI-Zugang ist nicht eingerichtet – bitte API-URL und Modell hinterlegen"
            + (f" – {hinweis}" if hinweis else ""))
    with _fehler(db):
        try:
            return ki.verbindung_testen(zugang)
        except ki.KiFehler as e:
            if hinweis:  # z. B. Key wegen http:// bzw. fehlender LLM_URL nicht gesendet
                raise ki.KiFehler(f"{e} – {hinweis}", status=e.status) from e
            raise


# ---------------------------------------------------------------- SharePoint-Auswahl (Admin)

@router.get("/sharepoint/websites")
def sharepoint_websites(suche: str = "", admin=Depends(nur_admin),
                        db: Session = Depends(get_db)):
    with _fehler(db), _graph(db) as gc:
        return gc.websites(suche)


@router.get("/sharepoint/bibliotheken")
def sharepoint_bibliotheken(site_id: str = "", admin=Depends(nur_admin),
                            db: Session = Depends(get_db)):
    if not site_id.strip():
        raise HTTPException(422, "Bitte eine Website wählen")
    with _fehler(db), _graph(db) as gc:
        return gc.bibliotheken(site_id.strip())


@router.get("/sharepoint/ordner")
def sharepoint_ordner(drive_id: str = "", item_id: str = "", admin=Depends(nur_admin),
                      db: Session = Depends(get_db)):
    if not drive_id.strip():
        raise HTTPException(422, "Bitte eine Dokumentbibliothek wählen")
    with _fehler(db), _graph(db) as gc:
        return gc.ordner(drive_id.strip(), item_id.strip() or None)


# ---------------------------------------------------------------- Lokale Ablage (Admin)

@router.get("/ablage/lokal")
def ablage_lokal(pfad: str = "", admin=Depends(nur_admin), db: Session = Depends(get_db)):
    with _fehler(db):
        return ablage.lokal_ordner(pfad)


@router.post("/ablage/lokal", status_code=201)
def ablage_lokal_anlegen(daten: dict, admin=Depends(nur_admin), db: Session = Depends(get_db)):
    with _fehler(db):
        pfad = ablage.lokal_ordner_anlegen(str(daten.get("eltern") or ""),
                                           str(daten.get("name") or ""))
        audit(db, admin, None, "ABLAGE_ORDNER_ANGELEGT", pfad)
        db.commit()
    return {"pfad": pfad}


# ---------------------------------------------------------------- Ablage je Mandant

def _ablage_antwort(db: Session, m: models.Mandant) -> dict:
    return {
        **ablage.mandant_ablage(m),
        "bestellungen_offen_einplanen": bool(m.bestellungen_offen_einplanen),
        "graph_konfiguriert": systemeinstellungen.graph_zugang(db) is not None,
    }


def _ablage_lokal_setzen(db: Session, m: models.Mandant, roh) -> None:
    """LOKAL: Pfad relativ zum Ablageverzeichnis prüfen und anlegen (leer = Standard)."""
    text = str(roh or "").strip()
    rel = None
    if text:
        basis = ablage.stamm()
        ziel = ablage.sicherer_pfad(text)
        if ziel == basis:
            raise ablage.AblageFehler(422, "Bitte einen Unterordner des Ablageverzeichnisses wählen")
        if ziel.exists() and not ziel.is_dir():
            raise ablage.AblageFehler(422, "Der Pfad ist kein Ordner")
        rel = ziel.relative_to(basis).as_posix()
        if len(rel) > 512:
            raise ablage.AblageFehler(422, "Der Pfad ist zu lang")
        # Ordner anderer Mandanten (auch deren Standardordner) und ihre Belege
        ablage.lokal_ueberschneidung(db, m, rel, belege=True)
        try:
            ziel.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ablage.AblageFehler(502, f"Ordner konnte nicht angelegt werden: {e.strerror or e}") from e
        neu = ablage.sicherer_pfad(text).relative_to(basis).as_posix()  # erneut prüfen (Symlinks)
        if neu != rel:
            raise ablage.AblageFehler(409, "Der Ordner hat sich beim Anlegen verändert – bitte erneut wählen")
    else:
        ablage.lokal_ueberschneidung(db, m, ablage.standard_lokal_pfad(m), belege=True)
    m.ablage_typ = AblageTyp.LOKAL.value
    m.ablage_pfad = rel
    m.ablage_site_id = m.ablage_drive_id = m.ablage_ordner_id = m.ablage_web_url = None


def _website_name(gc: sharepoint.GraphClient, site_id: str) -> str | None:
    """Anzeigename einer Website (GraphClient kennt nur die Suche) – None, wenn
    nicht ermittelbar."""
    try:
        d = gc._json("GET", f"/sites/{sharepoint._id(site_id, 'Website-ID')}",
                     params={"$select": "id,displayName,name,webUrl"}, was="Website")
    except sharepoint.GraphFehler:
        return None
    return sharepoint._site(d).get("name")


def _ablage_sharepoint_setzen(db: Session, m: models.Mandant, daten: dict) -> None:
    """SHAREPOINT: Ordner über Graph prüfen und den Anzeigepfad
    „Website › Bibliothek › /A/B“ serverseitig bilden (Client-Angaben zu Pfad und
    Link werden nicht übernommen)."""
    drive = str(daten.get("drive_id") or "").strip()
    ordner = str(daten.get("ordner_id") or "").strip() or None
    site = str(daten.get("site_id") or "").strip() or None
    if not drive:
        raise ablage.AblageFehler(422, "Bitte eine Dokumentbibliothek (Drive-ID) angeben")
    for wert, was in ((drive, "Drive-ID"), (ordner, "Ordner-ID"), (site, "Website-ID")):
        if wert and len(wert) > 255:
            raise ablage.AblageFehler(422, f"Die {was} ist zu lang")
    with _graph(db) as gc:
        e = gc.element(drive, ordner)
        if not e.get("ist_ordner"):
            raise ablage.AblageFehler(422, "Das gewählte Element ist kein Ordner")
        # Ordner anderer Mandanten derselben Bibliothek: gleich/darüber/darunter → 409
        ablage.sharepoint_ueberschneidung(db, gc, m, drive, e)
        bib = gc.bibliothek(drive)
        site_name = _website_name(gc, site) if site else None
    pfad = e.get("pfad") or "/"
    anzeige = " › ".join(x for x in (site_name, bib.get("name") or "Bibliothek", pfad) if x)
    web_url = e.get("web_url") or bib.get("web_url")
    m.ablage_typ = AblageTyp.SHAREPOINT.value
    m.ablage_pfad = anzeige[:512]
    m.ablage_site_id = site if site_name else None
    m.ablage_drive_id = drive
    m.ablage_ordner_id = None if e.get("ist_wurzel") or not ordner else e.get("id") or ordner
    m.ablage_web_url = web_url if web_url and len(web_url) <= 1024 else None


@router.get("/mandanten/{mandant_id}/ablage")
def mandant_ablage_abrufen(mandant_id: int, benutzer=Depends(aktueller_benutzer),
                           db: Session = Depends(get_db)):
    return _ablage_antwort(db, mandant_oder_403(db, benutzer, mandant_id))


@router.put("/mandanten/{mandant_id}/ablage")
def mandant_ablage_setzen(mandant_id: int, daten: dict, admin=Depends(nur_admin),
                          db: Session = Depends(get_db)):
    """Ablageort des Mandanten (nur Admin – der Graph-Zugang sieht alle Websites)."""
    m = mandant_oder_403(db, admin, mandant_id)
    typ = str(daten.get("typ") or "").strip().upper()
    with _fehler(db):
        if typ == AblageTyp.LOKAL.value:
            _ablage_lokal_setzen(db, m, daten.get("lokal_pfad"))
        elif typ == AblageTyp.SHAREPOINT.value:
            _ablage_sharepoint_setzen(db, m, daten)
        else:
            raise HTTPException(422, "Unbekannter Ablagetyp (LOKAL oder SHAREPOINT)")
        audit(db, admin, m.id, "ABLAGE_GEAENDERT", ablage.mandant_ablage(m)["beschreibung"])
        db.commit()
    return _ablage_antwort(db, m)


@router.get("/mandanten/{mandant_id}/ablage/durchsuchen")
def mandant_ablage_durchsuchen(mandant_id: int, element: str = "",
                               benutzer=Depends(aktueller_benutzer),
                               db: Session = Depends(get_db)):
    m = mandant_oder_403(db, benutzer, mandant_id)
    with _fehler(db):
        return ablage.mandant_durchsuchen(db, m, element or None)


# ---------------------------------------------------------------- Bestellungen

@router.get("/mandanten/{mandant_id}/bestellungen")
def bestellungen_liste(mandant_id: int, status: str | None = None, suche: str | None = None,
                       benutzer=Depends(aktueller_benutzer), db: Session = Depends(get_db)):
    m = mandant_oder_403(db, benutzer, mandant_id)
    with _fehler(db):
        liste = bestellungen.liste(db, m, status=status, suche=suche)
    return {
        "bestellungen": liste,
        "kreditoren": bestellungen.kreditoren(db, m),
        "darf_freigeben": darf_bestellfreigabe(benutzer),
        "ki_aktiv": systemeinstellungen.ki_zugang(db) is not None,
        "ablage": ablage.mandant_ablage(m),
    }


def _auslesen_upload(db: Session, m: models.Mandant, benutzer: models.Benutzer, daten: bytes,
                     name: str, mime: str) -> dict:
    with _fehler(db):
        try:
            ablage.zwischen_aufraeumen()
        except ablage.AblageFehler:
            pass
        token = ablage.zwischen_speichern(m.id, benutzer.id, name, daten, mime)
    erg = _ki_auslesen(db, m, benutzer, daten, name, mime)
    return {
        "token": token,
        "datei": {"name": name, "mime": mime, "groesse": len(daten)},
        **erg,
        "duplikat": _duplikat(db, m, name, len(daten), mime),
    }


@router.post("/mandanten/{mandant_id}/bestellungen/auslesen")
async def bestellung_auslesen(mandant_id: int, datei: UploadFile,
                              benutzer=Depends(aktueller_benutzer),
                              db: Session = Depends(get_db)):
    """Upload in die Zwischenablage + KI-Vorschlag für den Erfassungsdialog."""
    nur_schreibend(benutzer)
    m = mandant_oder_403(db, benutzer, mandant_id)
    daten, name, mime = await _upload_lesen(datei)
    return await run_in_threadpool(_auslesen_upload, db, m, benutzer, daten, name, mime)


@router.post("/mandanten/{mandant_id}/bestellungen/auslesen-ablage")
def bestellung_auslesen_ablage(mandant_id: int, daten: dict,
                               benutzer=Depends(aktueller_benutzer),
                               db: Session = Depends(get_db)):
    """Datei aus dem Ablageordner des Mandanten auslesen (sie bleibt, wo sie ist)."""
    nur_schreibend(benutzer)
    m = mandant_oder_403(db, benutzer, mandant_id)
    element = str(daten.get("element") or "").strip()
    if not element:
        raise HTTPException(422, "Keine Datei gewählt")
    with _fehler(db):
        ref, inhalt = ablage.mandant_datei(db, m, element)
    erg = _ki_auslesen(db, m, benutzer, inhalt, ref.name, ref.mime)
    return {
        "ablage_element": element,
        "datei": {"name": ref.name, "mime": ref.mime, "groesse": ref.groesse},
        **erg,
        "duplikat": _duplikat(db, m, ref.name, ref.groesse, ref.mime, ref),
    }


@router.get("/mandanten/{mandant_id}/bestellungen/zwischenablage/{token}")
def bestellung_zwischenablage(mandant_id: int, token: str,
                              benutzer=Depends(aktueller_benutzer),
                              db: Session = Depends(get_db)):
    """Vorschau der hochgeladenen, noch nicht gespeicherten Datei."""
    m = mandant_oder_403(db, benutzer, mandant_id)
    with _fehler(db):
        meta, inhalt = ablage.zwischen_lesen(token, m.id)
        _zwischen_eigen(meta, benutzer)
    kopf = ablage.datei_header(meta.get("dateiname") or "datei", meta.get("mime") or "")
    return Response(content=inhalt, media_type=kopf["Content-Type"], headers=kopf)


@router.post("/mandanten/{mandant_id}/bestellungen", status_code=201)
def bestellung_anlegen(mandant_id: int, daten: dict, benutzer=Depends(aktueller_benutzer),
                       db: Session = Depends(get_db)):
    """Bestellung erfassen (Status „zur Freigabe“). Mit `token`: hochgeladene Datei
    in der Mandantenablage speichern; mit `ablage_element`: vorhandene Datei der
    Ablage verknüpfen (ohne Kopie). `ust_satz` fehlt = Steuersatz des Kontos."""
    nur_schreibend(benutzer)
    m = mandant_oder_403(db, benutzer, mandant_id)
    token = str(daten.get("token") or "").strip() or None
    element = str(daten.get("ablage_element") or "").strip() or None
    if token and element:
        raise HTTPException(422, "Bitte entweder eine hochgeladene Datei oder eine Datei aus der Ablage angeben")
    ki_daten = daten.get("ki_daten")
    if ki_daten is not None and not isinstance(ki_daten, dict):
        raise HTTPException(422, "Ungültige KI-Daten")
    if ki_daten and len(json.dumps(ki_daten, ensure_ascii=False, default=str)) > KI_DATEN_MAX:
        raise HTTPException(422, "Die KI-Daten sind zu umfangreich")
    felder = {k: v for k, v in daten.items() if k in bestellungen.FELDER}

    with _fehler(db):
        meta = inhalt = ref = None
        if token:
            meta, inhalt = ablage.zwischen_lesen(token, m.id)
            _zwischen_eigen(meta, benutzer)
        elif element:
            ref, _ = ablage.mandant_datei(db, m, element)
        # erst prüfen und anlegen, dann speichern – keine verwaisten Dateien in der Ablage
        b = bestellungen.anlegen(db, m, benutzer, felder, datei=ref, ki_daten=ki_daten or None)
        if meta is not None:
            mime = meta.get("mime") or "application/octet-stream"
            ref = ablage.speichern(db, m, _ablage_name(b, meta.get("dateiname") or "", mime),
                                   inhalt, mime)
            _datei_eintragen(b, ref)
        audit(db, benutzer, m.id, "BESTELLUNG_ERFASST",
              f"#{b.id} {bestellungen.bestellung_text(b)}" + (f" – Datei {ref.name}" if ref else ""))
        db.commit()
    if token:
        try:
            ablage.zwischen_entfernen(token)
        except ablage.AblageFehler:
            pass
    return bestellungen.als_dict(db, b)


@router.get("/bestellungen/{bestellung_id}")
def bestellung_abrufen(bestellung_id: int, benutzer=Depends(aktueller_benutzer),
                       db: Session = Depends(get_db)):
    b, m = _bestellung(db, benutzer, bestellung_id)
    return {
        **bestellungen.als_dict(db, b),
        "journal": bestellungen.journal_liste(db, m, bestellung_id=b.id),
    }


@router.patch("/bestellungen/{bestellung_id}")
def bestellung_aendern(bestellung_id: int, daten: dict, benutzer=Depends(aktueller_benutzer),
                       db: Session = Depends(get_db)):
    nur_schreibend(benutzer)
    b, m = _bestellung(db, benutzer, bestellung_id)
    with _fehler(db):
        geaendert = bestellungen.aendern(db, b, benutzer, daten)
        if geaendert:
            audit(db, benutzer, m.id, "BESTELLUNG_GEAENDERT",
                  f"#{b.id} {bestellungen.bestellung_text(b)}: {', '.join(geaendert)}")
        db.commit()
    return bestellungen.als_dict(db, b)


def _datei_speichern(db: Session, b: models.Bestellung, m: models.Mandant,
                     benutzer: models.Benutzer, daten: bytes, name: str, mime: str) -> dict:
    with _fehler(db):
        ref = ablage.speichern(db, m, _ablage_name(b, name, mime), daten, mime)
        bestellungen.datei_setzen(db, b, benutzer, ref)
        audit(db, benutzer, m.id, "BESTELLUNG_DATEI", f"#{b.id} {ref.name}")
        db.commit()
    return bestellungen.als_dict(db, b)


@router.post("/bestellungen/{bestellung_id}/datei")
async def bestellung_datei(bestellung_id: int, datei: UploadFile,
                           benutzer=Depends(aktueller_benutzer), db: Session = Depends(get_db)):
    """Datei nachreichen bzw. ersetzen (die alte Datei bleibt in der Ablage)."""
    nur_schreibend(benutzer)
    b, m = _bestellung(db, benutzer, bestellung_id)
    daten, name, mime = await _upload_lesen(datei)
    return await run_in_threadpool(_datei_speichern, db, b, m, benutzer, daten, name, mime)


@router.delete("/bestellungen/{bestellung_id}")
def bestellung_loeschen(bestellung_id: int, benutzer=Depends(aktueller_benutzer),
                        db: Session = Depends(get_db)):
    """Löschen nur ohne Entscheidung und Ablösung; Journal und Datei bleiben."""
    nur_schreibend(benutzer)
    b, m = _bestellung(db, benutzer, bestellung_id)
    text = f"#{b.id} {bestellungen.bestellung_text(b)}"
    with _fehler(db):
        bestellungen.loeschen(db, b, benutzer)
        audit(db, benutzer, m.id, "BESTELLUNG_GELOESCHT", text)
        db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Freigabe

def _bestellung_gesperrt(db: Session, benutzer: models.Benutzer,
                         bestellung_id: int) -> tuple[models.Bestellung, models.Mandant]:
    """Wie `_bestellung`, aber mit Zeilensperre (SELECT … FOR UPDATE) für Statuswechsel:
    eine gleichzeitige zweite Anfrage wartet und sieht danach den neuen Status. Unter
    SQLite wirkungslos – dort schützt der bedingte Statuswechsel im Dienst."""
    b = db.get(models.Bestellung, bestellung_id, with_for_update=True, populate_existing=True)
    if b is None:
        raise HTTPException(404, "Bestellung nicht gefunden")
    return b, mandant_oder_403(db, benutzer, b.mandant_id)


@router.get("/bestellungen/{bestellung_id}/freigabe")
def bestellung_freigabe(bestellung_id: int, jahr: int | None = None, kw: int | None = None,
                        basis: str | None = None, benutzer=Depends(aktueller_benutzer),
                        db: Session = Depends(get_db)):
    """Freigabe-Kennzahlen; ohne jahr/kw die KW des Zahldatums (mind. laufende KW)."""
    b, m = _bestellung(db, benutzer, bestellung_id)
    j, w = _kw(jahr, kw, b.zahldatum)
    with _fehler(db):
        return _kennzahlen(db, m, b, j, w, basis)


@router.post("/bestellungen/{bestellung_id}/entscheidung")
def bestellung_entscheidung(bestellung_id: int, daten: dict,
                            benutzer=Depends(aktueller_benutzer),
                            db: Session = Depends(get_db)):
    """Freigeben/Ablehnen. Die Kennzahlen werden serverseitig neu berechnet und als
    Momentaufnahme im Journal gespeichert – Werte des Clients zählen nicht."""
    nur_freigabe(benutzer)
    b, m = _bestellung_gesperrt(db, benutzer, bestellung_id)
    entscheidung = str(daten.get("entscheidung") or "").strip().upper()
    if entscheidung not in ("FREIGEBEN", "ABLEHNEN"):
        raise HTTPException(422, "Unbekannte Entscheidung (FREIGEBEN oder ABLEHNEN)")
    if b.status != BestellStatus.OFFEN.value:
        raise HTTPException(
            409, f"Nur Bestellungen „zur Freigabe“ können entschieden werden "
                 f"(Status: {bestellungen.STATUS_TEXT.get(b.status, b.status)})")
    j, w = _kw(daten.get("jahr"), daten.get("kw"), b.zahldatum)
    with _fehler(db):
        kz = _kennzahlen(db, m, b, j, w, daten.get("basis"))
        bestellungen.entscheiden(db, b, benutzer, entscheidung, daten.get("begruendung"), kz)
        ebene = next((e for e in kz.get("ebenen", []) if e.get("ebene") == kz.get("massgeblich")), None)
        detail = f"#{b.id} {bestellungen.bestellung_text(b)}; {kz['kw']['label']}"
        if ebene:
            detail += (f", verfügbar {_eur(ebene['verfuegbar'])} €, nach Freigabe "
                       f"{_eur(ebene['nach_freigabe'])} € ({ebene['ebene']})")
        aktion = "BESTELLUNG_FREIGEGEBEN" if entscheidung == "FREIGEBEN" else "BESTELLUNG_ABGELEHNT"
        audit(db, benutzer, m.id, aktion, detail)
        db.commit()
    return bestellungen.als_dict(db, b)


@router.post("/bestellungen/{bestellung_id}/zuruecksetzen")
def bestellung_zuruecksetzen(bestellung_id: int, daten: dict,
                             benutzer=Depends(aktueller_benutzer),
                             db: Session = Depends(get_db)):
    nur_freigabe(benutzer)
    b, m = _bestellung_gesperrt(db, benutzer, bestellung_id)
    with _fehler(db):
        bestellungen.zuruecksetzen(db, b, benutzer, daten.get("begruendung"))
        audit(db, benutzer, m.id, "BESTELLUNG_ZURUECKGESETZT",
              f"#{b.id} {bestellungen.bestellung_text(b)}")
        db.commit()
    return bestellungen.als_dict(db, b)


@router.post("/bestellungen/{bestellung_id}/stornieren")
def bestellung_stornieren(bestellung_id: int, daten: dict | None = Body(None),
                          benutzer=Depends(aktueller_benutzer), db: Session = Depends(get_db)):
    nur_schreibend(benutzer)
    b, m = _bestellung_gesperrt(db, benutzer, bestellung_id)
    with _fehler(db):
        bestellungen.stornieren(db, b, benutzer, (daten or {}).get("begruendung"))
        audit(db, benutzer, m.id, "BESTELLUNG_STORNIERT",
              f"#{b.id} {bestellungen.bestellung_text(b)}")
        db.commit()
    return bestellungen.als_dict(db, b)


# ---------------------------------------------------------------- Ablösung

@router.get("/bestellungen/{bestellung_id}/abloesung/kandidaten")
def bestellung_abloese_kandidaten(bestellung_id: int, benutzer=Depends(aktueller_benutzer),
                                  db: Session = Depends(get_db)):
    b, m = _bestellung(db, benutzer, bestellung_id)
    return bestellungen.abloese_kandidaten(db, m, b)


@router.post("/bestellungen/{bestellung_id}/abloesung")
def bestellung_abloesen(bestellung_id: int, daten: dict, benutzer=Depends(aktueller_benutzer),
                        db: Session = Depends(get_db)):
    from sqlalchemy.exc import IntegrityError

    nur_schreibend(benutzer)
    b, m = _bestellung_gesperrt(db, benutzer, bestellung_id)
    betrag = daten.get("betrag")
    if isinstance(betrag, float):
        betrag = Decimal(str(betrag))
    try:
        with _fehler(db):
            a = bestellungen.abloesen(
                db, b, benutzer,
                art=str(daten.get("art") or ""),
                betrag=betrag,
                datum=daten.get("datum") or None,
                abschliessend=_wahr(daten.get("abschliessend"), True),
                buchung_id=_ganzzahl(daten.get("buchung_id"), "buchung_id"),
                transaktion_id=_ganzzahl(daten.get("transaktion_id"), "transaktion_id"),
                posten_id=_ganzzahl(daten.get("posten_id"), "posten_id"),
                notiz=daten.get("notiz"),
            )
            aktion = ("BESTELLUNG_ABGELOEST" if b.status == BestellStatus.ABGELOEST.value
                      else "BESTELLUNG_TEILZAHLUNG")
            audit(db, benutzer, m.id, aktion,
                  f"#{b.id} {bestellungen.bestellung_text(b)}: {a.art} {_eur(a.betrag)} € am {a.datum}")
            db.commit()
    except IntegrityError:
        # eindeutiger Index: dieselbe Zahlung wurde gleichzeitig einer anderen
        # Bestellung zugeordnet
        db.rollback()
        raise HTTPException(
            409, "Die Zahlung ist bereits einer Bestellung zugeordnet – bitte neu laden") from None
    return bestellungen.als_dict(db, b)


@router.delete("/bestell-abloesungen/{abloesung_id}")
def bestell_abloesung_loeschen(abloesung_id: int, benutzer=Depends(aktueller_benutzer),
                               db: Session = Depends(get_db)):
    nur_schreibend(benutzer)
    a = db.get(models.BestellAbloesung, abloesung_id)
    if a is None:
        raise HTTPException(404, "Ablösung nicht gefunden")
    m = mandant_oder_403(db, benutzer, a.mandant_id)
    b = a.bestellung
    detail = f"#{b.id} {bestellungen.bestellung_text(b)}: {a.art} {_eur(a.betrag)} € am {a.datum}"
    with _fehler(db):
        bestellungen.abloesung_aufheben(db, a, benutzer)
        audit(db, benutzer, m.id, "BESTELLUNG_ABLOESUNG_AUFGEHOBEN", detail)
        db.commit()
    return bestellungen.als_dict(db, b)


@router.get("/mandanten/{mandant_id}/bestellungen/abgleich")
def bestellungen_abgleich(mandant_id: int, benutzer=Depends(aktueller_benutzer),
                          db: Session = Depends(get_db)):
    m = mandant_oder_403(db, benutzer, mandant_id)
    return bestellungen.abloese_vorschlaege(db, m)


# ---------------------------------------------------------------- Übersicht, Journal, Plan

@router.get("/mandanten/{mandant_id}/bestellungen/freigabe-uebersicht")
def bestellungen_freigabe_uebersicht(mandant_id: int, jahr: int | None = None,
                                     kw: int | None = None, basis: str | None = None,
                                     benutzer=Depends(aktueller_benutzer),
                                     db: Session = Depends(get_db)):
    """Rahmen der KW (Ebene gesamt) mit den offenen und freigegebenen Bestellungen;
    ohne jahr/kw die laufende KW."""
    m = mandant_oder_403(db, benutzer, mandant_id)
    j, w = _kw(jahr, kw)
    with _fehler(db):
        return bestellungen.freigabe_uebersicht(
            db, m, jahr=j, kw=w, basis=basis if basis not in ("", None) else None)


@router.get("/mandanten/{mandant_id}/bestellungen/journal")
def bestellungen_journal(mandant_id: int, bestellung_id: int | None = None, limit: int = 200,
                         benutzer=Depends(aktueller_benutzer), db: Session = Depends(get_db)):
    """Journal der Bestellfreigabe, neueste zuerst."""
    m = mandant_oder_403(db, benutzer, mandant_id)
    return bestellungen.journal_liste(db, m, bestellung_id=bestellung_id, limit=limit)


@router.get("/mandanten/{mandant_id}/plan/bestellungen")
def plan_bestellungen(mandant_id: int, von: str | None = None, bis: str | None = None,
                      konto_id: str | None = None, benutzer=Depends(aktueller_benutzer),
                      db: Session = Depends(get_db)):
    """Bestellungen einer Planzelle; konto_id leer bzw. „TERMIN:BESTELLUNG“ =
    Zeile „Bestellungen (ohne Konto)“."""
    m = mandant_oder_403(db, benutzer, mandant_id)
    d_von, d_bis = _datum_param(von, "von"), _datum_param(bis, "bis")
    if d_bis < d_von:
        raise HTTPException(422, "Das Ende des Zeitraums liegt vor dem Beginn")
    roh = (konto_id or "").strip()
    kid = None if roh in ("", BESTELL_KEY) else _ganzzahl(roh, "konto_id")
    with _fehler(db):
        return bestellungen.plan_bestellungen(db, m, konto_id=kid, von=d_von, bis=d_bis)


# ---------------------------------------------------------------- Datei-Proxy (Seite)

@seiten.get("/mandanten/{mandant_id}/bestellungen/{bestellung_id}/datei")
def bestellung_datei_anzeigen(request: Request, mandant_id: int, bestellung_id: int,
                              db: Session = Depends(get_db)):
    """Bestelldatei ausliefern – lokal als Datei, aus SharePoint über den Server
    (die vorab signierte Download-URL erreicht den Browser nie). Header immer aus
    `ablage.datei_header` (inline nur PDF/Bilder, nosniff, kein Caching)."""
    from .pages import _benutzer_oder_login

    benutzer = _benutzer_oder_login(request, db)
    if benutzer is None:
        return RedirectResponse("/login", status_code=303)
    mandant_oder_403(db, benutzer, mandant_id)
    b = db.get(models.Bestellung, bestellung_id)
    if b is None or b.mandant_id != mandant_id:
        raise HTTPException(404, "Bestellung nicht gefunden")
    ref = ablage.ref_aus_bestellung(b)
    if ref is None:
        raise HTTPException(404, "Zur Bestellung ist keine Datei hinterlegt")
    kopf = ablage.datei_header(ref.name, ref.mime)
    with _fehler(db):
        if ref.ablage == AblageTyp.LOKAL.value:
            return FileResponse(ablage.lokal_dateipfad(ref), media_type=kopf["Content-Type"],
                                headers=kopf)
        inhalt = ablage.lesen(db, ref)
    return Response(content=inhalt, media_type=kopf["Content-Type"], headers=kopf)
