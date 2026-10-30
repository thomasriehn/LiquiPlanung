"""Globale Systemeinstellungen (Administrator): Microsoft-Graph- und KI-Zugang.

Schlüssel/Wert-Tabelle `system_einstellungen` mit fester Liste bekannter
Schlüssel (`SCHLUESSEL`). Geheimnisse liegen als Fernet-Token in der Datenbank
(siehe `geheimnisse`) und verlassen den Server nie im Klartext; optional haben
GRAPH_CLIENT_SECRET / LLM_API_KEY aus der Umgebung Vorrang.

Nicht entschlüsselbare Geheimnisse (anderer Schlüssel, fremde Sicherung) gelten
als „nicht eingerichtet“ – keine Funktion hier wirft deswegen eine Ausnahme.
"""

import re

from sqlalchemy.orm import Session

from .. import models
from ..config import Settings, get_settings
from . import geheimnisse
from .zugaenge import GraphZugang, KiZugang

# geheim: als Fernet-Token speichern, nie zurückgeben; umgebung: Feld in Settings
# mit Vorrang; bool: Wert "1"/"0"
SCHLUESSEL: dict[str, dict] = {
    "graph_tenant_id": {"geheim": False},
    "graph_client_id": {"geheim": False},
    "graph_client_secret": {"geheim": True, "umgebung": "graph_client_secret"},
    "llm_url": {"geheim": False},
    "llm_modell": {"geheim": False},
    "llm_api_key": {"geheim": True, "umgebung": "llm_api_key"},
    "llm_vision": {"geheim": False, "bool": True},
}

MAX_LAENGE = 2048
# Tenant-ID landet im Pfad der Anmelde-URL: GUID oder Domäne (contoso.onmicrosoft.com)
_TENANT_MUSTER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,254}$")
_URL_MUSTER = re.compile(r"^https?://[^\s/]+\S*$", re.IGNORECASE)
_WAHR = {"1", "true", "ja", "on", "yes"}
_FALSCH = {"0", "false", "nein", "off", "no"}


def _unsicherer_standard() -> str:
    return Settings.model_fields["secret_key"].default


def _pruefe_schluessel(schluessel: str) -> dict:
    info = SCHLUESSEL.get(schluessel)
    if info is None:
        raise ValueError(f"Unbekannte Einstellung: {schluessel}")
    return info


def _aus_umgebung(info: dict) -> str | None:
    feld = info.get("umgebung")
    if not feld:
        return None
    wert = getattr(get_settings(), feld, None)
    if isinstance(wert, str) and wert.strip():
        return wert.strip()
    return None


def _zeile(db: Session, schluessel: str) -> models.SystemEinstellung | None:
    return db.get(models.SystemEinstellung, schluessel)


def _gespeichert(zeile: models.SystemEinstellung | None) -> str | None:
    """Wert einer Zeile (entschlüsselt); None, wenn leer oder nicht lesbar."""
    if zeile is None or zeile.wert is None:
        return None
    if zeile.verschluesselt:
        return geheimnisse.entschluesseln(zeile.wert)
    return zeile.wert


def wert(db: Session, schluessel: str) -> str | None:
    """Aktueller Wert (entschlüsselt); Umgebungsvariable hat Vorrang bei Geheimnissen.
    None, wenn nicht gesetzt oder nicht entschlüsselbar."""
    info = _pruefe_schluessel(schluessel)
    umgebung = _aus_umgebung(info)
    if umgebung is not None:
        return umgebung
    wert_ = _gespeichert(_zeile(db, schluessel))
    if wert_ is None or wert_ == "":
        return None
    return wert_


def _normalisieren(schluessel: str, info: dict, wert_) -> str | None:
    """Eingabe → zu speichernder Text; None = Zeile entfernen."""
    if info.get("bool"):
        if wert_ is None or wert_ == "":
            return None
        if isinstance(wert_, bool):
            return "1" if wert_ else "0"
        if isinstance(wert_, int):
            return "1" if wert_ else "0"
        text = str(wert_).strip().lower()
        if text in _WAHR:
            return "1"
        if text in _FALSCH:
            return "0"
        raise ValueError(f"Ungültiger Wert für {schluessel} (ja/nein erwartet)")
    if isinstance(wert_, bool):
        return "1" if wert_ else "0"
    if wert_ is None:
        return None
    text = str(wert_).strip()
    if not text:
        return None
    if len(text) > MAX_LAENGE:
        raise ValueError(f"Wert für {schluessel} ist zu lang (max. {MAX_LAENGE} Zeichen)")
    if schluessel == "graph_tenant_id" and not _TENANT_MUSTER.match(text):
        raise ValueError(
            "Ungültige Tenant-ID – erwartet wird die Verzeichnis-(Tenant-)ID (GUID) "
            "oder die Domäne, z. B. contoso.onmicrosoft.com"
        )
    if schluessel == "graph_client_id" and re.search(r"\s", text):
        raise ValueError("Ungültige Anwendungs-(Client-)ID")
    if schluessel == "llm_url" and not _URL_MUSTER.match(text):
        raise ValueError("Die API-URL muss mit http:// oder https:// beginnen")
    return text


