"""ZIP-Kontoauszüge: Postenlisten von Sammelüberweisungen, Texterkennung."""

import zipfile
from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest

from app.services.pdf_kontoauszug import (
    OcrWort,
    _iban_finden,
    _parse_uc_ocr,
    ocr_verfuegbar,
    parse_datei,
    parse_pdf,
    parse_postenliste,
    parse_zip,
)

from test_api import _login
from test_pdf_umsaetze import _beispiel_pdf


def _iban(blz: str, konto: str) -> str:
    bban = blz + konto.zfill(10)
    return f"DE{98 - int(bban + '131400') % 97:02d}{bban}"


def _umbrochen(iban: str) -> str:
    """Schreibweise der Postenliste: Vierergruppen, Rest in der Folgezeile."""
    return " ".join(iban[i:i + 4] for i in range(0, 20, 4)) + "\n" + iban[20:]


IBAN_EIGEN = _iban("70020270", "4711")
IBAN_BUERO = _iban("70010080", "1008228")
IBAN_TELEFON = _iban("25010030", "2893")
IBAN_SPEDITION = _iban("61170076", "15115")
IBAN_FACTORING = _iban("79550000", "8206450")
IBAN_KUNDE = _iban("37040044", "532013000")


def _postenliste(nummer: str, posten_text: str, anzahl: int, gesamt: str) -> str:
    return f"""UC eBanking prime › SEPA Zahlungen (Credit)
Postenliste - SEPA Zahlung (CCT)
Auftrag: {nummer}
Allgemeine Informationen
Details Auftragsnummer {nummer} Beschreibung
Erstellungsdatum 09.10.2026 13:57:18 BIC HYVEDEMMXXX
Auftraggeber Muster, Max Ausführungsdatum 09.10.2026
IBAN {_umbrochen(IBAN_EIGEN)}
Währung EUR
Kontoname Muster Technik GmbH logische Datei(en) 1
Details
Empfänger BIC IBAN Verwendungszweck Betrag Währung
{posten_text}
Gesamtbetrag

Posten : {anzahl} {gesamt} EUR
Seite 1 von
1UC eBanking prime
Version 6.2.0
"""


LISTE_A = _postenliste("PRM01A", f"""Bürobedarf Schmidt {_umbrochen(IBAN_BUERO)}
RE 4711 79,09 EUR
Telefon Deutschland
GmbH
{_umbrochen(IBAN_TELEFON)}
RE 785 410 Kd.Nr. 174
Festnetzrechnung 09/2026
242,05 EUR
Spedition Nord GmbH & Co.
KG
NOLADE21XXX {_umbrochen(IBAN_SPEDITION)}
Fracht 10/2026 1.130,50 EUR""", 3, "1.451,64")

LISTE_B = _postenliste("PRM01B", f"""Factoring Süd GmbH
BYLADEM1ASA {_umbrochen(IBAN_FACTORING)}
Ablöse Eigentumsvorbehalt
2.300,00 EUR""", 1, "2.300,00")

# Kontoauszug im UC-eBanking-Layout mit Textebene: die Sammelüberweisung
# erscheint nur als Gesamtbetrag mit „POSTEN 3“
AUSZUG_ZEILEN = [
    "UC eBanking prime › CAMT Auszüge",
    "Muster Technik GmbH",
    "Auszug Nr. 7",
    "Kontoauszugsdatum 10.10.2026",
    "Datum Valuta Buchungsinformationen",
    "Umsatz EUR",
    "10.10.2026 10.10.2026 SEPA-Ueberweisungsdatei",
    "ÜBERWEISUNG",
    "POSTEN 3 DATEI",
    "CTD261010KMVE0001",
    "-1.451,64",
    "10.10.2026 10.10.2026 Ueberweisung",
    "Ablöse Eigentumsvorbehalt",
    "Factoring Süd GmbH",
    IBAN_FACTORING,
    "BYLADEM1ASA",
    "-2.300,00",
    "10.10.2026 10.10.2026 GUTSCHRIFT ÜBERWEISUNG",
    "RE 2026-117",
    "Kunde AG",
    IBAN_KUNDE,
    "500,00",
    "Seite 1 von 1",
]


