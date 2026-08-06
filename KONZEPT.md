# LiquiPlanung – 13-Wochen-Liquiditätsplanung für Insolvenzbuchhalter

Konzept- und Architekturdokument. Stand: Erstimplementierung.

## 1. Ziel und Kontext

Zentralisierte, mandantenfähige Webanwendung zur rollierenden 13-Wochen-Liquiditätsplanung
als Ablösung der bisherigen Excel-Lösung. Betreiber ist ein Insolvenzbuchhalter, der die
Planung für diverse Mandanten (Schuldnerunternehmen in Eigenverwaltung, vorläufigen und
eröffneten Verfahren sowie Regelmandate) auf einem eigenen Server (Proxmox / LXC) betreibt.

Kernanforderungen aus der Aufgabenstellung:

- Historienwerte als Basis: Buchungssätze und BWA aus Buchhaltungssystemen (DATEV, Addison).
- Auswählbare Kontenrahmen (SKR03, SKR04, eigene); je Konto definierte Umsatzsteuerpflicht
  inkl. Prozentsatz.
- Aufgelaufene Eingangsrechnungen (offene Posten) und Dauerverbindlichkeiten als Planbasis.
- Zahlungskalender für Sozialversicherungsbeiträge und Steuerarten (USt-VA, Lohnsteuer,
  Gewerbesteuer, Körperschaftsteuer).
- Budgetplanung auf Kontenebene für den Zukunftszeitraum.
- Rollierende 13-Wochen-Darstellung: Spalten = Tage, Zeilen = Konten der BWA inkl.
  Aggregationsknoten; Soll-/Ist-Vergleich mit Abweichungsausweis.
- Bestände (Banken, Kassen, Waren) und Liquiditätsentwicklung.
- Multimandantenfähig, PostgreSQL, Betrieb im LXC-Container (Postgres im Docker).

## 2. Was über die Aufgabenstellung hinaus notwendig ist

Diese Punkte wurden bewusst durchdacht; ein Teil ist bereits implementiert (✔),
ein Teil ist als Ausbaustufe vorgesehen (→ Roadmap, Kap. 10):

**Fachlich / insolvenzspezifisch**

1. ✔ **Plan-Einfrieren (Snapshots):** Ein ehrlicher Soll-/Ist-Vergleich ist nur möglich,
   wenn der Plan zum Zeitpunkt X eingefroren wird. Eine "lebende" Planung würde sich
   nachträglich den Ist-Werten annähern und Abweichungen verschleiern. Daher gibt es
   Plan-Snapshots (manuell oder wöchentlich), gegen die das Ist verglichen wird.
2. ✔ **Überfällige Posten:** Offene Posten mit Fälligkeit in der Vergangenheit dürfen
   nicht verschwinden – sie werden auf den nächsten Bankarbeitstag ab heute gerollt
   ("aktuelle Zahlungserwartung") und mindern die Liquiditätsprojektion.
3. ✔ **Forderungsseite:** Neben Eingangsrechnungen (Kreditoren) werden auch
   Ausgangsrechnungen/Forderungen (Debitoren) als erwartete Einzahlungen geführt.
4. ✔ **Doppelzählungs-Vermeidung ("Restbudget-Logik"):** Budget je Konto und Woche wird um
   bereits explizit geplante Posten (OPs, Dauerbuchungen, Kalendertermine) desselben Kontos
   gekürzt (Untergrenze 0). Sonst würde z. B. die Miete doppelt geplant (Dauerbuchung + Budget).
5. ✔ **Kontokorrent-/Kreditlinien:** Verfügbare Liquidität = Bestand + freie Linie.
   Je Bankkonto ist eine Kreditlinie hinterlegbar; die Darstellung weist beides aus.
