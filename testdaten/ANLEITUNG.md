# Testdaten „Nordlicht Möbelwerk GmbH“ – Einspielanleitung

Vollständiger Testdatensatz für einen neuen Mandanten: drei Monate
DATEV-Buchungsstoff auf **allen Konten der SKR03-Vorlage**, BWA-Historie,
OP-Liste mit Altverbindlichkeiten, Kontoauszüge (MT940 + CAMT.053) mit
Zahlungen, die exakt zu den offenen Posten passen. Damit lassen sich alle
Funktionen der Anwendung durchspielen – vom Import über den automatischen
OP-Ausgleich bis zu Szenarien, Soll/Ist und Export.

Die Dateien liegen fertig generiert in [`nordlicht/`](nordlicht/) (Stichtag
**06.08.2026**); alle konkreten Werte, Salden und erwarteten Ergebnisse stehen
in [`nordlicht/UEBERSICHT.md`](nordlicht/UEBERSICHT.md). Für einen Test „ab
heute“ den Datensatz einfach neu erzeugen – die Übersicht wird mitgeschrieben:

```bash
python testdaten/erzeuge_testdaten.py --stichtag 2026-09-14   # beliebiges Datum
```

Das Skript braucht nur die Python-Standardbibliothek. Fachlicher Rahmen:
Möbelhersteller im **vorläufigen Insolvenzverfahren** (Antrag = Stichtag − 3
Wochen), Buchhaltung eine Woche im Rückstand, rückläufige Umsätze, Bankbestand
von 165.000 € auf rund 40.000 € abgeschmolzen.

## 1. Mandant anlegen

„Mandanten → Neuer Mandant“ – Werte aus der Tabelle in `UEBERSICHT.md`
(Name *Nordlicht Möbelwerk GmbH*, SKR03, Schleswig-Holstein, USt monatlich
**mit Dauerfrist**, Verfahrensstatus *Vorläufiges Verfahren*, Insolvenz-Stichtag
**16.07.2026**). Der Insolvenz-Stichtag steuert die automatische
§ 38-Einstufung beim OP-Import – nicht vergessen.

## 2. IBAN am Bankkonto hinterlegen

„Konten“ → Konto **1200 Bank** → IBAN `DE02 1203 0000 0000 2020 51` eintragen.
Darüber ordnet der Kontoauszugsimport die Auszüge automatisch zu. Optional für
schönere Planverteilung: Konto 4110 (Löhne) auf Verteilung „Monatsende“,
Konto 4930 auf „Zahllauf freitags“ stellen.

## 3. Buchhaltung importieren („Import“-Seite)

1. `01_buchungen_2026-05_datev.csv`, `02_…-06…`, `03_…-07…` – Format
   „automatisch erkennen“. Erwartung: 46 / 48 / 44 Sätze, **keine** Meldung
   über nicht angelegte Konten.
2. `04_bwa_saldenliste_12monate.csv` – Format **„BWA / Saldenliste“** wählen
   (144 Werte, 12 Monate Historie).

## 4. OP-Liste importieren („Posten“-Seite, Reiter „Offene Posten“)

`05_op_liste.csv` über „OP-Liste importieren“ hochladen. Erwartung:
**20 Posten angelegt**, davon **2 automatisch als Insolvenzforderung (§ 38)**
eingestuft (Lackierwerk Brandt 12.680 €, Maschinenfabrik Otte 7.140 € –
Rechnungsdatum vor dem Stichtag). Die gesperrte Summe von **19.820 €**
erscheint als Hinweis auf der Posten- und der Plan-Seite und wird nicht als
Auszahlung geplant.

## 5. Kontoauszüge importieren („Import“-Seite, unterer Abschnitt)

Erst `06_kontoauszug_mt940.sta` (7 Umsätze), dann `07_kontoauszug_camt053.xml`
(2 Umsätze). Beide werden über die IBAN dem Konto 1200 zugeordnet; der Endsaldo
jedes Auszugs wird automatisch als **Bestandsanker** übernommen (Werte in der
Übersicht). Damit ist der Bankbestand tagesaktuell, obwohl die Buchhaltung nur
bis 30.07. reicht – so soll es sein.

## 6. Bestände erfassen („Einstellungen → Bestände“)

Kasse (Konto 1000) und Warenbestand mit den Werten/Daten aus der Übersicht
anlegen (Kasse **1.348,50 €**, Waren **68.500 €** per 05.08.2026).

