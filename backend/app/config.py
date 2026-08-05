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


@lru_cache
def get_settings() -> Settings:
    return Settings()
