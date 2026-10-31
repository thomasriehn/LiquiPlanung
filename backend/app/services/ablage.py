"""Belegablage (Bestelldateien): lokal unter ABLAGE_VERZEICHNIS oder in SharePoint.

- Pfadsicherheit: alle lokalen Pfade sind relativ zum Stammverzeichnis und werden
  über `sicherer_pfad` aufgelöst (keine absoluten Pfade, kein „..“, kein NUL,
  keine versteckten Ordner, kein Ausbruch über Symlinks).
- Mandantenordner: Durchsuchen und Lesen nur innerhalb des Ordners des Mandanten
  (SharePoint: Nachfahren-Prüfung über den Pfad der Elemente). Ordner zweier
  Mandanten dürfen weder gleich sein noch ineinander liegen (lokal vor jedem
  Zugriff geprüft, SharePoint beim Festlegen); Standardordner „<Kurzname>-<id>“.
  Ordner gelöschter Mandanten wandern nach `.geloescht` (nie gelöscht).
- Zwischenablage: Uploads liegen bis zum Speichern unter `.zwischenablage`
  (Token = uuid4-hex, an den Mandanten gebunden, 24 h gültig).
- Upload-Prüfung nach Inhalt (Magic Bytes), nie nach Client-Content-Type.
- Antwort-Header für den Datei-Proxy (Content-Disposition, nosniff, CSP).
"""

import io
import json
import logging
import mimetypes
import os
import re
import unicodedata
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from sqlalchemy import select

from .. import models
from ..config import get_settings
from ..models import AblageTyp
from . import sharepoint, systemeinstellungen
from .zugaenge import DateiRef

log = logging.getLogger(__name__)

ZWISCHEN_ORDNER = ".zwischenablage"
GELOESCHT_ORDNER = ".geloescht"     # Ordner gelöschter Mandanten (versteckt, nie gelöscht)
ZWISCHEN_MAX_STUNDEN = 24
MAX_TIEFE = 32
NAME_MAX = 120

ERLAUBTE_ENDUNGEN: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tif",
    "image/webp": ".webp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/plain": ".txt",
    "message/rfc822": ".eml",
}
INLINE_MIME = {"application/pdf", "image/png", "image/jpeg", "image/webp", "image/tiff"}

# Windows/SharePoint-untaugliche Zeichen (inkl. # und % für ältere SharePoint-Versionen)
_VERBOTEN = re.compile(r'[\x00-\x1f\x7f"*:<>?/\\|#%]')
_RESERVIERT = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9]|desktop\.ini)$", re.IGNORECASE)
_ENDUNG = re.compile(r"^\.[A-Za-z0-9]{1,5}$")
_TOKEN = re.compile(r"^[0-9a-f]{32}$")


class AblageFehler(Exception):
    """Fehler der Belegablage; `status` = HTTP-Status für den Router
    (404 | 403 | 409 | 413 | 422 | 502)."""

    def __init__(self, status: int, nachricht: str):
        super().__init__(nachricht)
        self.status = status
        self.nachricht = nachricht


# --- Stammverzeichnis und Pfadsicherheit -------------------------------------


def stamm() -> Path:
    """Stammverzeichnis der lokalen Ablage (absolut, wird angelegt)."""
    basis = Path(get_settings().ablage_verzeichnis).resolve()
    try:
        basis.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise AblageFehler(502, f"Ablageverzeichnis nicht verfügbar: {e.strerror or e}") from e
    return basis


def _relative_teile(relativ: str | None) -> tuple[str, ...]:
    """Prüft einen relativen Pfad (Text) und liefert seine Bestandteile."""
    if relativ is None:
        return ()
    if not isinstance(relativ, str):
        raise AblageFehler(422, "Ungültiger Pfad")
    if "\x00" in relativ:
        raise AblageFehler(422, "Ungültiger Pfad (Steuerzeichen)")
    text = relativ.replace("\\", "/").strip()
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text) or text.startswith("~"):
        raise AblageFehler(422, "Absolute Pfade sind nicht erlaubt – bitte relativ zum Ablageverzeichnis angeben")
    teile = PurePosixPath(text).parts if text else ()
    for teil in teile:
        if teil == "..":
            raise AblageFehler(422, "Ungültiger Pfad („..“ ist nicht erlaubt)")
        if teil.startswith("."):
            raise AblageFehler(422, "Ungültiger Pfad (versteckte Ordner sind nicht erlaubt)")
    return teile


def sicherer_pfad(relativ: str) -> Path:
    """Relativen Pfad sicher unter `stamm()` auflösen (Symlinks inklusive).
    Absolut, „..“, NUL, versteckte Namen oder Ausbruch → AblageFehler(422)."""
    teile = _relative_teile(relativ)
    basis = stamm()
    ziel = basis.joinpath(*teile).resolve() if teile else basis
    if ziel != basis:
        if not ziel.is_relative_to(basis):
            raise AblageFehler(422, "Der Pfad liegt außerhalb des Ablageverzeichnisses")
        if any(t.startswith(".") for t in ziel.relative_to(basis).parts):
            raise AblageFehler(422, "Ungültiger Pfad (versteckte Ordner sind nicht erlaubt)")
    return ziel


def _rel(pfad: Path, basis: Path | None = None) -> str:
    basis = basis or stamm()
    return "" if pfad == basis else pfad.relative_to(basis).as_posix()


def _sicherer_name(text: str | None, max_laenge: int = NAME_MAX) -> str:
    """Einzelner Namensbestandteil, SharePoint-/Windows-tauglich ('' wenn nichts übrig)."""
    name = unicodedata.normalize("NFC", str(text or ""))
    name = _VERBOTEN.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._")
    name = name.replace("_vti_", "_vti-").lstrip("~$")
    name = name[:max_laenge].rstrip(" .")
    if _RESERVIERT.match(name):
        name = f"_{name}"
    return name


