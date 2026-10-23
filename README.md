# LiquiPlanung

Mandantenfähige 13-Wochen-Liquiditätsplanung für Insolvenzbuchhalter – Web-Anwendung als
Ablösung der Excel-Planung. Fachkonzept und Architektur: **[KONZEPT.md](KONZEPT.md)** ·
Betrieb auf Proxmox/LXC: **[deploy/PROXMOX_LXC.md](deploy/PROXMOX_LXC.md)**

## Funktionsumfang

- **Rollierende 13-Wochen-Matrix**: Spalten = Tage (umschaltbar auf Wochen oder Monate), Zeilen =
  Konten mit BWA-Aggregationsknoten; Ist bis heute, Plan ab morgen, Abweichungsansicht.
  Der gewählte Stichtag (Fensterstart) bleibt beim Seitenwechsel innerhalb des
  Mandanten erhalten (auch für die Szenario-Vergleichsansicht).
- **Vortrag nicht gebuchter Dauerschuld-Zahlungen** (Werteauswahl „Ist bis heute,
  Plan ab morgen mit Vortrag“): Ist eine fällige Rate eines Dauerschuldverhältnisses
  nicht (vollständig) gebucht, wird der offene Rest auf den nächsten Bankarbeitstag
  ab morgen vorgetragen – farbig hervorgehoben, schraffiert wenn er mit einer
  regulären Planbuchung desselben Kontos zusammenfällt (Werte addiert); die
  Liquiditätsfortschreibung berücksichtigt den Vortrag. Ist-Zahlungen auf dem
  Konto werden angerechnet: betragsgleiche Zahlungen um die Fälligkeit zuerst,
  danach chronologisch auch Teil- und Vorauszahlungen. Je Dauerschuldverhältnis
  abschaltbar (Standard: vortragen; auch per CSV-Spalte „Vortrag“).
- **Soll-/Ist-Vergleich** gegen eingefrorene Plan-Snapshots („Plan einfrieren“,
  Stichtag = Wochenbeginn des gewählten Fensterstarts). Die **Soll-Referenz** ist
  wählbar (auf der Plan- und der Soll/Ist-Seite, serverseitig gespeichert und damit
  für alle Benutzer, die Abweichungsansicht und den Excel-Export gleich);
  eingefrorene Planstände lassen sich löschen.
- **Bestände & Liquiditätsentwicklung**: je Bank-/Kassenkonto, Gesamtliquidität,
  verfügbare Liquidität inkl. Kreditlinien, Warenbestand nachrichtlich.
- **Import**: DATEV-Buchungsstapel (EXTF/DTVF), generisches CSV (Addison abbildbar),
  BWA-/Saldenlisten, OP-Listen und Dauerschuldverhältnisse (CSV mit toleranter Kopfzeile,
  Duplikatprüfung); Meldung nicht angelegter Konten. In „Bisherige Importe“ öffnet
  „editieren“ die Satzliste jedes Imports (einzeln korrigier- und löschbar;
  Zahlungsbuchungen geben beim Löschen ihre Anrechnung an den Posten zurück),
  und „ansehen“ stellt bei PDF-Importen die Zuordnungsansicht jederzeit wieder
  her – mit den getroffenen Zuordnungen, die sich dort aufheben oder nachholen
  lassen (die PDF-Datei wird dazu am Import gespeichert). Die Kontoauswahl der
  Abstimmung ist durchsuchbar und nach BWA-Gruppen gegliedert; Ausgaben lassen
  sich optional auf mehrere Konten aufteilen (Teilbeträge müssen den
  Umsatzbetrag ergeben).
- **PDF-Kontoumsätze**: Kontoauszüge, die nur als PDF vorliegen, werden
  ausgewertet (automatische Formaterkennung: Sparkassen-Verfahrenskonto-Reports,
  Deutsche-Bank-Kontoauszüge, UC eBanking prime/HypoVereinsbank). Ausgaben
  erscheinen als Tabelle mit Kontoauswahl – die Auswahl verbucht den Betrag
  sofort zum Buchungsdatum als Ist-Buchung; Zahlungseingänge werden in einem
  zweiten Block offenen Posten zugewiesen (Anrechnung inkl. Teilzahlung und
  Sammelposten-Vortrag plus Ist-Buchung) – außerordentliche Zahlungen ohne
  offenen Posten lassen sich dort alternativ über die Kontosuche direkt auf
  ein Konto verbuchen (auch aufgeteilt). Jede Zuordnung ist über das ✕ am
  grünen Vermerk einzeln aufhebbar, ganze PDFs gesammelt über den Import.
  Bei sich überschneidenden Auszugszeiträumen werden bereits verbuchte
  Umsätze ausgegraut und sind nicht erneut verbuchbar (serverseitig gesperrt).
  PDFs ohne Textebene (Scan bzw. „Als PDF drucken“) werden per Texterkennung
  (tesseract) gelesen und gegen Anfangs-/Endsaldo geprüft. **ZIP-Pakete** aus
  Kontoauszug und UC-eBanking-Postenlisten lösen Sammelüberweisungen in die
  Einzelzahlungen je Empfänger auf, die sich dann einzeln Konten zuordnen
  lassen; bucht die Bank die Posten bereits einzeln, werden die Auszugszeilen
  mit Empfänger und Verwendungszweck der Postenliste abgeglichen. Mehrseitige
  Auszüge, SEPA-Felder (nur der eigentliche Verwendungszweck wird übernommen)
  und beigefügte Belege (werden übergangen) sind berücksichtigt.
