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
    request.session["benutzer_id"] = benutzer.id
    audit(db, benutzer, None, "LOGIN")
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