def sicherer_dateiname(name: str, endung: str | None = None) -> str:
    """Sicherer Dateiname (Stamm max. 120 Zeichen). `endung` (z. B. aus dem
    erkannten Dateityp) ersetzt eine vorhandene Endung."""
    roh = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    roh = unicodedata.normalize("NFC", roh).strip()
    stamm_, vorhanden = roh, ""
    punkt = roh.rfind(".")
    if punkt > 0 and _ENDUNG.match(roh[punkt:]) and re.search(r"[A-Za-z]", roh[punkt:]):
        stamm_, vorhanden = roh[:punkt], roh[punkt:]
    if endung is not None:
        ende = re.sub(r"[^A-Za-z0-9]", "", str(endung))[:10].lower()
    else:
        ende = vorhanden[1:].lower()
    sauber = _sicherer_name(stamm_) or "Datei"
    return f"{sauber}.{ende}" if ende else sauber


def standard_lokal_pfad(mandant) -> str:
    """Standardordner eines Mandanten: „<sicherer Kurzname>-<id>“ – durch die ID
    eindeutig, auch wenn verschiedene Kurznamen denselben sicheren Namen ergeben."""
    name = _sicherer_name(getattr(mandant, "kurzname", None), 64) or "mandant"
    return f"{name}-{getattr(mandant, 'id', None) or 0}"


def mandant_ablage(mandant) -> dict:
    """Ablageort des Mandanten für Oberfläche und Speichern."""
    if mandant.ablage_typ == AblageTyp.SHAREPOINT.value:
        pfad = mandant.ablage_pfad or ""
        return {
            "typ": AblageTyp.SHAREPOINT.value,
            "pfad": pfad,
            "lokal_pfad": None,
            "site_id": mandant.ablage_site_id,
            "drive_id": mandant.ablage_drive_id,
            "ordner_id": mandant.ablage_ordner_id,
            "web_url": mandant.ablage_web_url,
            "konfiguriert": bool(mandant.ablage_drive_id),
            "beschreibung": f"SharePoint: {pfad or 'kein Ordner gewählt'}",
        }
    lokal = (mandant.ablage_pfad or "").strip().strip("/") if mandant.ablage_typ == AblageTyp.LOKAL.value else ""
    lokal = lokal or standard_lokal_pfad(mandant)
    return {
        "typ": AblageTyp.LOKAL.value,
        "pfad": lokal,
        "lokal_pfad": lokal,
        "site_id": None,
        "drive_id": None,
        "ordner_id": None,
        "web_url": None,
        "konfiguriert": mandant.ablage_typ is not None,
        "beschreibung": f"lokal: {lokal}",
    }


# --- Mandantentrennung: Ordner dürfen sich nicht überschneiden ----------------


def _vergleichsform(rel: str | None) -> str:
    """Relativer Pfad für Vergleiche: Symlinks soweit möglich aufgelöst, NFC und
    ohne Groß/Klein (Windows/macOS unterscheiden das nicht)."""
    text = str(rel or "").replace("\\", "/").strip().strip("/")
    try:
        teile = _relative_teile(text)
        basis = Path(get_settings().ablage_verzeichnis).resolve()
        ziel = basis.joinpath(*teile).resolve() if teile else basis
        if ziel.is_relative_to(basis):
            text = _rel(ziel, basis)
    except (AblageFehler, OSError, ValueError):
        pass
    return unicodedata.normalize("NFC", text).casefold().strip("/")


def _ueberschneiden(a: str, b: str) -> bool:
    """Gleich oder ineinander (Vergleichsform); leer = Stammverzeichnis = alles."""
    if not a or not b:
        return True
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def lokal_wurzeln_anderer(db, mandant) -> list[tuple["models.Mandant", str]]:
    """Lokale Ordner aller anderen Mandanten: der Standardordner (immer
    reserviert – auch bei SharePoint, ein früher genutzter Ordner bleibt so
    geschützt) und ein festgelegter lokaler Ordner."""
    erg = []
    for anderer in db.scalars(select(models.Mandant).where(models.Mandant.id != mandant.id)):
        erg.append((anderer, standard_lokal_pfad(anderer)))
        eigen = (anderer.ablage_pfad or "").strip().strip("/")
        if anderer.ablage_typ == AblageTyp.LOKAL.value and eigen:
            erg.append((anderer, eigen))
    return erg


def lokal_ueberschneidung(db, mandant, rel: str, *, details: bool = True, belege: bool = False) -> None:
    """Lokale Ordner zweier Mandanten dürfen weder gleich sein noch ineinander
    liegen – sonst sähen Benutzer beim Durchsuchen fremde Belege (→ 409).
    `details`: anderen Mandanten und Ordner nennen (nur für Administratoren);
    `belege`: auch Belege von Bestellungen anderer Mandanten unter `rel` prüfen
    (z. B. ein früherer Ordner eines anderen Mandanten)."""
    eigen = _vergleichsform(rel)
    for anderer, fremd in lokal_wurzeln_anderer(db, mandant):
        if _ueberschneiden(eigen, _vergleichsform(fremd)):
            if details:
                raise AblageFehler(
                    409, f"Der Ordner überschneidet sich mit der Ablage des Mandanten „{anderer.name}“ "
                         f"({fremd}) – bitte einen eigenen Ordner wählen")
            raise AblageFehler(
                409, "Der Ablageordner des Mandanten überschneidet sich mit der Ablage eines anderen "
                     "Mandanten – bitte (Administrator) unter „Ablage“ einen eigenen Ordner festlegen")
    if not belege:
        return
    q = (
        select(models.Bestellung.datei_pfad, models.Mandant.name)
        .join(models.Mandant, models.Mandant.id == models.Bestellung.mandant_id)
        .where(models.Bestellung.mandant_id != mandant.id,
               models.Bestellung.datei_ablage == AblageTyp.LOKAL.value,
               models.Bestellung.datei_pfad.is_not(None))
    )
    for pfad, name in db.execute(q):
        p = unicodedata.normalize("NFC", str(pfad)).casefold().strip("/")
        if p == eigen or p.startswith(eigen + "/"):
            raise AblageFehler(
                409, f"Im Ordner liegen Belege von Bestellungen des Mandanten „{name}“ – bitte einen "
                     "eigenen Ordner wählen")


