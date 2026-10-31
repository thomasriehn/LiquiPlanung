"""Globale Systemeinstellungen (Administrator): Microsoft-Graph- und KI-Zugang.

Schlüssel/Wert-Tabelle `system_einstellungen` mit fester Liste bekannter
Schlüssel (`SCHLUESSEL`). Geheimnisse liegen als Fernet-Token in der Datenbank
(siehe `geheimnisse`) und verlassen den Server nie im Klartext; optional haben
GRAPH_CLIENT_SECRET / LLM_API_KEY aus der Umgebung Vorrang.

Nicht entschlüsselbare Geheimnisse (anderer Schlüssel, fremde Sicherung) gelten
als „nicht eingerichtet“ – keine Funktion hier wirft deswegen eine Ausnahme.

KI-API-Key und URL gehören zusammen (der Key darf nicht per URL-Wechsel an einen
anderen Server gehen):
- LLM_API_KEY aus der Umgebung wird nur gesendet, wenn auch die API-URL aus der
  Umgebung kommt (LLM_URL). Fehlt LLM_URL, gilt der Env-Key nicht (Hinweis über
  `ki_key_hinweis`); ein in der Oberfläche gespeicherter Key gilt weiter. Eine URL
  aus der Umgebung lässt sich über `setze` nicht ändern.
- Ein gespeicherter Key gehört zur gespeicherten URL: Ändert sich deren Ursprung
  (Schema, Host, Port) – auch von leer auf gesetzt oder durch Löschen – und kommt
  in derselben Änderung kein neuer Key mit, wird er verworfen (Audit). Wird ein
  Key gespeichert, während die URL aus der Umgebung kommt, gehört er zu dieser;
  eine (verdeckte) gespeicherte URL wird dabei entfernt.
- Über http:// geht ein Key nur an localhost bzw. Adressen im eigenen Netz;
  sonst lehnt `setze` ab und `ki_zugang` lässt den Key weg.
"""

import ipaddress
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
    "llm_url": {"geheim": False, "umgebung": "llm_url"},
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