def _pdf_aus_text(text: str) -> bytes:
    return _beispiel_pdf(text.splitlines())


def _zip(dateien: dict[str, bytes]) -> bytes:
    puffer = BytesIO()
    with zipfile.ZipFile(puffer, "w") as z:
        for name, inhalt in dateien.items():
            z.writestr(name, inhalt)
    return puffer.getvalue()


def _paket(auszug: bytes | None = None) -> bytes:
    dateien = {
        "ZA 1.451,64 Rechnungen.pdf": _pdf_aus_text(LISTE_A),
        "ZA 2.300,00 Factoring.pdf": _pdf_aus_text(LISTE_B),
        "Rechnung 4711.pdf": _beispiel_pdf(["Rechnung Nr. 4711", "Summe 79,09 EUR"]),
        "notiz.txt": b"nur eine Notiz",
        "__MACOSX/._Rechnung 4711.pdf": b"\x00",
    }
    if auszug is not None:
        dateien["10.10.2026 #7.pdf"] = auszug
    return _zip(dateien)


def test_iban_ueber_zeilenumbruch_mit_pruefziffer():
    text = f"Bürobedarf Schmidt {_umbrochen(IBAN_BUERO)}\nRE 4711 79,09 EUR"
    start, ende, iban = _iban_finden(text)
    assert iban == IBAN_BUERO
    assert text[:start].strip() == "Bürobedarf Schmidt"
    assert text[ende:].strip().startswith("RE 4711")
    # falsche Prüfziffer: keine IBAN
    assert _iban_finden("DE00 7001 0080 0100 8228 02") is None


def test_postenliste_parsen():
    auftrag, warnungen = parse_postenliste([LISTE_A, "RECHNUNG\nSeite 1 von 3"], "za.pdf")
    assert not warnungen
    assert auftrag.auftragsnummer == "PRM01A"
    assert auftrag.ausfuehrung == date(2026, 10, 9)
    assert (auftrag.anzahl, auftrag.gesamt) == (3, Decimal("1451.64"))
    assert [(p.empfaenger, p.verwendungszweck, p.betrag, p.iban) for p in auftrag.posten] == [
        ("Bürobedarf Schmidt", "RE 4711", Decimal("79.09"), IBAN_BUERO),
        ("Telefon Deutschland GmbH", "RE 785 410 Kd.Nr. 174 Festnetzrechnung 09/2026",
         Decimal("242.05"), IBAN_TELEFON),
        # BIC-Spalte zwischen Empfänger und IBAN gehört nicht zum Namen
        ("Spedition Nord GmbH & Co. KG", "Fracht 10/2026", Decimal("1130.50"), IBAN_SPEDITION),
    ]


def test_postenliste_summenpruefung():
    kaputt = LISTE_A.replace("Posten : 3 1.451,64", "Posten : 3 1.500,00")
    _, warnungen = parse_postenliste([kaputt], "za.pdf")
    assert any("bitte prüfen" in w for w in warnungen)


def test_zip_loest_sammelueberweisungen_auf():
    erg = parse_zip(_paket(_beispiel_pdf(AUSZUG_ZEILEN)))
    assert not erg.warnungen, erg.warnungen
    assert [(u.empfaenger, u.betrag, u.sammelauftrag) for u in erg.umsaetze] == [
        ("Bürobedarf Schmidt", Decimal("-79.09"), "PRM01A"),
        ("Telefon Deutschland GmbH", Decimal("-242.05"), "PRM01A"),
        ("Spedition Nord GmbH & Co. KG", Decimal("-1130.50"), "PRM01A"),
        ("Factoring Süd GmbH", Decimal("-2300.00"), "PRM01B"),
        ("Kunde AG", Decimal("500.00"), ""),
    ]
    einzel = erg.umsaetze[0]
    # Datum und Auszugsnummer kommen aus der Auszugszeile, nicht aus der Liste
    assert (einzel.auszug_nr, einzel.buchungstag, einzel.auszugsdatum) == (
        "7", date(2026, 10, 10), date(2026, 10, 10))
    assert einzel.ursprung[1].startswith("SEPA-Ueberweisungsdatei")
    assert any("2 Sammelüberweisung(en)" in h and "4 Einzelzahlungen" in h for h in erg.hinweise)
    assert any("Rechnung 4711.pdf" in h and "notiz.txt" in h for h in erg.hinweise)


