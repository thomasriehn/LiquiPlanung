from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.database import Base

BASIS = Path(__file__).resolve().parent.parent


def _cfg() -> Config:
    cfg = Config(str(BASIS / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASIS / "migrations"))
    return cfg


def test_migrationen_erzeugen_das_aktuelle_schema(tmp_path):
    """`alembic upgrade head` auf leerer DB muss exakt die Modelle abbilden.

    Schlägt dieser Test fehl, wurde ein Modell geändert, ohne eine Migration zu
    erzeugen (backend: `alembic revision --autogenerate -m "..."`).
    """
    engine = create_engine(f"sqlite:///{tmp_path}/migration.db")
    cfg = _cfg()
    with engine.begin() as verbindung:
        cfg.attributes["connection"] = verbindung
        command.upgrade(cfg, "head")

    inspector = inspect(engine)
    tabellen = set(inspector.get_table_names())
    erwartet = {t.name for t in Base.metadata.sorted_tables}
    assert erwartet <= tabellen
    assert "alembic_version" in tabellen

    with engine.connect() as verbindung:
        kontext = MigrationContext.configure(verbindung)
        differenzen = compare_metadata(kontext, Base.metadata)
    assert differenzen == [], (
        "Modelle und Migrationen weichen ab - bitte eine neue Alembic-Revision "
        f"erzeugen: {differenzen}"
    )
    engine.dispose()


def test_uebernahme_bestandsdatenbank(tmp_path, monkeypatch):
    """Alt-Installation (create_all, ohne alembic_version) wird beim Start
    gestempelt; ein zweiter Start läuft als normales Upgrade durch."""
    import app.main as hauptmodul
    import app.database as db_modul
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path}/bestand.db")
    Base.metadata.create_all(engine)  # simulierter Alt-Stand
    monkeypatch.setattr(db_modul, "engine", engine)
    monkeypatch.setattr(hauptmodul, "engine", engine)
    monkeypatch.setattr(
        hauptmodul, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False)
    )

    hauptmodul.migriere_datenbank()
    assert inspect(engine).has_table("alembic_version")
    hauptmodul.migriere_datenbank()  # idempotent (Upgrade ohne offene Revisionen)
    engine.dispose()