- **Bankimport (MT940/CAMT.053)**: Kontoauszüge liefern tagesaktuelle Bestandsanker
  (Endsaldo je Auszug, IBAN-Zuordnung) und Bankumsätze zur Referenz – unabhängig vom
  Buchhaltungsexport, ohne Doppelzählung im Ist.
- **Automatischer OP-Ausgleich**: Scoring-basierte Zuordnungsvorschläge Bankumsatz ↔
  offener Posten (Betrag/Skonto, Belegnummer, Partner, Datumsnähe); Übernahme setzt
  Posten auf „bezahlt“ und verknüpft den Umsatz, jederzeit aufhebbar. Teilzahlungen
  (nur bei starkem Signal, nie vorausgewählt) lassen den Posten mit Restbetrag offen –
  die Planung rechnet automatisch mit dem Rest. Sammelüberweisungen (1:n) werden
  erkannt, wenn eine Postenkombination die Zahlung exakt ergibt.
- **Manuelle Zahlungserfassung** je Posten (Betrag/Datum): Teilzahlung mit
  Restbetrag im Plan, optional Rest ausbuchen (Skonto/Erlass) und auf Wunsch
  direkt als Ist-Buchung (Konto des Postens gegen das gewählte Zahlungsmittel;
  abwählbar, wenn die Zahlung ohnehin über den Buchhaltungsimport kommt). **Sammelposten**
  bilden erwartete Sammelzahlungseingänge mehrerer Debitoren ab (z. B. je
  Kalenderwoche, auch per OP-CSV-Import über die Spalte „Sammel“); Überzahlungen
  werden automatisch auf die nächsten offenen Sammelposten vorgetragen – spätere
  geplante Einzahlungen sinken entsprechend.
  Offene Posten und Dauerschuldverhältnisse sind vollständig bearbeitbar.
- **Integrierte Sicherungen**: automatische Voll-Backups (Intervall/Aufbewahrung
  konfigurierbar, Docker-Volume), manuelle Sicherung, Download und geführte
  Wiederherstellung in der Admin-Oberfläche – datenbankunabhängig (PostgreSQL/SQLite),
  an die Schema-Revision gebunden, mit automatischer Vorab-Sicherung.
- **Kontenrahmen** SKR03/SKR04 als editierbare Vorlage je Mandant, USt-Satz je Konto;
  Administratoren definieren eigene Kontenrahmen-Vorlagen (neu oder als Kopie),
  BWA-Gruppen sind je Mandant frei anpassbar.
- **Planbasis**: offene Eingangsrechnungen/Forderungen, Dauerverbindlichkeiten,
  Budget je Konto/Monat (Restbudget-Logik, Netto→Brutto über USt-Satz) mit
  Verteilungsprofil je Konto (gleichmäßig, Monatsanfang/-mitte/-ende,
  wöchentlicher Zahllauf z. B. freitags).
- **Zahlungskalender**: SV-Beiträge (drittletzter Bankarbeitstag, Feiertage je
  Bundesland), USt-VA (Monat/Quartal, Dauerfrist), LSt, GewSt, KSt – Beträge fest,
  aus Historie geschätzt oder (USt-VA) als Zahllast-Vorschau aus dem Budget;
  Termine einzeln anpassbar.
- **Insolvenzspezifisch**: Forderungsklassen je Posten (§ 38 / § 55 InsO, Aus-/Absonderung)
  mit Zahlungssperre für Insolvenzforderungen und automatischer Vorbelegung anhand des
  Insolvenz-Stichtags; gesperrte Summen werden nachrichtlich ausgewiesen.
