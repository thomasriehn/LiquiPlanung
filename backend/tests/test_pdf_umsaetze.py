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
