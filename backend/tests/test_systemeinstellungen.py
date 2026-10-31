"""Geheimnisse (Fernet) und globale Systemeinstellungen (Graph-/KI-Zugang)."""

import json

import pytest

from app import models
from app.config import get_settings
from app.services import geheimnisse, systemeinstellungen as se


@pytest.fixture(autouse=True)
def _sauber(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "graph_client_secret", None)
    monkeypatch.setattr(s, "llm_api_key", None)
    monkeypatch.setattr(s, "geheimnis_schluessel", None)
    geheimnisse.cache_leeren()
    yield
    geheimnisse.cache_leeren()


def _admin(db):
    b = models.Benutzer(email="a@example.com", name="Admin", passwort_hash="x", rolle="ADMIN")
    db.add(b)
    db.flush()
    return b


GRAPH = {"graph_tenant_id": "contoso.onmicrosoft.com", "graph_client_id": "11111111-2222-3333-4444-555555555555",
         "graph_client_secret": "geheim~Secret.123"}


# --- geheimnisse ---------------------------------------------------------------


def test_verschluesseln_rundreise():
    token = geheimnisse.verschluesseln("Pässwort-ÄÖÜ")
    assert token != "Pässwort-ÄÖÜ" and "Pässwort" not in token
    assert geheimnisse.entschluesseln(token) == "Pässwort-ÄÖÜ"
    # Fernet ist randomisiert
    assert geheimnisse.verschluesseln("x") != geheimnisse.verschluesseln("x")


def test_anderer_schluessel_liefert_none(monkeypatch):
    token = geheimnisse.verschluesseln("abc")
    monkeypatch.setattr(get_settings(), "secret_key", "ganz-anderer-schluessel")
    geheimnisse.cache_leeren()
    assert geheimnisse.entschluesseln(token) is None


def test_geheimnis_schluessel_hat_vorrang(monkeypatch):
    monkeypatch.setattr(get_settings(), "geheimnis_schluessel", "eigener-schluessel")
    token = geheimnisse.verschluesseln("abc")
    # SECRET_KEY rotieren: Geheimnisse bleiben lesbar
    monkeypatch.setattr(get_settings(), "secret_key", "rotiert")
    assert geheimnisse.entschluesseln(token) == "abc"
    monkeypatch.setattr(get_settings(), "geheimnis_schluessel", None)
    assert geheimnisse.entschluesseln(token) is None


@pytest.mark.parametrize("muell", ["", "kein-token", "gAAAAA", None, "äöü"])
def test_entschluesseln_muell(muell):
    assert geheimnisse.entschluesseln(muell) is None


# --- setze / wert ------------------------------------------------------------------


def test_setze_und_wert(db):
    admin = _admin(db)
    geaendert = se.setze(db, admin, {**GRAPH, "llm_url": " https://api.openai.com/v1 ", "llm_vision": True})
    assert sorted(geaendert) == sorted([*GRAPH, "llm_url", "llm_vision"])
    assert se.wert(db, "graph_tenant_id") == "contoso.onmicrosoft.com"
    assert se.wert(db, "graph_client_secret") == "geheim~Secret.123"
    assert se.wert(db, "llm_url") == "https://api.openai.com/v1"
    assert se.wert(db, "llm_vision") == "1"

    zeile = db.get(models.SystemEinstellung, "graph_client_secret")
    assert zeile.verschluesselt is True
    assert "geheim" not in zeile.wert
    assert zeile.geaendert_von == admin.id
    assert db.get(models.SystemEinstellung, "graph_tenant_id").verschluesselt is False

    # unverändert → nicht gemeldet; Geheimnis None = unverändert
    assert se.setze(db, admin, {**GRAPH, "graph_client_secret": None}) == []
    assert se.setze(db, admin, {"graph_client_secret": "geheim~Secret.123"}) == []
    assert se.wert(db, "graph_client_secret") == "geheim~Secret.123"


def test_setze_loeschen(db):
    se.setze(db, None, {**GRAPH, "llm_modell": "gpt-4o-mini", "llm_vision": False})
    assert se.wert(db, "llm_vision") == "0"
    assert sorted(se.setze(db, None, {"graph_client_secret": "", "llm_modell": "", "llm_vision": None})) == [
        "graph_client_secret", "llm_modell", "llm_vision"]
    assert se.wert(db, "graph_client_secret") is None
    assert db.get(models.SystemEinstellung, "graph_client_secret") is None
    assert se.wert(db, "llm_modell") is None
    assert se.wert(db, "llm_vision") is None
    # Löschen nicht vorhandener Werte ist keine Änderung
    assert se.setze(db, None, {"llm_modell": None, "llm_api_key": ""}) == []


