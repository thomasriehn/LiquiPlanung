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

    # Export im Szenario: läuft durch und Kopfzeilen-Helfer liefern den Vermerk
    szenarien = client.get(f"/api/mandanten/{mandant_id}/szenarien").json()
    worst = next(s for s in szenarien if s["name"] == "Worst Case")
    antwort = client.get(
        f"/api/mandanten/{mandant_id}/export/plan.pdf?szenario_id={worst['id']}"
    )
    assert antwort.status_code == 200
    plan = client.get(
        f"/api/mandanten/{mandant_id}/plan?szenario_id={worst['id']}"
    ).json()
    from app.services.export import _szenario_text

    assert "Worst Case" in _szenario_text(plan)
    assert "Debitoren +14 Tage" in _szenario_text(plan)


def test_szenarien_crud_und_plan(client):
    _login(client)
    mandant_id = client.post(
        "/api/mandanten", json={"name": "Szenario GmbH", "kurzname": "szen"}
    ).json()["id"]

    # Standard-Vorlagen wurden angelegt
    szenarien = client.get(f"/api/mandanten/{mandant_id}/szenarien").json()
    namen = {s["name"] for s in szenarien}
    assert {"Best Case", "Worst Case"} <= namen
    worst = next(s for s in szenarien if s["name"] == "Worst Case")

    plan = client.get(
        f"/api/mandanten/{mandant_id}/plan?szenario_id={worst['id']}"
    ).json()
    assert plan["szenario"]["name"] == "Worst Case"

    # CRUD + Validierung
    neu = client.post(
        f"/api/mandanten/{mandant_id}/szenarien",
        json={"name": "Stress", "ein_faktor": 60, "aus_faktor": 100,
              "debitoren_verzoegerung_tage": 30},
    )
    assert neu.status_code == 201
    assert client.patch(
        f"/api/szenarien/{neu.json()['id']}", json={"ein_faktor": 9999}
    ).status_code == 422
    assert client.delete(f"/api/szenarien/{neu.json()['id']}").json()["ok"] is True
    # fremdes/unbekanntes Szenario
    assert client.get(
        f"/api/mandanten/{mandant_id}/plan?szenario_id=99999"
    ).status_code == 404


def test_insolvenzgeld_einstellungen_und_vorschau(client):
    _login(client)
    mandant_id = client.post(
        "/api/mandanten",
        json={"name": "IG GmbH", "kurzname": "ig", "verfahrensstatus": "VORLAEUFIG",
              "insolvenz_stichtag": "2026-07-15"},
    ).json()["id"]
    antwort = client.patch(
        f"/api/mandanten/{mandant_id}",
        json={"insolvenzgeld_aktiv": True, "insolvenzgeld_von": "2026-06-10",
              "insolvenzgeld_bis": "2026-09-09"},
    )
    assert antwort.status_code == 200
    vorschau = client.get(
        f"/api/mandanten/{mandant_id}/insolvenzgeld/vorschau"
    ).json()
    assert vorschau["aktiv"] is True
    assert vorschau["von"] == "2026-06-10"
    plan = client.get(f"/api/mandanten/{mandant_id}/plan").json()
    assert plan["insolvenzgeld"]["aktiv"] is True


MT940_BEISPIEL = (
    ":20:STMT-1\n"
    ":25:DE89370400440532013000\n"
    ":28C:152/1\n"
    ":60F:C260803EUR41250,00\n"
    ":61:2608040804CR1190,00NTRFNONREF\n"
    ":86:166?00GUTSCHRIFT?20SVWZ+RE-100?32Kunde Albrecht AG\n"
    ":61:260805DR500,00NDDT0815\n"
    ":86:105?20SVWZ+Miete?32Vermieter GmbH\n"
    ":62F:C260805EUR41940,00\n"
    "-\n"
).encode("cp1252")


