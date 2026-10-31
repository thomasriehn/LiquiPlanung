"""Globale Systemeinstellungen (Administrator): Microsoft-Graph- und KI-Zugang.

Schlüssel/Wert-Tabelle `system_einstellungen` mit fester Liste bekannter
Schlüssel (`SCHLUESSEL`). Geheimnisse liegen als Fernet-Token in der Datenbank
(siehe `geheimnisse`) und verlassen den Server nie im Klartext; optional haben
GRAPH_CLIENT_SECRET / LLM_API_KEY aus der Umgebung Vorrang.

Nicht entschlüsselbare Geheimnisse (anderer Schlüssel, fremde Sicherung) gelten
als „nicht eingerichtet“ – keine Funktion hier wirft deswegen eine Ausnahme.

KI-API-Key und URL gehören zusammen (der Key darf nicht per URL-Wechsel an einen
anderen Server gehen):
- Ändert sich der Ursprung (Schema, Host, Port) der API-URL – auch durch Löschen –
  ohne neuen Key, wird der gespeicherte Key verworfen (Audit).
- LLM_API_KEY aus der Umgebung wird an den Ursprung gebunden, der beim ersten
  solchen Wechsel galt (interne Zeile `_BINDUNG`, nur Ursprung + Fingerabdruck
  des Keys); an einen anderen Ursprung geht er nicht – dort gilt ein in der
  Oberfläche eingegebener Key. Ein neuer Env-Key (anderer Fingerabdruck) gilt
  wieder für die dann eingestellte URL. Ohne Wechsel bleibt alles wie bisher.
- Über http:// geht ein Key nur an localhost bzw. Adressen im eigenen Netz;
  sonst lehnt `setze` ab und `ki_zugang` lässt den Key weg.
"""

import hashlib
import ipaddress
import json
import re
import socket
from urllib.parse import urlsplit

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

# interne Zeile (nicht über `setze` änderbar): Ursprung, an den LLM_API_KEY gebunden ist
_BINDUNG = "llm_api_key_umgebung_bindung"
_LOKALE_ENDUNGEN = (".localhost", ".local", ".lan", ".internal", ".home.arpa", ".localdomain")
HTTP_KEY_FEHLER = (
    "Der API-Key wird nur verschlüsselt übertragen: http:// ist nur für localhost bzw. "
    "Adressen im eigenen Netz zulässig – bitte https:// verwenden oder den API-Key entfernen"
)


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


def _ursprung(url: str | None) -> str | None:
    """„schema://host:port“ einer URL (Vergleich beim URL-Wechsel); None ohne URL."""
    if not url:
        return None
    teile = urlsplit(url.strip())
    schema = (teile.scheme or "").lower()
    try:
        port = teile.port or {"https": 443, "http": 80}.get(schema)
    except ValueError:
        return f"{schema}://{teile.netloc.lower()}"
    return f"{schema}://{(teile.hostname or '').lower().rstrip('.')}:{port}"


def lokale_adresse(host: str | None) -> bool:
    """localhost, private bzw. link-lokale IP oder ein nur im eigenen Netz
    auflösbarer Name (ohne Punkt wie der Docker-Dienst „ollama“, .local, .lan …)."""
    h = (host or "").strip().strip("[]").lower().rstrip(".")
    if not h:
        return False
    if h == "localhost" or h.endswith(_LOKALE_ENDUNGEN):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        try:  # Zahlformen wie 2130706433 oder 127.1 – so löst sie auch das System auf
            ip = ipaddress.ip_address(socket.inet_aton(h))
        except OSError:
            return "." not in h
    return ip.is_private or ip.is_loopback or ip.is_link_local


def key_unverschluesselt(url: str | None) -> bool:
    """Ginge ein API-Key per http:// an eine Adresse außerhalb des eigenen Netzes?"""
    if not url:
        return False
    teile = urlsplit(url.strip())
    return (teile.scheme or "").lower() == "http" and not lokale_adresse(teile.hostname)


def _fingerabdruck(key: str) -> str:
    return hashlib.sha256(f"llm-bindung\0{key}".encode("utf-8")).hexdigest()[:16]


def _bindung(db: Session, env_key: str) -> str | None:
    """Ursprung, an den der Env-Key gebunden ist; None = ungebunden (auch wenn
    die Bindung zu einem früheren Env-Key gehört)."""
    zeile = _zeile(db, _BINDUNG)
    try:
        daten = json.loads(zeile.wert) if zeile is not None and zeile.wert else None
    except ValueError:
        return None
    if not isinstance(daten, dict) or daten.get("key") != _fingerabdruck(env_key):
        return None
    return daten.get("ursprung")


def _env_key_fuer(db: Session, url: str | None) -> str | None:
    """LLM_API_KEY aus der Umgebung – nur für den Ursprung, an den er gebunden ist."""
    env = _aus_umgebung(SCHLUESSEL["llm_api_key"])
    if env is None:
        return None
    gebunden = _bindung(db, env)
    return env if gebunden is None or gebunden == _ursprung(url) else None