def sharepoint_ueberschneidung(db, gc: "sharepoint.GraphClient", mandant, drive: str, ordner: dict) -> None:
    """SharePoint-Ordner zweier Mandanten in derselben Bibliothek dürfen weder
    gleich sein noch ineinander liegen; die Bibliothekswurzel überschneidet sich
    mit allem (→ 409). `ordner` = Element des neuen Ordners (gc.element)."""
    eigen_pfad = ordner.get("pfad")
    andere = db.scalars(select(models.Mandant).where(
        models.Mandant.id != mandant.id,
        models.Mandant.ablage_typ == AblageTyp.SHAREPOINT.value,
        models.Mandant.ablage_drive_id == drive,
    ))
    for anderer in andere:
        fremd_id = anderer.ablage_ordner_id
        konflikt = bool(ordner.get("ist_wurzel")) or not fremd_id or fremd_id == ordner.get("id")
        if not konflikt:
            try:
                fremd = gc.element(drive, fremd_id)
            except sharepoint.GraphFehler as e:
                if e.status == 404:
                    continue  # Ordner des anderen gibt es nicht mehr
                raise
            fremd_pfad = fremd.get("pfad")
            konflikt = (
                not eigen_pfad or not fremd_pfad or bool(fremd.get("ist_wurzel"))  # nicht prüfbar → ablehnen
                or sharepoint.pfad_unter(eigen_pfad, fremd_pfad)
                or sharepoint.pfad_unter(fremd_pfad, eigen_pfad)
            )
        if konflikt:
            raise AblageFehler(
                409, f"Der SharePoint-Ordner überschneidet sich mit der Ablage des Mandanten "
                     f"„{anderer.name}“ ({anderer.ablage_pfad or 'Bibliothek'}) – bitte einen eigenen "
                     "Ordner wählen")


def mandant_ordner_plan(db, mandant) -> tuple[str, list[Path]]:
    """Vor dem Löschen eines Mandanten: (Archivname „<name>-<id>“, vorhandene
    lokale Ordner) – Standardordner und festgelegter Ordner. Ordner, die sich
    mit der Ablage eines anderen Mandanten überschneiden, bleiben, wo sie sind."""
    name = f"{_sicherer_name(getattr(mandant, 'kurzname', None), 64) or 'mandant'}-{mandant.id}"
    basis = Path(get_settings().ablage_verzeichnis).resolve()
    if not basis.is_dir():
        return name, []
    kandidaten = [standard_lokal_pfad(mandant)]
    eigen = (mandant.ablage_pfad or "").strip().strip("/")
    if mandant.ablage_typ == AblageTyp.LOKAL.value and eigen:
        kandidaten.append(eigen)
    fremde = [_vergleichsform(f) for _, f in lokal_wurzeln_anderer(db, mandant)]
    ordner: list[Path] = []
    for rel in kandidaten:
        try:
            pfad = sicherer_pfad(rel)
        except AblageFehler:
            continue
        if pfad == basis or pfad in ordner or not pfad.is_dir():
            continue
        if any(_ueberschneiden(_vergleichsform(_rel(pfad, basis)), f) for f in fremde):
            log.warning("Ablageordner %s überschneidet sich mit einem anderen Mandanten – nicht verschoben", pfad)
            continue
        ordner.append(pfad)
    return name, ordner


def mandant_ordner_archivieren(plan: tuple[str, list[Path]]) -> list[tuple[Path, Path]]:
    """Ordner aus `mandant_ordner_plan` nach „.geloescht/<name>-<id>-<zeitstempel>“
    verschieben – nie löschen; ein später angelegter Mandant erbt so keine
    Belege. Liefert (alt, neu) für `archivierung_zuruecknehmen`."""
    name, ordner = plan
    if not ordner:
        return []
    archiv = stamm() / GELOESCHT_ORDNER
    try:
        archiv.mkdir(mode=0o700, exist_ok=True)
    except OSError as e:
        raise AblageFehler(502, f"Ordner {GELOESCHT_ORDNER} nicht verfügbar: {e.strerror or e}") from e
    if archiv.is_symlink() or not archiv.is_dir():
        raise AblageFehler(502, f"Ordner {GELOESCHT_ORDNER} nicht verfügbar")
    stempel = _jetzt().strftime("%Y%m%dT%H%M%SZ")
    verschoben: list[tuple[Path, Path]] = []
    # äußere Ordner zuerst; ein darin liegender wandert mit
    for pfad in sorted(ordner, key=lambda p: len(p.parts)):
        if any(pfad.is_relative_to(alt) for alt, _ in verschoben):
            continue
        ziel = archiv / f"{name}-{stempel}"
        n = 1
        while ziel.exists() or ziel.is_symlink():
            n += 1
            ziel = archiv / f"{name}-{stempel}-{n}"
        try:
            os.rename(pfad, ziel)
        except OSError as e:
            archivierung_zuruecknehmen(verschoben)
            raise AblageFehler(
                502, f"Der Ablageordner des Mandanten konnte nicht verschoben werden: {e.strerror or e}") from e
        verschoben.append((pfad, ziel))
    return verschoben


