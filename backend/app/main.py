import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, select, text
from starlette.middleware.sessions import SessionMiddleware

from . import models
from .config import get_settings
from .database import Base, SessionLocal, engine
from .routers import api, auth, pages
from .security import hash_passwort


def ensure_schema() -> None:
    """Ergänzt fehlende Spalten bestehender Tabellen (additiv).

    Wird nur für die Übernahme von Alt-Installationen benötigt, die vor der
    Alembic-Einführung per `create_all` entstanden sind: Ihr Schema wird auf den
    aktuellen Stand gebracht und anschließend als Baseline gestempelt; danach
    laufen Schemaänderungen ausschließlich über Alembic-Migrationen.
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        for tabelle in Base.metadata.sorted_tables:
            if not inspector.has_table(tabelle.name):
                continue
            vorhanden = {c["name"] for c in inspector.get_columns(tabelle.name)}
            for spalte in tabelle.columns:
                if spalte.name in vorhanden:
                    continue
                typ = spalte.type.compile(engine.dialect)
                conn.execute(
                    text(f'ALTER TABLE {tabelle.name} ADD COLUMN {spalte.name} {typ}')
                )
                if spalte.default is not None and getattr(spalte.default, "is_scalar", False):
                    conn.execute(
                        tabelle.update()
                        .where(spalte.is_(None))
                        .values({spalte: spalte.default.arg})
                    )
                print(f"[LiquiPlanung] Schema ergänzt: {tabelle.name}.{spalte.name}")


def _alembic_config():
    from alembic.config import Config

    basis = Path(__file__).resolve().parent.parent  # backend/
    cfg = Config(str(basis / "alembic.ini"))
    cfg.set_main_option("script_location", str(basis / "migrations"))
    return cfg


def migriere_datenbank() -> None:
    """Bringt das Schema per Alembic auf den aktuellen Stand.

    Bestandsinstallationen aus der Zeit vor Alembic (Tabellen vorhanden, aber
    keine alembic_version) werden übernommen: Schema additiv angleichen, dann
    als Baseline stempeln. Alles Weitere läuft über `alembic upgrade head`.
    """
    from alembic import command

    inspector = inspect(engine)
    hat_version = inspector.has_table("alembic_version")
    hat_tabellen = inspector.has_table("mandanten")
    cfg = _alembic_config()
    if not hat_version and hat_tabellen:
        Base.metadata.create_all(bind=engine)  # seither hinzugekommene Tabellen
        ensure_schema()                        # seither hinzugekommene Spalten
        with engine.begin() as verbindung:
            cfg.attributes["connection"] = verbindung
            command.stamp(cfg, "head")
        print("[LiquiPlanung] Bestandsdatenbank übernommen und als Baseline gestempelt.")
    else:
        with engine.begin() as verbindung:
            cfg.attributes["connection"] = verbindung
            command.upgrade(cfg, "head")


def init_db() -> None:
    migriere_datenbank()
    settings = get_settings()
    if settings.secret_key == "bitte-aendern-unsicherer-entwicklungsschluessel":
        print(
            "[LiquiPlanung] WARNUNG: SECRET_KEY ist der unsichere Entwicklungswert – "
            "für den Betrieb zwingend in .env setzen (openssl rand -hex 32)."
        )
    if settings.admin_password == "admin":
        print(
            "[LiquiPlanung] WARNUNG: ADMIN_PASSWORD steht auf dem Standardwert 'admin' – "
            "bitte in .env ändern."
        )
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


async def _backup_schleife() -> None:
    """Regelmäßige Sicherung (erste direkt beim Start, dann im Intervall)."""
    from .services import backup

    settings = get_settings()
    while True:
        try:
            pfad = await asyncio.to_thread(
                backup.erstelle_backup, settings.backup_verzeichnis
            )
            geloescht = await asyncio.to_thread(
                backup.raeume_auf,
                settings.backup_verzeichnis,
                settings.backup_aufbewahrung_tage,
            )
            meldung = f"[LiquiPlanung] Sicherung erstellt: {pfad.name}"
            if geloescht:
                meldung += f" ({geloescht} alte Sicherung(en) entfernt)"
            print(meldung)
        except Exception as e:  # Sicherung darf den Betrieb nie stoppen
            print(f"[LiquiPlanung] Sicherung fehlgeschlagen: {e}")
        await asyncio.sleep(get_settings().backup_intervall_stunden * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    backup_task = None
    if get_settings().backup_intervall_stunden > 0:
        backup_task = asyncio.create_task(_backup_schleife())
    yield
    if backup_task is not None:
        backup_task.cancel()
        with suppress(asyncio.CancelledError):
            await backup_task


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
