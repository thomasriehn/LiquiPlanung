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
   bereits explizit geplante Posten (OPs, Dauerschuldverhältnisse, Kalendertermine) desselben Kontos
   gekürzt (Untergrenze 0). Sonst würde z. B. die Miete doppelt geplant (Dauerschuldverhältnis + Budget).
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
   unterdrückt die Planung Personal-/SV-Budgets und -Dauerschuldverhältnisse (Nettolöhne über
   Insolvenzgeld bzw. Vorfinanzierung nach § 170 Abs. 4 SGB III, SV-Beiträge nach
   § 175 SGB III von der BA getragen) und kürzt SV-/LSt-Kalendertermine anteilig nach
   Beitrags-/Lohnmonat. Eine Vorschau zeigt die Entlastung je Position vor dem
   Aktivieren; Plan und Berichte weisen den Zeitraum und die Entlastung aus.
8. ✔ **Szenarien (Best/Base/Worst):** Je Mandant definierbare Planvarianten mit
   Faktoren auf geplante Einzahlungen und budgetbasierte (variable) Auszahlungen sowie
   Debitorenverzögerung in Tagen; vertraglich fixe Posten und Steuer-/SV-Termine
   bleiben unverändert. **Detailregeln je Konto oder BWA-Gruppe** übersteuern die
   globalen Faktoren (Vorrang: Konto vor Gruppe vor global). Auswahl auf der
   Plan-Seite (inkl. Export-Vermerk), eigene Vergleichsansicht; Snapshots und
   Soll/Ist beziehen sich stets auf den Basisplan. Best-/Worst-Vorlagen werden je
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
   Umsatz (aufhebbar). **Teilzahlungen** werden vorgeschlagen, wenn ein starkes
   Signal vorliegt (Belegnummer oder eindeutiger Partner) – nie vorausgewählt; die
   Übernahme erhöht nur den bezahlten Betrag, der Posten bleibt mit Restbetrag
   offen und die Planung rechnet mit dem Rest. **Sammelüberweisungen (1:n)** werden
   erkannt, wenn eine Teilmenge offener Posten (mit Beleg-/Partnersignal) die
   Zahlung exakt ergibt; die Zuordnungstabelle hält je Posten den Zahlungsanteil,
   die Aufhebung löst alle Anteile wieder. Reine Überzahlungen ohne passende
   Postenkombination werden abgelehnt.

   Unabhängig vom Bankabgleich lassen sich Zahlungen auch **manuell erfassen**
   (Betrag und Datum je Posten): Teilzahlungen lassen den Posten mit Restbetrag
   offen, optional wird der Rest ausgebucht (Skonto/Erlass). Für kleinere
   Verfahren ohne Einzel-OPs gibt es **Sammelposten** (erwartete
   Sammelzahlungseingänge über mehrere Debitoren, z. B. je Kalenderwoche):
   Zahlungen über den Restbetrag hinaus werden automatisch auf die nächsten
   offenen Sammelposten (nach Fälligkeit) vorgetragen – zahlen Debitoren früher
   als erwartet, sinken die später geplanten Einzahlungen entsprechend.

**Steuer-/SV-Regeln**

10. ✔ **Bankarbeitstage & Feiertage je Bundesland:** SV-Beiträge sind am drittletzten
    Bankarbeitstag des Monats fällig; Bankarbeitstage = Mo–Fr ohne gesetzliche Feiertage,
    ohne 24.12. und 31.12. Feiertage werden je Bundesland berechnet (inkl. beweglicher
    Feiertage über die Osterformel).
11. ✔ **Steuertermine mit Verschiebung:** Fälligkeiten (10. bzw. 15.) verschieben sich auf
    den nächsten Werktag, wenn sie auf Sa/So/Feiertag fallen (§ 108 Abs. 3 AO).
12. ✔ **USt-Zeitraum & Dauerfristverlängerung:** je Mandant monatlich/vierteljährlich,
    Dauerfristverlängerung (+1 Monat) konfigurierbar.
13. ✔ **Beitrags-/Steuerhöhe:** Je Terminart eine Regel: fester Betrag, Schätzung aus
    der Historie (Durchschnitt der letzten Ist-Zahlungen auf den verknüpften Konten)
    oder – für die USt-VA – **Vorschau aus dem Budget** (USt auf Erlösbudgets minus
    Vorsteuer auf Aufwands-/Investitionsbudgets des Voranmeldungszeitraums; negativ =
    erwartete Erstattung). Generierte Termine sind einzeln übersteuerbar.