def standardordner_pruefen(db, mandant) -> None:
    """Neuer Mandant: der Standardordner darf sich mit keiner anderen Ablage
    überschneiden und noch keine Dateien enthalten (→ 409)."""
    rel = standard_lokal_pfad(mandant)
    lokal_ueberschneidung(db, mandant, rel)
    ziel = Path(get_settings().ablage_verzeichnis).resolve() / rel
    if ziel.is_dir() and _anzahl(str(ziel)) > 0:
        raise AblageFehler(
            409, f"Der Ablageordner „{rel}“ für den neuen Mandanten existiert bereits und ist nicht "
                 "leer – bitte einen anderen Kurznamen wählen oder den Ordner verschieben")


def archivierung_zuruecknehmen(verschoben: list[tuple[Path, Path]]) -> None:
    """Verschobene Ordner zurückholen (Löschen des Mandanten fehlgeschlagen)."""
    for alt, neu in reversed(verschoben):
        try:
            if not alt.exists():
                os.rename(neu, alt)
        except OSError:
            log.exception("Ablageordner %s konnte nicht nach %s zurückverschoben werden", neu, alt)


# --- lokaler Ordnerdialog (Administrator) ------------------------------------


def _zeitpunkt(sekunden: float) -> str:
    return datetime.fromtimestamp(sekunden, timezone.utc).isoformat()


def _eintraege(ordner: Path) -> tuple[list[os.DirEntry], list[os.DirEntry]]:
    """Echte Unterordner und Dateien (ohne Symlinks, ohne versteckte Namen)."""
    ordner_, dateien = [], []
    try:
        with os.scandir(ordner) as it:
            for e in it:
                if e.name.startswith(".") or e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    ordner_.append(e)
                elif e.is_file(follow_symlinks=False):
                    dateien.append(e)
    except FileNotFoundError as ex:
        raise AblageFehler(404, "Ordner nicht gefunden") from ex
    except OSError as ex:
        raise AblageFehler(502, f"Ordner nicht lesbar: {ex.strerror or ex}") from ex
    return sorted(ordner_, key=_name_schluessel), sorted(dateien, key=_name_schluessel)


def _name_schluessel(e: os.DirEntry) -> str:
    return e.name.casefold()


def _anzahl(pfad: str) -> int:
    try:
        with os.scandir(pfad) as it:
            return sum(1 for e in it if not e.name.startswith("."))
    except OSError:
        return 0


def lokal_ordner(relativ: str = "") -> dict:
    """Inhalt eines Ordners unter dem Stammverzeichnis (nur echte Verzeichnisse)."""
    ziel = sicherer_pfad(relativ or "")
    if not ziel.is_dir():
        raise AblageFehler(404, "Ordner nicht gefunden")
    rel = _rel(ziel)
    ordner_, dateien = _eintraege(ziel)
    praefix = f"{rel}/" if rel else ""
    return {
        "pfad": rel,
        "unterordner": [
            {"name": e.name, "pfad": praefix + e.name, "elemente": _anzahl(e.path)} for e in ordner_
        ],
        "dateien": [
            {
                "name": e.name,
                "pfad": praefix + e.name,
                "groesse": e.stat(follow_symlinks=False).st_size,
                "geaendert": _zeitpunkt(e.stat(follow_symlinks=False).st_mtime),
            }
            for e in dateien
        ],
    }


def lokal_ordner_anlegen(eltern: str, name: str) -> str:
    """Neuen Unterordner anlegen; liefert seinen relativen Pfad."""
    eltern_pfad = sicherer_pfad(eltern or "")
    if not eltern_pfad.is_dir():
        raise AblageFehler(404, "Übergeordneter Ordner nicht gefunden")
    sauber = _sicherer_name(name)
    if not sauber:
        raise AblageFehler(422, "Bitte einen gültigen Ordnernamen angeben")
    ziel = eltern_pfad / sauber
    if ziel.exists() or ziel.is_symlink():
        raise AblageFehler(409, f"Der Ordner „{sauber}“ existiert bereits")
    try:
        ziel.mkdir()
    except FileExistsError as e:
        raise AblageFehler(409, f"Der Ordner „{sauber}“ existiert bereits") from e
    except OSError as e:
        raise AblageFehler(502, f"Ordner konnte nicht angelegt werden: {e.strerror or e}") from e
    return _rel(ziel)


# --- Mandantenbezogen ---------------------------------------------------------


def _graph(db) -> "sharepoint.GraphClient":
    zugang = systemeinstellungen.graph_zugang(db)
    if zugang is None:
        raise AblageFehler(
            409,
            "Microsoft Graph (SharePoint) ist nicht eingerichtet – bitte in den "
            "Systemeinstellungen Tenant-ID, Client-ID und Client-Secret hinterlegen",
        )
    return sharepoint.GraphClient(zugang)


def _graph_fehler(e: "sharepoint.GraphFehler") -> AblageFehler:
    if e.status == 404:
        return AblageFehler(404, str(e))
    if e.status in (413, 422):
        return AblageFehler(e.status, str(e))
    return AblageFehler(502, f"SharePoint: {e}")


def _sp_drive(mandant) -> str:
    if not mandant.ablage_drive_id:
        raise AblageFehler(409, "Für den Mandanten ist keine SharePoint-Bibliothek gewählt")
    return mandant.ablage_drive_id


def _lokal_wurzel(db, mandant, anlegen: bool = True) -> tuple[str, Path]:
    """(relativer Pfad, absoluter Pfad) des Mandantenordners – überschneidet er
    sich mit der Ablage eines anderen Mandanten → 409 (vor jedem Zugriff)."""
    rel = mandant_ablage(mandant)["lokal_pfad"]
    pfad = sicherer_pfad(rel)
    if pfad == stamm():
        raise AblageFehler(409, "Der Ablageordner des Mandanten ist ungültig")
    lokal_ueberschneidung(db, mandant, rel, details=False)
    if anlegen and not pfad.is_dir():
        try:
            pfad.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise AblageFehler(502, f"Ablageordner konnte nicht angelegt werden: {e.strerror or e}") from e
        pfad = sicherer_pfad(rel)  # erneut prüfen (Symlinks)
    return rel, pfad


