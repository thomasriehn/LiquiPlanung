"""PDF-Kontoumsätze: Parser und Verbuchung als Ist-Buchungen."""

from datetime import date
from decimal import Decimal
from io import BytesIO

from app.services.pdf_kontoauszug import parse_pdf, parse_text

from test_api import _login

BEISPIEL_TEXT = """Verfahrenskonto-Umsätze
1 IN 999/26 Muster GmbH
Amtsgericht Musterstadt
Sparkasse Musterstadt– Girokonto
IBAN DE00000000000000000000, BIC XXX
Erstellungsdatum:
07.09.2026
Seite 1
Abfragezeitraum: 08.08.2026 – 07.09.2026
Anzahl gebuchte Posten: 5
Startsaldo 27.08.2026:
Schlusssaldo 04.09.2026:
0,00 €
72.854,70 €
Auszug-Nr. Auszugsdatum Buchung Verwendungszweck Empfänger/Auftraggeber Betrag in €
00004 04.09.2026 04.09.2026 EINZELÜBERWEISUNG
Abschlag (Strom) September / 2026
ABWA: Verwalter
E.ON Energie Deutschland
GmbH
-140,00
00003 03.09.2026 03.09.2026 EINZELÜBERWEISUNG
Vorschuss auf Zinsen für 08/26
IV Verwalter/Muster -
InsG Sicherheiten
-6.150,00
00002 01.09.2026 01.09.2026 ABSCHLUSS
Abrechnung 31.08.2026      siehe Anlage
Kontostand am 31.08.2026
38.228,56 +
Rechnungsnummer: 20260831-X
0,00
00001 27.08.2026 24.08.2026 GUTSCHRIFT ÜBERWEISUNG
Auskehrung Kontoguthaben Teilbetrag
Muster GmbH 26.300,00
"""


def test_pdf_text_parser():
    erg = parse_text(BEISPIEL_TEXT)
    assert not erg.warnungen
    assert len(erg.umsaetze) == 4

    strom = erg.umsaetze[0]
    assert strom.auszug_nr == "00004"
    assert strom.auszugsdatum == date(2026, 9, 4)
    assert strom.buchungstag == date(2026, 9, 4)
    assert strom.betrag == Decimal("-140.00")
    # mehrzeiliger Empfänger (Umbruch vor „GmbH“) wird zusammengesetzt
    assert strom.empfaenger == "E.ON Energie Deutschland GmbH"
    assert "Abschlag (Strom)" in strom.verwendungszweck
    assert "GmbH" not in strom.verwendungszweck

    zinsen = erg.umsaetze[1]
    assert zinsen.betrag == Decimal("-6150.00")
    assert zinsen.empfaenger == "IV Verwalter/Muster - InsG Sicherheiten"

    # Abschlusszeile: innere Saldoangaben („38.228,56 +“) werden nicht als
    # Betrag gewertet, der Umsatz endet mit 0,00
    assert erg.umsaetze[2].betrag == Decimal("0.00")

    gutschrift = erg.umsaetze[3]
    assert gutschrift.betrag == Decimal("26300.00")
    assert gutschrift.empfaenger == "Muster GmbH"
    assert gutschrift.auszugsdatum == date(2026, 8, 27)
    assert gutschrift.buchungstag == date(2026, 8, 24)


def test_pdf_text_ohne_umsaetze():
    erg = parse_text("Irgendein Brief\nohne Umsatzzeilen\n")
    assert not erg.umsaetze
    assert any("Keine Umsätze" in w for w in erg.warnungen)