14. → **USt-Sondervorauszahlung (1/11)** und Schonfristen: Ausbaustufe.

**Technisch / organisatorisch**

15. ✔ **Benutzer- und Rollenmodell:** Admin / Bearbeiter / Leser, Zuordnung von Benutzern
    zu Mandanten (Mandantentrennung auf Anwendungsebene, jede Abfrage mandantengefiltert).
    Selbstverwaltung im Profil (Passwortwechsel) und optionale
    **Zwei-Faktor-Anmeldung** (TOTP nach RFC 6238, QR-Einrichtung für gängige
    Authenticator-Apps, Code-Wiederverwendungsschutz; Admin kann bei Geräteverlust
    zurücksetzen).
16. ✔ **Audit-Log:** Wesentliche Änderungen (Importe, Stammdaten, Snapshots) werden mit
    Benutzer und Zeitstempel protokolliert (GoBD-orientierte Nachvollziehbarkeit).
17. ✔ **Import-Validierung:** Buchungen auf Konten ohne Stammsatz gehen nicht verloren,
    sondern werden ausgewiesen ("unbekannte Konten"), damit der Kontenrahmen gepflegt
    werden kann.
18. ✔ **Demo-Mandant:** Auf Wunsch wird ein Beispielmandant mit Daten angelegt, damit die
    Darstellung sofort prüfbar ist.
19. ✔ **Export (PDF/Excel):** 13-Wochen-Plan als Excel-Arbeitsmappe (Blätter Info,
    Tage, Wochen und Monate mit Plan/Ist/Δ gegen die Soll-Referenz, Soll-Ist) und als
    PDF-Bericht (Querformat) für Gericht, Sachwalter, Gläubigerausschuss. Das PDF
    übernimmt Spaltenansicht (Tage/Wochen/Monate) und Werteauswahl der Plan-Seite;
    breite Pläne werden horizontal auf Folgeblätter verteilt (13 Wochen bzw. Monate
    oder 14 Tage je Blatt, einheitliche Spaltenbreite, Kontospalte und Kopfzeile je
    Blatt wiederholt, „Blatt x von y“).
20. ✔ **Integrierte Datensicherung:** Automatische Voll-Sicherung im konfigurierbaren
    Intervall (Standard: täglich) mit Aufbewahrungsfrist, dazu manuelle Sicherung,
    Download und Löschung in der Admin-Oberfläche. Sicherungen sind
    datenbankunabhängige Zip-Archive (alle Tabellen als JSON) mit Manifest inkl.
    Schema-Revision; die Wiederherstellung ersetzt den kompletten Datenbestand,
    verlangt die passende Schema-Revision, eine explizite Bestätigung und legt
    vorab automatisch eine Sicherung des Ist-Zustands an. Ergänzend bleibt der
    pg_dump-Cron im LXC empfohlen (`deploy/PROXMOX_LXC.md`).
21. → **DSGVO:** Personenbezogene Daten (Kreditoren-/Debitorennamen) – Löschkonzept nach
    Verfahrensende; TLS via Reverse Proxy (Anleitung enthalten).
22. ✔ **Alembic-Migrationen:** Schemaänderungen sind über Alembic versioniert
    (`backend/migrations`); beim Anwendungsstart läuft automatisch
    `alembic upgrade head`. Bestandsinstallationen aus der Zeit vor Alembic werden
    beim ersten Start übernommen (Schema additiv angleichen, Baseline stempeln).
    Ein Test stellt sicher, dass Modelle und Migrationen nie auseinanderlaufen.

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
| `budgets`, `budget_wochen`, `budget_tage` | Budget je Konto/Monat (netto) + Wochen- und Tages-Override |
| `termin_regeln`, `zahlungstermine` | Kalenderregeln (SV, USt-VA, LSt, GewSt, KSt) und generierte, editierbare Termine |
| `bestaende` | Anfangs-/Stichtagsbestände Bank/Kasse/Waren |
| `plan_snapshots`, `plan_snapshot_werte` | Eingefrorene Plandaten für den Soll-/Ist-Vergleich |
| `plan_verschiebungen` | Manuelle Verschiebungen von Planwerten/Vorträgen einer Planzeile (Ursprungstag → Zieltag, Teilbetrag) |
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
2. Dauerschuldverhältnisse (Intervall-Expansion mit Vorlauf über die Fenstergrenze, Verschiebung
   auf Bankarbeitstag),