def _lokal_innen(db, mandant, element: str | None, anlegen: bool = True) -> tuple[Path, Path, str]:
    """Element (relativ zum Mandantenordner) → (wurzel, pfad, relativ zur Wurzel)."""
    wurzel_rel, wurzel = _lokal_wurzel(db, mandant, anlegen=anlegen)
    teile = _relative_teile(element or "")
    if not teile:
        return wurzel, wurzel, ""
    pfad = sicherer_pfad("/".join((wurzel_rel, *teile)))
    if pfad != wurzel and not pfad.is_relative_to(wurzel):
        raise AblageFehler(403, "Das Element liegt außerhalb der Ablage des Mandanten")
    return wurzel, pfad, _rel(pfad, wurzel)


def speichern(db, mandant, dateiname: str, daten: bytes, mime: str) -> DateiRef:
    """Datei im Ablageordner des Mandanten speichern (nie überschreiben)."""
    name = sicherer_dateiname(dateiname)
    ab = mandant_ablage(mandant)
    if ab["typ"] == AblageTyp.SHAREPOINT.value:
        drive = _sp_drive(mandant)
        with _graph(db) as gc:
            try:
                erg = gc.hochladen(drive, mandant.ablage_ordner_id, name, daten, mime)
            except sharepoint.GraphFehler as e:
                raise _graph_fehler(e) from e
        return DateiRef(
            ablage=AblageTyp.SHAREPOINT.value,
            name=erg.get("name") or name,
            mime=mime,
            groesse=int(erg.get("groesse") or len(daten)),
            pfad=erg.get("pfad"),
            drive_id=drive,
            item_id=erg.get("id"),
            web_url=erg.get("web_url"),
        )

    _, ordner = _lokal_wurzel(db, mandant)
    basis = stamm()
    punkt = name.rfind(".")
    stamm_, endung = (name[:punkt], name[punkt:]) if punkt > 0 else (name, "")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    for n in range(1000):
        kandidat = ordner / (name if n == 0 else f"{stamm_} ({n}){endung}")
        try:
            fd = os.open(kandidat, flags, 0o640)
        except FileExistsError:
            continue
        except OSError as e:
            raise AblageFehler(502, f"Datei konnte nicht gespeichert werden: {e.strerror or e}") from e
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(daten)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            kandidat.unlink(missing_ok=True)
            raise AblageFehler(502, f"Datei konnte nicht gespeichert werden: {e.strerror or e}") from e
        return DateiRef(
            ablage=AblageTyp.LOKAL.value,
            name=kandidat.name,
            mime=mime,
            groesse=len(daten),
            pfad=kandidat.relative_to(basis).as_posix(),
        )
    raise AblageFehler(409, "Kein freier Dateiname im Ablageordner gefunden")


def ref_aus_bestellung(b) -> DateiRef | None:
    """DateiRef aus den datei_*-Feldern einer Bestellung (None ohne Datei)."""
    if not getattr(b, "datei_ablage", None):
        return None
    return DateiRef(
        ablage=b.datei_ablage,
        name=b.datei_name or "datei",
        mime=b.datei_mime or "application/octet-stream",
        groesse=int(b.datei_groesse or 0),
        pfad=b.datei_pfad,
        drive_id=b.datei_drive_id,
        item_id=b.datei_item_id,
        web_url=b.datei_web_url,
    )


def _max_bytes() -> int:
    return int(get_settings().ablage_max_mb) * 1024 * 1024


def lesen(db, ref: DateiRef) -> bytes:
    """Inhalt einer abgelegten Datei; fehlt sie → AblageFehler(404)."""
    if ref is None or not ref.ablage:
        raise AblageFehler(404, "Keine Datei hinterlegt")
    if ref.ablage == AblageTyp.SHAREPOINT.value:
        if not ref.drive_id or not ref.item_id:
            raise AblageFehler(404, "Datei in SharePoint nicht gefunden")
        with _graph(db) as gc:
            try:
                return gc.herunterladen(ref.drive_id, ref.item_id)
            except sharepoint.GraphFehler as e:
                if e.status == 404:
                    raise AblageFehler(404, "Datei in SharePoint nicht gefunden") from e
                raise _graph_fehler(e) from e
    if ref.ablage != AblageTyp.LOKAL.value or not ref.pfad:
        raise AblageFehler(404, "Datei nicht gefunden")
    pfad = sicherer_pfad(ref.pfad)
    if not pfad.is_file():
        raise AblageFehler(404, "Datei nicht gefunden (wurde sie in der Ablage verschoben oder gelöscht?)")
    try:
        return pfad.read_bytes()
    except OSError as e:
        raise AblageFehler(502, f"Datei nicht lesbar: {e.strerror or e}") from e


def lokal_dateipfad(ref: DateiRef) -> Path:
    """Absoluter Pfad einer lokal abgelegten Datei (für FileResponse) – geprüft."""
    if ref is None or ref.ablage != AblageTyp.LOKAL.value or not ref.pfad:
        raise AblageFehler(404, "Datei nicht gefunden")
    pfad = sicherer_pfad(ref.pfad)
    if not pfad.is_file():
        raise AblageFehler(404, "Datei nicht gefunden (wurde sie in der Ablage verschoben oder gelöscht?)")
    return pfad


