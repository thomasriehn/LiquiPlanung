"""KI-Client (OpenAI-kompatibel, gemockt) und Normalisierung der Bestellvorschläge."""
import json
from datetime import date
from decimal import Decimal
from io import BytesIO

import httpx
import pytest
from sqlalchemy import select

from app import models
from app.services import beleg_text as beleg_modul
from app.services import ki, pdf_kontoauszug
from app.services.beleg_text import BelegText
from app.services.ki import (
    KiFehler,
    auszahlungskonten,
    bestellung_auslesen,
    chat_json,
    chat_url,
    datum_wert,
    json_aus_text,
    verbindung_testen,
    vorschlag_normalisieren,
    zahl_wert,
)
from app.services.kontenrahmen import lege_kontenrahmen_an
from app.services.zugaenge import KiZugang

HEUTE = date(2026, 10, 30)   # Freitag
ZUGANG = KiZugang(url="https://ki.example/v1", modell="gpt-test", api_key="sk-geheim-123")
IBAN_LIEFERANT = "DE89370400440532013000"
IBAN_EIGEN = "DE02120300000000202051"

DOKUMENT = [
    "Bürobedarf Schulz GmbH · Hauptstraße 1 · 12345 Berlin",
    "Nordlicht Handel GmbH, Hafenstraße 5, 20457 Hamburg",
    "Auftragsbestätigung Nr. AB-4711 vom 03.11.2026",
    "Pos 1   Kopierpapier A4, 50 Kartons          1.000,00",
    "Summe netto 1.000,00 EUR   zzgl. 19 % USt 190,00   Gesamt 1.190,00 EUR",
    "Zahlbar innerhalb 14 Tagen netto",
]


# ------------------------------------------------------------------ Hilfen

def _mandant(db, bundesland="NW") -> models.Mandant:
    m = models.Mandant(name="Nordlicht Handel GmbH", kurzname="nordlicht",
                       kontenrahmen="SKR03", bundesland=bundesland)
    db.add(m)
    db.flush()
    lege_kontenrahmen_an(db, m)
    bank = _konto(db, m, "1200")
    bank.iban = "DE02 1203 0000 0000 2020 51"
    db.flush()
    return m


def _konto(db, m, nummer) -> models.Konto:
    return db.scalar(select(models.Konto).where(
        models.Konto.mandant_id == m.id, models.Konto.nummer == nummer))


def _bestellung(db, m, kreditor, konto_nummer):
    b = models.Bestellung(
        mandant_id=m.id, kreditor=kreditor, bestelldatum=date(2026, 9, 1),
        zahldatum=date(2026, 10, 1), betrag_netto=Decimal("100.00"),
        konto_id=_konto(db, m, konto_nummer).id,
    )
    db.add(b)
    db.flush()
    return b


def _pdf(zeilen) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    puffer = BytesIO()
    c = canvas.Canvas(puffer, pagesize=A4)
    y = 800
    for zeile in zeilen:
        c.drawString(40, y, zeile)
        y -= 16
    c.showPage()
    c.save()
    return puffer.getvalue()


def _bild_pdf(zeilen) -> bytes:
    import pypdfium2 as pdfium
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    vorlage = pdfium.PdfDocument(_pdf(zeilen))
    bild = vorlage[0].render(scale=100 / 72).to_pil()
    vorlage.close()
    puffer = BytesIO()
    c = canvas.Canvas(puffer, pagesize=A4)
    c.drawImage(ImageReader(bild), 0, 0, width=A4[0], height=A4[1])
    c.showPage()
    c.save()
    return puffer.getvalue()


def _antwort(inhalt, status=200, modell="gpt-test-2026"):
    if isinstance(inhalt, dict):
        inhalt = json.dumps(inhalt, ensure_ascii=False)
    return httpx.Response(status, json={
        "id": "chatcmpl-1", "model": modell,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": inhalt}}],
    })


class Dienst:
    """Gemockter Chat-Endpunkt: zeichnet Anfragen auf, Antworten per Funktion."""

    def __init__(self, monkeypatch, antworten):
        self.anfragen: list[httpx.Request] = []
        self.antworten = antworten
        monkeypatch.setattr(ki, "http_client_fabrik",
                            lambda: httpx.Client(transport=httpx.MockTransport(self._bearbeiten)))

    def _bearbeiten(self, request):
        self.anfragen.append(request)
        antwort = self.antworten(request, len(self.anfragen))
        if isinstance(antwort, Exception):
            raise antwort
        return antwort

    def nutzlast(self, nr=-1) -> dict:
        return json.loads(self.anfragen[nr].content)