def _beispiel_pdf(zeilen: list[str]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    puffer = BytesIO()
    c = canvas.Canvas(puffer, pagesize=A4)
    y = 800
    for zeile in zeilen:
        c.drawString(40, y, zeile)
        y -= 14
    c.showPage()
    c.save()
    return puffer.getvalue()


PDF_ZEILEN = [
    "Verfahrenskonto-Umsätze",
    "00002 04.09.2026 04.09.2026 EINZELÜBERWEISUNG",
    "Abschlag Strom September",
    "ABWA: Verwalter",
    "Stadtwerke Musterstadt -140,00",
    "00001 27.08.2026 24.08.2026 GUTSCHRIFT ÜBERWEISUNG",
    "Auskehrung Kontoguthaben",
    "Muster GmbH 26.300,00",
]


def test_pdf_datei_parser():
    erg = parse_pdf(_beispiel_pdf(PDF_ZEILEN))
    assert not erg.warnungen
    assert [float(u.betrag) for u in erg.umsaetze] == [-140.00, 26300.00]
    assert erg.umsaetze[0].empfaenger == "Stadtwerke Musterstadt"


def test_pdf_kaputt():
    erg = parse_pdf(b"kein pdf")
    assert not erg.umsaetze
    assert erg.warnungen


def test_pdf_umsaetze_api(client):
    _login(client)
    mandant_id = client.post(
        "/api/mandanten", json={"name": "PDF GmbH", "kurzname": "pdf-gmbh"}
    ).json()["id"]
    konten = {k["nummer"]: k for k in
              client.get(f"/api/mandanten/{mandant_id}/konten").json()["konten"]}

    antwort = client.post(
        f"/api/mandanten/{mandant_id}/pdf-umsaetze",
        files={"datei": ("auszug.pdf", _beispiel_pdf(PDF_ZEILEN), "application/pdf")},
    )
    assert antwort.status_code == 200, antwort.text
    erg = antwort.json()
    batch_id = erg["batch_id"]
    ausgaben = [u for u in erg["umsaetze"] if u["betrag"] < 0]
    assert len(ausgaben) == 1
    u = ausgaben[0]

    # Ausgabe auf Konto 4240 verbuchen: Sachkonto an Bank (DATEV-Logik)
    verbuchung = {
        "batch_id": batch_id,
        "konto_id": konten["4240"]["id"],
        "bank_konto_id": konten["1200"]["id"],
        "datum": u["buchungstag"],
        "betrag": u["betrag"],
        "text": f'{u["empfaenger"]} – {u["verwendungszweck"]}',
    }
    antwort = client.post(f"/api/mandanten/{mandant_id}/pdf-umsaetze/verbuchen", json=verbuchung)
    assert antwort.status_code == 200, antwort.text
    assert antwort.json()["konto"].startswith("4240")

    # Doppelte Übernahme desselben Umsatzes wird abgelehnt
    assert client.post(
        f"/api/mandanten/{mandant_id}/pdf-umsaetze/verbuchen", json=verbuchung
    ).status_code == 409
    # Konto = Gegenkonto und Betrag 0 sind ungültig
    assert client.post(
        f"/api/mandanten/{mandant_id}/pdf-umsaetze/verbuchen",
        json={**verbuchung, "konto_id": konten["1200"]["id"]},
    ).status_code == 422
    assert client.post(
        f"/api/mandanten/{mandant_id}/pdf-umsaetze/verbuchen",
        json={**verbuchung, "betrag": 0},
    ).status_code == 422

    # Einzahlung (positiver Betrag): Bank an Erlöskonto
    einzahlung = [x for x in erg["umsaetze"] if x["betrag"] > 0][0]
    antwort = client.post(
        f"/api/mandanten/{mandant_id}/pdf-umsaetze/verbuchen",
        json={"batch_id": batch_id, "konto_id": konten["8400"]["id"],
              "bank_konto_id": konten["1200"]["id"], "datum": einzahlung["buchungstag"],
              "betrag": einzahlung["betrag"], "text": "Gutschrift"},
    )
    assert antwort.status_code == 200

    # Import-Batch zählt die Buchungen und ist gesammelt löschbar
    batch = next(b for b in client.get(f"/api/mandanten/{mandant_id}/importe").json()
                 if b["id"] == batch_id)
    assert batch["format"] == "PDF"
    assert batch["anzahl"] == 2
    assert client.delete(f"/api/importe/{batch_id}").json()["ok"] is True


DEUTSCHE_BANK_TEXT = """8. Juli 2026
Kontoauszug vom 08.07.2026 bis 08.07.2026
Kontoinhaber: Max Verwalter c/o Kanzlei
Alter Saldo per 07.07.2026IBANvonSeiteAuszug
+ 178.228,05EURDE62 7007 0024 0130 9665 375122
HabenSollVorgangValutaBuchung
+ 204,37SEPA Überweisung von
Bersch Hubert Schlosserei/Landtechnik
08.07.
2026
08.07.
2026
Verwendungszweck/ Kundenreferenz
Renr. 202651234 v. 24.06.26 Kdnr. 27250
NONREF
- 14,99SEPA Überweisung an
1 + 1 Telekom
08.07.
2026
08.07.
2026
IBAN DE97500400000589011600
BIC COBADEFFXXX
Verwendungszweck/ Kundenreferenz
Rechnung Mobil Juni-Juli RG Nr. 1511575056
IBANvonSeiteAuszug
DE62 7007 0024 0130 9665 375222
0000000003 / 06500900 / 20260708
HabenSollVorgangValutaBuchung
- 1.899,09SEPA Überweisung an
ABL-Technic Entlackung
08.07.
2026
08.07.
2026
Verwendungszweck/ Kundenreferenz
Rechnung-Reparatur
2607080811-0000015
Deutsche Bank AG
Service Center Berlin
Herrn
Max Verwalter
10585 Berlin
Neuer Saldo
+ 176.518,34EUR
"""


def test_pdf_deutsche_bank():
    erg = parse_text(DEUTSCHE_BANK_TEXT)
    assert not erg.warnungen
    assert len(erg.umsaetze) == 3

    haben = erg.umsaetze[0]
    assert haben.betrag == Decimal("204.37")
    assert haben.empfaenger == "Bersch Hubert Schlosserei/Landtechnik"
    assert haben.buchungstag == date(2026, 7, 8)
    assert haben.auszugsdatum == date(2026, 7, 8)        # „Kontoauszug … bis“
    assert "Renr. 202651234" in haben.verwendungszweck
    assert haben.verwendungszweck.startswith("SEPA Überweisung von")

    soll = erg.umsaetze[1]
    assert soll.betrag == Decimal("-14.99")
    assert soll.empfaenger == "1 + 1 Telekom"
    # IBAN/BIC des Zahlungsempfängers landen nicht im Verwendungszweck
    assert "DE97" not in soll.verwendungszweck
    assert "Rechnung Mobil" in soll.verwendungszweck

    # Adressblock am Seitenende verschmutzt den Zweck nicht
    letzte = erg.umsaetze[2]
    assert letzte.betrag == Decimal("-1899.09")
    assert "Deutsche Bank" not in letzte.verwendungszweck
    assert "Berlin" not in letzte.verwendungszweck
    assert "2607080811-0000015" in letzte.verwendungszweck
    # Saldozeilen sind keine Umsätze
    assert all(abs(u.betrag) < Decimal("100000") for u in erg.umsaetze)


UC_TEXT = """UC eBanking prime › CAMT Auszüge
Muster GmbH vIV
Auszug Nr. 32
Kontonummer 406030038 Endsaldo 137.789,65  EUR
Bankleitzahl 60050101 Kontoauszugsdatum 09.09.2026
IBAN DE21 6005 0101 0406 0300 38 Anzahl Posten 3
Summe Soll (2 Posten) -2.421,60 EUR
Datum Valuta Buchungsinformationen
Umsatz EUR
09.09.2026 09.09.2026 GUTSCHRIFT ÜBERWEISUNG
Belegnummer IN26101172
Eryurt, Ferhat und Yasemin
DE45670505050039698382
MANSDE66
85,90
09.09.2026 09.09.2026 EINZELÜBERWEISUNG
Rechnung 00241578 Cloud für September
2026
weclapp
NL06ABNA0127334777
ABNANL2AXXX
-2.326,45
09.09.2026 09.09.2026 EINZELÜBERWEISUNG
FPC 2026FPC/22
J.P.Freitas, Lda
PT50004513244033815380934
CCCMPTPLXXX
-95,15
Seite 1 von 1
UC eBanking prime
Version 6.2.0
© UniCredit Bank GmbH
"""


def test_pdf_uc_ebanking():
    erg = parse_text(UC_TEXT)
    assert not erg.warnungen
    assert len(erg.umsaetze) == 3
    assert all(u.auszug_nr == "32" for u in erg.umsaetze)
    assert all(u.auszugsdatum == date(2026, 9, 9) for u in erg.umsaetze)

    gutschrift = erg.umsaetze[0]
    assert gutschrift.betrag == Decimal("85.90")
    assert gutschrift.empfaenger == "Eryurt, Ferhat und Yasemin"
    assert gutschrift.verwendungszweck == "GUTSCHRIFT ÜBERWEISUNG Belegnummer IN26101172"

    weclapp = erg.umsaetze[1]
    assert weclapp.betrag == Decimal("-2326.45")
    assert weclapp.empfaenger == "weclapp"
    # Zweck-Zeile „2026“ (Umbruch) wird nicht als Betrag fehlinterpretiert
    assert "Cloud für September 2026" in weclapp.verwendungszweck
    assert "NL06" not in weclapp.verwendungszweck

    assert erg.umsaetze[2].empfaenger == "J.P.Freitas, Lda"
