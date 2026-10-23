from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..security import (
    aktueller_benutzer,
    ist_admin,
    mandant_oder_403,
    sichere_mandanten_liste,
)

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Cache-Busting: Versionsstempel aus der neuesten Änderungszeit der statischen
# Dateien (beim Start berechnet). Browser laden CSS/JS nach jedem Update neu,
# statt mit veralteten Stylesheets zu arbeiten.
_static_verzeichnis = Path(__file__).resolve().parent.parent / "static"
templates.env.globals["static_version"] = str(
    max(
        (int(p.stat().st_mtime) for p in _static_verzeichnis.glob("*") if p.is_file()),
        default=0,
    )
)

router = APIRouter()


def _benutzer_oder_login(request: Request, db: Session) -> models.Benutzer | None:
    benutzer_id = request.session.get("benutzer_id")
    if benutzer_id is None:
        return None
    benutzer = db.get(models.Benutzer, benutzer_id)
    if benutzer is None or not benutzer.aktiv:
        request.session.clear()
        return None
    return benutzer


@router.get("/")
def startseite(request: Request, db: Session = Depends(get_db)):
    benutzer = _benutzer_oder_login(request, db)
    if benutzer is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "mandanten.html",
        {
            "benutzer": benutzer,
            "ist_admin": ist_admin(benutzer),
            "mandanten": sichere_mandanten_liste(db, benutzer),
        },
    )


def _mandanten_seite(
    request: Request, db: Session, mandant_id: int, vorlage: str, extra: dict | None = None
):
    benutzer = _benutzer_oder_login(request, db)
    if benutzer is None:
        return RedirectResponse("/login", status_code=303)
    mandant = mandant_oder_403(db, benutzer, mandant_id)
    kontext = {
        "benutzer": benutzer,
        "ist_admin": ist_admin(benutzer),
        "leser": benutzer.rolle == models.Rolle.LESER.value,
        "mandant": mandant,
    }
    if extra:
        kontext.update(extra)
    return templates.TemplateResponse(request, vorlage, kontext)


@router.get("/mandanten/{mandant_id}/plan")
def plan_seite(request: Request, mandant_id: int, db: Session = Depends(get_db)):
    return _mandanten_seite(request, db, mandant_id, "plan.html")


@router.get("/mandanten/{mandant_id}/konten")
def konten_seite(request: Request, mandant_id: int, db: Session = Depends(get_db)):
    return _mandanten_seite(request, db, mandant_id, "konten.html")


@router.get("/mandanten/{mandant_id}/import")
def import_seite(request: Request, mandant_id: int, db: Session = Depends(get_db)):
    return _mandanten_seite(request, db, mandant_id, "import.html")


@router.get("/mandanten/{mandant_id}/posten")
def posten_seite(request: Request, mandant_id: int, db: Session = Depends(get_db)):
    return _mandanten_seite(request, db, mandant_id, "posten.html")


@router.get("/mandanten/{mandant_id}/budget")
def budget_seite(request: Request, mandant_id: int, db: Session = Depends(get_db)):
    return _mandanten_seite(request, db, mandant_id, "budget.html")


@router.get("/mandanten/{mandant_id}/kalender")
def kalender_seite(request: Request, mandant_id: int, db: Session = Depends(get_db)):
    return _mandanten_seite(request, db, mandant_id, "kalender.html")


@router.get("/mandanten/{mandant_id}/kpi")
def kpi_seite(request: Request, mandant_id: int, db: Session = Depends(get_db)):
    return _mandanten_seite(request, db, mandant_id, "kpi.html")


@router.get("/mandanten/{mandant_id}/sollist")
def sollist_seite(request: Request, mandant_id: int, db: Session = Depends(get_db)):
    return _mandanten_seite(request, db, mandant_id, "sollist.html")


@router.get("/mandanten/{mandant_id}/szenarien")
def szenarien_seite(request: Request, mandant_id: int, db: Session = Depends(get_db)):
    return _mandanten_seite(request, db, mandant_id, "szenvergleich.html")


@router.get("/mandanten/{mandant_id}/einstellungen")
def einstellungen_seite(request: Request, mandant_id: int, db: Session = Depends(get_db)):
    from ..services.feiertage import BUNDESLAENDER

    return _mandanten_seite(
        request, db, mandant_id, "einstellungen.html", {"bundeslaender": BUNDESLAENDER}
    )


@router.get("/profil")
def profil_seite(request: Request, db: Session = Depends(get_db)):
    benutzer = _benutzer_oder_login(request, db)
    if benutzer is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request, "profil.html",
        {"benutzer": benutzer, "ist_admin": ist_admin(benutzer)},
    )


@router.get("/mandanten-wizard")
def mandanten_wizard_seite(request: Request, db: Session = Depends(get_db)):
    benutzer = _benutzer_oder_login(request, db)
    if benutzer is None:
        return RedirectResponse("/login", status_code=303)
    if not ist_admin(benutzer):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "wizard.html", {"benutzer": benutzer, "ist_admin": True}
    )


@router.get("/kontenrahmen")
def kontenrahmen_seite(request: Request, db: Session = Depends(get_db)):
    benutzer = _benutzer_oder_login(request, db)
    if benutzer is None:
        return RedirectResponse("/login", status_code=303)
    if not ist_admin(benutzer):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "kontenrahmen.html", {"benutzer": benutzer, "ist_admin": True}
    )


@router.get("/benutzer")
def benutzer_seite(request: Request, db: Session = Depends(get_db)):
    benutzer = _benutzer_oder_login(request, db)
    if benutzer is None:
        return RedirectResponse("/login", status_code=303)
    if not ist_admin(benutzer):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "benutzer.html",
        {
            "benutzer": benutzer,
            "ist_admin": True,
            "mandanten": sichere_mandanten_liste(db, benutzer),
        },
    )