def _ki_aktiv(monkeypatch, zugang=ZUGANG):
    monkeypatch.setattr(ki, "_ki_zugang", lambda db: zugang)


KI_JSON = {
    "dokumentart": "AUFTRAGSBESTAETIGUNG",
    "kreditor": "Bürobedarf Schulz GmbH",
    "bestellnummer": "AB-4711",
    "beschreibung": "50 Kartons Kopierpapier A4",
    "bestelldatum": "03.11.2026",
    "lieferdatum": None,
    "zahldatum": None,
    "zahlungsziel_tage": 14,
    "betrag_netto": "1.000,00",
    "ust_satz": "19 %",
    "betrag_brutto": 1190,
    "konto_nummer": "4930",
    "iban": "DE89 3704 0044 0532 0130 00",
    "hinweise": ["Lieferdatum nicht angegeben"],
}


# ------------------------------------------------------------------ HTTP-Client

def test_chat_url():
    assert chat_url("https://api.openai.com/v1") == "https://api.openai.com/v1/chat/completions"
    assert chat_url("https://api.openai.com/v1/") == "https://api.openai.com/v1/chat/completions"
    assert chat_url("https://x/v1/chat/completions") == "https://x/v1/chat/completions"
    assert chat_url("http://localhost:11434") == "http://localhost:11434/v1/chat/completions"
    azure = "https://r.openai.azure.com/openai/deployments/d/chat/completions?api-version=2024-10-21"
    assert chat_url(azure) == azure
    assert chat_url("https://r.openai.azure.com/openai/deployments/d?api-version=1") == \
        "https://r.openai.azure.com/openai/deployments/d/chat/completions?api-version=1"


def test_chat_json_anfrage(monkeypatch):
    dienst = Dienst(monkeypatch, lambda req, nr: _antwort({"a": 1}))
    assert chat_json(ZUGANG, "System", "Hallo JSON") == {"a": 1}
    req = dienst.anfragen[0]
    assert str(req.url) == "https://ki.example/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer sk-geheim-123"
    n = dienst.nutzlast()
    assert n["model"] == "gpt-test"
    assert n["temperature"] == 0
    assert n["response_format"] == {"type": "json_object"}
    assert n["messages"] == [{"role": "system", "content": "System"},
                             {"role": "user", "content": "Hallo JSON"}]


def test_ohne_api_key_kein_authorization_header(monkeypatch):
    dienst = Dienst(monkeypatch, lambda req, nr: _antwort("{}"))
    chat_json(KiZugang(url="http://localhost:11434/v1", modell="llama3", api_key=None), "s", "u")
    assert "authorization" not in dienst.anfragen[0].headers


def test_400_erneut_ohne_response_format(monkeypatch):
    def antworten(req, nr):
        if "response_format" in json.loads(req.content):
            return httpx.Response(400, json={"error": {"message": "Unknown parameter: response_format"}})
        return _antwort('{"ok": true}')

    dienst = Dienst(monkeypatch, antworten)
    assert chat_json(ZUGANG, "s", "u") == {"ok": True}
    assert len(dienst.anfragen) == 2
    assert "response_format" not in dienst.nutzlast(1)
    assert dienst.nutzlast(1)["temperature"] == 0


def test_400_wegen_temperature(monkeypatch):
    def antworten(req, nr):
        if "temperature" in json.loads(req.content):
            return httpx.Response(400, json={"error": {
                "message": "Unsupported value: 'temperature' does not support 0 with this model."}})
        return _antwort('{"ok": 1}')

    dienst = Dienst(monkeypatch, antworten)
    assert chat_json(ZUGANG, "s", "u") == {"ok": 1}
    assert dienst.nutzlast(1)["response_format"] == {"type": "json_object"}


def test_400_dauerhaft_ergibt_kifehler(monkeypatch):
    dienst = Dienst(monkeypatch, lambda req, nr: httpx.Response(400, json={"error": {"message": "kaputt"}}))
    with pytest.raises(KiFehler) as fehler:
        chat_json(ZUGANG, "s", "u")
    assert fehler.value.status == 400
    assert "kaputt" in str(fehler.value)
    assert len(dienst.anfragen) == 3


