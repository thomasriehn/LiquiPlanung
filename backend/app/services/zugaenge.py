"""Gemeinsame Datentypen für externe Zugänge (Microsoft Graph, KI) und
Dateireferenzen der Belegablage.

Bewusst ohne Abhängigkeiten, damit Einstellungen, Graph-Client, KI-Client,
Ablage und Bestellungen sie gleichermaßen verwenden können.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GraphZugang:
    """App-only-Zugang zu Microsoft Graph (Client-Credentials-Flow)."""

    tenant_id: str
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class KiZugang:
    """Zugang zu einem Sprachmodell mit OpenAI-kompatibler Schnittstelle."""

    url: str                  # Basis-URL, z. B. https://api.openai.com/v1
    modell: str               # z. B. gpt-4o-mini
    api_key: str | None       # lokale Server (Ollama, vLLM) ggf. ohne Schlüssel
    vision: bool = False      # Seitenbilder mitsenden (Scans, Fotos)


@dataclass
class DateiRef:
    """Ort einer abgelegten Datei.

    LOKAL:      `pfad` relativ zu ABLAGE_VERZEICHNIS (POSIX-Schreibweise).
    SHAREPOINT: `drive_id` + `item_id` sind maßgeblich; `pfad` ist der
                Anzeigepfad, `web_url` der Link in SharePoint.
    """

    ablage: str               # models.AblageTyp
    name: str                 # Dateiname (Anzeige / Content-Disposition)
    mime: str
    groesse: int
    pfad: str | None = None
    drive_id: str | None = None
    item_id: str | None = None
    web_url: str | None = None