def test_zip_sammelauftrag_nicht_im_auszug():
    ohne_factoring = [z for z in AUSZUG_ZEILEN if z not in (
        "10.10.2026 10.10.2026 Ueberweisung", "Ablöse Eigentumsvorbehalt",
        "Factoring Süd GmbH", IBAN_FACTORING, "BYLADEM1ASA", "-2.300,00")]
    erg = parse_zip(_paket(_beispiel_pdf(ohne_factoring)))
    assert any("PRM01B" in w and "nicht im Kontoauszug" in w for w in erg.warnungen)
    assert not any(u.sammelauftrag == "PRM01B" for u in erg.umsaetze)


def test_zip_nur_postenlisten():
    erg = parse_datei(_paket(auszug=None))
    assert len(erg.umsaetze) == 4
    assert all(u.buchungstag == date(2026, 10, 9) for u in erg.umsaetze)  # Ausführungsdatum
    assert any("Ausführungsdatum" in w for w in erg.warnungen)


def test_zip_kaputt():
    erg = parse_datei(b"PK\x03\x04kaputt")
    assert not erg.umsaetze and erg.warnungen


def _w(text: str, links: float, rechts: float) -> OcrWort:
    return OcrWort(text, links, rechts)


def _ocr_seite(anfang: str, ende: str) -> list[list[OcrWort]]:
    return [
        [_w("Auszug", .108, .152), _w("Nr.", .158, .174), _w("7", .180, .19)],
        [_w("Anfangssaldo", .607, .688), _w(anfang, .754, .831), _w("EUR", .84, .867)],
        [_w("Endsaldo", .634, .688), _w(ende, .754, .831), _w("EUR", .84, .867)],
        [_w("Kontoauszugsdatum", .567, .688), _w("10.10.2026", .802, .867)],
        [_w("Datum", .108, .134), _w("Valuta", .194, .22), _w("Buchungsinformationen", .281, .377),
         _w("Umsatz", .894, .924), _w("EUR", .927, .942)],
        [_w("10.10.2026", .109, .174), _w("10.10.2026", .195, .26),
         _w("SEPA-Ueberweisungsdatei", .281, .442), _w("-1.451,64", .887, .942)],
        [_w("POSTEN", .282, .335), _w("3", .396, .402), _w("DATEI", .419, .456)],
        [_w("10.10.2026", .109, .174), _w("10.10.2026", .195, .26), _w("Ueberweisung", .282, .366),
         _w("Factoring", .528, .58), _w("Süd", .585, .61), _w("GmbH", .615, .65),
         _w("-2.300,00", .887, .942)],
        [_w("Ablöse", .281, .321), _w("Eigentumsvorbehalt", .326, .444), _w("ATV", .448, .474),
         _w(IBAN_FACTORING, .528, .695)],
        [_w("Technik", .281, .33), _w("BYLADEM1ASA", .528, .626)],
        [_w("Endsaldo", .108, .144), _w("(in", .147, .156), _w("EUR)", .16, .178),
         _w(ende, .871, .942)],
        [_w("UC", .108, .117), _w("eBanking", .121, .154), _w("prime", .158, .18),
         _w("Gedruckt", .277, .31), _w("durch", .313, .333), _w("Benutzer", .337, .37)],
    ]


def test_ocr_layout_spalten_und_pruefsumme():
    erg = _parse_uc_ocr([_ocr_seite("10.000,00", "6.248,36")])
    assert [(u.empfaenger, u.verwendungszweck, u.betrag) for u in erg.umsaetze] == [
        ("", "SEPA-Ueberweisungsdatei POSTEN 3 DATEI", Decimal("-1451.64")),
        # Empfänger/IBAN/BIC stehen rechts neben dem Zweck in derselben Zeile
        ("Factoring Süd GmbH", "Ueberweisung Ablöse Eigentumsvorbehalt ATV Technik",
         Decimal("-2300.00")),
    ]
    assert erg.umsaetze[0].auszug_nr == "7"
    assert not erg.warnungen
    assert any("Prüfsumme stimmt" in h for h in erg.hinweise)

    # Lesefehler (Saldo passt nicht zu den Umsätzen) wird gemeldet
    erg = _parse_uc_ocr([_ocr_seite("10.000,00", "6.248,86")])
    assert any("Prüfsumme weicht ab" in w for w in erg.warnungen)