def _sp_wurzel(gc, mandant) -> tuple[str, dict, str]:
    """(drive_id, Wurzelelement, Anzeigename) der SharePoint-Ablage des Mandanten."""
    drive = _sp_drive(mandant)
    wurzel = gc.element(drive, mandant.ablage_ordner_id or None)
    if not wurzel.get("ist_ordner"):
        raise AblageFehler(409, "Der SharePoint-Ablageort des Mandanten ist kein Ordner")
    if mandant.ablage_ordner_id:
        name = wurzel.get("name") or "Ablage"
    else:
        try:
            name = gc.bibliothek(drive).get("name") or "Bibliothek"
        except sharepoint.GraphFehler:
            name = "Bibliothek"
    return drive, wurzel, name


def _sp_innen(gc, drive: str, e: dict, wurzel: dict) -> list[dict] | None:
    """Prüft, dass `e` die Mandantenwurzel ist oder darunter liegt (Pfadvergleich,
    sonst 403) und liefert die Vorfahren zwischen Wurzel und `e` (oberster zuerst)
    für die Brotkrumen – None, wenn die Kette nicht ermittelbar ist."""
    if e.get("id") == wurzel.get("id"):
        return []
    if e.get("drive_id") and wurzel.get("drive_id") and e["drive_id"] != wurzel["drive_id"]:
        raise AblageFehler(403, "Das Element liegt außerhalb der Ablage des Mandanten")
    if e.get("ist_wurzel") or not sharepoint.pfad_unter(e.get("pfad"), wurzel.get("pfad")) \
            or (e.get("pfad") or "").casefold() == (wurzel.get("pfad") or "").casefold():
        raise AblageFehler(403, "Das Element liegt außerhalb der Ablage des Mandanten")
    kette: list[dict] = []
    aktuell = e
    for _ in range(MAX_TIEFE):
        eltern = aktuell.get("eltern_id")
        if not eltern:
            return None
        if eltern == wurzel.get("id"):
            return list(reversed(kette))
        try:
            aktuell = gc.element(drive, eltern)
        except sharepoint.GraphFehler:
            return None
        if aktuell.get("ist_wurzel") and not wurzel.get("ist_wurzel"):
            # Bibliothekswurzel erreicht, ohne die Mandantenwurzel zu treffen
            raise AblageFehler(403, "Das Element liegt außerhalb der Ablage des Mandanten")
        kette.append(aktuell)
    return None


def _sp_brotkrumen(wurzel_name: str, wurzel: dict, e: dict, kette: list[dict] | None) -> list[dict]:
    teile = [{"element": "", "name": wurzel_name}]
    if e.get("id") == wurzel.get("id"):
        return teile
    if kette is None:
        # Kette unbekannt: Namen aus dem Pfad, Zwischenstufen nicht anklickbar
        rest = (e.get("pfad") or "")[len((wurzel.get("pfad") or "").rstrip("/")):].strip("/")
        namen = [t for t in rest.split("/") if t]
        teile += [{"element": None, "name": n} for n in namen[:-1]]
    else:
        teile += [{"element": a["id"], "name": a["name"]} for a in kette]
    teile.append({"element": e["id"], "name": e["name"]})
    return teile


def mandant_durchsuchen(db, mandant, element: str | None = None) -> dict:
    """Ordnerinhalt NUR innerhalb des Mandantenordners (+ Unterordner).
    LOKAL: element = relativer Pfad unterhalb des Mandantenordners ("" = Wurzel);
    SHAREPOINT: element = item_id (Nachfahre des Mandantenordners, sonst 403)."""
    ab = mandant_ablage(mandant)
    if ab["typ"] == AblageTyp.SHAREPOINT.value:
        with _graph(db) as gc:
            try:
                drive, wurzel, wurzel_name = _sp_wurzel(gc, mandant)
                ziel = (element or "").strip()
                if not ziel or ziel == wurzel.get("id"):
                    e, kette = wurzel, []
                    inhalt = gc.ordner(drive, mandant.ablage_ordner_id or None)
                else:
                    e = gc.element(drive, ziel)
                    kette = _sp_innen(gc, drive, e, wurzel)
                    if not e.get("ist_ordner"):
                        raise AblageFehler(422, "Das Element ist kein Ordner")
                    inhalt = gc.ordner(drive, ziel)
            except sharepoint.GraphFehler as ex:
                raise _graph_fehler(ex) from ex
        return {
            "typ": AblageTyp.SHAREPOINT.value,
            "wurzel_name": wurzel_name,
            "element": "" if e is wurzel else e["id"],
            "pfad_teile": _sp_brotkrumen(wurzel_name, wurzel, e, kette),
            "unterordner": [
                {"element": o["id"], "name": o["name"], "elemente": o["elemente"]}
                for o in inhalt["unterordner"]
            ],
            "dateien": [
                {"element": d["id"], "name": d["name"], "groesse": d["groesse"], "mime": d["mime"],
                 "geaendert": d["geaendert"]}
                for d in inhalt["dateien"]
            ],
        }

    wurzel, pfad, rel = _lokal_innen(db, mandant, element)
    if not pfad.is_dir():
        raise AblageFehler(404, "Ordner nicht gefunden")
    ordner_, dateien = _eintraege(pfad)
    wurzel_name = PurePosixPath(ab["lokal_pfad"]).name or ab["lokal_pfad"]
    teile = [{"element": "", "name": wurzel_name}]
    bisher: list[str] = []
    for t in PurePosixPath(rel).parts if rel else ():
        bisher.append(t)
        teile.append({"element": "/".join(bisher), "name": t})
    praefix = f"{rel}/" if rel else ""
    return {
        "typ": AblageTyp.LOKAL.value,
        "wurzel_name": wurzel_name,
        "element": rel,
        "pfad_teile": teile,
        "unterordner": [
            {"element": praefix + o.name, "name": o.name, "elemente": _anzahl(o.path)} for o in ordner_
        ],
        "dateien": [
            {
                "element": praefix + d.name,
                "name": d.name,
                "groesse": d.stat(follow_symlinks=False).st_size,
                "mime": mimetypes.guess_type(d.name)[0],
                "geaendert": _zeitpunkt(d.stat(follow_symlinks=False).st_mtime),
            }
            for d in dateien
        ],
    }