3. Zahlungstermine aus dem Kalender (SV/Steuern; Deduplizierung über die fachliche
   Periode, damit manuell verschobene Termine bei Neugenerierung nicht doppeln),
4. Budget: tagesgenaue Verteilung gemäß **Verteilungsprofil je Konto** – gleichmäßig
   je Bankarbeitstag, Monatsanfang/-mitte/-ende (z. B. Lohnlauf) oder wöchentlicher
   Zahllauf an einem Wochentag (z. B. freitags); Wochen-Overrides folgen dem
   Wochenprofil, Tages-Overrides ersetzen den Wert ihres Tages (Vorrang Tag >
   Woche > Monat; 0 = kein Budget). Monats- und Wochenwerte tragen eine
   **Aufteilung**: `KW` = eine Zahlung je Kalenderwoche (Anteil der Woche = ihre
   Bankarbeitstage im Monat ÷ alle Bankarbeitstage; Zahltag = Wochentag laut
   Konto bzw. erster Bankarbeitstag der Woche), `TAG` = gleichmäßig auf die
   Bankarbeitstage, `KONTO`/leer = Verteilungsprofil des Kontos (ältere Werte).
   Monatswerte mit `fortschreiben` gelten auch in allen Folgemonaten bis zum
   nächsten erfassten Monatswert (`MonatsBudgets`); das nutzen Engine,
   Budgetseite, USt-Vorschau und Insolvenzgeld-Vorschau gleichermaßen. Die Verteilung je Woche liefert eine gemeinsame
   Funktion (`budget_basis_woche`), die auch die Budgetseite nutzt
   (`services/budget.py`: wirksames Netto-Budget je Tag, summiert auf Tage, Wochen
   oder Monate). Wochenweise **gekürzt** um explizite Posten desselben Kontos
   (Restbudget ≥ 0). Netto-Budgets werden über den USt-Satz des Kontos auf
   Brutto-Zahlungswirkung umgerechnet.
   Persönliche Anzeige-Einstellungen (z. B. Spalten Tage/Wochen/Monate für Plan und
   Budget) stehen in `benutzer.einstellungen` (JSON, `PUT /api/profil/einstellungen`).

**Bestände & Liquidität:** Je Finanzkonto Ankerbestand (Stichtag) + Ist-Bewegungen bis
heute; ab morgen Fortschreibung über den **Restplan** (nur offene Posten, künftige nicht
erledigte Termine und Dauerraten – bereits Gezahltes steckt im Ist-Bestand und zählt
nicht doppelt). Warenbestand wird nachrichtlich fortgeschrieben (letzter erfasster
Wert). Verfügbare Liquidität = Bestand + freie Kreditlinien.

**Soll/Ist:** Für vergangene Tage im Fenster wird das Ist gegen den eingefrorenen Plan
gestellt; Abweichung = Ist − Plan. Maßgeblich ist die gewählte **Soll-Referenz**
(Kennzeichen `plan_snapshots.referenz`, höchstens ein Snapshot je Mandant – bewusst
keine Fremdschlüssel-Spalte am Mandanten, um einen Zyklus Mandant ↔ Snapshot zu
vermeiden); ohne Wahl der neueste Snapshot, dessen Fenster den Tag abdeckt. Ohne
Snapshot dient der Live-Plan als Vergleichsbasis (gekennzeichnet). Gelöschte
Snapshots verlieren ihre Referenz-Rolle automatisch.

**Vortrag (Ansicht „mit Vortrag“):** Fälligkeiten von Dauerschuldverhältnissen
(Kennzeichen `vortragen`, nur mit Konto) im Fenster bis heute werden gegen die
einzelnen Ist-Zahlungen ihres Kontos (gleiche Richtung, ab 14 Tage vor Fälligkeit)
abgeglichen: (1) betragsgleiche Zahlung (±1 %, mind. 1 €) bis 14 Tage um die
Fälligkeit, (2) danach chronologische Anrechnung der übrigen Zahlungen – Teilzahlungen
mindern den offenen Rest, der Rest einer angerechneten Zahlung wirkt als
Vorauszahlung auf spätere Fälligkeiten; jede Zahlung zählt höchstens einmal. Der
ungedeckte Rest wird auf den nächsten Bankarbeitstag ab morgen vorgetragen (getrennt
vom Plan als `vortrag` je Zeile ausgeliefert, damit die Oberfläche hervorheben und bei
Zusammentreffen mit einer Planbuchung desselben Kontos schraffieren kann). Die
Liquidität wird im Vortragsmodus um die kumulierten Vorträge fortgeschrieben.
Grenze: Zahlungen auf demselben Konto, die zu anderen Vorgängen gehören (z. B. ein
offener Posten), werden mangels Zuordnung mit angerechnet.