def _ki_key_plan(db: Session, plan: list[tuple[str, dict, str | None]]) -> tuple[bool, str | None]:
    """Folgen einer Änderung von llm_url/llm_api_key → (gespeicherten Key
    verwerfen?, neue Bindung des Env-Keys oder None). Ginge danach ein Key per
    http:// nach außen → ValueError."""
    neu = {s: w for s, _, w in plan}
    if "llm_url" not in neu and "llm_api_key" not in neu:
        return False, None
    alt_url = _gespeichert(_zeile(db, "llm_url")) or None
    neu_url = neu.get("llm_url", alt_url)
    wechsel = "llm_url" in neu and alt_url is not None and _ursprung(alt_url) != _ursprung(neu_url)
    verwerfen = wechsel and "llm_api_key" not in neu and _zeile(db, "llm_api_key") is not None
    env = _aus_umgebung(SCHLUESSEL["llm_api_key"])
    gebunden = _bindung(db, env) if env else None
    bindung = _ursprung(alt_url) if env and wechsel and gebunden is None else None
    if "llm_api_key" in neu:
        gespeichert = neu["llm_api_key"]
    else:
        gespeichert = None if verwerfen else _gespeichert(_zeile(db, "llm_api_key"))
    gebunden = bindung or gebunden
    env_nutzbar = env is not None and (gebunden is None or gebunden == _ursprung(neu_url))
    if (gespeichert or env_nutzbar) and key_unverschluesselt(neu_url):
        raise ValueError(HTTP_KEY_FEHLER)
    return verwerfen, bindung


def setze(db: Session, benutzer, aenderungen: dict[str, str | bool | None]) -> list[str]:
    """Einstellungen ändern; liefert die tatsächlich geänderten Schlüssel (für das
    Audit, ohne Werte). Kein Commit.

    Geheimnis: None = unverändert, "" = löschen, sonst verschlüsselt speichern.
    Normal: None/"" = löschen, bool → "1"/"0".
    Neuer Ursprung der llm_url ohne neuen Key: gespeicherter Key wird verworfen
    (gemeldet als „llm_api_key (verworfen …)“), siehe Moduldokumentation.
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
    key_verwerfen, bindung = _ki_key_plan(db, plan)

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
    if key_verwerfen:
        # gehört zum bisherigen Server – nie an einen anderen senden
        db.delete(_zeile(db, "llm_api_key"))
        geaendert.append("llm_api_key (verworfen: API-URL auf anderen Server geändert)")
    if bindung is not None:
        zeile = _zeile(db, _BINDUNG) or models.SystemEinstellung(schluessel=_BINDUNG)
        if zeile not in db:
            db.add(zeile)
        zeile.wert = json.dumps({"ursprung": bindung,
                                 "key": _fingerabdruck(_aus_umgebung(SCHLUESSEL["llm_api_key"]))})
        zeile.verschluesselt = False
        zeile.geaendert_am = models.utcnow()
        zeile.geaendert_von = benutzer_id
    if geaendert or bindung is not None:
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


def _ki_api_key(db: Session, url: str | None) -> str | None:
    """API-Key für `url`: Env-Key (nur für seinen Ursprung, siehe Modul), sonst
    der gespeicherte; nie per http:// nach außen."""
    key = _env_key_fuer(db, url) or _gespeichert(_zeile(db, "llm_api_key")) or None
    return None if key and key_unverschluesselt(url) else key


def ki_key_hinweis(db: Session) -> str | None:
    """Warum ein eingerichteter API-Key nicht gesendet wird – None, wenn er es wird."""
    url = wert(db, "llm_url")
    env = _aus_umgebung(SCHLUESSEL["llm_api_key"])
    gespeichert = _gespeichert(_zeile(db, "llm_api_key"))
    if (_env_key_fuer(db, url) or gespeichert) and key_unverschluesselt(url):
        return HTTP_KEY_FEHLER
    if env and _env_key_fuer(db, url) is None and not gespeichert:
        return ("Der API-Key aus der Umgebungsvariable LLM_API_KEY wird nicht an diese API-URL "
                "gesendet, weil sie auf einen anderen Server geändert wurde – bitte den API-Key für "
                "die neue URL hier eingeben oder die URL zurücksetzen")
    return None


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
        api_key=_ki_api_key(db, url),
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
            "api_key_hinweis": ki_key_hinweis(db),
            "konfiguriert": ki_zugang(db) is not None,
        },
        "ablage": {
            "verzeichnis": str(Path(s.ablage_verzeichnis).resolve()),
            "max_mb": int(s.ablage_max_mb),
        },
        "secret_key_unsicher": secret_key_unsicher(),
    }