# ------------------------------------------- echte Texterkennung (tesseract)

def _auszug_als_bild_pdf() -> bytes:
    """UC-Auszug zeichnen, rendern und nur als Bild wieder einbetten – wie
    ein Scan bzw. „Microsoft Print to PDF“ ohne Textebene."""
    import pypdfium2 as pdfium
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    breite, hoehe = A4
    puffer = BytesIO()
    c = canvas.Canvas(puffer, pagesize=A4)
    c.setFont("Helvetica", 10)
    x = {"datum": .108 * breite, "valuta": .194 * breite, "info": .281 * breite,
         "partner": .528 * breite, "rechts": .942 * breite}
    y = hoehe - 60

    def zeile(*teile, betrag=None):
        nonlocal y
        for spalte, text in teile:
            c.drawString(x[spalte], y, text)
        if betrag:
            c.drawRightString(x["rechts"], y, betrag)
        y -= 15

    zeile(("datum", "UC eBanking prime CAMT Auszüge"))
    zeile(("datum", "Auszug Nr. 7"))
    zeile(("info", "Anfangssaldo 10.000,00 EUR"))
    zeile(("info", "Endsaldo 6.248,36 EUR"))
    zeile(("info", "Kontoauszugsdatum 10.10.2026"))
    y -= 10
    zeile(("datum", "Datum"), ("valuta", "Valuta"), ("info", "Buchungsinformationen"),
          betrag="Umsatz EUR")
    zeile(("datum", "10.10.2026"), ("valuta", "10.10.2026"), ("info", "SEPA-Ueberweisungsdatei"),
          betrag="-1.451,64")
    zeile(("info", "POSTEN 3 DATEI"))
    zeile(("datum", "10.10.2026"), ("valuta", "10.10.2026"), ("info", "Ueberweisung"),
          ("partner", "Factoring Süd GmbH"), betrag="-2.300,00")
    zeile(("info", "Ablöse Eigentumsvorbehalt"), ("partner", IBAN_FACTORING))
    zeile(("partner", "BYLADEM1ASA"))
    y -= 10
    zeile(("datum", "Endsaldo (in EUR)"), betrag="6.248,36")
    c.showPage()
    c.save()

    bild = pdfium.PdfDocument(puffer.getvalue())[0].render(scale=200 / 72).to_pil()
    ausgabe = BytesIO()
    c = canvas.Canvas(ausgabe, pagesize=A4)
    c.drawImage(ImageReader(bild), 0, 0, width=breite, height=hoehe)
    c.showPage()
    c.save()
    return ausgabe.getvalue()


ocr_noetig = pytest.mark.skipif(not ocr_verfuegbar(), reason="tesseract nicht installiert")


@ocr_noetig
def test_ocr_auszug_ohne_textebene():
    erg = parse_pdf(_auszug_als_bild_pdf())
    assert [u.betrag for u in erg.umsaetze] == [Decimal("-1451.64"), Decimal("-2300.00")]
    assert erg.umsaetze[1].empfaenger == "Factoring Süd GmbH"
    assert erg.umsaetze[1].buchungstag == date(2026, 10, 10)
    assert any("Prüfsumme stimmt" in h for h in erg.hinweise), (erg.hinweise, erg.warnungen)


@ocr_noetig
def test_ocr_zip_mit_postenlisten():
    erg = parse_zip(_paket(_auszug_als_bild_pdf()))
    assert not erg.warnungen, erg.warnungen
    assert [u.sammelauftrag for u in erg.umsaetze] == ["PRM01A"] * 3 + ["PRM01B"]
    assert sum(u.betrag for u in erg.umsaetze) == Decimal("-3751.64")