**Manuelle Verschiebungen (`plan_verschiebungen`):** Der Zellendialog legt je
Ursprungstag einen Datensatz an (Planzeile = Konto oder Termin-Typ, `von` → `nach`,
Betrag mit Vorzeichen des Flusses; entnommen wird chronologisch aus den künftigen
Tagen der Zelle). Ziel ist der gewählte Tag bzw. der erste Bankarbeitstag der
Woche/des Monats, frühestens morgen. Die Engine wendet die Verschiebungen nach
`plan_fluesse` in Anlagereihenfolge an, jeweils **begrenzt auf den am Ursprungstag
noch geplanten Betrag** gleicher Richtung (Ursprung außerhalb des Fensters: Planbetrag
dieses Tages aus einer Wochenberechnung). Planverschiebungen wirken dauerhaft
(auch für den Soll/Ist-Vergleich vergangener Tage). Ist der Ursprungstag vorbei,
der Zieltag aber nicht, sind überfällige offene Posten inzwischen auf den Rolltag
gewandert – der noch fehlende Teil wird dann von dort genommen (nur aus dem
gerollten Postenanteil, nicht aus anderen Planwerten des Rolltags). Die
Restplan-Sicht der Bestandsfortschreibung übernimmt die Wirkung: künftige
Ursprungstage werden entlastet, bei vergangenem Ursprungstag nur gerollte Posten;
der Zieltag erhält den Betrag, solange er in der Zukunft liegt. Für den Vortrag
gelten verschobene Dauerschuld-Raten am Ursprungstag als nicht fällig, am Zieltag
als fällig. **Vortragsverschiebungen** greifen auf den jeweils aktuellen
Vortragstag zu (begrenzt auf den offenen Vortrag) und enden mit ihrem Zieltag –
danach ist ein weiterhin offener Betrag wieder regulär vorzutragen. Grenze: wird
ein Posten nach einer Verschiebung auf einen früheren Tag dort nur teilweise
bezahlt, mindert die Verschiebung den Rest am Ursprungstag weiterhin.
Zeilen liefern `verschoben`/`verschoben_vortrag` (abgebende Tage negativ,
empfangende positiv) für die Eckmarke der Oberfläche; eingefrorene Planstände
enthalten die Planverschiebungen.

**Ist-Buchungen einer Zelle:** `GET /mandanten/{id}/plan/ist-buchungen` liefert die
Zahlungsbuchungen (genau eine Seite Bank/Kasse) der Planzeile im Zeitraum;
`POST /buchungen/{id}/sachkonto` bucht die Sachkontoseite auf ein anderes
(Nicht-Finanz-)Konto um – Bankseite, Betrag und Postenbezug bleiben, protokolliert
im Audit-Log.

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
- **PDF-Kontoumsätze:** Kontoauszüge, die nur als PDF vorliegen, werden über
  Textextraktion geparst. Drei Formate mit automatischer Erkennung (es gewinnt
  der Format-Parser mit den meisten Treffern): Sparkassen-Verfahrenskonto-Report
  (Auszugsnummer + zwei Datumsangaben als Blockbeginn, Betrag am Blockende),
  Deutsche-Bank-Kontoauszug (Vorzeichen + Betrag mit Vorgangstext verklebt,
  Datumsangaben über zwei Zeilen umbrochen, Adressblöcke werden gefiltert) und
  UC eBanking prime „CAMT Auszüge“ (HVB/UniCredit; Buchungs-/Valutadatum als
  Blockbeginn, Empfänger vor der IBAN-Zeile, Betrag allein am Blockende);
  mehrzeilige Empfänger per Heuristik. Ausgaben werden einem Konto zugeordnet,
  Zahlungseingänge einem offenen Posten (Anrechnung über dieselbe Logik wie die
  manuelle Zahlungserfassung inkl. Sammelposten-Vortrag); je Zahlungsanteil
  entsteht eine Ist-Buchung mit Posten-Referenz (`buchungen.posten_id`), über
  die eine Zuordnung einzeln aufhebbar ist (Buchung löschen + Anteil vom Posten
  abziehen). Die manuelle Zahlungserfassung kann ebenfalls direkt ins Ist buchen
  (wählbares Zahlungsmittelkonto, abwählbar zur Vermeidung von Doppelzählung
  mit dem Buchhaltungsimport). Ausgaben werden in der Oberfläche einzeln
  einem Konto zugeordnet und sofort zum Buchungsdatum als Ist-Buchung übernommen
  (Sachkonto an Bank, DATEV-Logik); alle Buchungen eines PDFs hängen an einem
  Import-Batch und sind gesammelt löschbar.