@pytest.mark.parametrize("status,text", [
    (401, "API-Key"), (404, "Modellname"), (429, "Kontingent"), (503, "Serverfehler"),
])
def test_http_fehler_verstaendlich_ohne_schluessel(monkeypatch, status, text):
    Dienst(monkeypatch, lambda req, nr: httpx.Response(
        status, json={"error": {"message": "Key sk-geheim-123 abgelehnt"}}))
    with pytest.raises(KiFehler) as fehler:
        chat_json(ZUGANG, "s", "u")
    assert text in str(fehler.value)
    assert f"HTTP {status}" in str(fehler.value)
    assert "sk-geheim-123" not in str(fehler.value)


def test_netzwerk_und_zeitueberschreitung(monkeypatch):
    Dienst(monkeypatch, lambda req, nr: httpx.ConnectError("Verbindung abgelehnt"))
    with pytest.raises(KiFehler, match="nicht erreichbar"):
        chat_json(ZUGANG, "s", "u")
    Dienst(monkeypatch, lambda req, nr: httpx.ReadTimeout("zu langsam"))
    with pytest.raises(KiFehler, match="Zeitüberschreitung"):
        chat_json(ZUGANG, "s", "u")


def test_antwortformate(monkeypatch):
    Dienst(monkeypatch, lambda req, nr: httpx.Response(200, text="<html>Login</html>"))
    with pytest.raises(KiFehler, match="kein JSON"):
        chat_json(ZUGANG, "s", "u")
    Dienst(monkeypatch, lambda req, nr: httpx.Response(200, json={"choices": []}))
    with pytest.raises(KiFehler, match="choices"):
        chat_json(ZUGANG, "s", "u")
    Dienst(monkeypatch, lambda req, nr: httpx.Response(200, json={"choices": [
        {"message": {"content": None, "refusal": "Nein"}}]}))
    with pytest.raises(KiFehler, match="verweigert"):
        chat_json(ZUGANG, "s", "u")
    # Inhalt als Liste von Teilen
    Dienst(monkeypatch, lambda req, nr: httpx.Response(200, json={"choices": [
        {"message": {"content": [{"type": "text", "text": '{"x": '}, {"type": "text", "text": "2}"}]}}]}))
    assert chat_json(ZUGANG, "s", "u") == {"x": 2}


def test_json_aus_text():
    assert json_aus_text('{"a": 1}') == {"a": 1}
    assert json_aus_text('```json\n{"a": 2}\n```') == {"a": 2}
    assert json_aus_text('Gern!\n```\n{"a": 3,}\n```\nViel Erfolg') == {"a": 3}
    assert json_aus_text('Ergebnis: {"a": {"b": [1, 2]}} – fertig {kein json}') == {"a": {"b": [1, 2]}}
    assert json_aus_text('<think>{"falsch": 1} überlegen …</think>{"a": 4}') == {"a": 4}
    assert json_aus_text('[{"a": 5}]') == {"a": 5}
    with pytest.raises(KiFehler):
        json_aus_text("Leider kann ich das Dokument nicht lesen.")
    with pytest.raises(KiFehler):
        json_aus_text("")


def test_verbindung_testen(monkeypatch):
    dienst = Dienst(monkeypatch, lambda req, nr: _antwort('{"antwort": "ok"}'))
    assert verbindung_testen(ZUGANG) == {"ok": True, "modell": "gpt-test-2026", "antwort": "ok"}
    assert "JSON" in dienst.nutzlast()["messages"][1]["content"]


# ------------------------------------------------------------------ Auslesen

