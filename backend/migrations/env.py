"""Alembic-Umgebung.

Die Datenbank-URL kommt aus der App-Konfiguration (DATABASE_URL). Läuft die
Migration programmatisch aus der Anwendung (app.main.init_db), wird die dort
geöffnete Verbindung über config.attributes["connection"] mitgenutzt, damit
Tests und App denselben Engine verwenden.
"""

import sys
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: F401  (Modelle an der Metadata registrieren)
from app.config import get_settings
from app.database import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _laufe(verbindung) -> None:
    context.configure(
        connection=verbindung,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite-kompatible ALTERs
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    verbindung = config.attributes.get("connection")
    if verbindung is not None:
        _laufe(verbindung)
        return
    engine = create_engine(get_settings().database_url, poolclass=pool.NullPool)
    with engine.connect() as verbindung:
        _laufe(verbindung)
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
