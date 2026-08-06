from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..security import audit, pruefe_passwort
from .pages import templates

router = APIRouter()


@router.get("/login")
def login_seite(request: Request):
    if request.session.get("benutzer_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"fehler": None})


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    passwort: str = Form(...),
    db: Session = Depends(get_db),
):
    benutzer = db.scalar(
        select(models.Benutzer).where(models.Benutzer.email == email.strip().lower())
    )
    if benutzer is None or not benutzer.aktiv or not pruefe_passwort(
        passwort, benutzer.passwort_hash
    ):
        return templates.TemplateResponse(
            request, "login.html",
            {"fehler": "E-Mail oder Passwort falsch."},
            status_code=401,
        )
    request.session.clear()
    if benutzer.totp_aktiv and benutzer.totp_geheimnis:
        # Passwort korrekt, aber noch keine Sitzung: zweiter Faktor erforderlich
        request.session["zwei_faktor_benutzer_id"] = benutzer.id
        return RedirectResponse("/login/2fa", status_code=303)
    request.session["benutzer_id"] = benutzer.id
    audit(db, benutzer, None, "LOGIN")
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/login/2fa")
def zwei_faktor_seite(request: Request):
    if not request.session.get("zwei_faktor_benutzer_id"):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "login2fa.html", {"fehler": None})


@router.post("/login/2fa")
def zwei_faktor_pruefen(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    from ..services import totp

    benutzer_id = request.session.get("zwei_faktor_benutzer_id")
    benutzer = db.get(models.Benutzer, benutzer_id) if benutzer_id else None
    if benutzer is None or not benutzer.aktiv or not benutzer.totp_geheimnis:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
    schritt = totp.pruefe_code(
        benutzer.totp_geheimnis, code, benutzer.totp_letzter_schritt or 0
    )
    if schritt is None:
        audit(db, benutzer, None, "LOGIN_2FA_FEHLGESCHLAGEN")
        db.commit()
        return templates.TemplateResponse(
            request, "login2fa.html",
            {"fehler": "Code ungültig oder bereits verwendet."},
            status_code=401,
        )
    benutzer.totp_letzter_schritt = schritt
    request.session.clear()
    request.session["benutzer_id"] = benutzer.id
    audit(db, benutzer, None, "LOGIN")
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