def test_auslesen_text_pdf(db, monkeypatch):
    m = _mandant(db)
    _ki_aktiv(monkeypatch)
    dienst = Dienst(monkeypatch, lambda req, nr: _antwort(KI_JSON))
    erg = bestellung_auslesen(db, m, _pdf(DOKUMENT), "AB-4711.pdf", "application/pdf", heute=HEUTE)

    v = erg["vorschlag"]
    assert v["kreditor"] == "Bürobedarf Schulz GmbH"
    assert v["bestellnummer"] == "AB-4711"
    assert v["bestelldatum"] == "2026-11-03"
    assert v["zahlungsziel_tage"] == 14
    assert v["zahldatum"] == "2026-11-17"            # 03.11. + 14 Tage (Dienstag)
    assert v["betrag_netto"] == 1000.0
    assert v["ust_satz"] == 19.0
    assert v["betrag_brutto"] == 1190.0
    assert v["konto_id"] == _konto(db, m, "4930").id
    assert v["konto_quelle"] == "KI"
    assert v["iban"] == IBAN_LIEFERANT
    assert erg["text_quelle"] == "TEXT"
    assert "AB-4711" in erg["text_auszug"]
    k = erg["ki"]
    assert k["aktiv"] is True and k["fehler"] is None
    assert k["modell"] == "gpt-test" and k["quelle"] == "TEXT"
    assert {"kreditor", "betrag_netto", "ust_satz", "konto_id", "iban", "bestelldatum"} <= set(k["felder"])
    assert "zahldatum" not in k["felder"] and "lieferdatum" not in k["felder"]
    assert "KI: Lieferdatum nicht angegeben" in k["hinweise"]
    assert any("Zahldatum geschätzt: Bestelldatum + 14 Tage" in h for h in k["hinweise"])
    json.dumps(erg)   # JSON-tauglich

    # Systemprompt: Mandant als Besteller, nur Auszahlungskonten
    system = dienst.nutzlast()["messages"][0]["content"]
    nutzer = dienst.nutzlast()["messages"][1]["content"]
    assert "„Nordlicht Handel GmbH“" in system and "NIE der Kreditor" in system
    assert "4930 Bürobedarf (Sonstige Auszahlungen)" in system
    assert "3400 Wareneingang 19 % Vorsteuer" in system
    for fremd in ("1200 Bank", "1000 Kasse", "8400 Erlöse", "3970 Warenbestand", "1400 Forderungen"):
        assert fremd not in system
    assert "Heute ist der 30.10.2026." in nutzer
    assert "Dateiname: AB-4711.pdf" in nutzer
    assert "Summe netto 1.000,00 EUR" in nutzer


def test_auslesen_ohne_zugang(db, monkeypatch):
    m = _mandant(db)
    monkeypatch.setattr(ki, "_ki_zugang", lambda db: None)
    dienst = Dienst(monkeypatch, lambda req, nr: _antwort(KI_JSON))
    erg = bestellung_auslesen(db, m, _pdf(DOKUMENT), "a.pdf", "application/pdf", heute=HEUTE)
    assert dienst.anfragen == []
    assert erg["ki"]["aktiv"] is False and erg["ki"]["fehler"] is None
    assert any("KI nicht eingerichtet" in h for h in erg["ki"]["hinweise"])
    assert erg["ki"]["felder"] == []
    assert erg["vorschlag"]["kreditor"] is None
    assert erg["vorschlag"]["zahldatum"] == "2026-11-30"   # heute + 30 = So 29.11. → Mo 30.11.
    assert erg["text_quelle"] == "TEXT"


def test_auslesen_echte_systemeinstellungen_ohne_eintrag(db):
    """Ohne Monkeypatch: die Naht liest die (leeren) Systemeinstellungen."""
    m = _mandant(db)
    erg = bestellung_auslesen(db, m, b"Bestellung", "notiz.txt", "text/plain", heute=HEUTE)
    assert erg["ki"]["aktiv"] is False


@pytest.mark.parametrize("antwort", [
    lambda req, nr: _antwort("Ich kann leider kein JSON liefern."),
    lambda req, nr: httpx.Response(500, text="Interner Fehler"),
    lambda req, nr: httpx.ConnectError("keine Verbindung"),
])
def test_auslesen_fehler_landen_in_ki_fehler(db, monkeypatch, antwort):
    m = _mandant(db)
    _ki_aktiv(monkeypatch)
    Dienst(monkeypatch, antwort)
    erg = bestellung_auslesen(db, m, _pdf(DOKUMENT), "a.pdf", "application/pdf", heute=HEUTE)
    assert erg["ki"]["aktiv"] is True
    assert erg["ki"]["fehler"]
    assert erg["ki"]["felder"] == []
    assert erg["vorschlag"]["zahldatum"] == "2026-11-30"


def test_auslesen_unerwarteter_fehler(db, monkeypatch):
    m = _mandant(db)
    _ki_aktiv(monkeypatch)

    def kaputt(*a, **k):
        raise RuntimeError("Absturz")

    monkeypatch.setattr(ki, "chat_json", kaputt)
    erg = bestellung_auslesen(db, m, _pdf(DOKUMENT), "a.pdf", "application/pdf", heute=HEUTE)
    assert "Absturz" in erg["ki"]["fehler"]