6. ✔ **Masseverbindlichkeiten vs. Insolvenzforderungen (§ 38 / § 55 InsO):** Je offenem
   Posten ist eine Forderungsklasse erfasst (Masse, Insolvenzforderung, Aus-/Absonderung).
   Insolvenzforderungen unterliegen der **Zahlungssperre**: Sie werden nicht als
   Auszahlung geplant und mindern die Projektion nicht; ihre offene Summe wird in Plan,
   Postenliste und Berichten nachrichtlich ausgewiesen. Eingangsrechnungen mit
   Rechnungsdatum vor dem Insolvenz-Stichtag werden automatisch als Insolvenzforderung
   vorbelegt (übersteuerbar).
7. ✔ **Insolvenzgeld-Assistent:** Je Mandant ist ein Insolvenzgeldzeitraum hinterlegbar
   (Vorschlag: 3 Monate vor erwarteter Eröffnung, §§ 165 ff. SGB III). Im Zeitraum
   unterdrückt die Planung Personal-/SV-Budgets und -Dauerbuchungen (Nettolöhne über
   Insolvenzgeld bzw. Vorfinanzierung nach § 170 Abs. 4 SGB III, SV-Beiträge nach
   § 175 SGB III von der BA getragen) und kürzt SV-/LSt-Kalendertermine anteilig nach
   Beitrags-/Lohnmonat. Eine Vorschau zeigt die Entlastung je Position vor dem
   Aktivieren; Plan und Berichte weisen den Zeitraum und die Entlastung aus.
8. ✔ **Szenarien (Best/Base/Worst):** Je Mandant definierbare Planvarianten mit
   Faktoren auf geplante Einzahlungen und budgetbasierte (variable) Auszahlungen sowie
   Debitorenverzögerung in Tagen; vertraglich fixe Posten und Steuer-/SV-Termine
   bleiben unverändert. Auswahl auf der Plan-Seite (inkl. Export-Vermerk); Snapshots
   und Soll/Ist beziehen sich stets auf den Basisplan. Best-/Worst-Vorlagen werden je
   Mandant angelegt.
9. ✔ **Bankdatenimport (MT940 / CAMT.053):** Kontoauszüge als tagesaktuelle
   Bestandsquelle unabhängig vom Buchhaltungsexport. Endsalden werden je Auszug als
   Bestandsanker übernommen (der Kontoauszug ist maßgeblich); die Bestandsfortschreibung
   verankert den Verlauf stückweise an allen Auszugssalden, dazwischen zählen die
   Buchhaltungsbewegungen. Zuordnung über die IBAN am Bankkonto; Einzelumsätze werden
   zur Referenz gespeichert. Die BWA-Zeilen (Ist-Zahlungsflüsse) speisen sich bewusst
   weiterhin nur aus den Buchhaltungs-Buchungen – keine Doppelzählung.
   **Automatischer OP-Ausgleich:** Ein Scoring-Verfahren (Richtung, Betrag exakt bzw.
   Skonto-Toleranz 3 %, Belegnummer im Verwendungszweck, Partnername, Datumsnähe)
   schlägt eindeutige Zuordnungen Bankumsatz ↔ offener Posten vor; sichere Treffer
   sind vorausgewählt, die Übernahme setzt den Posten auf BEZAHLT und verknüpft den
   Umsatz (aufhebbar). Teilzahlungen werden bewusst nicht vorgeschlagen.

**Steuer-/SV-Regeln**

10. ✔ **Bankarbeitstage & Feiertage je Bundesland:** SV-Beiträge sind am drittletzten
    Bankarbeitstag des Monats fällig; Bankarbeitstage = Mo–Fr ohne gesetzliche Feiertage,
    ohne 24.12. und 31.12. Feiertage werden je Bundesland berechnet (inkl. beweglicher
    Feiertage über die Osterformel).
11. ✔ **Steuertermine mit Verschiebung:** Fälligkeiten (10. bzw. 15.) verschieben sich auf
    den nächsten Werktag, wenn sie auf Sa/So/Feiertag fallen (§ 108 Abs. 3 AO).
