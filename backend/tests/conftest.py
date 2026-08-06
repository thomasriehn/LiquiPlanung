import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("DEMO_DATEN", "false")
os.environ.setdefault("SECRET_KEY", "test-schluessel")
os.environ.setdefault("BACKUP_INTERVALL_STUNDEN", "0")  # kein Zeitplaner in Tests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
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

    engine = create_engine(
        f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False}
    )
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_modul, "engine", engine)
    monkeypatch.setattr(db_modul, "SessionLocal", Session)
    monkeypatch.setattr(hauptmodul, "engine", engine)
    monkeypatch.setattr(hauptmodul, "SessionLocal", Session)
    Base.metadata.create_all(engine)

    with TestClient(hauptmodul.app) as c:
        yield c
    engine.dispose()
