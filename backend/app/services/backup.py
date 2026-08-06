"""Integrierte Sicherungen.

Anwendungsseitige, datenbankunabhängige Voll-Sicherung (PostgreSQL und SQLite):
alle Tabellen als JSON in einem Zip-Archiv mit Manifest (Zeitstempel,
Alembic-Revision, Zeilenzahlen). Die Wiederherstellung ersetzt den kompletten
Datenbestand und verlangt eine passende Schema-Revision – Sicherungen sind damit
an die Anwendungsversion gebunden, die sie erstellt hat (bzw. eine mit gleichem
Schemastand). Ergänzend bleibt pg_dump auf Systemebene empfohlen.
"""

import json
import re
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import select, text

from .. import database
from ..database import Base

NAME_MUSTER = re.compile(r"^liqui-backup-\d{8}-\d{6}(-\d+)?\.zip$")
FORMAT_VERSION = 1


def aktuelle_revision() -> str | None:
    from alembic.migration import MigrationContext

    with database.engine.connect() as verbindung:
        return MigrationContext.configure(verbindung).get_current_revision()


def _serialisiere(wert):
    if isinstance(wert, Decimal):
        return str(wert)
    if isinstance(wert, (date, datetime)):
        return wert.isoformat()
    return wert


def _lade_wert(spalte: sa.Column, wert):
    if wert is None:
        return None
    if isinstance(spalte.type, sa.Numeric):
        return Decimal(str(wert))
    if isinstance(spalte.type, sa.DateTime):
        return datetime.fromisoformat(str(wert))
    if isinstance(spalte.type, sa.Date):
        return date.fromisoformat(str(wert))
    if isinstance(spalte.type, sa.Boolean):
        return bool(wert)
    return wert


def erstelle_backup(verzeichnis: str | Path) -> Path:
    """Schreibt eine konsistente Voll-Sicherung (eine Transaktion) als Zip."""
    ziel = Path(verzeichnis)
    ziel.mkdir(parents=True, exist_ok=True)
    jetzt = datetime.now(timezone.utc)
    basis = f"liqui-backup-{jetzt.strftime('%Y%m%d-%H%M%S')}"
    pfad = ziel / f"{basis}.zip"
    laufnummer = 0
    while pfad.exists():
        laufnummer += 1
        pfad = ziel / f"{basis}-{laufnummer}.zip"

    zeilenzahlen: dict[str, int] = {}
    with database.engine.begin() as verbindung, zipfile.ZipFile(
        pfad, "w", compression=zipfile.ZIP_DEFLATED
    ) as archiv:
        for tabelle in Base.metadata.sorted_tables:
            zeilen = [
                {k: _serialisiere(v) for k, v in zeile.items()}
                for zeile in verbindung.execute(select(tabelle)).mappings()
            ]
            zeilenzahlen[tabelle.name] = len(zeilen)
            archiv.writestr(
                f"tabellen/{tabelle.name}.json",
                json.dumps(zeilen, ensure_ascii=False),
            )
        archiv.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format": FORMAT_VERSION,
                    "erstellt_am": jetzt.isoformat(),
                    "revision": aktuelle_revision(),
                    "tabellen": zeilenzahlen,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return pfad


def liste(verzeichnis: str | Path) -> list[dict]:
    ziel = Path(verzeichnis)
    if not ziel.is_dir():
        return []
    eintraege = []
    for pfad in sorted(ziel.iterdir(), reverse=True):
        if not NAME_MUSTER.match(pfad.name):
            continue
        eintraege.append(
            {
                "name": pfad.name,
                "groesse": pfad.stat().st_size,
                "geaendert_am": datetime.fromtimestamp(
                    pfad.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
            }
        )
    return eintraege


def raeume_auf(verzeichnis: str | Path, aufbewahrung_tage: int) -> int:
    """Löscht Sicherungen, deren Zeitstempel (aus dem Dateinamen) älter ist."""
    if aufbewahrung_tage <= 0:
        return 0
    ziel = Path(verzeichnis)
    if not ziel.is_dir():
        return 0
    grenze = datetime.now(timezone.utc)
    geloescht = 0
    for pfad in ziel.iterdir():
        m = NAME_MUSTER.match(pfad.name)
        if not m:
            continue
        try:
            stempel = datetime.strptime(
                pfad.name[len("liqui-backup-"):len("liqui-backup-") + 15],
                "%Y%m%d-%H%M%S",
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if (grenze - stempel).days > aufbewahrung_tage:
            pfad.unlink()
            geloescht += 1
    return geloescht


def stelle_wieder_her(daten: bytes) -> dict:
    """Ersetzt den kompletten Datenbestand durch die Sicherung.

    Verlangt eine identische Alembic-Revision; setzt auf PostgreSQL anschließend
    die ID-Sequenzen zurück.
    """
    try:
        archiv = zipfile.ZipFile(BytesIO(daten))
        manifest = json.loads(archiv.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as e:
        raise ValueError(f"Keine gültige Sicherungsdatei: {e}")
    if manifest.get("format") != FORMAT_VERSION:
        raise ValueError("Unbekanntes Sicherungsformat")
    revision = aktuelle_revision()
    if manifest.get("revision") != revision:
        raise ValueError(
            f"Sicherung stammt von Schema-Revision {manifest.get('revision')}, "
            f"aktuell ist {revision} – bitte eine Anwendungsversion mit passendem "
            "Schemastand verwenden"
        )
    namen = set(archiv.namelist())
    zeilenzahlen: dict[str, int] = {}
    with database.engine.begin() as verbindung:
        for tabelle in reversed(Base.metadata.sorted_tables):
            verbindung.execute(tabelle.delete())
        for tabelle in Base.metadata.sorted_tables:
            eintrag = f"tabellen/{tabelle.name}.json"
            if eintrag not in namen:
                continue
            zeilen = json.loads(archiv.read(eintrag))
            zeilenzahlen[tabelle.name] = len(zeilen)
            if not zeilen:
                continue
            konvertiert = [
                {
                    k: _lade_wert(tabelle.columns[k], v)
                    for k, v in zeile.items()
                    if k in tabelle.columns
                }
                for zeile in zeilen
            ]
            verbindung.execute(tabelle.insert(), konvertiert)
        if database.engine.dialect.name == "postgresql":
            for tabelle in Base.metadata.sorted_tables:
                if "id" in tabelle.columns:
                    verbindung.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('{tabelle.name}', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM {tabelle.name}), 1))"
                    ))
    return {"tabellen": zeilenzahlen, "erstellt_am": manifest.get("erstellt_am")}