12. ✔ **USt-Zeitraum & Dauerfristverlängerung:** je Mandant monatlich/vierteljährlich,
    Dauerfristverlängerung (+1 Monat) konfigurierbar.
13. ✔ **Beitrags-/Steuerhöhe:** Je Terminart eine Regel: fester Betrag oder Schätzung aus
    der Historie (Durchschnitt der letzten Ist-Zahlungen auf den verknüpften Konten).
    Generierte Termine sind einzeln übersteuerbar.
14. → **USt-Sondervorauszahlung (1/11)** und Schonfristen: Ausbaustufe.

**Technisch / organisatorisch**

15. ✔ **Benutzer- und Rollenmodell:** Admin / Bearbeiter / Leser, Zuordnung von Benutzern
    zu Mandanten (Mandantentrennung auf Anwendungsebene, jede Abfrage mandantengefiltert).
16. ✔ **Audit-Log:** Wesentliche Änderungen (Importe, Stammdaten, Snapshots) werden mit
    Benutzer und Zeitstempel protokolliert (GoBD-orientierte Nachvollziehbarkeit).
17. ✔ **Import-Validierung:** Buchungen auf Konten ohne Stammsatz gehen nicht verloren,
    sondern werden ausgewiesen ("unbekannte Konten"), damit der Kontenrahmen gepflegt
    werden kann.
18. ✔ **Demo-Mandant:** Auf Wunsch wird ein Beispielmandant mit Daten angelegt, damit die
    Darstellung sofort prüfbar ist.
19. ✔ **Export (PDF/Excel):** 13-Wochen-Plan als Excel-Arbeitsmappe (Blätter Info,
    Tage, Wochen mit Plan/Ist/Δ, Soll-Ist) und als PDF-Bericht (Querformat, Wochenspalten)
    für Gericht, Sachwalter, Gläubigerausschuss.
20. → **Datensicherung:** pg_dump-Cron im LXC (Anleitung in `deploy/PROXMOX_LXC.md`),
    später integrierte Sicherung.
21. → **DSGVO:** Personenbezogene Daten (Kreditoren-/Debitorennamen) – Löschkonzept nach
    Verfahrensende; TLS via Reverse Proxy (Anleitung enthalten).
22. → **Alembic-Migrationen:** Erststand nutzt `create_all`; sobald produktive Daten
    vorliegen, werden Schemaänderungen über Alembic versioniert.

## 3. Architektur

```
┌─────────────────────────── LXC-Container (Proxmox) ───────────────────────────┐
│  ┌──────────── Docker ────────────┐      ┌──────────── Docker ─────────────┐  │
│  │  app: FastAPI + Uvicorn        │◄────►│  db: PostgreSQL 16              │  │
│  │  (Jinja2-Seiten + JSON-API)    │      │  Volume: pgdata                 │  │
│  └────────────────────────────────┘      └─────────────────────────────────┘  │
│           ▲  Port 8000 (per Reverse Proxy / TLS veröffentlichen)              │
└───────────┼───────────────────────────────────────────────────────────────────┘
            │
   Browser der Sachbearbeiter (Mandantenauswahl, Import, Planung, Soll/Ist)
```

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2, PostgreSQL (Entwicklung/Tests auch SQLite).
- **Frontend:** Serverseitig gerenderte Seiten (Jinja2) + leichtgewichtiges Vanilla-JS für
  die Plan-Matrix, Budget-Editor usw. Kein Node-Build nötig → einfacher Betrieb im LXC.
- **Auth:** Session-Cookie (signiert), Passwörter mit PBKDF2-HMAC-SHA256.
- **Deployment:** `docker compose up -d` im LXC (Anleitung: `deploy/PROXMOX_LXC.md`).

## 4. Datenmodell (Kern)