def test_setze_validierung(db):
    with pytest.raises(ValueError, match="Unbekannte Einstellung"):
        se.setze(db, None, {"graph_tenant_id": "t", "boese": "x"})
    # nichts geschrieben (erst prüfen, dann schreiben)
    assert se.wert(db, "graph_tenant_id") is None
    with pytest.raises(ValueError, match="Tenant"):
        se.setze(db, None, {"graph_tenant_id": "../../evil"})
    with pytest.raises(ValueError, match="http"):
        se.setze(db, None, {"llm_url": "ftp://x"})
    with pytest.raises(ValueError, match="ja/nein"):
        se.setze(db, None, {"llm_vision": "vielleicht"})
    with pytest.raises(ValueError, match="zu lang"):
        se.setze(db, None, {"llm_api_key": "x" * 5000})
    with pytest.raises(ValueError):
        se.wert(db, "unbekannt")


def test_umgebung_hat_vorrang(db, monkeypatch):
    se.setze(db, None, {**GRAPH, "llm_api_key": "db-key"})
    monkeypatch.setattr(get_settings(), "graph_client_secret", "aus-env")
    monkeypatch.setattr(get_settings(), "llm_api_key", "env-key")
    assert se.wert(db, "graph_client_secret") == "aus-env"
    assert se.wert(db, "llm_api_key") == "env-key"
    assert se.graph_zugang(db).client_secret == "aus-env"
    o = se.oeffentlich(db)
    assert o["graph"]["client_secret"] == {"gesetzt": True, "lesbar": True, "aus_umgebung": True}
    assert o["llm"]["api_key"]["aus_umgebung"] is True
    # leerer Env-Wert zählt nicht
    monkeypatch.setattr(get_settings(), "graph_client_secret", "  ")
    assert se.wert(db, "graph_client_secret") == "geheim~Secret.123"


# --- Zugänge -----------------------------------------------------------------------


def test_graph_zugang(db):
    assert se.graph_zugang(db) is None
    se.setze(db, None, {"graph_tenant_id": GRAPH["graph_tenant_id"], "graph_client_id": GRAPH["graph_client_id"]})
    assert se.graph_zugang(db) is None  # Secret fehlt
    se.setze(db, None, {"graph_client_secret": GRAPH["graph_client_secret"]})
    z = se.graph_zugang(db)
    assert (z.tenant_id, z.client_id, z.client_secret) == tuple(GRAPH.values())


def test_ki_zugang(db):
    assert se.ki_zugang(db) is None
    se.setze(db, None, {"llm_url": "http://localhost:11434/v1"})
    assert se.ki_zugang(db) is None  # Modell fehlt
    se.setze(db, None, {"llm_modell": "llama3.2"})
    z = se.ki_zugang(db)
    assert (z.url, z.modell, z.api_key, z.vision) == ("http://localhost:11434/v1", "llama3.2", None, False)
    se.setze(db, None, {"llm_api_key": "sk-test", "llm_vision": "ja"})
    z = se.ki_zugang(db)
    assert z.api_key == "sk-test" and z.vision is True


def test_nicht_entschluesselbar_wirft_nie(db, monkeypatch):
    se.setze(db, None, {**GRAPH, "llm_url": "https://x/v1", "llm_modell": "m", "llm_api_key": "sk-1"})
    # z. B. Sicherung einer anderen Installation eingespielt
    monkeypatch.setattr(get_settings(), "secret_key", "anderer-schluessel")
    geheimnisse.cache_leeren()
    assert se.wert(db, "graph_client_secret") is None
    assert se.graph_zugang(db) is None
    z = se.ki_zugang(db)
    assert z is not None and z.api_key is None  # ohne Key weiter nutzbar (lokale Server)
    o = se.oeffentlich(db)
    assert o["graph"]["client_secret"] == {"gesetzt": True, "lesbar": False, "aus_umgebung": False}
    assert o["graph"]["konfiguriert"] is False
    assert o["llm"]["api_key"]["lesbar"] is False
    # neu eingeben macht es wieder lesbar
    assert se.setze(db, None, {"graph_client_secret": "neu"}) == ["graph_client_secret"]
    assert se.graph_zugang(db).client_secret == "neu"


