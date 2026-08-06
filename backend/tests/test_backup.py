from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from app.services import backup


def test_backup_roundtrip_ueber_api(client, tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "backup_verzeichnis", str(tmp_path))
    from test_api import _login

    _login(client)
    mandant_id = client.post(
        "/api/mandanten", json={"name": "Sicherung GmbH", "kurzname": "sicherung"}
    ).json()["id"]
    client.post(
        f"/api/mandanten/{mandant_id}/posten",
        json={"art": "KREDITOR", "partner": "Ü-Umlaut & Söhne", "faellig_am": "2026-09-01",
              "betrag_brutto": 123.45},
    )

    # Sicherung erstellen und prüfen
    erg = client.post("/api/backups")
    assert erg.status_code == 201, erg.text
    name = erg.json()["name"]
    assert backup.NAME_MUSTER.match(name)
    inhalt = client.get(f"/api/backups/{name}")
    assert inhalt.status_code == 200
    archiv = ZipFile(BytesIO(inhalt.content))
    assert "manifest.json" in archiv.namelist()
    assert "tabellen/mandanten.json" in archiv.namelist()

    # Daten verändern, dann wiederherstellen
    client.patch(f"/api/mandanten/{mandant_id}", json={"name": "Umbenannt GmbH"})
    antwort = client.post(
        "/api/backups/wiederherstellen",
        files={"datei": (name, inhalt.content, "application/zip")},
        data={"bestaetigung": "WIEDERHERSTELLEN"},
    )
    assert antwort.status_code == 200, antwort.text
    assert antwort.json()["vorab_sicherung"]
    mandanten = client.get("/api/mandanten").json()
    assert any(m["name"] == "Sicherung GmbH" for m in mandanten)
    posten = client.get(f"/api/mandanten/{mandant_id}/posten").json()
    assert posten[0]["partner"] == "Ü-Umlaut & Söhne"
    assert posten[0]["betrag_brutto"] == 123.45

    # Wiederherstellung ohne Bestätigung wird abgelehnt
    antwort = client.post(
        "/api/backups/wiederherstellen",
        files={"datei": (name, inhalt.content, "application/zip")},
        data={"bestaetigung": "ja"},
    )
    assert antwort.status_code == 422

    # ungültiger Dateiname wird abgewiesen (kein Pfad-Ausbruch)
    assert client.get("/api/backups/..%2F..%2Fetc%2Fpasswd").status_code in (404, 422)
    assert client.get("/api/backups/kaputt.zip").status_code == 422


def test_backup_nur_fuer_admins(client, tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "backup_verzeichnis", str(tmp_path))
    from test_api import _login

    _login(client)
    client.post(
        "/api/benutzer",
        json={"email": "n@example.com", "name": "N", "passwort": "geheim123",
              "rolle": "BEARBEITER", "mandanten_ids": []},
    )
    client.get("/logout")
    client.post("/login", data={"email": "n@example.com", "passwort": "geheim123"})
    assert client.get("/api/backups").status_code == 403
    assert client.post("/api/backups").status_code == 403


def test_aufraeumen_nach_aufbewahrung(tmp_path):
    alt = datetime.now(timezone.utc) - timedelta(days=40)
    neu = datetime.now(timezone.utc) - timedelta(days=5)
    (tmp_path / f"liqui-backup-{alt.strftime('%Y%m%d-%H%M%S')}.zip").write_bytes(b"x")
    (tmp_path / f"liqui-backup-{neu.strftime('%Y%m%d-%H%M%S')}.zip").write_bytes(b"x")
    (tmp_path / "anderes.zip").write_bytes(b"x")  # fremde Dateien bleiben unberührt
    geloescht = backup.raeume_auf(tmp_path, 30)
    assert geloescht == 1
    verbleibend = {p.name for p in Path(tmp_path).iterdir()}
    assert f"liqui-backup-{neu.strftime('%Y%m%d-%H%M%S')}.zip" in verbleibend
    assert "anderes.zip" in verbleibend