| Tabelle | Zweck |
|---|---|
| `benutzer`, `benutzer_mandanten` | Benutzer, Rollen, Mandantenzuordnung |
| `mandanten` | Mandant inkl. Kontenrahmen-Typ, Bundesland, USt-Zeitraum, Dauerfrist, Verfahrensstatus/-stichtag |
| `konto_gruppen` | BWA-Aggregationsknoten (Baum), Richtung EIN/AUS/INFO |
| `konten` | Konten je Mandant: Nummer, Bezeichnung, Typ (BANK/KASSE/ERLOES/…), **USt-Satz**, Gruppe, Kreditlinie |
| `import_batches`, `buchungen` | Importierte Ist-Buchungssätze (DATEV/CSV) |
| `offene_posten` | Eingangs-/Ausgangsrechnungen: Fälligkeit, geplantes Zahldatum, Status |
| `dauerbuchungen` | Dauerverbindlichkeiten/-aufträge mit Intervall und Stichtag |
| `budgets`, `budget_wochen` | Budget je Konto/Monat (netto) + Wochen-Override |
| `termin_regeln`, `zahlungstermine` | Kalenderregeln (SV, USt-VA, LSt, GewSt, KSt) und generierte, editierbare Termine |
| `bestaende` | Anfangs-/Stichtagsbestände Bank/Kasse/Waren |
| `plan_snapshots`, `plan_snapshot_werte` | Eingefrorene Plandaten für den Soll-/Ist-Vergleich |
| `audit_log` | Protokoll wesentlicher Aktionen |

Alle mandantenbezogenen Tabellen tragen `mandant_id`; jeder Zugriff läuft über eine
Berechtigungsprüfung (Benutzer ↔ Mandant).

## 5. Planungslogik (Engine)

Planungsfenster: rollierend 13 ISO-Wochen (91 Tage), Beginn Montag der laufenden Woche.

**Ist** (aus importierten Buchungen): Ein Buchungssatz ist liquiditätswirksam, wenn genau
eine Seite ein Finanzkonto (Bank/Kasse) ist. Der Zahlungsfluss (+ Einzahlung / − Auszahlung,
Vorzeichen aus Soll/Haben der Finanzkontoseite) wird der Sachkontoseite zugeordnet –
dadurch entstehen die BWA-Zeilen. Bank-an-Bank = Umbuchung (nur Bestandsverschiebung).

**Plan** je Konto/Tag aus vier Quellen:

1. Offene Posten (geplantes Zahldatum, sonst Fälligkeit; überfällig → nächster
   Bankarbeitstag ab heute),
2. Dauerbuchungen (Intervall-Expansion mit Vorlauf über die Fenstergrenze, Verschiebung
   auf Bankarbeitstag),
3. Zahlungstermine aus dem Kalender (SV/Steuern; Deduplizierung über die fachliche
   Periode, damit manuell verschobene Termine bei Neugenerierung nicht doppeln),
4. Budget: tagesgenaue Verteilung (Monatsbudget je Bankarbeitstag des Monats bzw.
   Wochen-Override), wochenweise **gekürzt** um explizite Posten desselben Kontos
   (Restbudget ≥ 0). Netto-Budgets werden über den USt-Satz des Kontos auf
   Brutto-Zahlungswirkung umgerechnet.

**Bestände & Liquidität:** Je Finanzkonto Ankerbestand (Stichtag) + Ist-Bewegungen bis
heute; ab morgen Fortschreibung über den **Restplan** (nur offene Posten, künftige nicht
erledigte Termine und Dauerraten – bereits Gezahltes steckt im Ist-Bestand und zählt
nicht doppelt). Warenbestand wird nachrichtlich fortgeschrieben (letzter erfasster
Wert). Verfügbare Liquidität = Bestand + freie Kreditlinien.

**Soll/Ist:** Für vergangene Tage im Fenster wird das Ist gegen den eingefrorenen Plan
(neuester Snapshot, dessen Fenster den Tag abdeckt) gestellt; Abweichung = Ist − Plan.
Ohne Snapshot dient der Live-Plan als Vergleichsbasis (gekennzeichnet).

## 6. Zahlungskalender-Regeln (implementiert)