def test_ohne_ocr_klare_meldung(monkeypatch):
    from app.services import pdf_kontoauszug

    monkeypatch.setattr(pdf_kontoauszug, "ocr_verfuegbar", lambda: False)
    erg = parse_pdf(_beispiel_pdf([]))
    assert not erg.umsaetze
    assert any("tesseract" in w for w in erg.warnungen)


# ------------------------------------------------------------------- API

def _mandant(client, kurz: str):
    _login(client)
    mandant_id = client.post(
        "/api/mandanten", json={"name": f"ZIP {kurz}", "kurzname": kurz}
    ).json()["id"]
    konten = {k["nummer"]: k for k in
              client.get(f"/api/mandanten/{mandant_id}/konten").json()["konten"]}
    return mandant_id, konten


def test_zip_upload_verbuchen_und_ansehen(client):
    mandant_id, konten = _mandant(client, "zip-api")
    antwort = client.post(
        f"/api/mandanten/{mandant_id}/pdf-umsaetze",
        files={"datei": ("10.10.2026_7.zip", _paket(_beispiel_pdf(AUSZUG_ZEILEN)),
                         "application/zip")},
    )
    assert antwort.status_code == 200, antwort.text
    erg = antwort.json()
    assert [u["sammelauftrag"] for u in erg["umsaetze"]] == ["PRM01A"] * 3 + ["PRM01B", None]
    assert any("Einzelzahlungen" in h for h in erg["hinweise"])

    telefon = erg["umsaetze"][1]
    antwort = client.post(f"/api/mandanten/{mandant_id}/pdf-umsaetze/verbuchen", json={
        "batch_id": erg["batch_id"], "konto_id": konten["4920"]["id"],
        "bank_konto_id": konten["1200"]["id"], "datum": telefon["buchungstag"],
        "betrag": telefon["betrag"],
        "text": f'{telefon["empfaenger"]} – {telefon["verwendungszweck"]}',
    })
    assert antwort.status_code == 200, antwort.text

    # „ansehen“ wertet das gespeicherte ZIP erneut aus
    ansicht = client.get(f"/api/importe/{erg['batch_id']}/pdf-ansicht").json()
    assert ansicht["dateiname"] == "10.10.2026_7.zip"
    zugeordnet = [u for u in ansicht["umsaetze"] if u["zuordnung"]]
    assert len(zugeordnet) == 1
    assert zugeordnet[0]["empfaenger"] == "Telefon Deutschland GmbH"
    assert zugeordnet[0]["zuordnung"]["konto"].startswith("4920")
    assert zugeordnet[0]["sammelauftrag"] == "PRM01A"


def test_zip_sammelzeile_schon_als_ganzes_verbucht(client):
    """Wurde die Sammelüberweisung früher (Auszug ohne Postenlisten) als ein
    Betrag verbucht, sind ihre Einzelzahlungen im ZIP gesperrt."""
    mandant_id, konten = _mandant(client, "zip-sperre")
    erg = client.post(
        f"/api/mandanten/{mandant_id}/pdf-umsaetze",
        files={"datei": ("auszug.pdf", _beispiel_pdf(AUSZUG_ZEILEN), "application/pdf")},
    ).json()
    sammel = next(u for u in erg["umsaetze"] if u["betrag"] == -1451.64)
    assert client.post(f"/api/mandanten/{mandant_id}/pdf-umsaetze/verbuchen", json={
        "batch_id": erg["batch_id"], "konto_id": konten["4930"]["id"],
        "bank_konto_id": konten["1200"]["id"], "datum": sammel["buchungstag"],
        "betrag": sammel["betrag"],
        "text": f'{sammel["empfaenger"]} – {sammel["verwendungszweck"]}',
    }).status_code == 200

    erg = client.post(
        f"/api/mandanten/{mandant_id}/pdf-umsaetze",
        files={"datei": ("paket.zip", _paket(_beispiel_pdf(AUSZUG_ZEILEN)), "application/zip")},
    ).json()
    gesperrt = {u["sammelauftrag"]: u["bereits_verbucht"] for u in erg["umsaetze"]}
    assert gesperrt == {"PRM01A": True, "PRM01B": False, None: False}