def mandant_datei(db, mandant, element: str) -> tuple[DateiRef, bytes]:
    """Datei aus dem Mandantenordner lesen („Aus Ablage übernehmen“) – gleiche
    Nachfahren-Prüfung wie beim Durchsuchen; Typ/Größe wie beim Upload geprüft."""
    if not element or not str(element).strip():
        raise AblageFehler(422, "Keine Datei gewählt")
    grenze = _max_bytes()
    ab = mandant_ablage(mandant)
    if ab["typ"] == AblageTyp.SHAREPOINT.value:
        with _graph(db) as gc:
            try:
                drive, wurzel, _ = _sp_wurzel(gc, mandant)
                e = gc.element(drive, str(element).strip())
                _sp_innen(gc, drive, e, wurzel)
                if e.get("ist_ordner"):
                    raise AblageFehler(422, "Das gewählte Element ist ein Ordner")
                if e.get("groesse") and int(e["groesse"]) > grenze:
                    raise AblageFehler(413, f"Die Datei ist zu groß (max. {get_settings().ablage_max_mb} MB)")
                daten = gc.herunterladen(drive, e["id"], max_bytes=grenze)
            except sharepoint.GraphFehler as ex:
                if ex.status == 413:
                    raise AblageFehler(413, f"Die Datei ist zu groß (max. {get_settings().ablage_max_mb} MB)") from ex
                raise _graph_fehler(ex) from ex
        mime = pruefe_upload(daten, e.get("name") or "")
        return DateiRef(
            ablage=AblageTyp.SHAREPOINT.value,
            name=e.get("name") or "datei",
            mime=mime,
            groesse=len(daten),
            pfad=e.get("pfad"),
            drive_id=drive,
            item_id=e["id"],
            web_url=e.get("web_url"),
        ), daten

    _, pfad, rel = _lokal_innen(db, mandant, str(element), anlegen=False)
    if not rel or not pfad.is_file():
        raise AblageFehler(404, "Datei nicht gefunden")
    if pfad.stat().st_size > grenze:
        raise AblageFehler(413, f"Die Datei ist zu groß (max. {get_settings().ablage_max_mb} MB)")
    try:
        daten = pfad.read_bytes()
    except OSError as e:
        raise AblageFehler(502, f"Datei nicht lesbar: {e.strerror or e}") from e
    mime = pruefe_upload(daten, pfad.name)
    return DateiRef(
        ablage=AblageTyp.LOKAL.value,
        name=pfad.name,
        mime=mime,
        groesse=len(daten),
        pfad=pfad.relative_to(stamm()).as_posix(),
    ), daten


# --- Zwischenablage -----------------------------------------------------------


def _jetzt() -> datetime:
    return datetime.now(timezone.utc)


def _zwischen_verzeichnis() -> Path:
    pfad = stamm() / ZWISCHEN_ORDNER
    try:
        pfad.mkdir(mode=0o700, exist_ok=True)
    except OSError as e:
        raise AblageFehler(502, f"Zwischenablage nicht verfügbar: {e.strerror or e}") from e
    if pfad.is_symlink() or not pfad.is_dir():
        raise AblageFehler(502, "Zwischenablage nicht verfügbar")
    return pfad


def _token_pruefen(token) -> str | None:
    if not isinstance(token, str):
        return None
    token = token.strip().lower()
    return token if _TOKEN.match(token) else None


