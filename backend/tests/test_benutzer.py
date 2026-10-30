"""Benutzerverwaltung: Rolle ändern, Passwort zurücksetzen, erzwungener
Passwortwechsel, Sitzungsende nach Passwortänderung, Schutz des letzten Admins."""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models

from test_api import _login


def _zweiter_client():
    import app.main as hauptmodul

    return TestClient(hauptmodul.app)


def _anmelden(c, email, passwort):
    return c.post("/login", data={"email": email, "passwort": passwort}, follow_redirects=False)


def _benutzer_anlegen(client, email="maria@example.com", rolle="BEARBEITER", **extra):
    r = client.post("/api/benutzer", json={"email": email, "name": "Maria Muster",
                                            "passwort": "Startpasswort1", "rolle": rolle, **extra})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_passwortwechsel_beim_ersten_login_erzwungen(client):
    _login(client)
    mid = client.post("/api/mandanten", json={"name": "A GmbH", "kurzname": "a"}).json()["id"]
    bid = _benutzer_anlegen(client, passwort_aendern=True, mandanten_ids=[mid])
    assert next(b for b in client.get("/api/benutzer").json() if b["id"] == bid)["passwort_aendern"]

    maria = _zweiter_client()
    r = _anmelden(maria, "maria@example.com", "Startpasswort1")
    assert r.status_code == 303 and r.headers["location"] == "/profil"
    # bis zum Passwortwechsel nur das Profil
    assert maria.get("/api/mandanten").status_code == 403
    assert maria.get(f"/mandanten/{mid}/plan", follow_redirects=False).headers["location"] == "/profil"
    assert maria.get("/profil").status_code == 200
    assert maria.get("/api/profil").json()["passwort_aendern"] is True
    # gleiches Passwort abgewiesen, neues angenommen
    r = maria.post("/api/profil/passwort", json={"aktuelles_passwort": "Startpasswort1",
                                                 "neues_passwort": "Startpasswort1"})
    assert r.status_code == 422
    r = maria.post("/api/profil/passwort", json={"aktuelles_passwort": "Startpasswort1",
                                                 "neues_passwort": "MeinEigenes!42"})
    assert r.status_code == 200
    # diese Sitzung bleibt gültig, alles wieder erreichbar
    assert maria.get("/api/mandanten").status_code == 200
    assert maria.get("/api/profil").json()["passwort_aendern"] is False


def test_passwort_zuruecksetzen_beendet_sitzungen(client):
    _login(client)
    bid = _benutzer_anlegen(client)
    maria = _zweiter_client()
    assert _anmelden(maria, "maria@example.com", "Startpasswort1").headers["location"] == "/"
    assert maria.get("/api/mandanten").status_code == 200

    assert client.patch(f"/api/benutzer/{bid}", json={"passwort": "kurz"}).status_code == 422
    r = client.patch(f"/api/benutzer/{bid}", json={"passwort": "Neu-Gesetzt-77", "passwort_aendern": True})
    assert r.status_code == 200
    # alte Sitzung abgemeldet, altes Passwort ungültig, neues mit Pflichtwechsel
    assert maria.get("/api/mandanten").status_code == 401
    assert maria.get("/", follow_redirects=False).headers["location"] == "/login"
    assert _anmelden(maria, "maria@example.com", "Startpasswort1").status_code == 401
    assert _anmelden(maria, "maria@example.com", "Neu-Gesetzt-77").headers["location"] == "/profil"


def test_eigener_passwortwechsel_meldet_andere_geraete_ab(client):
    _login(client)
    _benutzer_anlegen(client)
    geraet1, geraet2 = _zweiter_client(), _zweiter_client()
    for g in (geraet1, geraet2):
        _anmelden(g, "maria@example.com", "Startpasswort1")
    assert geraet1.post("/api/profil/passwort", json={"aktuelles_passwort": "Startpasswort1",
                                                      "neues_passwort": "Anderes-Pw-99"}).status_code == 200
    assert geraet1.get("/api/mandanten").status_code == 200
    assert geraet2.get("/api/mandanten").status_code == 401


def test_rolle_aendern_und_letzter_admin_geschuetzt(client):
    import app.database as db_modul

    _login(client)
    bid = _benutzer_anlegen(client)
    assert client.patch(f"/api/benutzer/{bid}", json={"rolle": "CHEF"}).status_code == 422
    assert client.patch(f"/api/benutzer/{bid}", json={"rolle": "LESER", "name": "Maria M."}).status_code == 200
    maria = next(b for b in client.get("/api/benutzer").json() if b["id"] == bid)
    assert (maria["rolle"], maria["name"]) == ("LESER", "Maria M.")
    with db_modul.SessionLocal() as s:
        eintrag = s.scalars(select(models.AuditLog).where(models.AuditLog.aktion == "BENUTZER_GEAENDERT")
                            .order_by(models.AuditLog.id.desc())).first()
        assert "Rolle Bearbeiter → Leser" in eintrag.detail and "Name" in eintrag.detail

    admin_id = next(b["id"] for b in client.get("/api/benutzer").json() if b["email"] == "admin@example.com")
    # einziger Admin: weder herabstufen noch sperren
    r = client.patch(f"/api/benutzer/{admin_id}", json={"rolle": "BEARBEITER"})
    assert r.status_code == 409 and "Administrator" in r.json()["detail"]
    assert client.patch(f"/api/benutzer/{admin_id}", json={"aktiv": False}).status_code == 409
    assert client.get("/api/benutzer").status_code == 200          # weiterhin Admin

    # mit zweitem Admin darf der erste herabgestuft werden
    assert client.patch(f"/api/benutzer/{bid}", json={"rolle": "ADMIN"}).status_code == 200
    assert client.patch(f"/api/benutzer/{admin_id}", json={"rolle": "BEARBEITER"}).status_code == 200
    assert client.get("/api/benutzer").status_code == 403


def test_benutzerseite_zeigt_bearbeiten(client):
    _login(client)
    _benutzer_anlegen(client)
    html = client.get("/benutzer").text
    assert "bearbeiten-dialog" in html and "Passwort zurücksetzen" in html