def test_oeffentlich_ohne_klartext(db, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "ablage_verzeichnis", str(tmp_path / "ablage"))
    se.setze(db, None, {**GRAPH, "llm_url": "https://api.openai.com/v1", "llm_modell": "gpt-4o-mini",
                        "llm_api_key": "sk-supergeheim", "llm_vision": True})
    o = se.oeffentlich(db)
    text = json.dumps(o)
    assert "geheim~Secret" not in text and "sk-supergeheim" not in text
    for zeile in db.query(models.SystemEinstellung).filter_by(verschluesselt=True):
        assert zeile.wert not in text
    assert o["graph"] == {
        "tenant_id": "contoso.onmicrosoft.com",
        "client_id": GRAPH["graph_client_id"],
        "client_secret": {"gesetzt": True, "lesbar": True, "aus_umgebung": False},
        "konfiguriert": True,
    }
    assert o["llm"]["url"] == "https://api.openai.com/v1"
    assert o["llm"]["modell"] == "gpt-4o-mini"
    assert o["llm"]["vision"] is True
    assert o["llm"]["konfiguriert"] is True
    assert o["ablage"] == {"verzeichnis": str((tmp_path / "ablage").resolve()), "max_mb": get_settings().ablage_max_mb}
    assert o["secret_key_unsicher"] is False  # Tests: SECRET_KEY=test-schluessel