# Zeile aus einer nie ausgelieferten Zwischenstufe (Bindung des Env-Keys an einen
# Ursprung): wird ignoriert und beim nächsten Speichern entfernt
_ALTE_BINDUNG = "llm_api_key_umgebung_bindung"
_LOKALE_ENDUNGEN = (".localhost", ".local", ".lan", ".internal", ".home.arpa", ".localdomain")
HTTP_KEY_FEHLER = (
    "Der API-Key wird nur verschlüsselt übertragen: http:// ist nur für localhost bzw. "
    "Adressen im eigenen Netz zulässig – bitte https:// verwenden oder den API-Key entfernen"
)
URL_UMGEBUNG_FEHLER = (
    "Die API-URL ist über die Umgebungsvariable LLM_URL festgelegt und lässt sich hier nicht ändern"
)
ENV_KEY_OHNE_URL = (
    "Der API-Key aus der Umgebungsvariable LLM_API_KEY wird nur an eine ebenfalls in der Umgebung "
    "festgelegte API-URL gesendet – bitte LLM_URL ebenfalls in der Umgebung setzen"
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


def _env_url() -> str | None:
    """LLM_URL aus der Umgebung (None = URL kommt aus der Oberfläche)."""
    return _aus_umgebung(SCHLUESSEL["llm_url"])


def _umgebung_wirksam(schluessel: str, info: dict) -> str | None:
    """Wert aus der Umgebung, der tatsächlich gilt: LLM_API_KEY nur zusammen mit LLM_URL."""
    wert_ = _aus_umgebung(info)
    if wert_ is not None and schluessel == "llm_api_key" and _env_url() is None:
        return None
    return wert_


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
    """Aktueller Wert (entschlüsselt); eine Umgebungsvariable hat Vorrang (Geheimnisse,
    LLM_URL; LLM_API_KEY nur zusammen mit LLM_URL). None, wenn nicht gesetzt oder
    nicht entschlüsselbar."""
    info = _pruefe_schluessel(schluessel)
    umgebung = _umgebung_wirksam(schluessel, info)
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


def _ki_key_plan(db: Session, plan: list[tuple[str, dict, str | None]]) -> tuple[bool, bool]:
    """Folgen einer Änderung von llm_url/llm_api_key → (gespeicherten Key verwerfen?,
    verdeckte gespeicherte URL entfernen?). ValueError, wenn die URL aus der Umgebung
    kommt und geändert werden soll oder danach ein gespeicherter Key per http://
    nach außen ginge."""
    neu = {s: w for s, _, w in plan}
    if "llm_url" not in neu and "llm_api_key" not in neu:
        return False, False
    env_url = _env_url()
    if env_url is not None and "llm_url" in neu:
        raise ValueError(URL_UMGEBUNG_FEHLER)
    if env_url is not None:
        # Key gehört zur URL aus der Umgebung; eine gespeicherte URL ist verdeckt und
        # darf ihn später (ohne LLM_URL) nicht bekommen
        ziel, wechsel = env_url, False
        url_entfernen = bool(neu.get("llm_api_key")) and _zeile(db, "llm_url") is not None
    else:
        alt_url = _gespeichert(_zeile(db, "llm_url")) or None
        ziel = neu.get("llm_url", alt_url)
        # auch leer → gesetzt und gesetzt → leer ist ein Wechsel
        wechsel = _ursprung(alt_url) != _ursprung(ziel)
        url_entfernen = False
    verwerfen = wechsel and "llm_api_key" not in neu and _zeile(db, "llm_api_key") is not None
    if "llm_api_key" in neu:
        gespeichert = neu["llm_api_key"]
    else:
        gespeichert = None if verwerfen else _gespeichert(_zeile(db, "llm_api_key"))
    # ein wirksamer Env-Key (LLM_URL + LLM_API_KEY) ist Sache des Betreibers – dann
    # wird der gespeicherte gar nicht gesendet (Hinweis über `ki_key_hinweis`)
    env_key = _umgebung_wirksam("llm_api_key", SCHLUESSEL["llm_api_key"])
    if env_key is None and gespeichert and key_unverschluesselt(ziel):
        raise ValueError(HTTP_KEY_FEHLER)
    return verwerfen, url_entfernen


def setze(db: Session, benutzer, aenderungen: dict[str, str | bool | None]) -> list[str]:
    """Einstellungen ändern; liefert die tatsächlich geänderten Schlüssel (für das
    Audit, ohne Werte). Kein Commit.

    Geheimnis: None = unverändert, "" = löschen, sonst verschlüsselt speichern.
    Normal: None/"" = löschen, bool → "1"/"0".
    Neuer Ursprung der llm_url ohne neuen Key: gespeicherter Key wird verworfen
    (gemeldet als „llm_api_key (verworfen …)“); llm_url bei LLM_URL aus der
    Umgebung → ValueError. Siehe Moduldokumentation.
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
    key_verwerfen, url_entfernen = _ki_key_plan(db, plan)

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
    if url_entfernen:
        db.delete(_zeile(db, "llm_url"))
        geaendert.append("llm_url (entfernt: API-URL kommt aus der Umgebung)")
    alte_bindung = _zeile(db, _ALTE_BINDUNG)
    if alte_bindung is not None:
        db.delete(alte_bindung)
    if geaendert or alte_bindung is not None:
        db.flush()
    return geaendert


def _geheimnis_status(db: Session, schluessel: str) -> dict:
    info = SCHLUESSEL[schluessel]
    if _umgebung_wirksam(schluessel, info) is not None:
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
    """API-Key für `url`: Env-Key (nur zusammen mit LLM_URL, siehe Modul), sonst
    der gespeicherte; nie per http:// nach außen."""
    key = wert(db, "llm_api_key")
    return None if key and key_unverschluesselt(url) else key


def ki_key_hinweis(db: Session) -> str | None:
    """Warum ein eingerichteter API-Key nicht gesendet wird – None, wenn er es wird."""
    if wert(db, "llm_api_key") and key_unverschluesselt(wert(db, "llm_url")):
        return HTTP_KEY_FEHLER
    if _aus_umgebung(SCHLUESSEL["llm_api_key"]) is not None and _env_url() is None:
        if _gespeichert(_zeile(db, "llm_api_key")):
            return f"{ENV_KEY_OHNE_URL}; bis dahin gilt der hier gespeicherte API-Key"
        return f"{ENV_KEY_OHNE_URL}; bis dahin wird kein API-Key gesendet"
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
            "url_aus_umgebung": _env_url() is not None,
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
