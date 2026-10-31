import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("DEMO_DATEN", "false")
os.environ.setdefault("SECRET_KEY", "test-schluessel")
os.environ.setdefault("BACKUP_INTERVALL_STUNDEN", "0")  # kein Zeitplaner in Tests

# Optional: Tests gegen PostgreSQL statt SQLite, z. B.
# TEST_DATABASE_URL=postgresql+psycopg://liqui_test:liqui_test@localhost:5432/liqui_test
# Achtung: das Schema "public" dieser Datenbank wird je Test neu angelegt.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base


def _leere_postgres_engine():
    """Engine auf TEST_DATABASE_URL mit frischem, leerem Schema public."""
    # lock_timeout: ein Test, der auf eine eigene Zeilensperre wartet, schlägt fehl statt zu hängen
    engine = create_engine(TEST_DATABASE_URL, connect_args={"options": "-c lock_timeout=30s"})
    with engine.begin() as verbindung:
        # liegengebliebene Verbindungen früherer Tests würden DROP SCHEMA blockieren
        verbindung.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND usename = current_user AND backend_type = 'client backend'"
        ))
        verbindung.execute(text("SET LOCAL lock_timeout = '10s'"))
        verbindung.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        verbindung.execute(text("CREATE SCHEMA public"))
    return engine


def leere_engine(sqlite_url: str):
    """Leere Test-Datenbank: PostgreSQL (falls TEST_DATABASE_URL) sonst SQLite."""
    if TEST_DATABASE_URL:
        return _leere_postgres_engine()
    return create_engine(sqlite_url, connect_args={"check_same_thread": False})


@pytest.fixture()
def leere_datenbank(tmp_path):
    """Leere Datenbank ohne Tabellen (z. B. für Migrationstests)."""
    engine = leere_engine(f"sqlite:///{tmp_path}/leer.db")
    yield engine
    engine.dispose()


@pytest.fixture()
def db():
    engine = leere_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app.database as db_modul
    import app.main as hauptmodul

    engine = leere_engine(f"sqlite:///{tmp_path}/test.db")
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_modul, "engine", engine)
    monkeypatch.setattr(db_modul, "SessionLocal", Session)
    monkeypatch.setattr(hauptmodul, "engine", engine)
    monkeypatch.setattr(hauptmodul, "SessionLocal", Session)
    Base.metadata.create_all(engine)

    with TestClient(hauptmodul.app) as c:
        yield c
    engine.dispose()
