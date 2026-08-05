import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .database import get_db

_ITERATIONEN = 240_000


def hash_passwort(passwort: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", passwort.encode(), bytes.fromhex(salt), _ITERATIONEN
    ).hex()
    return f"pbkdf2${_ITERATIONEN}${salt}${digest}"


def pruefe_passwort(passwort: str, gespeichert: str) -> bool:
    try:
        _, iterationen, salt, digest = gespeichert.split("$")
        neu = hashlib.pbkdf2_hmac(
            "sha256", passwort.encode(), bytes.fromhex(salt), int(iterationen)
        ).hex()
        return hmac.compare_digest(neu, digest)
    except (ValueError, TypeError):
        return False


def aktueller_benutzer(request: Request, db: Session = Depends(get_db)) -> models.Benutzer:
    benutzer_id = request.session.get("benutzer_id")
    if benutzer_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Nicht angemeldet")
    benutzer = db.get(models.Benutzer, benutzer_id)
    if benutzer is None or not benutzer.aktiv:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Nicht angemeldet")
    return benutzer


def ist_admin(benutzer: models.Benutzer) -> bool:
    return benutzer.rolle == models.Rolle.ADMIN.value


def nur_admin(benutzer: models.Benutzer = Depends(aktueller_benutzer)) -> models.Benutzer:
    if not ist_admin(benutzer):
        raise HTTPException(status_code=403, detail="Nur für Administratoren")
    return benutzer


def darf_mandant(benutzer: models.Benutzer, mandant_id: int) -> bool:
    if ist_admin(benutzer):
        return True
    return any(m.id == mandant_id for m in benutzer.mandanten)


def mandant_oder_403(
    db: Session, benutzer: models.Benutzer, mandant_id: int
) -> models.Mandant:
    mandant = db.get(models.Mandant, mandant_id)
    if mandant is None:
        raise HTTPException(status_code=404, detail="Mandant nicht gefunden")
    if not darf_mandant(benutzer, mandant_id):
        raise HTTPException(status_code=403, detail="Kein Zugriff auf diesen Mandanten")
    return mandant


def nur_schreibend(benutzer: models.Benutzer) -> None:
    if benutzer.rolle == models.Rolle.LESER.value:
        raise HTTPException(status_code=403, detail="Nur-Lese-Berechtigung")


def audit(
    db: Session,
    benutzer: models.Benutzer | None,
    mandant_id: int | None,
    aktion: str,
    detail: str | None = None,
) -> None:
    db.add(
        models.AuditLog(
            benutzer_id=benutzer.id if benutzer else None,
            mandant_id=mandant_id,
            aktion=aktion,
            detail=detail,
        )
    )


def sichere_mandanten_liste(db: Session, benutzer: models.Benutzer) -> list[models.Mandant]:
    if ist_admin(benutzer):
        return list(
            db.scalars(select(models.Mandant).order_by(models.Mandant.name))
        )
    return sorted(benutzer.mandanten, key=lambda m: m.name)