- **Texterkennung (OCR):** PDFs ohne Textebene (Scan, „Microsoft Print to PDF“
  mit Glyphen als Vektorpfaden) werden mit pypdfium2 gerendert (300 dpi) und
  per tesseract (`deu`, Wortpositionen als TSV) gelesen. Für das UC-Layout
  trennen die Wortpositionen die Spalten (Datum | Valuta | Zweck | Empfänger/
  IBAN/BIC | Umsatz); die Umsatzsumme wird gegen Anfangs-/Endsaldo des Auszugs
  geprüft und eine Abweichung als möglicher Lesefehler gemeldet. Andere Layouts
  laufen mit dem erkannten Fließtext durch die Text-Parser. Ohne installiertes
  tesseract gibt es eine klare Meldung statt eines leeren Ergebnisses.
- **ZIP-Pakete (Auszug + Postenlisten):** Ein ZIP bündelt den Kontoauszug mit
  den UC-eBanking-Postenlisten der Sammelüberweisungen („Postenliste – SEPA
  Zahlung (CCT)“, jeweils mit angehängten Belegen). Jede PDF im Paket wird
  anhand ihrer ersten Seite eingeordnet (Postenliste, Auszug mit/ohne
  Textebene, sonstiger Beleg – nur so weit gelesen wie nötig). Postenlisten:
  Empfänger, IBAN (Prüfziffer-validiert, auch über Zeilenumbrüche), Zweck und
  Betrag je Posten, gegengeprüft gegen Gesamtbetrag und Postenzahl. Die
  Auszugszeile einer Sammelüberweisung wird über Gesamtbetrag (exakt),
  „POSTEN n“ und Nähe zum Ausführungsdatum gefunden und durch die
  Einzelzahlungen ersetzt (Datum/Auszugsnummer aus der Auszugszeile). War die
  Sammelzeile früher bereits als Ganzes verbucht, gelten ihre Einzelzahlungen
  als verbucht. Manche Banken (z. B. Deutsche Bank im selben UC-Layout) buchen
  die Posten eines Sammelauftrags einzeln: Dann wird jeder Posten über Betrag,
  Empfänger-IBAN (aus „KTO: …“ der Empfängerspalte, auch von der OCR zerteilt)
  und Wortübereinstimmung im Verwendungszweck seiner Auszugszeile zugeordnet –
  letztere trennt gleiche Beträge an denselben Empfänger. Die Zeile übernimmt
  Empfänger und Zweck aus der textbasierten Postenliste (zuverlässiger als die
  OCR). Bei unvollständigem Abgleich zählen nur IBAN-bestätigte Treffer.
  Mehrseitige Auszüge: Spaltenkopf je Seite, der Seitenfuß beendet den
  laufenden Block; die Seiten werden parallel erkannt (je tesseract-Prozess ein
  Thread – das interne OpenMP-Threading überbucht sich sonst). SEPA-Buchungs-
  texte werden auf Buchungsart + SVWZ+-Inhalt reduziert (EREF+/KREF+ entfallen;
  27-Zeichen-Zeilen gelten als mitten im Text umbrochen). Ohne lesbaren Auszug werden die Einzelzahlungen zum
  Ausführungsdatum übernommen (mit Hinweis). Schutz gegen ZIP-Bomben:
  max. 100 Dateien bzw. 200 MB entpackt.

Unbekannte Konten werden beim Import gemeldet und können direkt angelegt werden.

## 7a. KPI-Seite (Fact Sheet)