def test_auslesen_text_wird_gekuerzt(db, monkeypatch):
    m = _mandant(db)
    _ki_aktiv(monkeypatch)
    dienst = Dienst(monkeypatch, lambda req, nr: _antwort({}))
    text = ("Position Schrauben 1,00\n" * 3000) + "GESAMTSUMME 9.999,00"
    erg = bestellung_auslesen(db, m, text.encode(), "lang.txt", "text/plain", heute=HEUTE)
    nutzer = dienst.nutzlast()["messages"][1]["content"]
    assert len(nutzer) < 31_000
    assert "[… Text gekürzt …]" in nutzer
    assert "GESAMTSUMME 9.999,00" in nutzer          # Ende (Summen) bleibt erhalten
    assert any("gekürzt" in h for h in erg["ki"]["hinweise"])
    assert len(erg["text_auszug"]) == 2000


def test_vision_bei_scan_ohne_text(db, monkeypatch):
    m = _mandant(db)
    monkeypatch.setattr(pdf_kontoauszug, "ocr_verfuegbar", lambda: False)
    _ki_aktiv(monkeypatch, KiZugang(url="https://ki.example/v1", modell="gpt-4o", api_key="k", vision=True))
    dienst = Dienst(monkeypatch, lambda req, nr: _antwort(KI_JSON))
    erg = bestellung_auslesen(db, m, _bild_pdf(DOKUMENT), "scan.pdf", "application/pdf", heute=HEUTE)
    assert erg["text_quelle"] == "LEER"
    assert erg["ki"]["quelle"] == "VISION"
    assert erg["ki"]["fehler"] is None
    inhalt = dienst.nutzlast()["messages"][1]["content"]
    assert isinstance(inhalt, list)
    assert inhalt[0]["type"] == "text" and "Seitenbilder" in inhalt[0]["text"]
    bilder = [t for t in inhalt if t["type"] == "image_url"]
    assert len(bilder) == 1
    assert bilder[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert erg["vorschlag"]["kreditor"] == "Bürobedarf Schulz GmbH"


def test_ohne_vision_kein_text_meldet_fehler(db, monkeypatch):
    m = _mandant(db)
    monkeypatch.setattr(pdf_kontoauszug, "ocr_verfuegbar", lambda: False)
    _ki_aktiv(monkeypatch)
    dienst = Dienst(monkeypatch, lambda req, nr: _antwort(KI_JSON))
    erg = bestellung_auslesen(db, m, _bild_pdf(DOKUMENT), "scan.pdf", "application/pdf", heute=HEUTE)
    assert dienst.anfragen == []
    assert "Seitenbilder mitsenden" in erg["ki"]["fehler"]
    assert any("tesseract" in h for h in erg["ki"]["hinweise"])
    # winzige Textebene (nur Seitenzahl) zählt nicht als Dokumenttext
    erg = bestellung_auslesen(db, m, _pdf(["Seite 1"]), "scan.pdf", "application/pdf", heute=HEUTE)
    assert erg["text_quelle"] == "LEER"
    assert dienst.anfragen == []
    assert erg["ki"]["fehler"]


def test_vision_mit_text_sendet_nur_text(db, monkeypatch):
    """Textebene vorhanden → keine Bilder, auch wenn Vision eingeschaltet ist."""
    m = _mandant(db)
    _ki_aktiv(monkeypatch, KiZugang(url="https://ki.example/v1", modell="m", api_key=None, vision=True))
    dienst = Dienst(monkeypatch, lambda req, nr: _antwort(KI_JSON))
    erg = bestellung_auslesen(db, m, _pdf(DOKUMENT), "a.pdf", "application/pdf", heute=HEUTE)
    assert isinstance(dienst.nutzlast()["messages"][1]["content"], str)
    assert erg["ki"]["quelle"] == "TEXT"


def test_vision_abgelehnt_rueckfall_auf_ocr_text(db, monkeypatch):
    m = _mandant(db)
    _ki_aktiv(monkeypatch, KiZugang(url="https://ki.example/v1", modell="m", api_key=None, vision=True))
    monkeypatch.setattr(beleg_modul, "beleg_text", lambda *a, **k: BelegText(
        "Bürobedarf Schulz GmbH Auftragsbestätigung AB-4711", "OCR", 1, ["per OCR"]))

    def antworten(req, nr):
        if isinstance(json.loads(req.content)["messages"][1]["content"], list):
            return httpx.Response(400, json={"error": {"message": "image input is not supported"}})
        return _antwort(KI_JSON)

    dienst = Dienst(monkeypatch, antworten)
    from PIL import Image

    puffer = BytesIO()
    Image.new("RGB", (300, 400), "white").save(puffer, "PNG")
    erg = bestellung_auslesen(db, m, puffer.getvalue(), "foto.png", "image/png", heute=HEUTE)
    assert erg["ki"]["fehler"] is None
    assert erg["ki"]["quelle"] == "OCR"
    assert any("Seitenbilder abgelehnt" in h for h in erg["ki"]["hinweise"])
    assert isinstance(dienst.nutzlast()["messages"][1]["content"], str)
    assert erg["vorschlag"]["bestellnummer"] == "AB-4711"


# ------------------------------------------------------------------ Normalisierung

@pytest.mark.parametrize("roh,erwartet", [
    ("1.234,56", "1234.56"), ("1,234.56", "1234.56"), (1234.5, "1234.5"), ("1234,56 €", "1234.56"),
    ("EUR 1.234.567,89", "1234567.89"), ("1.234", "1234"), ("12.5", "12.5"), ("0.500", "0.500"),
    ("19 %", "19"), ("-12,30", "-12.30"), ("1 234,00", "1234.00"), (1190, "1190"),
    ("abc", None), ("", None), (None, None), (True, None), ("1,2,3,4", "1234"), (float("nan"), None),
])
def test_zahl_wert(roh, erwartet):
    assert zahl_wert(roh) == (Decimal(erwartet) if erwartet is not None else None)


@pytest.mark.parametrize("roh,erwartet", [
    ("2026-11-03", date(2026, 11, 3)), ("2026-11-03T00:00:00Z", date(2026, 11, 3)),
    ("03.11.2026", date(2026, 11, 3)), ("3.11.26", date(2026, 11, 3)), ("2026/11/03", date(2026, 11, 3)),
    ("3. November 2026", date(2026, 11, 3)), ("03. Okt. 2026", date(2026, 10, 3)),
    (date(2026, 1, 2), date(2026, 1, 2)), ("31.02.2026", None), ("1899-01-01", None),
    ("demnächst", None), (None, None), (20261103, None),
])
def test_datum_wert(roh, erwartet):
    assert datum_wert(roh) == erwartet


def test_netto_brutto_ust_ableitung(db):
    m = _mandant(db)
    v, h = vorschlag_normalisieren(db, m, {"betrag_brutto": "1.190,00", "ust_satz": 19}, heute=HEUTE)
    assert v["betrag_netto"] == 1000.0 and v["betrag_brutto"] == 1190.0
    assert any("aus dem Bruttobetrag berechnet" in x for x in h)
    v, _ = vorschlag_normalisieren(db, m, {"betrag_netto": 200, "ust_satz": 0.07}, heute=HEUTE)
    assert v["ust_satz"] == 7.0 and v["betrag_brutto"] == 214.0
    # USt aus Netto/Brutto erkannt
    v, _ = vorschlag_normalisieren(db, m, {"betrag_netto": 100, "betrag_brutto": 107}, heute=HEUTE)
    assert v["ust_satz"] == 7.0
    # widersprüchlich: netto führend, Hinweis
    v, h = vorschlag_normalisieren(db, m, {"betrag_netto": 100, "betrag_brutto": 150, "ust_satz": 19},
                                   heute=HEUTE)
    assert v["betrag_netto"] == 100.0 and v["betrag_brutto"] == 119.0
    assert any("passt nicht" in x for x in h)
    # nur brutto ohne Satz und ohne Konto
    v, h = vorschlag_normalisieren(db, m, {"betrag_brutto": 50}, heute=HEUTE)
    assert v["betrag_netto"] is None and v["betrag_brutto"] == 50.0
    assert any("Nettobetrag bitte ergänzen" in x for x in h)
    # unplausibler Satz
    v, h = vorschlag_normalisieren(db, m, {"betrag_netto": 10, "ust_satz": 190}, heute=HEUTE)
    assert v["ust_satz"] is None and any("USt-Satz" in x for x in h)


def test_ust_standard_aus_konto(db):
    m = _mandant(db)
    v, _ = vorschlag_normalisieren(db, m, {"betrag_netto": "500", "konto_nummer": "4930"}, heute=HEUTE)
    assert v["ust_satz"] == 19.0 and v["betrag_brutto"] == 595.0
    v, _ = vorschlag_normalisieren(db, m, {"betrag_netto": "500", "konto_nummer": "4210"}, heute=HEUTE)
    assert v["konto_id"] == _konto(db, m, "4210").id
    assert v["ust_satz"] is None and v["betrag_brutto"] is None   # Miete ohne USt-Vorgabe


def test_konto_nur_auszahlungskonten(db):
    m = _mandant(db)
    v, h = vorschlag_normalisieren(db, m, {"konto_nummer": "1200"}, heute=HEUTE)
    assert v["konto_id"] is None and v["konto_quelle"] is None
    assert any("kein Auszahlungskonto" in x for x in h)
    v, _ = vorschlag_normalisieren(db, m, {"konto_nummer": "8400"}, heute=HEUTE)
    assert v["konto_id"] is None
    v, _ = vorschlag_normalisieren(db, m, {"konto_nummer": "420 Büroeinrichtung"}, heute=HEUTE)
    assert v["konto_id"] == _konto(db, m, "0420").id
    inaktiv = _konto(db, m, "4600")
    inaktiv.aktiv = False
    db.flush()
    v, _ = vorschlag_normalisieren(db, m, {"konto_nummer": "4600"}, heute=HEUTE)
    assert v["konto_id"] is None
    nummern = {k.nummer for k in auszahlungskonten(db, m)}
    assert {"3400", "4930", "4110", "0420"} <= nummern
    assert not nummern & {"1000", "1200", "8400", "1400", "3970", "4600"}


def test_konto_historie_vor_ki(db):
    m = _mandant(db)
    _bestellung(db, m, "Bürobedarf Schulz GmbH", "3400")
    _bestellung(db, m, "BÜROBEDARF SCHULZ GMBH", "4930")       # jüngste Bestellung zählt
    _bestellung(db, m, "Anderer Lieferant AG", "4800")
    v, h = vorschlag_normalisieren(
        db, m, {"kreditor": "Bürobedarf Schulz GmbH", "konto_nummer": "3400"}, heute=HEUTE)
    assert v["konto_id"] == _konto(db, m, "4930").id
    assert v["konto_quelle"] == "HISTORIE"
    assert v["kreditor"] == "BÜROBEDARF SCHULZ GMBH"           # Schreibweise der Historie
    assert any("letzten Bestellung" in x and "KI-Vorschlag: 3400" in x for x in h)


def test_konto_historie_ohne_rechtsform(db):
    m = _mandant(db)
    _bestellung(db, m, "Schulz Bürobedarf GmbH & Co. KG", "4930")
    v, h = vorschlag_normalisieren(db, m, {"kreditor": "Schulz Bürobedarf"}, heute=HEUTE)
    assert v["konto_id"] == _konto(db, m, "4930").id
    assert v["kreditor"] == "Schulz Bürobedarf GmbH & Co. KG"
    assert any("früheren Bestellungen geschrieben" in x for x in h)


def test_konto_historie_ungueltiges_konto_faellt_auf_ki_zurueck(db):
    m = _mandant(db)
    b = _bestellung(db, m, "Holz Meier GmbH", "4600")
    _konto(db, m, "4600").aktiv = False
    db.flush()
    v, _ = vorschlag_normalisieren(db, m, {"kreditor": "Holz Meier GmbH", "konto_nummer": "3400"},
                                   heute=HEUTE)
    assert b.konto_id is not None
    assert v["konto_id"] == _konto(db, m, "3400").id and v["konto_quelle"] == "KI"


def test_zahldatum_regeln(db):
    m = _mandant(db)
    # KI-Wert, Samstag → Montag
    v, _ = vorschlag_normalisieren(db, m, {"zahldatum": "07.11.2026", "bestelldatum": "2026-11-01"},
                                   heute=HEUTE)
    assert v["zahldatum"] == "2026-11-09"
    # Lieferdatum vor Bestelldatum als Basis, Zahlungsziel der KI
    v, h = vorschlag_normalisieren(db, m, {"bestelldatum": "2026-11-02", "lieferdatum": "2026-11-16",
                                           "zahlungsziel_tage": "10 Tage"}, heute=HEUTE)
    assert v["zahldatum"] == "2026-11-26" and v["zahlungsziel_tage"] == 10
    assert any("Lieferdatum + 10 Tage" in x for x in h)
    # Standard 30 Tage ab Bestelldatum; landet auf dem 1. Weihnachtstag → Montag 28.12.
    v, h = vorschlag_normalisieren(db, m, {"bestelldatum": "2026-11-25"}, heute=HEUTE)
    assert v["zahldatum"] == "2026-12-28"
    assert any("geschätzt" in x and "30 Tage" in x for x in h)
    # Zahldatum vor Bestelldatum ist unplausibel
    v, h = vorschlag_normalisieren(db, m, {"bestelldatum": "2026-11-02", "zahldatum": "2026-10-01"},
                                   heute=HEUTE)
    assert v["zahldatum"] == "2026-12-02"
    assert any("vor dem Bestelldatum" in x for x in h)


def test_zahldatum_feiertag_je_bundesland(db):
    nw = _mandant(db, "NW")
    v, _ = vorschlag_normalisieren(db, nw, {"bestelldatum": "2026-05-05"}, heute=HEUTE)
    assert v["zahldatum"] == "2026-06-05"          # Fronleichnam 04.06. in NRW
    be = models.Mandant(name="Spree GmbH", kurzname="spree", kontenrahmen="SKR03", bundesland="BE")
    db.add(be)
    db.flush()
    v, _ = vorschlag_normalisieren(db, be, {"bestelldatum": "2026-05-05"}, heute=HEUTE)
    assert v["zahldatum"] == "2026-06-04"


def test_mandant_ist_nie_kreditor(db):
    m = _mandant(db)
    v, h = vorschlag_normalisieren(db, m, {"kreditor": "Nordlicht Handel GmbH"}, heute=HEUTE)
    assert v["kreditor"] is None
    assert any("Mandanten selbst" in x for x in h)
    v, _ = vorschlag_normalisieren(db, m, {"kreditor": "NORDLICHT HANDEL GmbH & Co. KG"}, heute=HEUTE)
    assert v["kreditor"] is None
    v, _ = vorschlag_normalisieren(db, m, {"kreditor": "Nordlicht Logistik GmbH"}, heute=HEUTE)
    assert v["kreditor"] == "Nordlicht Logistik GmbH"


def test_iban_pruefung(db):
    m = _mandant(db)
    v, _ = vorschlag_normalisieren(db, m, {"iban": "de89 3704 0044 0532 0130 00"}, heute=HEUTE)
    assert v["iban"] == IBAN_LIEFERANT
    v, h = vorschlag_normalisieren(db, m, {"iban": "DE89370400440532013001"}, heute=HEUTE)
    assert v["iban"] is None and any("ungültig" in x for x in h)
    v, h = vorschlag_normalisieren(db, m, {"iban": IBAN_EIGEN}, heute=HEUTE)
    assert v["iban"] is None and any("Bankkonto des Mandanten" in x for x in h)


def test_texte_und_dokumentart(db):
    m = _mandant(db)
    v, h = vorschlag_normalisieren(db, m, {
        "kreditor": "  Holz   Meier GmbH \n", "bestellnummer": 4711, "beschreibung": "x" * 900,
        "dokumentart": "Rechnung", "hinweise": "Betrag unsicher",
    }, heute=HEUTE)
    assert v["kreditor"] == "Holz Meier GmbH"
    assert v["bestellnummer"] == "4711"
    assert len(v["beschreibung"]) == 500
    assert any("Rechnung" in x for x in h)
    assert "KI: Betrag unsicher" in h


def test_unsinnige_typen_ohne_absturz(db):
    m = _mandant(db)
    roh = {k: {"verschachtelt": [1, 2]} for k in ki.KI_SCHLUESSEL}
    roh["hinweise"] = [None, 5, {"a": 1}]
    v, _ = vorschlag_normalisieren(db, m, roh, heute=HEUTE)
    assert v["kreditor"] is None and v["betrag_netto"] is None and v["konto_id"] is None
    assert v["zahldatum"] == "2026-11-30"
    v, _ = vorschlag_normalisieren(db, m, None, heute=HEUTE)   # type: ignore[arg-type]
    assert v["zahldatum"] == "2026-11-30"
    json.dumps(v)
