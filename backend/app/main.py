from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from . import models
from .config import get_settings
from .database import Base, SessionLocal, engine
from .routers import api, auth, pages
from .security import hash_passwort


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    settings = get_settings()
    with SessionLocal() as db:
        if db.scalar(select(models.Benutzer.id).limit(1)) is None:
            db.add(
                models.Benutzer(
                    email=settings.admin_email.lower(),
                    name="Administrator",
                    passwort_hash=hash_passwort(settings.admin_password),
                    rolle=models.Rolle.ADMIN.value,
                )
            )
            db.commit()
            print(
                f"[LiquiPlanung] Admin angelegt: {settings.admin_email} "
                "(Passwort aus ADMIN_PASSWORD – bitte nach dem ersten Login ändern)"
            )
        if settings.demo_daten:
            from .services.demo import lege_demo_mandant_an

            if lege_demo_mandant_an(db):
                print("[LiquiPlanung] Demo-Mandant 'Muster GmbH (Demo)' angelegt.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        max_age=settings.session_max_age,
        same_site="lax",
    )

    @app.exception_handler(401)
    async def nicht_angemeldet(request: Request, exc):
        if request.url.path.startswith("/api"):
            return JSONResponse({"detail": "Nicht angemeldet"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(api.router)
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
        name="static",
    )

    @app.get("/gesundheit")
    def gesundheit():
        return {"status": "ok"}

    return app


app = create_app()
