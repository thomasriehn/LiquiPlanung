# Testdatensatz „Nordlicht Möbelwerk GmbH“ – Übersicht

Erzeugt mit `erzeuge_testdaten.py --stichtag 2026-08-06`. Alle Werte sind
in sich konsistent durchgerechnet; die Schritt-für-Schritt-Anleitung steht in
[ANLEITUNG.md](../ANLEITUNG.md).

## Mandant (bei „Neuer Mandant“ eintragen)

| Feld | Wert |
|---|---|
| Name | Nordlicht Möbelwerk GmbH |
| Kurzname | nordlicht |
| Kontenrahmen | SKR03 |
| Bundesland | Schleswig-Holstein (SH) |
| USt-Zeitraum | Monat, **mit Dauerfristverlängerung** |
| Verfahrensstatus | Vorläufiges Verfahren |
| Aktenzeichen | 58 IN 71/26 (AG Flensburg) |
| Insolvenz-Stichtag (Antrag) | **16.07.2026** |
| Insolvenzgeld (optional) | 02.07.2026 – 30.09.2026 (erwartete Eröffnung ≈ 01.10.2026) |

Bankkonto 1200: IBAN **DE02120300000000202051** (unter „Konten“ eintragen, sonst
findet der Kontoauszugsimport das Konto nicht automatisch).

## Bestände (unter „Einstellungen → Bestände“ manuell erfassen)

| Typ | Konto | Datum | Wert |
|---|---|---|---|
| Kasse | 1000 | 05.08.2026 | **1348,50 €** |
| Waren (nachrichtlich) | – | 05.08.2026 | 68500,00 € |

Der Bankbestand kommt automatisch aus den Kontoauszügen (Endsaldo je Auszug).

## Dateien und Salden

| Datei | Inhalt | Sätze |
|---|---|---|
| `01_buchungen_2026-05_datev.csv` | DATEV-Buchungsstapel Mai 2026 | 46 |
| `02_buchungen_2026-06_datev.csv` | DATEV-Buchungsstapel Juni 2026 | 48 |
| `03_buchungen_2026-07_datev.csv` | DATEV-Buchungsstapel Juli 2026 | 44 |
| `04_bwa_saldenliste_12monate.csv` | BWA-/Saldenliste 12 Monate | 144 |
| `05_op_liste.csv` | OP-Liste (offene Posten) | 20 |
| `06_kontoauszug_mt940.sta` | Kontoauszug MT940 (03.08.–05.08.) | 7 |
| `07_kontoauszug_camt053.xml` | Kontoauszug CAMT.053 (06.08.) | 2 |

Buchhaltung erfasst bis **30.07.2026** (bewusst ~1 Woche Rückstand –
die aktuelle Woche kommt nur über die Kontoauszüge herein, wie in der Praxis).

| Saldo | Wert |
|---|---|
| Bank zu Beginn (01.05.2026) | 165000,00 € |
| Bank nach Buchhaltung (= Anfangssaldo MT940, 31.07.2026) | **40383,00 €** |
| Endsaldo MT940 (05.08.2026) | **38945,50 €** |
| Endsaldo CAMT.053 (06.08.2026) | **45925,50 €** |
| Kasse (30.07.2026) | 1348,50 € |

## Offene Posten (Import der OP-Liste)

- 20 Posten: 12 Eingangsrechnungen (40025,00 €), 8 Forderungen (78255,00 €).
- **2 Posten** (Lackierwerk Brandt, Maschinenfabrik Otte; Rechnungsdatum vor dem
  16.07.2026) werden automatisch als **Insolvenzforderung (§ 38)** eingestuft
  und gesperrt: zusammen **19820,00 €**.
- Wohnwelt Harms (07.07.2026) ist überfällig – die Planung rollt
  den Eingang automatisch auf den nächsten Bankarbeitstag.

## Erwartete Abgleichvorschläge (Posten → Bankabgleich)

| Bankumsatz | Betrag | Vorschlag | Art | Konfidenz |
|---|---|---|---|---|
| Möbelhaus Kern (03.08.) | +11.900,00 | RE-2026-1041 | exakt | sicher |
| Holz Petersen (04.08.) | −7.735,00 | RE-P-77812 + RE-P-77903 | Sammelüberweisung (2 Posten) | sicher |
| Objekt Living (05.08.) | +5.000,00 | RE-2026-1042 | Teilzahlung (Rest 7.495,00) | möglich, nie vorausgewählt |
| Callsen IT (05.08.) | −2.910,00 | RE-IT-2201 (3 % Skonto) | voll mit Skonto | sicher |
| Küchenstudio Lund (06.08., CAMT) | +8.330,00 | RE-2026-1043 | exakt | sicher |
| Stadtwerke Flensburg (06.08., CAMT) | −1.350,00 | A-2026-08 | exakt (ohne Beleg im Zweck) | sicher |

Ohne Vorschlag bleiben: Miete −8.900,00 (Dauerbuchung, kein OP),
Kartenumsätze +1.250,00, Kontoführung −42,50 – so soll es sein.