def _exklusiv_schreiben(pfad: Path, daten: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(pfad, flags, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(daten)


def zwischen_speichern(mandant_id: int, benutzer_id: int | None, dateiname: str, daten: bytes, mime: str) -> str:
    """Upload bis zum Speichern der Bestellung zwischenlagern; liefert das Token."""
    ordner = _zwischen_verzeichnis()
    token = uuid.uuid4().hex
    meta = {
        "dateiname": str(dateiname or "datei").replace("\\", "/").rsplit("/", 1)[-1][:255] or "datei",
        "mime": mime,
        "groesse": len(daten),
        "mandant_id": int(mandant_id),
        "benutzer_id": benutzer_id,
        "zeit": _jetzt().isoformat(),
    }
    try:
        _exklusiv_schreiben(ordner / f"{token}.bin", daten)
        _exklusiv_schreiben(ordner / f"{token}.json", json.dumps(meta, ensure_ascii=False).encode("utf-8"))
    except OSError as e:
        (ordner / f"{token}.bin").unlink(missing_ok=True)
        raise AblageFehler(502, f"Zwischenablage nicht beschreibbar: {e.strerror or e}") from e
    return token


def _meta_lesen(pfad: Path) -> dict | None:
    try:
        meta = json.loads(pfad.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _abgelaufen(meta: dict, max_alter_stunden: float, jetzt: datetime) -> bool:
    try:
        zeit = datetime.fromisoformat(str(meta.get("zeit")))
    except ValueError:
        return True
    if zeit.tzinfo is None:
        zeit = zeit.replace(tzinfo=timezone.utc)
    return jetzt - zeit > timedelta(hours=max_alter_stunden)


def zwischen_lesen(token: str, mandant_id: int) -> tuple[dict, bytes]:
    """Zwischengespeicherte Datei lesen; fremd, unbekannt oder abgelaufen → 404."""
    nicht_gefunden = AblageFehler(404, "Die hochgeladene Datei ist nicht mehr vorhanden – bitte erneut hochladen")
    t = _token_pruefen(token)
    if t is None:
        raise nicht_gefunden
    ordner = _zwischen_verzeichnis()
    meta = _meta_lesen(ordner / f"{t}.json")
    if meta is None or meta.get("mandant_id") != mandant_id:
        raise nicht_gefunden
    if _abgelaufen(meta, ZWISCHEN_MAX_STUNDEN, _jetzt()):
        zwischen_entfernen(t)
        raise nicht_gefunden
    try:
        daten = (ordner / f"{t}.bin").read_bytes()
    except OSError:
        raise nicht_gefunden from None
    return meta, daten


def zwischen_entfernen(token: str) -> None:
    t = _token_pruefen(token)
    if t is None:
        return
    ordner = _zwischen_verzeichnis()
    for endung in (".bin", ".json"):
        try:
            (ordner / f"{t}{endung}").unlink(missing_ok=True)
        except OSError:
            pass


def zwischen_aufraeumen(max_alter_stunden: int = ZWISCHEN_MAX_STUNDEN) -> int:
    """Abgelaufene Zwischendateien löschen; liefert die Anzahl entfernter Uploads."""
    ordner = _zwischen_verzeichnis()
    jetzt = _jetzt()
    grenze = jetzt - timedelta(hours=max_alter_stunden)
    entfernt = 0
    tokens: dict[str, set[str]] = {}
    for p in ordner.iterdir():
        t = _token_pruefen(p.stem)
        if t is not None and p.suffix in (".bin", ".json"):
            tokens.setdefault(t, set()).add(p.suffix)
    for t, endungen in tokens.items():
        if ".json" in endungen:
            meta = _meta_lesen(ordner / f"{t}.json")
            alt = meta is None or _abgelaufen(meta, max_alter_stunden, jetzt)
        else:  # verwaiste Datei ohne Metadaten: nach Änderungszeit
            try:
                alt = datetime.fromtimestamp((ordner / f"{t}.bin").stat().st_mtime, timezone.utc) < grenze
            except OSError:
                alt = False
        if alt:
            zwischen_entfernen(t)
            entfernt += 1
    return entfernt


# --- Upload-Prüfung -----------------------------------------------------------


def _ist_text(daten: bytes) -> bool:
    if b"\x00" in daten:
        return False
    for kodierung in ("utf-8-sig", "cp1252"):
        try:
            daten.decode(kodierung)
            return True
        except UnicodeDecodeError:
            continue
    return False


def dateityp_erkennen(daten: bytes, dateiname: str) -> str | None:
    """Dateityp nach Inhalt (Magic Bytes); TXT/EML nach Endung + dekodierbar."""
    if not daten:
        return None
    if b"%PDF-" in daten[:1024]:
        return "application/pdf"
    if daten.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if daten.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if daten[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if daten[:4] == b"RIFF" and daten[8:12] == b"WEBP":
        return "image/webp"
    if daten[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(daten)) as z:
                namen = z.namelist()
        except (zipfile.BadZipFile, ValueError, OSError):
            return None
        if "[Content_Types].xml" not in namen:
            return None
        if any(n.startswith("word/") for n in namen):
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if any(n.startswith("xl/") for n in namen):
            return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return None
    endung = PurePosixPath(str(dateiname or "").replace("\\", "/")).suffix.lower()
    if endung in (".txt", ".eml") and _ist_text(daten):
        return "text/plain" if endung == ".txt" else "message/rfc822"
    return None


def pruefe_upload(daten: bytes, dateiname: str) -> str:
    """Upload prüfen; liefert den erkannten MIME-Typ.
    Leer → 422, zu groß → 413, unbekannter Typ → 422."""
    if not daten:
        raise AblageFehler(422, "Die Datei ist leer")
    max_mb = int(get_settings().ablage_max_mb)
    if len(daten) > max_mb * 1024 * 1024:
        raise AblageFehler(413, f"Die Datei ist zu groß (max. {max_mb} MB)")
    mime = dateityp_erkennen(daten, dateiname)
    if mime is None:
        raise AblageFehler(
            422,
            "Dateityp nicht unterstützt – erlaubt sind PDF, Bilder (PNG, JPEG, TIFF, WebP), "
            "Word (DOCX), Excel (XLSX), E-Mail (EML) und Text (TXT)",
        )
    return mime


# --- Antwort-Header für den Datei-Proxy ---------------------------------------


def inline_erlaubt(mime: str) -> bool:
    return (mime or "").split(";")[0].strip().lower() in INLINE_MIME


def content_disposition(dateiname: str, art: str = "inline") -> str:
    """Content-Disposition mit ASCII-Ersatz und UTF-8-Namen (RFC 6266/5987)."""
    art = art if art in ("inline", "attachment") else "attachment"
    name = re.sub(r'[\x00-\x1f\x7f"\\/]', "_", str(dateiname or "")).strip() or "datei"
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    ascii_ = re.sub(r"[%;]", "_", ascii_).strip()
    if not ascii_ or ascii_.startswith("."):
        ascii_ = f"datei{ascii_}"
    return f"{art}; filename=\"{ascii_}\"; filename*=UTF-8''{quote(name, safe='')}"


def datei_header(dateiname: str, mime: str) -> dict:
    """Header für ausgelieferte Belegdateien: inline nur für PDF/Bilder, sonst
    Download; kein MIME-Sniffing, kein Caching, CSP-Sandbox außer für PDF
    (Chromes PDF-Viewer verträgt keine Sandbox)."""
    typ = (mime or "").split(";")[0].strip().lower()
    sicher = typ if typ in ERLAUBTE_ENDUNGEN else "application/octet-stream"
    kopf = {
        "Content-Type": sicher,
        "Content-Disposition": content_disposition(dateiname, "inline" if inline_erlaubt(sicher) else "attachment"),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    }
    if sicher != "application/pdf":
        kopf["Content-Security-Policy"] = "sandbox"
    return kopf