- **Insolvenzgeld-Assistent**: Zeitraum je Mandant (Vorschlag: 3 Monate vor erwarteter
  Eröffnung); unterdrückt Personal-/SV-Zahlungen und kürzt SV-/LSt-Termine anteilig
  (§§ 165 ff., 175 SGB III), mit Entlastungsvorschau je Position.
- **Szenarien (Best/Base/Worst)**: Faktoren auf Einzahlungen und variable Auszahlungen,
  Debitorenverzögerung; umschaltbar auf der Plan-Seite, im Export vermerkt; Soll/Ist
  bleibt auf dem Basisplan. Detailregeln je Konto/BWA-Gruppe übersteuern die globalen
  Faktoren. Eigene **Vergleichsansicht** mit allen Szenarien
  nebeneinander (Liquidität je Wochenende, Netto-Cashflow, minimale Liquidität).
- **Export**: 13-Wochen-Plan als Excel-Arbeitsmappe (Tage, Wochen und Monate mit
  Plan/Ist/Δ gegen die Soll-Referenz, Soll-Ist) und als PDF-Bericht für Gericht,
  Sachwalter, Gläubigerausschuss – in der gewählten Spaltenansicht (Tage/Wochen/
  Monate) und Werteauswahl; breite Pläne (z. B. 26 Wochen) laufen nach rechts auf
  Folgeblätter weiter (13 Wochen bzw. 14 Tage je Blatt, Kontospalte wiederholt).
- **Mandantenverwaltung**: Neue Mandanten wahlweise per Formular oder über den
  **Onboarding-Wizard** („Mit Wizard anlegen“) – acht geführte Schritte von den
  Stammdaten über Bankverbindung (IBAN) und Anfangssalden bis zu den Importen
  (OP-Liste, Dauerschuldverhältnisse, optional Bankdaten, Buchungsstapel, BWA).
  Admins löschen Mandanten samt aller Daten endgültig
  (Bestätigung über den Kurznamen; Audit-Log bleibt erhalten).
- **Mehrbenutzerbetrieb**: Rollen Admin/Bearbeiter/Leser, Mandantenzuordnung, Audit-Log,
  Profil mit Passwortwechsel und optionaler Zwei-Faktor-Anmeldung (TOTP mit
  QR-Einrichtung; Admin-Reset bei Geräteverlust).

## Schnellstart (Docker)

```bash
cp .env.example .env   # Passwörter/SECRET_KEY setzen
docker compose up -d --build
# http://localhost:8000 – Login mit ADMIN_EMAIL/ADMIN_PASSWORD aus .env
```

Beim ersten Start wird ein Demo-Mandant „Muster GmbH (Demo)“ angelegt (abschaltbar über
`DEMO_DATEN=false`).

## Testdaten

Unter [`testdaten/`](testdaten/ANLEITUNG.md) liegt ein vollständiger, in sich
konsistenter Datensatz zum Einspielen bei einem neuen Mandanten („Nordlicht
Möbelwerk GmbH“): drei Monate DATEV-Buchungsstapel auf allen SKR03-Konten,
BWA-Historie, OP-Liste (mit automatischer § 38-Einstufung), Kontoauszüge
MT940/CAMT.053 mit Zahlungen passend zu den offenen Posten (exakt, Teilzahlung,
Sammelüberweisung, Skonto). `testdaten/erzeuge_testdaten.py --stichtag …`
erzeugt den Satz für ein beliebiges Datum neu, inklusive Ergebnis-Übersicht.

## Entwicklung

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
DATABASE_URL=sqlite:///dev.db uvicorn app.main:app --reload
pytest
```

Für die Texterkennung von Kontoauszügen ohne Textebene wird lokal
`tesseract-ocr` mit deutschen Sprachdaten benötigt (`apt install tesseract-ocr
tesseract-ocr-deu`; im Docker-Image enthalten) – ohne tesseract laufen alle
übrigen Funktionen, die OCR-Tests werden übersprungen.

Ohne `DATABASE_URL` erwartet die Anwendung PostgreSQL unter
`postgresql+psycopg://liqui:liqui@localhost:5432/liqui`. API-Dokumentation: `/api/docs`.

**Schemaänderungen** laufen über Alembic (`backend/migrations`): Beim Anwendungsstart
wird automatisch `alembic upgrade head` ausgeführt (Bestandsdatenbanken aus der Zeit
vor Alembic werden beim ersten Start übernommen). Nach Modelländerungen:

```bash
cd backend && alembic revision --autogenerate -m "Beschreibung"
```

Ein Test (`tests/test_migrationen.py`) schlägt fehl, wenn Modelle und Migrationen
auseinanderlaufen.

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