def test_oeffentlich_leer_und_unsicherer_schluessel(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", "bitte-aendern-unsicherer-entwicklungsschluessel")
    o = se.oeffentlich(db)
    assert o["secret_key_unsicher"] is True
    assert o["graph"]["client_secret"] == {"gesetzt": False, "lesbar": False, "aus_umgebung": False}
    assert o["graph"]["konfiguriert"] is False and o["llm"]["konfiguriert"] is False
    assert o["llm"]["vision"] is False


# --- KI: API-Key gehört zur URL -----------------------------------------------------


def test_url_wechsel_verwirft_gespeicherten_key(db):
    se.setze(db, None, {"llm_url": "https://api.openai.com/v1", "llm_modell": "m", "llm_api_key": "sk-alt"})
    # gleicher Ursprung (nur Pfad): Key bleibt
    assert se.setze(db, None, {"llm_url": "https://API.openai.com:443/v2"}) == ["llm_url"]
    assert se.ki_zugang(db).api_key == "sk-alt"
    # anderer Host ohne neuen Key: verworfen (und fürs Audit gemeldet)
    geaendert = se.setze(db, None, {"llm_url": "https://anderer.example/v1"})
    assert geaendert[0] == "llm_url" and geaendert[1].startswith("llm_api_key (verworfen")
    assert db.get(models.SystemEinstellung, "llm_api_key") is None
    assert se.ki_zugang(db).api_key is None
    # mit neuem Key: dieser gilt
    se.setze(db, None, {"llm_url": "https://dritter.example/v1", "llm_api_key": "sk-neu"})
    assert se.ki_zugang(db).api_key == "sk-neu"
    # anderer Port bzw. Löschen der URL zählt ebenfalls als Wechsel
    se.setze(db, None, {"llm_url": "https://dritter.example:8443/v1"})
    assert se.ki_zugang(db).api_key is None
    se.setze(db, None, {"llm_url": "https://dritter.example/v1", "llm_api_key": "sk-neu"})
    se.setze(db, None, {"llm_url": ""})
    se.setze(db, None, {"llm_url": "https://boese.example/v1"})
    assert se.ki_zugang(db).api_key is None


def test_env_key_bleibt_an_bisherigen_server_gebunden(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", "env-key")
    # Ersteinrichtung: URL in der Oberfläche, Key aus der Umgebung
    se.setze(db, None, {"llm_url": "https://api.openai.com/v1", "llm_modell": "m"})
    assert se.ki_zugang(db).api_key == "env-key"
    assert se.oeffentlich(db)["llm"]["api_key_hinweis"] is None
    # Wechsel auf einen anderen Server: Env-Key geht nicht dorthin
    se.setze(db, None, {"llm_url": "https://fremd.example/v1"})
    assert se.ki_zugang(db).api_key is None
    assert "LLM_API_KEY" in se.oeffentlich(db)["llm"]["api_key_hinweis"]
    # Löschen und neu setzen umgeht die Bindung nicht
    se.setze(db, None, {"llm_url": ""})
    se.setze(db, None, {"llm_url": "https://noch-fremder.example/v1"})
    assert se.ki_zugang(db).api_key is None
    # für den neuen Server gilt ein in der Oberfläche eingegebener Key
    se.setze(db, None, {"llm_api_key": "ui-key"})
    assert se.ki_zugang(db).api_key == "ui-key"
    # zurück zum ursprünglichen Server: Env-Key wieder (UI-Key verworfen)
    se.setze(db, None, {"llm_url": "https://api.openai.com/v1/"})
    assert se.ki_zugang(db).api_key == "env-key"
    # Bindung ist eine interne Zeile, nicht über die API änderbar
    with pytest.raises(ValueError, match="Unbekannte Einstellung"):
        se.setze(db, None, {se._BINDUNG: "{}"})
    assert "env-key" not in db.get(models.SystemEinstellung, se._BINDUNG).wert


def test_neuer_env_key_gilt_fuer_aktuelle_url(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", "env-alt")
    se.setze(db, None, {"llm_url": "https://a.example/v1", "llm_modell": "m"})
    se.setze(db, None, {"llm_url": "https://b.example/v1"})
    assert se.ki_zugang(db).api_key is None
    # Betreiber hinterlegt einen neuen Key (anderer Fingerabdruck) → gilt für die eingestellte URL
    monkeypatch.setattr(get_settings(), "llm_api_key", "env-neu")
    assert se.ki_zugang(db).api_key == "env-neu"


@pytest.mark.parametrize("host, lokal", [
    ("localhost", True), ("127.0.0.1", True), ("[::1]", True), ("10.1.2.3", True),
    ("192.168.178.20", True), ("172.16.0.5", True), ("169.254.1.1", True), ("fd00::1", True),
    ("ollama", True), ("llm.local", True), ("ki.intern.lan", True), ("gpu.internal", True),
    ("2130706433", True),            # = 127.0.0.1
    ("api.openai.com", False), ("8.8.8.8", False), ("fremder-host.example", False),
    ("134744072", False),            # = 8.8.8.8 in Zahlform
    ("", False), (None, False),
])
def test_lokale_adresse(host, lokal):
    assert se.lokale_adresse(host) is lokal


def test_key_nie_per_http_nach_aussen(db):
    with pytest.raises(ValueError, match="https://"):
        se.setze(db, None, {"llm_url": "http://fremder-host.example:8080/v1", "llm_modell": "m",
                            "llm_api_key": "sk-1"})
    assert se.wert(db, "llm_url") is None  # nichts geschrieben
    # lokal bzw. ohne Key erlaubt
    se.setze(db, None, {"llm_url": "http://ollama:11434/v1", "llm_modell": "m", "llm_api_key": "sk-1"})
    assert se.ki_zugang(db).api_key == "sk-1"
    se.setze(db, None, {"llm_url": "http://fremder-host.example:8080/v1"})   # Key wird verworfen
    assert se.ki_zugang(db).api_key is None
    with pytest.raises(ValueError, match="https://"):
        se.setze(db, None, {"llm_api_key": "sk-2"})


def test_env_key_nie_per_http_nach_aussen(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", "env-key")
    with pytest.raises(ValueError, match="https://"):
        se.setze(db, None, {"llm_url": "http://fremder-host.example/v1", "llm_modell": "m"})


def test_alte_http_einstellung_sendet_key_nicht(db):
    # vor dieser Prüfung gespeichert: http:// nach außen mit Key
    db.add(models.SystemEinstellung(schluessel="llm_url", wert="http://fremder-host.example/v1"))
    db.add(models.SystemEinstellung(schluessel="llm_modell", wert="m"))
    db.add(models.SystemEinstellung(schluessel="llm_api_key", wert=geheimnisse.verschluesseln("sk-1"),
                                    verschluesselt=True))
    db.flush()
    assert se.ki_zugang(db).api_key is None
    assert se.oeffentlich(db)["llm"]["api_key_hinweis"] == se.HTTP_KEY_FEHLER
