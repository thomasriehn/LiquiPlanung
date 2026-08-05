import pytest
from fastapi.testclient import TestClient

import app.main as hauptmodul
from app.database import Base
import app.database as db_modul


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(
        f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False}
    )
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_modul, "engine", engine)
    monkeypatch.setattr(db_modul, "SessionLocal", Session)
    monkeypatch.setattr(hauptmodul, "engine", engine)
    monkeypatch.setattr(hauptmodul, "SessionLocal", Session)
    Base.metadata.create_all(engine)

    with TestClient(hauptmodul.app) as c:
        yield c
    engine.dispose()


def _login(client):
    antwort = client.post(
        "/login", data={"email": "admin@example.com", "passwort": "admin"},
        follow_redirects=False,
    )
    assert antwort.status_code == 303


def test_login_und_mandant_workflow(client):
    # ohne Login: API 401, Seite -> Redirect
    assert client.get("/api/mandanten").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303

    _login(client)
    assert client.get("/api/mandanten").json() == []

    antwort = client.post(
        "/api/mandanten",
        json={"name": "Testfirma GmbH", "kurzname": "testfirma", "kontenrahmen": "SKR04",
              "bundesland": "BY", "verfahrensstatus": "EROEFFNET"},
    )
    assert antwort.status_code == 201, antwort.text
    mandant_id = antwort.json()["id"]

    konten = client.get(f"/api/mandanten/{mandant_id}/konten").json()
    nummern = {k["nummer"] for k in konten["konten"]}
    assert {"1800", "4400", "6110", "3820"} <= nummern
    k4400 = next(k for k in konten["konten"] if k["nummer"] == "4400")
    assert k4400["ust_satz"] == 19.0

    plan = client.get(f"/api/mandanten/{mandant_id}/plan").json()
    assert len(plan["tage"]) == 13 * 7
    assert plan["vergleichsbasis"] == "LIVE"

    # Termine generieren und im Plan sehen
    regeln = client.get(f"/api/mandanten/{mandant_id}/termin-regeln").json()
    sv = next(r for r in regeln if r["typ"] == "SV")
    client.patch(f"/api/termin-regeln/{sv['id']}", json={"betrag_modus": "FIX", "betrag_fix": 5000})
    erg = client.post(f"/api/mandanten/{mandant_id}/termine/generieren", json={}).json()
    assert erg["angelegt"] >= 3

    snapshot = client.post(f"/api/mandanten/{mandant_id}/plan/snapshot", json={})
    assert snapshot.status_code == 201
    sollist = client.get(f"/api/mandanten/{mandant_id}/sollist").json()
    assert sollist["snapshot"]["id"] == snapshot.json()["id"]


def test_import_datev(client):
    _login(client)
    mandant_id = client.post(
        "/api/mandanten", json={"name": "Import AG", "kurzname": "import-ag"}
    ).json()["id"]

    extf = (
        '"EXTF";700;21;"Buchungsstapel";12;;;"";"";"";1;1;20260101;4;20260801;20260831;"";"";1\n'
        "Umsatz (ohne Soll/Haben-Kz);Soll/Haben-Kennzeichen;WKZ;Kurs;Basis;WKZ2;Konto;"
        "Gegenkonto (ohne BU-Schlüssel);BU;Belegdatum;Belegfeld 1;Belegfeld 2;Skonto;Buchungstext\n"
        '1.190,00;"S";;;;;1200;8400;;0308;"RE-1";;;"Zahlungseingang"\n'
    ).encode("cp1252")
    antwort = client.post(
        f"/api/mandanten/{mandant_id}/import",
        files={"datei": ("buchungen.csv", extf, "text/csv")},
    )
    assert antwort.status_code == 200, antwort.text
    erg = antwort.json()
    assert erg["format"] == "DATEV"
    assert erg["anzahl"] == 1