Nach dem Vorbild des Excel-„Fact Sheet“ (Liquidität · Ein-/Auszahlungen ·
Anpassungen gegenüber früheren Planständen · KPIs zu Umsatz und Kosten). Alle
Werte stammen aus `berechne_plan` (Ist bis heute, Plan ab morgen) und rechnen
damit identisch zum 13-Wochen-Plan (`services/kpi.py`, `GET /mandanten/{id}/kpi`).

- **Soll-Liquidität eines Planstands:** Ist-Liquidität am Vortag des Stichtags
  (Bestandsanker bis heute, ggf. von einem späteren Kontoauszug zurückgerechnet)
  plus kumulierte liquiditätswirksame Planflüsse des Snapshots. Die Soll-Linie nutzt
  die Soll-Referenz bzw. den jüngsten passenden Planstand.
- **Planvergleich („Anpassungen“):** aktueller Stand (Ist + Prognose) gegen einen
  frei wählbaren Planstand, im gemeinsamen Zeitraum je BWA-Gruppe und Monat
  (Liquiditätswirkung, positiv = besser) sowie als Monatsendliquidität. Mehrere
  Vergleiche des Fact Sheets (Vorwoche, Antrag, Stichtag) entsprechen der Wahl
  verschiedener Snapshots.
- **Vergleich zur Vorwoche / zum Antrag** (Fact-Sheet-Blöcke „Entwicklung KW“ und
  „Entwicklung Antrag“, beide mit identischer Logik `_planstand_vergleich`):
  Planstand Vorwoche = jüngster Snapshot mit Stichtag vor dem Montag der laufenden
  Woche (bei wöchentlichem Einfrieren der Plan der Vorwoche), Antrag = jüngster
  Snapshot mit „Antrag“ im Kommentar, sonst der älteste; beide per Parameter
  `vorwoche_id`/`antrag_id` übersteuerbar. Die letzte abgeschlossene Woche
  (Montag–Sonntag vor der laufenden Woche) wird unabhängig vom angezeigten Fenster
  berechnet: Soll = Liquidität des Planstands zum Sonntag, Ist = Ist-Liquidität;
  je Kategorie Planflüsse des Snapshots gegen Ist-Flüsse der Woche, dazu die
  Über-/Unterdeckung. „Ergebnis“ = Liquidität am letzten gemeinsamen Tag von
  Fenster und Planstand; „Veränderung nach der Woche“ = Ergebnisveränderung −
  Wochenabweichung (Anteil, der sich bis dahin durch Zeitverschiebungen bzw.
  Planänderungen ausgleicht oder hinzukommt). Veränderungen je Kategorie und Monat
  wie beim Planvergleich; Vorwoche zusätzlich mit den Ein-/Auszahlungen der
  kommenden vier Wochen, Antrag mit der Gegenüberstellung Antrag/Vorwoche/Ist.
- **Kennzahlen:** tiefster Stand und erste Unterdeckung ab heute; Ist gegen Soll
  zum Ende der letzten abgeschlossenen Woche (sonst heute); Cash-Marge
  (Netto-Cashflow ÷ Einzahlungen) mit und ohne Gruppen mit Kennzeichen
  `konto_gruppen.sondereffekt` (bei der Migration für Gruppen mit „Insolvenz“ bzw.
  „Anfechtung“ im Namen vorbelegt); offene Forderungen/Verbindlichkeiten als
  Restbeträge.
- **Darstellung:** eigene, abhängigkeitsfreie SVG-Diagramme (`static/diagramme.js`)
  nach festen Regeln: schmale Marken mit gerundetem Datenende, Haarlinien-Raster,
  eine y-Achse, Legende ab zwei Reihen, sparsame Direktbeschriftung, Tooltip
  (Fadenkreuz bei Linien), Tabellenansicht je Diagramm; Farben gegen die weiße
  Kartenfläche auf Farbfehlsichtigkeit geprüft (Blau/Orange, divergierend Blau/Rot,
  Vergleichsstände als Grau-Hervorhebung). Die Kategorie-×-Monat-Matrix ist eine
  Tabelle (zu viele Klassen für ein Diagramm); Verteilungen sind Balken statt
  Torten (bessere Vergleichbarkeit bei vielen Kategorien).
