# LiquiPlanung

Mandantenfähige 13-Wochen-Liquiditätsplanung für Insolvenzbuchhalter – Web-Anwendung als
Ablösung der Excel-Planung. Fachkonzept und Architektur: **[KONZEPT.md](KONZEPT.md)** ·
Betrieb auf Proxmox/LXC: **[deploy/PROXMOX_LXC.md](deploy/PROXMOX_LXC.md)**

## Funktionsumfang

- **Rollierende 13-Wochen-Matrix**: Spalten = Tage (umschaltbar auf Wochen), Zeilen =
  Konten mit BWA-Aggregationsknoten; Ist bis heute, Plan ab morgen, Abweichungsansicht.
- **Soll-/Ist-Vergleich** gegen eingefrorene Plan-Snapshots („Plan einfrieren“).
- **Bestände & Liquiditätsentwicklung**: je Bank-/Kassenkonto, Gesamtliquidität,
  verfügbare Liquidität inkl. Kreditlinien, Warenbestand nachrichtlich.
- **Import**: DATEV-Buchungsstapel (EXTF/DTVF), generisches CSV (Addison abbildbar),
  BWA-/Saldenlisten; Meldung nicht angelegter Konten.
- **Kontenrahmen** SKR03/SKR04 als editierbare Vorlage je Mandant, USt-Satz je Konto.
- **Planbasis**: offene Eingangsrechnungen/Forderungen, Dauerverbindlichkeiten,
  Budget je Konto/Monat (Restbudget-Logik, Netto→Brutto über USt-Satz).
- **Zahlungskalender**: SV-Beiträge (drittletzter Bankarbeitstag, Feiertage je
  Bundesland), USt-VA (Monat/Quartal, Dauerfrist), LSt, GewSt, KSt – Beträge fest oder
  aus Historie geschätzt, Termine einzeln anpassbar.
- **Insolvenzspezifisch**: Forderungsklassen je Posten (§ 38 / § 55 InsO, Aus-/Absonderung)
  mit Zahlungssperre für Insolvenzforderungen und automatischer Vorbelegung anhand des
  Insolvenz-Stichtags; gesperrte Summen werden nachrichtlich ausgewiesen.
- **Insolvenzgeld-Assistent**: Zeitraum je Mandant (Vorschlag: 3 Monate vor erwarteter
  Eröffnung); unterdrückt Personal-/SV-Zahlungen und kürzt SV-/LSt-Termine anteilig
  (§§ 165 ff., 175 SGB III), mit Entlastungsvorschau je Position.
- **Szenarien (Best/Base/Worst)**: Faktoren auf Einzahlungen und variable Auszahlungen,
  Debitorenverzögerung; umschaltbar auf der Plan-Seite, im Export vermerkt; Soll/Ist
  bleibt auf dem Basisplan.
- **Export**: 13-Wochen-Plan als Excel-Arbeitsmappe (Tage, Wochen mit Plan/Ist/Δ,
  Soll-Ist) und als PDF-Bericht für Gericht, Sachwalter, Gläubigerausschuss.
- **Mehrbenutzerbetrieb**: Rollen Admin/Bearbeiter/Leser, Mandantenzuordnung, Audit-Log.

## Schnellstart (Docker)

```bash
cp .env.example .env   # Passwörter/SECRET_KEY setzen
docker compose up -d --build
# http://localhost:8000 – Login mit ADMIN_EMAIL/ADMIN_PASSWORD aus .env
```

Beim ersten Start wird ein Demo-Mandant „Muster GmbH (Demo)“ angelegt (abschaltbar über
`DEMO_DATEN=false`).

## Entwicklung

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
DATABASE_URL=sqlite:///dev.db uvicorn app.main:app --reload
pytest
```

Ohne `DATABASE_URL` erwartet die Anwendung PostgreSQL unter
`postgresql+psycopg://liqui:liqui@localhost:5432/liqui`. API-Dokumentation: `/api/docs`.

## Projektstruktur

```
backend/app
├── main.py            App-Assemblierung, Startinitialisierung (Schema, Admin, Demo)
├── models.py          SQLAlchemy-Datenmodell (Mandanten, Konten, Buchungen, Plandaten …)
├── security.py        Login, Rollen, Mandantenberechtigung, Audit
├── routers/           api.py (JSON-API), pages.py (Seiten), auth.py (Login)
├── services/
│   ├── feiertage.py         Feiertage je Bundesland, Bankarbeitstage
│   ├── zahlungskalender.py  SV-/Steuertermine, Termin-Generierung
│   ├── datev.py             DATEV-/CSV-/BWA-Parser
│   ├── liquiditaet.py       13-Wochen-Engine, Snapshots, Soll/Ist
│   ├── kontenrahmen.py      SKR03/SKR04-Vorlagen inkl. USt und BWA-Gruppen
│   └── demo.py              Demo-Mandant
├── templates/ + static/     Oberfläche (Jinja2 + Vanilla-JS)
└── tests/                   pytest (Kalender, Parser, Engine, API, Berechtigungen)
```