def test_bank_import_mt940(client):
    _login(client)
    mandant_id = client.post(
        "/api/mandanten", json={"name": "Bank GmbH", "kurzname": "bank"}
    ).json()["id"]
    konten = client.get(f"/api/mandanten/{mandant_id}/konten").json()["konten"]
    bank_konto = next(k for k in konten if k["nummer"] == "1200")
    # IBAN mit Leerzeichen -> wird normalisiert gespeichert
    client.patch(f"/api/konten/{bank_konto['id']}", json={"iban": "DE89 3704 0044 0532 0130 00"})

    antwort = client.post(
        f"/api/mandanten/{mandant_id}/bank-import",
        files={"datei": ("auszug.sta", MT940_BEISPIEL, "text/plain")},
    )
    assert antwort.status_code == 200, antwort.text
    erg = antwort.json()
    assert erg["format"] == "MT940"
    assert erg["anzahl"] == 2
    assert erg["bestandsanker"][0]["datum"] == "2026-08-05"
    assert erg["bestandsanker"][0]["wert"] == 41940.0

    umsaetze = client.get(f"/api/mandanten/{mandant_id}/bank-umsaetze").json()
    assert len(umsaetze) == 2
    assert umsaetze[0]["partner"] == "Vermieter GmbH"  # neuester zuerst

    # Endsaldo wirkt als Bestandsanker in der Planung
    plan = client.get(f"/api/mandanten/{mandant_id}/plan").json()
    bank_zeile = next(
        f for f in plan["bestaende"]["finanzkonten"] if f["nummer"] == "1200"
    )
    assert bank_zeile["anker"] == {"datum": "2026-08-05", "wert": 41940.0}
    assert plan["bestaende"]["liquiditaet"][plan["heute"]] == 41940.0

    # Re-Import: Dubletten-Warnung; Anker wird aktualisiert, nicht dupliziert
    antwort = client.post(
        f"/api/mandanten/{mandant_id}/bank-import",
        files={"datei": ("auszug.sta", MT940_BEISPIEL, "text/plain")},
    )
    assert any("Dubletten" in w for w in antwort.json()["warnungen"])
    bestaende = client.get(f"/api/mandanten/{mandant_id}/bestaende").json()
    assert len([b for b in bestaende if b["datum"] == "2026-08-05"]) == 1

    # Import löschen entfernt die Bankumsätze des Batches
    client.delete(f"/api/importe/{erg['batch_id']}")
    umsaetze = client.get(f"/api/mandanten/{mandant_id}/bank-umsaetze").json()
    assert len(umsaetze) == 2  # die des zweiten Imports bleiben


def test_bank_import_ohne_zuordnung(client):
    _login(client)
    mandant_id = client.post(
        "/api/mandanten", json={"name": "Bank2 GmbH", "kurzname": "bank2"}
    ).json()["id"]
    # keine IBAN hinterlegt, aber zwei Finanzkonten (Bank + Kasse) -> Warnung, 0 Umsätze
    antwort = client.post(
        f"/api/mandanten/{mandant_id}/bank-import",
        files={"datei": ("auszug.sta", MT940_BEISPIEL, "text/plain")},
    )
    erg = antwort.json()
    assert erg["anzahl"] == 0
    assert any("übersprungen" in w for w in erg["warnungen"])
    # mit expliziter Kontoauswahl klappt es
    konten = client.get(f"/api/mandanten/{mandant_id}/konten").json()["konten"]
    bank_konto = next(k for k in konten if k["nummer"] == "1200")
    antwort = client.post(
        f"/api/mandanten/{mandant_id}/bank-import?konto_id={bank_konto['id']}",
        files={"datei": ("auszug.sta", MT940_BEISPIEL, "text/plain")},
    )
    assert antwort.json()["anzahl"] == 2


def test_szenarien_vergleich_endpoint(client):
    _login(client)
    mandant_id = client.post(
        "/api/mandanten", json={"name": "Vergleich AG", "kurzname": "vgl"}
    ).json()["id"]
    v = client.get(f"/api/mandanten/{mandant_id}/szenarien-vergleich").json()
    namen = [s["name"] for s in v["szenarien"]]
    assert namen[0] == "Basisplan"
    assert {"Best Case", "Worst Case"} <= set(namen)
    assert len(v["szenarien"][0]["liquiditaet_wochenende"]) == len(v["wochen"])
    # Seite rendert
    assert client.get(f"/mandanten/{mandant_id}/szenarien").status_code == 200


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