- **PDF-Bericht** (`services/kpi_pdf.py`, `GET /mandanten/{id}/export/kpi.pdf`):
  gleiche Daten und Parameter (Start, Wochen, Vergleichsstand) wie die Seite; die
  Diagramme werden serverseitig mit reportlab-Grafiken nach denselben Regeln und
  Farben gezeichnet (A4 quer, zwei Karten je Zeile). Der Bericht besteht aus
  Blöcken (Überschrift, Diagramme und deren Wertetabellen direkt darunter), die
  jeweils geschlossen auf einer Seite stehen; Tabellen werden nicht umbrochen.
  Passt ein Block nicht auf eine Seite (z. B. Kennzahlen + Liquidität bei 26 Wochen),
  werden seine Diagramme stufenweise flacher gezeichnet; Wochenwerte stehen als
  Quertabelle (je 13 Wochen eine Tabelle), die Veränderung der Monatsendliquidität
  als Fußzeile der Tabelle „Veränderung je Kategorie und Monat“.

## 8. Kontenrahmen

SKR03 und SKR04 werden als Vorlage mit den planungsrelevanten Konten (inkl. USt-Satz und
BWA-Gruppenzuordnung) je Mandant eingespielt und sind danach frei editier- und erweiterbar
(auch als CSV-Import). Ein leerer, eigener Kontenrahmen ist ebenfalls möglich. Die
Vorbelegungen (USt-Sätze, Gruppenzuordnung) sind Vorschlagswerte und je Mandant zu prüfen.

Zusätzlich können Administratoren **eigene Kontenrahmen-Vorlagen** definieren (Seite
„Kontenrahmen“): neu oder als Kopie von SKR03/SKR04 bzw. einer anderen Vorlage, mit
eigenen BWA-Gruppen und Konten. Beim Anlegen eines Mandanten steht jede Vorlage zur
Auswahl und wird – wie die festen – in den Mandanten kopiert; spätere Änderungen an der
Vorlage wirken nur auf danach angelegte Mandanten. Terminregeln neuer Mandanten aus
eigenen Vorlagen starten ohne Konto­verknüpfung (für die Historienschätzung das Konto an
der Regel nachtragen). Je Mandant sind auch die **BWA-Gruppen** selbst editierbar
(Konten-Seite): anlegen, umbenennen, Richtung/Sortierung ändern, löschen – zugeordnete
Konten bleiben dann ohne Gruppe erhalten.

## 9. Sicherheit & Mandantentrennung

- Jede API-Route prüft Login und Mandantenberechtigung; Queries sind immer auf
  `mandant_id` gefiltert.
- Rollen: `ADMIN` (alles, Benutzerverwaltung), `BEARBEITER` (zugeordnete Mandanten
  schreibend), `LESER` (lesend).
- Session-Cookies signiert (`SECRET_KEY` zwingend setzen), Passwort-Hashing PBKDF2.
  Die Sitzung trägt einen Fingerabdruck des Passwort-Hashes: Nach einer
  Passwortänderung (eigene oder Zurücksetzen durch den Admin) sind alle übrigen
  Anmeldungen des Benutzers ungültig; die Sitzung, in der man das eigene Passwort
  ändert, bleibt bestehen.
- Benutzerverwaltung (Admin): Name, Rolle, Mandanten, Sperren, 2FA- und
  Passwort-Reset. Mit `benutzer.passwort_aendern` (beim Anlegen bzw. Zurücksetzen
  wählbar) muss der Benutzer beim nächsten Login ein eigenes, abweichendes Passwort
  festlegen; bis dahin leiten Seiten auf das Profil um und die API antwortet außer
  unter `/api/profil` mit 403. Der letzte aktive Administrator kann weder
  herabgestuft noch gesperrt werden, das eigene Konto nicht gesperrt; Änderungen
  stehen mit Details (z. B. „Rolle Bearbeiter → Leser“, nie das Passwort) im Audit-Log.
- Betrieb hinter Reverse Proxy mit TLS empfohlen (siehe Deployment-Doku).

## 10. Ausblick (mögliche weitere Ausbaustufen)

Die ursprüngliche Roadmap ist vollständig umgesetzt. Sinnvolle nächste Schritte:

1. USt-Sondervorauszahlung (1/11) und Schonfristen im Zahlungskalender.
2. Löschkonzept nach Verfahrensende (DSGVO) mit Mandanten-Archivierung.
3. Optionale Übernahme von Bankumsätzen als Ist-Quelle für Mandanten ohne
   laufende Buchhaltung (heute bewusst getrennt gehalten).
4. Mehrsprachigkeit und Mandanten-Branding für Berichte.
