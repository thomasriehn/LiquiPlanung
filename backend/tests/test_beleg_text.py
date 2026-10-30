"""Belegtext und Seitenbilder für das KI-Auslesen (synthetische Dokumente)."""
import base64
import zipfile
from datetime import date
from email.message import EmailMessage
from io import BytesIO

import pytest

from app.services import pdf_kontoauszug
from app.services.beleg_text import (
    DOCX,
    EML,
    PDF,
    XLSX,
    beleg_text,
    mime_bestimmen,
    vision_bilder,
)
from app.services.pdf_kontoauszug import ocr_verfuegbar

ocr_noetig = pytest.mark.skipif(not ocr_verfuegbar(), reason="tesseract nicht installiert")

ZEILEN = [
    "Bürobedarf Schulz GmbH · Hauptstraße 1 · 12345 Berlin",
    "Auftragsbestätigung Nr. AB-4711",
    "Pos 1   Kopierpapier A4, 50 Kartons          1.234,56",
    "Summe netto 1.234,56 EUR",
]


def _text_pdf(zeilen: list[str], seiten: int = 1) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    puffer = BytesIO()
    c = canvas.Canvas(puffer, pagesize=A4)
    for nr in range(seiten):
        y = 800
        for zeile in [*zeilen, f"Seite {nr + 1}"]:
            c.drawString(40, y, zeile)
            y -= 16
        c.showPage()
    c.save()
    return puffer.getvalue()


def _seitenbild(zeilen: list[str], dpi: int = 200):
    import pypdfium2 as pdfium

    dokument = pdfium.PdfDocument(_text_pdf(zeilen))
    try:
        return dokument[0].render(scale=dpi / 72).to_pil()
    finally:
        dokument.close()


