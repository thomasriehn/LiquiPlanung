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
- **Zellendialog im Plan** (Klick auf eine Kontozelle):
  - **Verschieben** von geplanten Werten und Vorträgen in einen anderen Zeitraum –
    in der Tagesansicht auf einen Tag, in der Wochen-/Monatsansicht auf den ersten
    Bankarbeitstag der gewählten Woche bzw. des Monats. Der Betrag ist frei
    wählbar (Standard: Gesamtbetrag), z. B. bleiben von 6.000 € 2.000 € stehen und
    4.000 € wandern in eine spätere Woche. Verschobene Zellen tragen eine
    orange Eckmarke (Tooltip mit Beträgen); jede Verschiebung ist im Dialog mit
    Kommentar und Benutzer aufgeführt und per ✕ zurücknehmbar. Verschiebungen
    sind auf den tatsächlich geplanten Betrag begrenzt (fällt die Planbasis weg,
    entsteht kein Phantombetrag); verschobene Dauerschuld-Raten werden für den
    Vortrag erst am Zieltag fällig, ein verschobener Vortrag gilt bis zu seinem
    Zieltag. Eingefrorene Planstände übernehmen die Verschiebungen.
  - **Ist-Buchungen** einer Zelle bis heute mit Text, Beleg, Zahlungsmittel und
    Import-Herkunft; die Kontoverknüpfung lässt sich per ✕ lösen und über dieselbe
    durchsuchbare, nach BWA-Gruppen gegliederte Kontoauswahl wie beim PDF-Import
    neu festlegen (die Sachkontoseite wird umgebucht, die Bankseite bleibt).
- **Kontoauswahl** (durchsuchbar, nach BWA-Gruppen gegliedert) einheitlich im
  PDF-Import, im Ist-Buchungsdialog und in den Bearbeitungsformularen der
  offenen Posten und Dauerschuldverhältnisse.
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
- **Budgetplanung im Aufbau des 13-Wochen-Plans**: BWA-Gruppen (auf-/zuklappbar)
  mit ihren Konten, Tages-, Wochen- oder Monatsspalten im Fenster des Plans und
  Summenzeilen (Einzahlungen, Auszahlungen, Saldo; netto). Ein Klick auf eine
  Kontozelle öffnet den **Eingabedialog**: Gültigkeit (Monat bzw. nur diese KW / nur
  dieser Tag), Betrag, **Aufteilung je KW** (Standard: eine Zahlung je Kalenderwoche
  am Wochentag laut Konto bzw. am ersten Bankarbeitstag), je Tag (gleichmäßig auf die
  Bankarbeitstage) oder laut Verteilungsprofil des Kontos, und **Fortschreiben in die
  Folgemonate** (Standard an; gilt bis zum nächsten erfassten Monatswert, spätere
  Monatswerte lassen sich dabei ersetzen). Wochen- bzw. Tageswerte übersteuern den
  verteilten Monatswert (Vorrang Tag vor Woche vor Monat, 0 = kein Budget). In der
  Matrix sind erfasste Werte fett, fortgeschriebene kursiv, übersteuerte markiert.
  Die Spaltenwahl (Tage/Wochen/Monate) wird je Benutzer gespeichert und gilt gemeinsam
  für den 13-Wochen-Plan und das Budget – auch auf anderen Geräten.