## 7. Dauerbuchungen und Budgets anlegen (Planbasis)

„Posten → Dauerbuchungen“ (passend zum importierten Zahlungsverhalten):

| Name | Konto | Betrag brutto | Intervall | Stichtag |
|---|---|---|---|---|
| Miete Werk + Büro | 4210 | 8.900,00 | monatlich | 1 |
| Versicherungspaket | 4360 | 1.180,00 | monatlich | 5 |
| Darlehenszinsen Nordbank | 2100 | 640,00 | monatlich | 15 |
| Tilgung Darlehen Nordbank | 0630 | 2.100,00 | monatlich | 15 |
| Buchführung StB Petersen | 4955 | 980,00 | monatlich | 24 |

„Budget“ (netto je Monat, für den laufenden und die drei Folgemonate):

| Konto | netto/Monat | | Konto | netto/Monat |
|---|---|---|---|---|
| 8400 Erlöse 19 % | 62.000 | | 4120 Gehälter | 11.200 |
| 8300 Erlöse 7 % | 3.200 | | 4900 Sonst. Aufwand | 1.000 |
| 3400 Wareneingang | 26.000 | | 4950 Beratung | 4.000 |
| 3100 Fremdleistungen | 3.000 | | 4920 Telefon | 350 |
| 4110 Löhne | 16.500 | | 4930 Bürobedarf | 250 |

## 8. Zahlungstermine generieren („Kalender“-Seite)

SV, USt-VA und LSt sind ab Werk aktiv mit Betragsmodus **Historie** – die
Beträge werden aus den importierten Buchungen geschätzt (SV ≈ 9.800, LSt ≈
5.100, USt ≈ 6.600). „Termine generieren“ ausführen. Optional unter
„Einstellungen → Terminregeln“: GewSt (fix 2.850, nächster Termin 17.08.) und
KSt (fix 1.600) aktivieren; USt-VA auf **Budget** umstellen zeigt die
Zahllast-Vorschau aus den Budgets von Schritt 7.

## 9. Automatischen OP-Ausgleich testen („Posten → Bankabgleich“)

„Vorschläge aktualisieren“ – erwartet werden **6 Vorschläge**, alle vier
Mechanismen auf einmal (Details in der Übersicht):

- **Exakt**: Möbelhaus Kern +11.900 → RE-2026-1041 (sicher, vorausgewählt);
  ebenso Küchenstudio Lund +8.330 und Stadtwerke −1.350 aus dem CAMT-Auszug.
- **Sammelüberweisung**: Holz Petersen −7.735 → RE-P-77812 **+** RE-P-77903
  (beide Belegnummern im Verwendungszweck, sicher).
- **Teilzahlung**: Objekt Living +5.000 auf RE-2026-1042 (12.495) – „möglich“,
  nie vorausgewählt; nach Übernahme bleibt der Posten mit **7.495 € Rest**
  offen und die Planung rechnet mit dem Rest.
- **Skonto**: Callsen IT −2.910 auf RE-IT-2201 (3.000, exakt 3 % – sicher).

Miete −8.900 (Dauerbuchung, kein OP), Kartenumsätze +1.250 und Kontoführung
−42,50 bekommen **keinen** Vorschlag – erwünschtes Verhalten. Nach „Ausgewählte
übernehmen“ sind die Posten bezahlt bzw. teilbezahlt („aufheben“ macht alles
rückgängig).

## 10. Plan prüfen und weiterspielen

Auf der Plan-Seite sollte jetzt ein vollständiges Bild stehen: Ist-Zahlen bis
30.07., Bankbestand aus den Auszugsankern, Plan aus offenen Posten (der
überfällige Eingang Wohnwelt Harms wird auf den nächsten Bankarbeitstag
gerollt), Dauerbuchungen, Budgets (mit Restbudget-Logik) und Steuerterminen;
dazu der Sperrhinweis über 19.820 € Insolvenzforderungen. Von hier aus lässt
sich alles Weitere durchspielen:

- **Insolvenzgeld-Assistent** (Einstellungen): Zeitraum aus der Übersicht
  aktivieren → Personal-/SV-Zahlungen im Fenster entfallen, SV/LSt-Termine
  werden anteilig gekürzt.
- **Szenarien**: Best/Worst Case auf der Plan-Seite umschalten,
  Vergleichsansicht öffnen, Detailregeln je Konto/Gruppe ergänzen.
- **Plan einfrieren** → Soll/Ist-Vergleich; **Excel-/PDF-Export** erzeugen.