def test_mandantentrennung(client):
    _login(client)
    m1 = client.post("/api/mandanten", json={"name": "A", "kurzname": "a"}).json()["id"]

    # Bearbeiter ohne Mandantenzuordnung anlegen
    client.post(
        "/api/benutzer",
        json={"email": "b@example.com", "name": "B", "passwort": "geheim123",
              "rolle": "BEARBEITER", "mandanten_ids": []},
    )
    client.get("/logout")
    client.post("/login", data={"email": "b@example.com", "passwort": "geheim123"})
    assert client.get("/api/mandanten").json() == []
    assert client.get(f"/api/mandanten/{m1}/plan").status_code == 403
    # keine Admin-Funktionen
    assert client.get("/api/benutzer").status_code == 403


def test_forderungsklasse_automatik(client):
    _login(client)
    mandant_id = client.post(
        "/api/mandanten",
        json={"name": "InsO GmbH", "kurzname": "inso", "verfahrensstatus": "VORLAEUFIG",
              "insolvenz_stichtag": "2026-07-15"},
    ).json()["id"]

    # Rechnungsdatum vor dem Stichtag -> Insolvenzforderung
    alt = client.post(
        f"/api/mandanten/{mandant_id}/posten",
        json={"art": "KREDITOR", "partner": "Alt", "rechnungsdatum": "2026-07-01",
              "faellig_am": "2026-08-20", "betrag_brutto": 5000},
    ).json()
    assert alt["forderungsklasse"] == "INSOLVENZFORDERUNG"
    # nach dem Stichtag -> Masse
    neu = client.post(
        f"/api/mandanten/{mandant_id}/posten",
        json={"art": "KREDITOR", "partner": "Neu", "rechnungsdatum": "2026-07-20",
              "faellig_am": "2026-08-20", "betrag_brutto": 700},
    ).json()
    assert neu["forderungsklasse"] == "MASSE"
    # explizite Angabe hat Vorrang; ungültige Klasse wird abgelehnt
    antwort = client.post(
        f"/api/mandanten/{mandant_id}/posten",
        json={"art": "KREDITOR", "partner": "X", "rechnungsdatum": "2026-07-01",
              "faellig_am": "2026-08-20", "betrag_brutto": 1,
              "forderungsklasse": "QUATSCH"},
    )
    assert antwort.status_code == 422
    # gesperrte Forderung erscheint nicht im Plan, aber im Sperr-Ausweis
    plan = client.get(f"/api/mandanten/{mandant_id}/plan").json()
    assert plan["gesperrte_insolvenzforderungen"] == 5000.0


def test_export_xlsx_und_pdf(client):
    from io import BytesIO

    from openpyxl import load_workbook

    _login(client)
    mandant_id = client.post(
        "/api/mandanten", json={"name": "Export AG", "kurzname": "export-ag"}
    ).json()["id"]
    client.post(f"/api/mandanten/{mandant_id}/plan/snapshot", json={})

    antwort = client.get(f"/api/mandanten/{mandant_id}/export/plan.xlsx")
    assert antwort.status_code == 200
    assert "spreadsheetml" in antwort.headers["content-type"]
    wb = load_workbook(BytesIO(antwort.content))
    assert {"Info", "Tage", "Wochen", "Soll-Ist"} <= set(wb.sheetnames)
    assert wb["Info"]["B2"].value == "Export AG"

    antwort = client.get(f"/api/mandanten/{mandant_id}/export/plan.pdf")
    assert antwort.status_code == 200
    assert antwort.headers["content-type"] == "application/pdf"
    assert antwort.content.startswith(b"%PDF")


def test_leser_darf_nicht_schreiben(client):
    _login(client)
    m1 = client.post("/api/mandanten", json={"name": "C", "kurzname": "c"}).json()["id"]
    client.post(
        "/api/benutzer",
        json={"email": "l@example.com", "name": "L", "passwort": "geheim123",
              "rolle": "LESER", "mandanten_ids": [m1]},
    )
    client.get("/logout")
    client.post("/login", data={"email": "l@example.com", "passwort": "geheim123"})
    assert client.get(f"/api/mandanten/{m1}/plan").status_code == 200
    antwort = client.post(
        f"/api/mandanten/{m1}/posten",
        json={"art": "KREDITOR", "partner": "X", "faellig_am": "2026-09-01",
              "betrag_brutto": 100},
    )
    assert antwort.status_code == 403