| Terminart | Regel |
|---|---|
| SV-Beiträge | drittletzter Bankarbeitstag des Monats (Mo–Fr, ohne Feiertage des Bundeslands, ohne 24.12./31.12.) |
| USt-VA | 10. des Folgemonats (Monat) bzw. 10. nach Quartalsende; Dauerfrist: +1 Monat; Verschiebung auf nächsten Werktag |
| Lohnsteuer | 10. des Folgemonats (Verschiebung wie oben) |
| GewSt-Vorauszahlung | 15.02. / 15.05. / 15.08. / 15.11. |
| KSt-/ESt-Vorauszahlung | 10.03. / 10.06. / 10.09. / 10.12. |

Beträge je Regel: fest oder Historien-Schätzung (Ø der letzten 3 Monats-Zahlungen auf dem
verknüpften Konto). Generierte Termine sind editier- und löschbar; manuell angepasste
Termine werden bei Neugenerierung nicht überschrieben.

## 7. Importformate

- **DATEV Buchungsstapel (EXTF/DTVF, CSV):** Header wird ausgewertet (Wirtschaftsjahr,
  Zeitraum); Spalten per Kopfzeile oder Positionsfallback (Umsatz, S/H, Konto, Gegenkonto,
  Belegdatum TTMM, Belegfeld 1, Buchungstext). Encoding-Erkennung (CP1252/UTF-8),
  Dezimalkomma.
- **Generisches CSV** (für Addison u. a.): Spalten `Datum;Konto;Gegenkonto;Betrag;SH;Belegfeld;Text`
  (Kopfzeilen-Erkennung, flexible Datumsformate). Ein Addison-Export lässt sich darauf abbilden.
- **BWA-/Saldenimport:** `Konto;Jahr;Monat;Betrag` als Historienbasis für Budgetvorschläge.
- **Kontoauszüge (MT940 / CAMT.053):** Bankumsätze und Salden; Endsaldo je Auszug wird
  als Bestandsanker übernommen (Zuordnung über IBAN am Bankkonto oder manuelle Auswahl,
  Dubletten-Warnung bei überlappenden Zeiträumen).

Unbekannte Konten werden beim Import gemeldet und können direkt angelegt werden.

## 8. Kontenrahmen

SKR03 und SKR04 werden als Vorlage mit den planungsrelevanten Konten (inkl. USt-Satz und
BWA-Gruppenzuordnung) je Mandant eingespielt und sind danach frei editier- und erweiterbar
(auch als CSV-Import). Ein leerer, eigener Kontenrahmen ist ebenfalls möglich. Die
Vorbelegungen (USt-Sätze, Gruppenzuordnung) sind Vorschlagswerte und je Mandant zu prüfen.

## 9. Sicherheit & Mandantentrennung

- Jede API-Route prüft Login und Mandantenberechtigung; Queries sind immer auf
  `mandant_id` gefiltert.
- Rollen: `ADMIN` (alles, Benutzerverwaltung), `BEARBEITER` (zugeordnete Mandanten
  schreibend), `LESER` (lesend).
- Session-Cookies signiert (`SECRET_KEY` zwingend setzen), Passwort-Hashing PBKDF2.
- Betrieb hinter Reverse Proxy mit TLS empfohlen (siehe Deployment-Doku).

## 10. Roadmap (bewusst noch nicht enthalten)

1. Teilzahlungs-Ausgleich (ein Bankumsatz gleicht einen Posten anteilig aus,
   Restbetrag bleibt offen) und Sammelüberweisungs-Matching (1:n).
2. Alembic-Migrationen (der Erststand zieht additive Spalten beim Start automatisch
   nach), integrierte Backups, 2-Faktor-Login.
3. USt-Zahllast-Vorschau aus Budget (Erlöse × Satz − Vorsteuer) statt Historienschätzung.
4. Feingranulare Verteilungsprofile für Budgets (z. B. Zahllauf freitags).
5. Szenario-Detailregeln je Konto/Gruppe (statt globaler Faktoren).