def _bild_pdf(zeilen: list[str]) -> bytes:
    """Seite rendern und nur als Bild wieder einbetten (Scan ohne Textebene)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    breite, hoehe = A4
    puffer = BytesIO()
    c = canvas.Canvas(puffer, pagesize=A4)
    c.drawImage(ImageReader(_seitenbild(zeilen)), 0, 0, width=breite, height=hoehe)
    c.showPage()
    c.save()
    return puffer.getvalue()


def _png(bild) -> bytes:
    puffer = BytesIO()
    bild.save(puffer, "PNG")
    return puffer.getvalue()


def _docx() -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    dokument = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document {w}><w:body>
  <w:p><w:r><w:t>Bestellung Nr. B-2026-17</w:t></w:r></w:p>
  <w:p><w:r><w:t xml:space="preserve">An: </w:t></w:r><w:r><w:t>Metallbau Kraft KG</w:t></w:r></w:p>
  <w:tbl>
    <w:tr><w:tc><w:p><w:r><w:t>Pos</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Artikel</w:t></w:r></w:p></w:tc>
          <w:tc><w:p><w:r><w:t>Betrag</w:t></w:r></w:p></w:tc></w:tr>
    <w:tr><w:tc><w:p><w:r><w:t>1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Stahlträger</w:t></w:r></w:p></w:tc>
          <w:tc><w:p><w:r><w:t>2.500,00</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl>
  <w:p><w:r><w:t>Summe</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t>2.500,00 EUR</w:t></w:r></w:p>
</w:body></w:document>"""
    kopf = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:hdr {w}><w:p><w:r><w:t>Nordlicht Handel GmbH · Hafenstraße 5</w:t></w:r></w:p></w:hdr>"""
    puffer = BytesIO()
    with zipfile.ZipFile(puffer, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", dokument)
        z.writestr("word/header1.xml", kopf)
        z.writestr("word/header2.xml", kopf)   # gleiche Kopfzeile in zweitem Abschnitt
    return puffer.getvalue()


def _xlsx() -> bytes:
    from openpyxl import Workbook

    mappe = Workbook()
    blatt = mappe.active
    blatt.title = "Bestellung"
    blatt.append(["Lieferant", "Holz Meier GmbH"])
    blatt.append(["Datum", date(2026, 11, 3)])
    blatt.append(["Artikel", "Menge", "Preis"])
    blatt.append(["Eichenbretter", 40, 12.5])
    blatt.append(["Summe", None, 500.0])
    mappe.create_sheet("Leer")
    puffer = BytesIO()
    mappe.save(puffer)
    return puffer.getvalue()


def _eml(anhang: bytes | None = None, html: bool = False) -> bytes:
    nachricht = EmailMessage()
    nachricht["Subject"] = "Bestellung 4711 – Büromöbel"
    nachricht["From"] = "einkauf@nordlicht.example"
    nachricht["To"] = "vertrieb@moebel-hansen.example"
    if html:
        nachricht.set_content("<html><body><p>Sehr geehrte Damen und Herren,</p>"
                              "<table><tr><td>Schreibtisch</td><td>899,00</td></tr></table>"
                              "<script>alert(1)</script></body></html>", subtype="html")
    else:
        nachricht.set_content("Hiermit bestellen wir 3 Schreibtische zu je 899,00 EUR netto.")
    if anhang is not None:
        nachricht.add_attachment(anhang, maintype="application", subtype="pdf",
                                 filename="bestellung.pdf")
    return bytes(nachricht)


def _bild_aus_data_url(url: str):
    from PIL import Image

    assert url.startswith("data:image/png;base64,")
    return Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1])))


# ------------------------------------------------------------------ PDF

def test_pdf_mit_textebene():
    erg = beleg_text(_text_pdf(ZEILEN), PDF)
    assert erg.quelle == "TEXT"
    assert erg.seiten == 1
    assert "Auftragsbestätigung Nr. AB-4711" in erg.text
    assert "1.234,56" in erg.text
    assert "        " not in erg.text      # Layout-Leerraum verdichtet
    assert erg.warnungen == []


def test_pdf_nur_erste_seiten():
    erg = beleg_text(_text_pdf(ZEILEN, seiten=7), PDF, max_seiten=5)
    assert erg.seiten == 7
    assert "Seite 5" in erg.text and "Seite 6" not in erg.text
    assert any("5 von 7 Seiten" in w for w in erg.warnungen)


def test_pdf_ohne_textebene_ohne_ocr(monkeypatch):
    monkeypatch.setattr(pdf_kontoauszug, "ocr_verfuegbar", lambda: False)
    erg = beleg_text(_bild_pdf(ZEILEN), PDF)
    assert erg.quelle == "LEER"
    assert erg.text == ""
    assert any("tesseract" in w for w in erg.warnungen)


@ocr_noetig
def test_pdf_ohne_textebene_per_ocr():
    erg = beleg_text(_bild_pdf(ZEILEN), PDF)
    assert erg.quelle == "OCR", erg.warnungen
    assert "AB-4711" in erg.text
    assert "1.234,56" in erg.text
    assert any("Texterkennung" in w for w in erg.warnungen)


def test_defekte_dateien_ohne_ausnahme(monkeypatch):
    monkeypatch.setattr(pdf_kontoauszug, "ocr_verfuegbar", lambda: False)
    leer = beleg_text(b"", PDF)
    assert leer.quelle == "LEER" and leer.warnungen
    kaputt = beleg_text(b"%PDF-1.4\nkaputt", PDF)
    assert kaputt.quelle == "LEER"
    assert any("nicht lesbar" in w for w in kaputt.warnungen)
    unbekannt = beleg_text(b"\x00\x01\x02", "application/octet-stream", dateiname="x.bin")
    assert unbekannt.quelle == "LEER"
    assert any("nicht ausgelesen" in w for w in unbekannt.warnungen)
    kaputtes_bild = beleg_text(b"\x89PNG\r\n\x1a\nnichts", "image/png")
    assert kaputtes_bild.quelle == "LEER"
    kaputtes_docx = beleg_text(b"PK\x03\x04kaputt", DOCX)
    assert kaputtes_docx.quelle == "LEER"
    assert any("nicht lesbar" in w for w in kaputtes_docx.warnungen)


# ------------------------------------------------------------------ Bilder

def test_bild_ohne_ocr(monkeypatch):
    monkeypatch.setattr(pdf_kontoauszug, "ocr_verfuegbar", lambda: False)
    erg = beleg_text(_png(_seitenbild(ZEILEN)), "image/png")
    assert erg.quelle == "LEER"
    assert erg.seiten == 1
    assert any("tesseract" in w for w in erg.warnungen)


@ocr_noetig
def test_bild_per_ocr():
    bild = _seitenbild(ZEILEN).convert("RGB")
    puffer = BytesIO()
    bild.save(puffer, "JPEG", quality=90)
    erg = beleg_text(puffer.getvalue(), "image/jpeg")
    assert erg.quelle == "OCR", erg.warnungen
    assert "AB-4711" in erg.text


@ocr_noetig
def test_kleines_bild_wird_fuer_ocr_vergroessert():
    erg = beleg_text(_png(_seitenbild(ZEILEN, dpi=100)), "image/png")
    assert erg.quelle == "OCR", erg.warnungen
    assert "4711" in erg.text


# ------------------------------------------------------------------ Office, Mail, Text

def test_docx_mit_kopfzeile_und_tabelle():
    erg = beleg_text(_docx(), DOCX)
    assert erg.quelle == "TEXT"
    assert erg.text.startswith("Nordlicht Handel GmbH · Hafenstraße 5")
    assert erg.text.count("Nordlicht Handel GmbH") == 1
    assert "An: Metallbau Kraft KG" in erg.text
    assert "1 | Stahlträger | 2.500,00" in erg.text
    assert "Summe\t2.500,00 EUR" in erg.text


def test_xlsx_zellwerte():
    erg = beleg_text(_xlsx(), XLSX)
    assert erg.quelle == "TEXT"
    assert erg.seiten == 2
    assert "Tabellenblatt „Bestellung“" in erg.text
    assert "Lieferant | Holz Meier GmbH" in erg.text
    assert "Datum | 2026-11-03" in erg.text
    assert "Eichenbretter | 40 | 12.5" in erg.text
    assert "Summe |  | 500" in erg.text


def test_eml_mit_pdf_anhang():
    erg = beleg_text(_eml(_text_pdf(ZEILEN)), EML)
    assert erg.quelle == "TEXT"
    assert "Betreff: Bestellung 4711 – Büromöbel" in erg.text
    assert "3 Schreibtische" in erg.text
    assert "--- Anhang: bestellung.pdf ---" in erg.text
    assert "AB-4711" in erg.text


def test_eml_nur_html():
    erg = beleg_text(_eml(html=True), "message/rfc822")
    assert "Sehr geehrte Damen und Herren" in erg.text
    assert "Schreibtisch | 899,00" in erg.text
    assert "alert" not in erg.text and "<p>" not in erg.text


def test_txt_kodierungen():
    assert beleg_text("Bestellung für Müller".encode(), "text/plain").text == "Bestellung für Müller"
    erg = beleg_text("Grüße\r\n\r\n\r\n\r\nSumme 10,00 €".encode("cp1252"), "text/plain; charset=windows-1252")
    assert erg.text == "Grüße\n\nSumme 10,00 €"
    assert erg.quelle == "TEXT"
    assert beleg_text(b"   \n ", "text/plain").quelle == "LEER"


def test_mime_nach_inhalt_und_endung():
    assert mime_bestimmen(_text_pdf(ZEILEN), "application/octet-stream") == PDF
    assert mime_bestimmen(_docx(), "application/zip") == DOCX
    assert mime_bestimmen(_xlsx(), None) == XLSX
    assert mime_bestimmen(b"Hallo", "application/octet-stream", "notiz.TXT") == "text/plain"
    assert mime_bestimmen(b"From: x", "", "mail.eml") == EML
    assert mime_bestimmen(b"\xff\xd8\xff\xe0", "image/jpg") == "image/jpeg"
    # Inhalt schlägt Angabe: als PDF deklariertes PNG
    assert mime_bestimmen(_png(_seitenbild(["x"], dpi=30)), "application/pdf") == "image/png"


# ------------------------------------------------------------------ Seitenbilder

def test_vision_pdf_seiten_150_dpi():
    urls = vision_bilder(_text_pdf(ZEILEN, seiten=5), PDF)
    assert len(urls) == 3
    bild = _bild_aus_data_url(urls[0])
    assert bild.format == "PNG"
    assert abs(bild.size[0] - 1240) <= 2 and abs(bild.size[1] - 1754) <= 2   # A4 bei 150 dpi
    assert len(vision_bilder(_text_pdf(ZEILEN, seiten=5), PDF, max_seiten=1)) == 1


def test_vision_nutzt_gemeinsame_pdfium_sperre(monkeypatch):
    original = pdf_kontoauszug._PDFIUM_SPERRE
    eintritte = []

    class Sperre:
        def __enter__(self):
            eintritte.append(1)
            return original.__enter__()

        def __exit__(self, *args):
            return original.__exit__(*args)

    monkeypatch.setattr(pdf_kontoauszug, "_PDFIUM_SPERRE", Sperre())
    assert len(vision_bilder(_text_pdf(ZEILEN), PDF)) == 1
    assert eintritte == [1]


def test_vision_bild_verkleinert_und_ohne_transparenz():
    from PIL import Image

    gross = Image.new("RGBA", (3000, 1000), (255, 0, 0, 0))
    urls = vision_bilder(_png(gross), "image/png")
    assert len(urls) == 1
    bild = _bild_aus_data_url(urls[0])
    assert bild.size == (2000, 667)
    assert bild.mode == "RGB"
    assert bild.getpixel((10, 10)) == (255, 255, 255)   # transparent → weiß


def test_vision_mehrseitiges_tiff():
    from PIL import Image

    seiten = [Image.new("L", (400, 600), farbe) for farbe in (0, 128, 255, 60)]
    puffer = BytesIO()
    seiten[0].save(puffer, "TIFF", save_all=True, append_images=seiten[1:])
    assert len(vision_bilder(puffer.getvalue(), "image/tiff")) == 3
    assert len(vision_bilder(puffer.getvalue(), "image/tiff", max_seiten=2)) == 2


def test_vision_eml_anhang_und_sonstige_typen():
    assert len(vision_bilder(_eml(_text_pdf(ZEILEN, seiten=2)), EML)) == 2
    assert vision_bilder(_eml(), EML) == []
    assert vision_bilder(_docx(), DOCX) == []
    assert vision_bilder(b"%PDF-1.4 kaputt", PDF) == []
    assert vision_bilder(b"", PDF) == []