- **Bestellungen und Bestellfreigabe** (Menüpunkt „Bestellungen“):
  - **Erfassung** per Datei-Upload (PDF, Bild, Word, Excel, E-Mail, Text), aus der
    Belegablage des Mandanten („Aus Ablage wählen“) oder rein manuell: Kreditor,
    Bestellnummer, Bestell- und Lieferdatum, **Nettobetrag** mit USt-Satz (Vorgabe
    laut Konto, Brutto wird angezeigt), Konto über die durchsuchbare Kontoauswahl und
    **erwartetes Zahldatum** (mit KW). Ist ein KI-Zugang hinterlegt, liest ein
    Sprachmodell die Angaben aus der Datei aus (Text, bei Scans per Texterkennung
    bzw. optional als Seitenbild). Ausgelesene Felder sind markiert und zu prüfen.
    Das Konto wird bevorzugt aus der letzten Bestellung desselben Kreditors
    übernommen. Ohne Zahlungsziel gilt das Liefer- bzw. Bestelldatum plus 30 Tage
    (nächster Bankarbeitstag).
  - **Ablage**: Hochgeladene Dateien landen im gewählten Ordner des Mandanten, lokal
    auf dem Server oder in einer SharePoint-Dokumentbibliothek. Die Datei ist über
    📄 im Browser anzeigbar; SharePoint-Dateien laufen über einen Proxy der
    Anwendung, damit kein SharePoint-Zugang im Browser nötig ist.
  - **Freigabe je KW**: Für die gewählte Kalenderwoche wird die Bestellung (brutto)
    dem verfügbaren Rahmen gegenübergestellt. Der Rahmen ist der Plan der KW ab
    heute laut Soll-Referenz, abzüglich der bereits gebundenen Zahlungen der KW
    (freigegebene Bestellungen, offene Rechnungen, Dauerschuldverhältnisse,
    Steuer-/SV-Termine) und abzüglich der bisherigen Abweichung Ist gegen Plan bis
    gestern. In die Abweichung zählen auch die Vorträge nicht gezahlter Raten und
    überfällige Posten. Ausgewiesen wird das für das Konto, seine BWA-Gruppe und
    alle Auszahlungen, dazu die Liquidität zum Ende der KW vor und nach Freigabe.
    Basis wählbar: Soll-Referenz bzw. automatisch, ein bestimmter Planstand oder der
    aktuelle Plan. Freigabe oder Ablehnung (mit Pflichtbegründung) erfolgt durch
    Benutzer mit Freigaberecht.
  - **Journal**: Erfassung, Änderungen, Freigabe, Ablehnung, Rücknahme, Ablösung
    und Storno werden mit Benutzer, Zeitpunkt, Begründung und den bei der
    Entscheidung gezeigten Kennzahlen protokolliert. Das Journal ist
    unveränderlich und bleibt auch nach dem Löschen einer Bestellung erhalten.
  - **Plan**: Bestellungen fließen brutto zum erwarteten Zahldatum als Auszahlung
    in die Liquiditätsplanung ein und verbrauchen dabei das Restbudget des Kontos,
    statt es zu verdoppeln. Überfällige Bestellungen rollen wie offene Posten nach
    vorn. Noch nicht freigegebene Bestellungen fließen ebenfalls ein (vorsichtige
    Planung, je Mandant abschaltbar), abgelehnte und stornierte nie.
  - **Ablösung**: Eine Bestellung wird durch die tatsächliche Ausgangszahlung
    (Ist-Buchung oder Bankumsatz), durch die Eingangsrechnung (offener Posten
    übernimmt die Planung) oder manuell abgelöst. Der Zahlbetrag darf abweichen,
    z. B. bei einer Minderlieferung; die Abweichung wird ausgewiesen. Teilzahlungen
    lassen den Rest geplant. Der Zahlungsabgleich schlägt passende Zahlungen vor
    (Kreditor, Bestellnummer, Konto, Betrag, Datum). Wird die zugeordnete Buchung
    gelöscht, lebt die Bestellung im Plan wieder auf.
