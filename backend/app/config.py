from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "LiquiPlanung"
    database_url: str = "postgresql+psycopg://liqui:liqui@localhost:5432/liqui"
    secret_key: str = "bitte-aendern-unsicherer-entwicklungsschluessel"
    session_max_age: int = 60 * 60 * 12

    # Initialer Admin-Benutzer (wird nur angelegt, wenn noch kein Benutzer existiert)
    admin_email: str = "admin@example.com"
    admin_password: str = "admin"

    # Legt beim Start einen Beispielmandanten mit Daten an
    demo_daten: bool = True

    # Integrierte Sicherungen: Zielverzeichnis, Intervall (0 = aus), Aufbewahrung
    backup_verzeichnis: str = "backups"
    backup_intervall_stunden: int = 24
    backup_aufbewahrung_tage: int = 30

    # Lokale Belegablage (Bestelldateien): Stammverzeichnis; je Mandant ein
    # Unterordner (Standard: Kurzname). Maximale Dateigröße eines Uploads.
    ablage_verzeichnis: str = "ablage"
    ablage_max_mb: int = 25

    # Schlüssel für gespeicherte Geheimnisse (Graph-Secret, KI-API-Key);
    # ohne Angabe wird er aus SECRET_KEY abgeleitet.
    geheimnis_schluessel: str | None = None
    # Optional: Geheimnisse nur in der Umgebung (.env) statt in der Datenbank;
    # gesetzt haben sie Vorrang vor den in der Oberfläche hinterlegten Werten.
    graph_client_secret: str | None = None
    llm_api_key: str | None = None
    graph_timeout_sekunden: int = 30
    llm_timeout_sekunden: int = 120


@lru_cache
def get_settings() -> Settings:
    return Settings()