def setze(db: Session, benutzer, aenderungen: dict[str, str | bool | None]) -> list[str]:
    """Einstellungen ändern; liefert die tatsächlich geänderten Schlüssel (für das
    Audit, ohne Werte). Kein Commit.

    Geheimnis: None = unverändert, "" = löschen, sonst verschlüsselt speichern.
    Normal: None/"" = löschen, bool → "1"/"0".
    """
    # erst alles prüfen, dann schreiben (keine halben Änderungen)
    plan: list[tuple[str, dict, str | None]] = []
    for schluessel, wert_ in (aenderungen or {}).items():
        info = _pruefe_schluessel(schluessel)
        if info.get("geheim"):
            if wert_ is None:
                continue
            if isinstance(wert_, bool):
                raise ValueError(f"Ungültiger Wert für {schluessel}")
            text = str(wert_).strip()
            if len(text) > MAX_LAENGE:
                raise ValueError(f"Wert für {schluessel} ist zu lang (max. {MAX_LAENGE} Zeichen)")
            plan.append((schluessel, info, text or None))
        else:
            plan.append((schluessel, info, _normalisieren(schluessel, info, wert_)))

    benutzer_id = getattr(benutzer, "id", None)
    geaendert: list[str] = []
    for schluessel, info, neu in plan:
        zeile = _zeile(db, schluessel)
        if neu is None:
            if zeile is not None:
                db.delete(zeile)
                geaendert.append(schluessel)
            continue
        if zeile is not None and _gespeichert(zeile) == neu and zeile.verschluesselt == bool(info.get("geheim")):
            continue  # unverändert
        gespeichert = geheimnisse.verschluesseln(neu) if info.get("geheim") else neu
        if zeile is None:
            zeile = models.SystemEinstellung(schluessel=schluessel)
            db.add(zeile)
        zeile.wert = gespeichert
        zeile.verschluesselt = bool(info.get("geheim"))
        zeile.geaendert_am = models.utcnow()
        zeile.geaendert_von = benutzer_id
        geaendert.append(schluessel)
    if geaendert:
        db.flush()
    return geaendert


def _geheimnis_status(db: Session, schluessel: str) -> dict:
    info = SCHLUESSEL[schluessel]
    if _aus_umgebung(info) is not None:
        return {"gesetzt": True, "lesbar": True, "aus_umgebung": True}
    zeile = _zeile(db, schluessel)
    gesetzt = zeile is not None and bool(zeile.wert)
    return {
        "gesetzt": gesetzt,
        "lesbar": gesetzt and bool(_gespeichert(zeile)),
        "aus_umgebung": False,
    }


def graph_zugang(db: Session) -> GraphZugang | None:
    """Graph-Zugang oder None (Tenant/Client/Secret fehlt bzw. Secret nicht lesbar)."""
    tenant = wert(db, "graph_tenant_id")
    client = wert(db, "graph_client_id")
    secret = wert(db, "graph_client_secret")
    if not (tenant and client and secret):
        return None
    return GraphZugang(tenant_id=tenant, client_id=client, client_secret=secret)


def ki_zugang(db: Session) -> KiZugang | None:
    """KI-Zugang oder None (URL oder Modell fehlt); der API-Key darf fehlen
    (lokale Server) – ist er gespeichert, aber nicht lesbar, fehlt er ebenfalls."""
    url = wert(db, "llm_url")
    modell = wert(db, "llm_modell")
    if not (url and modell):
        return None
    return KiZugang(
        url=url,
        modell=modell,
        api_key=wert(db, "llm_api_key"),
        vision=wert(db, "llm_vision") == "1",
    )


def secret_key_unsicher() -> bool:
    return get_settings().secret_key == _unsicherer_standard()


def oeffentlich(db: Session) -> dict:
    """Einstellungen für die Oberfläche – Geheimnisse nur als Status, nie im Klartext."""
    from pathlib import Path

    s = get_settings()
    return {
        "graph": {
            "tenant_id": wert(db, "graph_tenant_id"),
            "client_id": wert(db, "graph_client_id"),
            "client_secret": _geheimnis_status(db, "graph_client_secret"),
            "konfiguriert": graph_zugang(db) is not None,
        },
        "llm": {
            "url": wert(db, "llm_url"),
            "modell": wert(db, "llm_modell"),
            "vision": wert(db, "llm_vision") == "1",
            "api_key": _geheimnis_status(db, "llm_api_key"),
            "konfiguriert": ki_zugang(db) is not None,
        },
        "ablage": {
            "verzeichnis": str(Path(s.ablage_verzeichnis).resolve()),
            "max_mb": int(s.ablage_max_mb),
        },
        "secret_key_unsicher": secret_key_unsicher(),
    }