- **Systemeinstellungen** (Administratoren, Menüpunkt „System“): SharePoint-Anbindung
  über Microsoft Graph (Tenant-ID, Anwendungs-/Client-ID, Client-Secret) und
  KI-Zugang mit OpenAI-kompatibler Schnittstelle (API-URL, Modell, API-Key,
  optional Vision), jeweils mit Verbindungstest. Geheimnisse werden verschlüsselt
  gespeichert (Schlüssel aus `GEHEIMNIS_SCHLUESSEL` bzw. `SECRET_KEY`) und nie
  angezeigt; alternativ kommen sie aus der Umgebung (`GRAPH_CLIENT_SECRET`,
  `LLM_API_KEY`). Je Mandant wählt der Administrator die Belegablage in den
  Einstellungen: einen lokalen Ordner oder einen SharePoint-Ordner über einen
  Auswahldialog (Website → Dokumentbibliothek → Ordner, auch per Website-URL oder
  manueller Drive-/Ordner-ID).
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
- **KPI-Seite (Fact Sheet)**: Kennzahlen-Kacheln (Liquidität heute und zum Planende,
  tiefster Stand, Reichweite bis zur Unterdeckung, Ist gegen Soll der letzten Woche,
  Cash-Marge mit/ohne Sondereffekte, offene Forderungen/Verbindlichkeiten) und
  Diagramme: Liquidität je Woche Soll (Soll-Referenz) gegen Ist/Prognose,
  Monatsendliquidität, Netto-Cashflow sowie Ein-/Auszahlungen je Monat,
  **Planvergleich** gegen einen wählbaren früheren Planstand (z. B. Vorwoche oder
  Plan zum Antrag: Veränderung je Kategorie und Monat, Monatsendliquidität) und die
  Verteilung der Ein- und Auszahlungen. Jedes Diagramm hat eine Tabellenansicht.
  Kontogruppen lassen sich als **Sondereffekt** kennzeichnen (z. B. insolvenzspezifische
  Kosten, Anfechtungen) – sie werden in den Kennzahlen „ohne Sondereffekte“ herausgerechnet.
  Wie im Fact Sheet („Entwicklung KW“ / „Entwicklung Antrag“) gibt es je einen Bereich
  **Vergleich zur Vorwoche** und **Vergleich zum Antrag**: letzte abgeschlossene Woche
  Ist gegen den Planstand (Liquidität, Über-/Unterdeckung und je Kategorie),
  Ergebnisveränderung (Liquidität zum Ende des gemeinsamen Zeitraums), Abweichung
  Woche → Ergebnis mit Differenz (Zeitverschiebungen und Planänderungen),
  Veränderung je Kategorie und Monat sowie der Monatsendliquidität; dazu für die
  Vorwoche die Ein-/Auszahlungen der kommenden vier Wochen und für den Antrag die
  Gegenüberstellung Antragsplan / Vorwochenplan / Ist. Als Vorwoche gilt automatisch
  der jüngste vor der laufenden Woche eingefrorene Planstand, als Antrag der
  Planstand mit „Antrag“ im Kommentar (sonst der älteste) – beides auf der Seite
  übersteuerbar.
  „PDF ↓“ exportiert die Seite als **KPI-Bericht** (A4 quer: Kacheln, alle Diagramme
  in gleicher Gestaltung, die zugehörigen Wertetabellen jeweils direkt darunter auf
  derselben Seite – Blöcke und Tabellen werden nicht umbrochen) mit Start, Wochen und
  den gewählten Planständen der Seite.
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
- **Mehrbenutzerbetrieb**: Rollen Admin/Bearbeiter/Leser, Mandantenzuordnung, Audit-Log.
  In der Benutzerverwaltung öffnet „bearbeiten“ einen Dialog für Name, **Rolle** und
  Mandanten sowie zum **Zurücksetzen des Passworts** (Generator für ein Zufallspasswort,
  optional mit Pflicht zum eigenen Passwort beim nächsten Login – bis dahin ist nur das
  Profil erreichbar; bestehende Anmeldungen des Benutzers werden beendet). Der letzte
  aktive Administrator kann weder herabgestuft noch gesperrt werden, das eigene Konto
  nicht gesperrt. Jeder Benutzer hat ein Profil mit Passwortwechsel und optionaler
  Zwei-Faktor-Anmeldung (TOTP mit QR-Einrichtung; Admin-Reset bei Geräteverlust).

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
│   ├── kpi.py               Kennzahlen und Planvergleich für die KPI-Seite
│   ├── kpi_pdf.py           KPI-Bericht als PDF (Diagramme mit reportlab)
│   ├── kontenrahmen.py      SKR03/SKR04-Vorlagen inkl. USt und BWA-Gruppen
│   └── demo.py              Demo-Mandant
├── templates/ + static/     Oberfläche (Jinja2 + Vanilla-JS)
└── tests/                   pytest (Kalender, Parser, Engine, API, Berechtigungen)
```
