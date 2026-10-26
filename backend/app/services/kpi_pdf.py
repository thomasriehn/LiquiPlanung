"""KPI-Übersicht als PDF (A4 quer).

Spiegelt die KPI-Seite: Kennzahlen-Kacheln, Diagramme (gleiche Farben und
Formen wie diagramme.js: Haarlinien-Raster, schmale Säulen mit gerundetem
Datenende, Prognose gestrichelt) und Wertetabellen für den Ausdruck.
Grundlage sind die Daten aus kpi.kpi_daten().
"""

from __future__ import annotations

import math
from io import BytesIO

from reportlab.graphics.shapes import Circle, Drawing, Line, Path, PolyLine, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (
    KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from .. import models
from .export import VERFAHREN_NAMEN

FARBE = {
    "blau": colors.HexColor("#2a78d6"), "orange": colors.HexColor("#eb6834"),
    "grau": colors.HexColor("#898781"), "rot": colors.HexColor("#e34948"),
    "gitter": colors.HexColor("#e1e0d9"), "achse": colors.HexColor("#c3c2b7"),
    "text": colors.HexColor("#1c2530"), "text2": colors.HexColor("#52514e"),
    "gruen": colors.HexColor("#1e7f4f"), "rand": colors.HexColor("#d6dde6"),
}
SCHRIFT, SCHRIFT_FETT = "Helvetica", "Helvetica-Bold"
GROESSE = 7.5                       # Achsen- und Datenbeschriftung

SEITE_BREITE = landscape(A4)[0] - 20 * mm
SPALTE = (SEITE_BREITE - 10) / 2   # zwei Karten je Zeile
PLOT_BREITE = SPALTE - 16


# ------------------------------------------------------------------ Formate

def _zahl(v: float, stellen: int, fest: bool = False) -> str:
    text = f"{v:,.{stellen}f}"
    if not fest and stellen and "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def euro_kompakt(v: float | None) -> str:
    if v is None:
        return "–"
    a = abs(v)
    if a >= 1e6:
        return _zahl(v / 1e6, 1) + " Mio."
    if a >= 1e3:
        return _zahl(v / 1e3, 0 if a >= 1e5 else 1) + " T"
    return _zahl(round(v), 0)


def euro_voll(v: float | None) -> str:
    return "–" if v is None else _zahl(v, 2, fest=True) + " €"


def prozent(v: float | None, stellen: int = 1) -> str:
    return "–" if v is None else _zahl(v * 100, stellen, fest=True) + " %"


def datum_de(iso: str | None) -> str:
    return f"{iso[8:10]}.{iso[5:7]}.{iso[0:4]}" if iso else "–"


def planstand_text(s: dict | None) -> str:
    if not s:
        return "–"
    return (f"{s['kommentar']} · " if s.get("kommentar") else "") + f"Stichtag {datum_de(s['stichtag'])}"


def _breite(text: str, groesse: float = GROESSE, schrift: str = SCHRIFT) -> float:
    return stringWidth(text, schrift, groesse)


def _ticks(lo: float, hi: float, ziel: int = 5) -> list[float]:
    """„Schöne“ Achsenteilung inkl. Nulllinie (wie dgTicks)."""
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    roh = (hi - lo) / ziel
    basis = 10 ** math.floor(math.log10(roh))
    schritt = next((f * basis for f in (1, 2, 2.5, 5, 10) if f * basis >= roh), 10 * basis)
    von, bis = math.floor(lo / schritt) * schritt, math.ceil(hi / schritt) * schritt
    ticks, t = [], von
    while t <= bis + schritt / 2:
        ticks.append(round(t, 6))
        t += schritt
    return ticks


# ------------------------------------------------------------------ Zeichnen
# Koordinaten wie im SVG (y nach unten); _Y rechnet in PDF-Koordinaten um.

class _Flaeche:
    def __init__(self, breite: float, hoehe: float):
        self.d = Drawing(breite, hoehe)
        self.h = hoehe

    def y(self, v: float) -> float:
        return self.h - v

    def linie(self, x1, y1, x2, y2, farbe, staerke=0.5, strich=None):
        self.d.add(Line(x1, self.y(y1), x2, self.y(y2), strokeColor=farbe, strokeWidth=staerke,
                        strokeDashArray=strich))

    def text(self, x, y, text, farbe=None, anker="start", groesse=GROESSE, schrift=SCHRIFT):
        self.d.add(String(x, self.y(y), text, fontName=schrift, fontSize=groesse,
                          fillColor=farbe or FARBE["grau"], textAnchor=anker))

    def balken(self, x, y, b, h, ende, farbe):
        """Säule/Balken mit gerundetem Datenende, eckig an der Nulllinie."""
        r = min(2.5, b / 2, h / 2 if ende in ("oben", "unten") else b / 2)
        if ende in ("rechts", "links"):
            r = min(2.5, h / 2, b)
        p = Path(fillColor=farbe, strokeColor=None, strokeWidth=0)
        Y = self.y
        if (ende in ("oben", "unten") and h <= 0.4) or (ende in ("rechts", "links") and b <= 0.4):
            hh = max(h, 0.4) if ende in ("oben", "unten") else h
            bb = max(b, 0.4) if ende in ("rechts", "links") else b
            p.moveTo(x, Y(y)); p.lineTo(x + bb, Y(y)); p.lineTo(x + bb, Y(y + hh)); p.lineTo(x, Y(y + hh))
            p.closePath()
            self.d.add(p)
            return
        k = 0.5523 * r      # Kreisbogen-Näherung
        if ende == "oben":
            p.moveTo(x, Y(y + h)); p.lineTo(x, Y(y + r))
            p.curveTo(x, Y(y + r - k), x + r - k, Y(y), x + r, Y(y))
            p.lineTo(x + b - r, Y(y))
            p.curveTo(x + b - r + k, Y(y), x + b, Y(y + r - k), x + b, Y(y + r))
            p.lineTo(x + b, Y(y + h))
        elif ende == "unten":
            p.moveTo(x, Y(y)); p.lineTo(x, Y(y + h - r))
            p.curveTo(x, Y(y + h - r + k), x + r - k, Y(y + h), x + r, Y(y + h))
            p.lineTo(x + b - r, Y(y + h))
            p.curveTo(x + b - r + k, Y(y + h), x + b, Y(y + h - r + k), x + b, Y(y + h - r))
            p.lineTo(x + b, Y(y))
        elif ende == "rechts":
            p.moveTo(x, Y(y)); p.lineTo(x + b - r, Y(y))
            p.curveTo(x + b - r + k, Y(y), x + b, Y(y + r - k), x + b, Y(y + r))
            p.lineTo(x + b, Y(y + h - r))
            p.curveTo(x + b, Y(y + h - r + k), x + b - r + k, Y(y + h), x + b - r, Y(y + h))
            p.lineTo(x, Y(y + h))
        else:
            p.moveTo(x + b, Y(y)); p.lineTo(x + r, Y(y))
            p.curveTo(x + r - k, Y(y), x, Y(y + r - k), x, Y(y + r))
            p.lineTo(x, Y(y + h - r))
            p.curveTo(x, Y(y + h - r + k), x + r - k, Y(y + h), x + r, Y(y + h))
            p.lineTo(x + b, Y(y + h))
        p.closePath()
        self.d.add(p)


def _leer(breite: float, text: str = "Keine Werte im Zeitraum.") -> Drawing:
    f = _Flaeche(breite, 40)
    f.text(breite / 2, 24, text, anker="middle", groesse=8)
    return f.d


def _y_achse(f: _Flaeche, ticks, y, links, rechts, fmt=euro_kompakt):
    for t in ticks:
        f.linie(links, y(t), rechts, y(t), FARBE["achse"] if t == 0 else FARBE["gitter"], 0.5)
        f.text(links - 5, y(t) + 2.6, fmt(t), anker="end")


def _x_beschriftung(f: _Flaeche, kategorien, x_mitte, y_pos, platz, unter=None):
    breiteste = max(_breite(k) for k in kategorien)
    jede = max(1, math.ceil((breiteste + 6) / max(platz, 1)))
    n = len(kategorien)
    for i, k in enumerate(kategorien):
        if i % jede and i != n - 1:
            continue
        if i == n - 1 and i % jede and (i % jede) < jede / 2:
            continue
        f.text(x_mitte(i), y_pos, k, anker="middle")
        if unter and unter[i]:
            f.text(x_mitte(i), y_pos + 9, unter[i], anker="middle", groesse=6.5)


def linien_diagramm(kategorien, reihen, breite=PLOT_BREITE, hoehe=165) -> Drawing:
    alle = [v for r in reihen for v in r["werte"] if v is not None]
    if not alle:
        return _leer(breite)
    ticks = _ticks(min(0, *alle), max(0, *alle))
    links = max(_breite(euro_kompakt(t)) for t in ticks) + 9
    rechts, oben, unten = breite - 42, 6, hoehe - 16
    f = _Flaeche(breite, hoehe)
    y = lambda v: unten - (v - ticks[0]) / (ticks[-1] - ticks[0]) * (unten - oben)   # noqa: E731
    schritt = (rechts - links) / (len(kategorien) - 1) if len(kategorien) > 1 else 0
    x = lambda i: links + i * schritt   # noqa: E731
    _y_achse(f, ticks, y, links, rechts)
    _x_beschriftung(f, kategorien, x, hoehe - 4, schritt)

    def punkte(werte, von, bis):
        pts = []
        for i in range(von, bis + 1):
            if werte[i] is not None:
                pts += [x(i), f.y(y(werte[i]))]
        return pts

    end_punkte = []
    for r in reihen:
        letzter = max((i for i, v in enumerate(r["werte"]) if v is not None), default=-1)
        if letzter < 0:
            continue
        pa = r.get("prognose_ab")
        grenze = letzter if pa is None else min(pa - 1, letzter)
        if grenze >= 0:
            pts = punkte(r["werte"], 0, grenze)
            if len(pts) >= 4:
                f.d.add(PolyLine(pts, strokeColor=r["farbe"], strokeWidth=1.5, strokeLineJoin=1,
                                 strokeLineCap=1))
        if grenze < letzter:
            pts = punkte(r["werte"], max(grenze, 0), letzter)
            if len(pts) >= 4:
                f.d.add(PolyLine(pts, strokeColor=r["farbe"], strokeWidth=1.5, strokeLineJoin=1,
                                 strokeLineCap=1, strokeDashArray=[3, 2.5]))
        f.d.add(Circle(x(letzter), f.y(y(r["werte"][letzter])), 2.6, fillColor=r["farbe"],
                       strokeColor=colors.white, strokeWidth=1.2))
        end_punkte.append((y(r["werte"][letzter]), euro_kompakt(r["werte"][letzter]), x(letzter)))
    sortiert = sorted(end_punkte)
    if not any(sortiert[i][0] - sortiert[i - 1][0] < 9 for i in range(1, len(sortiert))):
        for yy, text, xx in end_punkte:
            f.text(xx + 6, yy + 2.6, text, farbe=FARBE["text2"])
    return f.d


def saeulen_diagramm(kategorien, reihen, unter=None, divergierend=False, beschriften=None,
                     breite=PLOT_BREITE, hoehe=165) -> Drawing:
    alle = [v for r in reihen for v in r["werte"] if v is not None]
    if not alle:
        return _leer(breite)
    luft = 1 if beschriften is None else 1.14
    ticks = _ticks(min(0, *alle) * luft, max(0, *alle) * luft)
    links = max(_breite(euro_kompakt(t)) for t in ticks) + 9
    unter_hoehe = 9 if unter else 0
    rechts, oben, unten = breite - 6, 10, hoehe - 16 - unter_hoehe
    f = _Flaeche(breite, hoehe)
    y = lambda v: unten - (v - ticks[0]) / (ticks[-1] - ticks[0]) * (unten - oben)   # noqa: E731
    band = (rechts - links) / len(kategorien)
    x_mitte = lambda i: links + band * (i + 0.5)   # noqa: E731
    _y_achse(f, ticks, y, links, rechts)
    _x_beschriftung(f, kategorien, x_mitte, unten + 11, band, unter)
    k = len(reihen)
    b = max(3, min(14, (band * 0.72 - (k - 1) * 1.5) / k))
    gruppe = k * b + (k - 1) * 1.5
    for j, r in enumerate(reihen):
        for i, v in enumerate(r["werte"]):
            if v is None:
                continue
            x0 = x_mitte(i) - gruppe / 2 + j * (b + 1.5)
            y0, h = y(max(v, 0)), abs(y(v) - y(0))
            farbe = (FARBE["blau"] if v >= 0 else FARBE["rot"]) if divergierend else r["farbe"]
            f.balken(x0, y0, b, h, "oben" if v >= 0 else "unten", farbe)
            if beschriften == j and band >= _breite(euro_kompakt(v)) + 2:
                f.text(x0 + b / 2, y0 - 3 if v >= 0 else y0 + h + 8, euro_kompakt(v),
                       farbe=FARBE["text2"], anker="middle")
    return f.d


def balken_diagramm(kategorien, werte, farbe=None, divergierend=False, zusatz=None,
                    breite=PLOT_BREITE) -> Drawing:
    if not werte:
        return _leer(breite)
    farbe = farbe or FARBE["blau"]
    zeile, dicke, oben = 17, 9, 4
    hoehe = oben + zeile * len(kategorien) + 4
    label_breite = min(max(_breite(k) for k in kategorien) + 8, breite * 0.4)
    beschriftung = [euro_kompakt(v) + (zusatz[i] if zusatz else "") for i, v in enumerate(werte)]
    reserve = max(_breite(t) for t in beschriftung) + 6
    lo, hi = min(0, *werte), max(0, *werte)
    links = label_breite + (reserve if lo < 0 else 0)
    rechts = breite - (reserve if hi > 0 else 4)
    skala = (rechts - links) / ((hi - lo) or 1)
    x = lambda v: links + (v - lo) * skala   # noqa: E731
    f = _Flaeche(breite, hoehe)
    f.linie(x(0), oben - 1, x(0), hoehe - 2, FARBE["achse"], 0.5)
    for i, (k, v) in enumerate(zip(kategorien, werte)):
        y_mitte = oben + zeile * i + zeile / 2
        name = k
        while _breite(name) > label_breite - 8 and len(name) > 4:
            name = name[:-2]
        f.text(0, y_mitte + 2.6, k if name == k else name + "…", farbe=FARBE["text2"])
        x0, w = min(x(0), x(v)), abs(x(v) - x(0))
        fb = (FARBE["blau"] if v >= 0 else FARBE["rot"]) if divergierend else farbe
        f.balken(x0, y_mitte - dicke / 2, w, dicke, "rechts" if v >= 0 else "links", fb)
        f.text(x(v) + 4 if v >= 0 else x(v) - 4, y_mitte + 2.6, beschriftung[i],
               farbe=FARBE["text2"], anker="start" if v >= 0 else "end")
    return f.d


def legende(eintraege: list[tuple[str, object, str]], breite=PLOT_BREITE) -> Drawing | None:
    """eintraege: (name, farbe, art) mit art "flaeche" oder "linie"."""
    if not eintraege:
        return None
    zeilen, x, aktuell = [], 0.0, []
    for name, farbe, art in eintraege:
        w = 14 + _breite(name, 7.5) + 12
        if aktuell and x + w > breite:
            zeilen.append(aktuell)
            aktuell, x = [], 0.0
        aktuell.append((x, name, farbe, art))
        x += w
    zeilen.append(aktuell)
    f = _Flaeche(breite, 11 * len(zeilen))
    for z, zeile in enumerate(zeilen):
        y = z * 11 + 7
        for x0, name, farbe, art in zeile:
            if art == "linie":
                f.linie(x0, y - 2.5, x0 + 10, y - 2.5, farbe, 1.6)
            else:
                f.d.add(Rect(x0, f.y(y), 9, 7, rx=1.5, ry=1.5, fillColor=farbe, strokeColor=None))
            f.text(x0 + 14, y + 0.2, name, farbe=FARBE["text2"])
    return f.d


# ------------------------------------------------------------------ Layout

_STIL = {
    "titel": ParagraphStyle("titel", fontName=SCHRIFT_FETT, fontSize=14, leading=18, spaceAfter=2),
    "meta": ParagraphStyle("meta", fontName=SCHRIFT, fontSize=8, leading=10.5,
                           textColor=colors.HexColor("#444444")),
    "h2": ParagraphStyle("h2", fontName=SCHRIFT_FETT, fontSize=11, leading=14, spaceBefore=6,
                         spaceAfter=4, textColor=FARBE["text"]),
    "karte": ParagraphStyle("karte", fontName=SCHRIFT_FETT, fontSize=9.5, leading=12,
                            textColor=FARBE["text"]),
    "unter": ParagraphStyle("unter", fontName=SCHRIFT, fontSize=7.5, leading=9.5,
                            textColor=FARBE["text2"], spaceAfter=3),
    "k_label": ParagraphStyle("k_label", fontName=SCHRIFT, fontSize=7.5, leading=9.5,
                              textColor=FARBE["text2"]),
    "k_wert": ParagraphStyle("k_wert", fontName=SCHRIFT_FETT, fontSize=13, leading=16,
                             textColor=FARBE["text"]),
    "k_zeile": ParagraphStyle("k_zeile", fontName=SCHRIFT, fontSize=7, leading=9,
                              textColor=FARBE["text2"]),
    "zelle": ParagraphStyle("zelle", fontName=SCHRIFT, fontSize=7.5, leading=9),
    "kopf": ParagraphStyle("kopf", fontName=SCHRIFT_FETT, fontSize=7.5, leading=9),
    "kopf_rechts": ParagraphStyle("kopf_rechts", fontName=SCHRIFT_FETT, fontSize=7.5, leading=9,
                                  alignment=2),
    "hinweis": ParagraphStyle("hinweis", fontName=SCHRIFT, fontSize=7.5, leading=9.5,
                              textColor=FARBE["text2"], spaceBefore=4),
}


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _karte(titel: str, untertitel: str, diagramm: Drawing, eintraege=None) -> Table:
    inhalt = [[Paragraph(_esc(titel), _STIL["karte"])], [Paragraph(_esc(untertitel), _STIL["unter"])]]
    leg = legende(eintraege or [])
    if leg is not None:
        inhalt.append([leg])
    inhalt.append([diagramm])
    t = Table(inhalt, colWidths=[SPALTE])
    t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, FARBE["gitter"]),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("TOPPADDING", (0, 0), (-1, 0), 7), ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
    ]))
    return t


def _kartenzeile(links, rechts=None) -> Table:
    t = Table([[links, rechts or ""]], colWidths=[SPALTE + 5, SPALTE + 5])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (0, -1), 5),
        ("LEFTPADDING", (1, 0), (1, -1), 5), ("RIGHTPADDING", (1, 0), (1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def _delta_zeile(wert: float | None, text: str) -> tuple[str, object] | None:
    if wert is None:
        return None
    return (f"{'+' if wert >= 0 else ''}{euro_voll(wert)} {text}",
            FARBE["gruen"] if wert >= 0 else FARBE["rot"])


def _kachel(label: str, wert: str, zeilen=(), status=None) -> list:
    teile = [Paragraph(_esc(label), _STIL["k_label"]), Paragraph(_esc(wert), _STIL["k_wert"])]
    if status:
        art, text = status
        farbe = FARBE["rot"] if art == "kritisch" else FARBE["gruen"]
        teile.append(Paragraph(f'<font color="#{farbe.hexval()[2:]}"><b>{_esc(text)}</b></font>',
                               _STIL["k_zeile"]))
    for z in zeilen:
        if not z:
            continue
        text, farbe = z if isinstance(z, tuple) else (z, None)
        inhalt = _esc(text)
        if farbe is not None:
            inhalt = f'<font color="#{farbe.hexval()[2:]}">{inhalt}</font>'
        teile.append(Paragraph(inhalt, _STIL["k_zeile"]))
    return teile


def _kacheln(kpi: dict) -> Table:
    z = kpi["kennzahlen"]
    kacheln = [
        _kachel("Liquidität heute", euro_voll(z["liquiditaet_heute"]),
                [f"Bank + Kasse, Stand {datum_de(kpi['heute'])}"]),
        _kachel("Liquidität zum Planende", euro_voll(z["liquiditaet_ende"]), [
            _delta_zeile(z["liquiditaet_ende"] - z["liquiditaet_ende_vergleich"], "ggü. Vergleichsstand")
            if kpi.get("vergleich") and z.get("liquiditaet_ende_vergleich") is not None else None,
            f"Netto-Cashflow im Zeitraum {euro_voll(z['netto_fenster'])}"]),
        _kachel("Tiefster Stand", euro_voll(z["tiefstand"]["wert"]),
                [f"am {datum_de(z['tiefstand']['datum'])}"],
                ("kritisch", "Unterdeckung") if (z["tiefstand"]["wert"] or 0) < 0 else None),
    ]
    if z["unterdeckung_am"]:
        from datetime import date
        tage = (date.fromisoformat(z["unterdeckung_am"]) - date.fromisoformat(kpi["heute"])).days
        kacheln.append(_kachel("Liquiditätsreichweite", f"bis {datum_de(z['unterdeckung_am'])}",
                               [f"{tage} Tage ab heute"], ("kritisch", "Unterdeckung im Planungszeitraum")))
    else:
        kacheln.append(_kachel("Liquiditätsreichweite", f"über {datum_de(kpi['ende'])}",
                               ["über das Ende des Planungszeitraums hinaus"],
                               ("gut", "keine Unterdeckung")))
    s = z.get("kw_sollist")
    kacheln.append(
        _kachel(f"Liquidität {s['label']}: Ist gegen Soll", euro_voll(s["ist"]),
                [_delta_zeile(s["abweichung"], "ggü. Soll"),
                 f"Soll {euro_voll(s['soll'])} ({datum_de(s['bis'])})"])
        if s else _kachel("Liquidität Ist gegen Soll", "–",
                          ["keine Soll-Referenz für abgeschlossene Wochen im Zeitraum"]))
    kacheln.append(_kachel("Cash-Marge (Umsatzrentabilität)", prozent(z["umsatzrentabilitaet"]), [
        f"ohne Sondereffekte {prozent(z['umsatzrentabilitaet_ohne_effekte'])}",
        "Netto-Cashflow ÷ Einzahlungen im Zeitraum"]))
    kacheln.append(_kachel("Offene Forderungen", euro_voll(z["forderungen_offen"]),
                           ["Debitoren, Restbeträge"]))
    kacheln.append(_kachel("Offene Verbindlichkeiten", euro_voll(z["verbindlichkeiten_offen"]),
                           [f"davon gesperrt (§ 38 InsO) {euro_voll(z['gesperrte_insolvenzforderungen'])}"]))
    breite = (SEITE_BREITE - 3 * 6) / 4
    zeilen = [kacheln[:4], kacheln[4:8]]
    t = Table(zeilen, colWidths=[breite + 6] * 3 + [breite], rowHeights=None)
    stil = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    for r in range(2):
        for c in range(4):
            stil.append(("BOX", (c, r), (c, r), 0.6, FARBE["gitter"]))
    t.setStyle(TableStyle(stil))
    return t


def _wertetabelle(kopf: list[str], zeilen: list[list], breiten: list[float],
                  zahlen_ab: int = 1, fett_letzte: bool = False) -> Table:
    kopf_zellen = [Paragraph(_esc(text), _STIL["kopf_rechts" if i >= zahlen_ab else "kopf"])
                   for i, text in enumerate(kopf)]
    daten = [kopf_zellen] + zeilen
    t = Table(daten, colWidths=breiten, repeatRows=1)
    stil = [
        ("FONT", (0, 0), (-1, -1), SCHRIFT, 7.5),
        ("FONT", (0, 0), (-1, 0), SCHRIFT_FETT, 7.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf1fa")),
        ("ALIGN", (zahlen_ab, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#edf0f4")),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    if fett_letzte:
        stil.append(("FONT", (-1, 1), (-1, -1), SCHRIFT_FETT, 7.5))
    for r, zeile in enumerate(zeilen, start=1):
        for c, wert in enumerate(zeile):
            if isinstance(wert, str) and wert.startswith("-") and c >= zahlen_ab:
                stil.append(("TEXTCOLOR", (c, r), (c, r), FARBE["rot"]))
    t.setStyle(TableStyle(stil))
    return t


def _zahl_text(v: float | None) -> str:
    return "" if v is None or abs(v) < 0.005 else _zahl(v, 2, fest=True)


# ------------------------------------------------------------------ Bericht

def kpi_pdf(kpi: dict, mandant: models.Mandant) -> bytes:
    puffer = BytesIO()
    dokument = SimpleDocTemplate(
        puffer, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm,
        topMargin=10 * mm, bottomMargin=10 * mm, title=f"KPI-Übersicht {mandant.name}",
    )
    S = _STIL
    el: list = [Paragraph("KPI-Übersicht", S["titel"])]
    meta = (
        f"{_esc(mandant.name)}"
        + (f" · {_esc(mandant.aktenzeichen)}" if mandant.aktenzeichen else "")
        + f" · {VERFAHREN_NAMEN.get(mandant.verfahrensstatus, mandant.verfahrensstatus)}"
        + f" · Zeitraum {datum_de(kpi['start'])} – {datum_de(kpi['ende'])}"
        + f" · Ist bis {datum_de(kpi['heute'])}, danach Plan"
    )
    el.append(Paragraph(meta, S["meta"]))
    el.append(Paragraph(
        f"Soll-Referenz: {_esc(planstand_text(kpi['referenz'])) if kpi['referenz'] else 'noch kein eingefrorener Planstand'}"
        + (f" · Vergleich mit Planstand: {_esc(planstand_text(kpi['vergleich']))}" if kpi["vergleich"] else ""),
        S["meta"]))
    el.append(Spacer(0, 3 * mm))
    el.append(_kacheln(kpi))

    # ---- Liquidität ----
    w = kpi["liquiditaet_wochen"]
    prognose_ab = next((i for i, x in enumerate(w) if x["prognose"]), None)
    hat_soll = any(x["soll"] is not None for x in w)
    soll_name = f"Soll ({planstand_text(kpi['referenz'])})" if kpi["referenz"] else "Soll"
    reihen = []
    if hat_soll:
        reihen.append({"name": soll_name, "farbe": FARBE["grau"], "werte": [x["soll"] for x in w]})
    reihen.append({"name": "Ist bis heute, danach Prognose", "farbe": FARBE["blau"],
                   "werte": [x["wert"] for x in w], "prognose_ab": prognose_ab})
    karte_wochen = _karte(
        "Liquidität je Wochenende: Soll / Ist",
        "Bank + Kasse; gestrichelt: Prognose aus dem aktuellen Plan",
        linien_diagramm([x["label"] for x in w], reihen),
        ([(soll_name, FARBE["grau"], "linie")] if hat_soll else [])
        + [("Ist bis heute, danach Prognose", FARBE["blau"], "linie")])
    m = kpi["monate"]
    mit_vgl = bool(kpi["vergleich"]) and any(x["vergleich_liquiditaet_ende"] is not None for x in m)
    vgl_name = f"Vergleich ({planstand_text(kpi['vergleich'])})"
    reihen_m = []
    if mit_vgl:
        reihen_m.append({"name": vgl_name, "farbe": FARBE["grau"],
                         "werte": [x["vergleich_liquiditaet_ende"] for x in m]})
    reihen_m.append({"name": "Aktuell", "farbe": FARBE["blau"], "werte": [x["liquiditaet_ende"] for x in m]})
    karte_monate = _karte(
        "Liquidität zum Monatsende", "Randmonate bis zum Fensterbeginn bzw. -ende",
        saeulen_diagramm([x["label"] for x in m], reihen_m, unter=[x["status"] for x in m],
                         beschriften=len(reihen_m) - 1),
        [(vgl_name, FARBE["grau"], "flaeche"), ("Aktuell", FARBE["blau"], "flaeche")] if mit_vgl else None)
    el.append(Spacer(0, 2 * mm))
    el.append(KeepTogether([Paragraph("Liquidität", S["h2"]), _kartenzeile(karte_wochen, karte_monate)]))

    # ---- Ein- und Auszahlungen ----
    labels, status = [x["label"] for x in m], [x["status"] for x in m]
    karte_netto = _karte(
        "Netto-Cashflow je Monat", "Einzahlungen – Auszahlungen; Ist bis heute, danach Plan",
        saeulen_diagramm(labels, [{"name": "Netto", "farbe": FARBE["blau"], "werte": [x["netto"] for x in m]}],
                         unter=status, divergierend=True, beschriften=0),
        [("Überschuss", FARBE["blau"], "flaeche"), ("Unterdeckung", FARBE["rot"], "flaeche")])
    karte_einaus = _karte(
        "Einzahlungen und Auszahlungen je Monat", "Beträge; Ist bis heute, danach Plan",
        saeulen_diagramm(labels, [
            {"name": "Einzahlungen", "farbe": FARBE["blau"], "werte": [x["einzahlungen"] for x in m]},
            {"name": "Auszahlungen", "farbe": FARBE["orange"],
             "werte": [-x["auszahlungen"] if x["auszahlungen"] is not None else None for x in m]},
        ], unter=status),
        [("Einzahlungen", FARBE["blau"], "flaeche"), ("Auszahlungen", FARBE["orange"], "flaeche")])
    el.append(PageBreak())
    el.append(KeepTogether([Paragraph("Ein- und Auszahlungen", S["h2"]),
                            _kartenzeile(karte_netto, karte_einaus)]))

    # ---- Planvergleich ----
    if kpi["vergleich"]:
        titel = f"Planvergleich: aktueller Stand gegenüber {planstand_text(kpi['vergleich'])}"
        kat = sorted((k for k in kpi["kategorien"] if abs(k["veraenderung"] or 0) >= 0.005),
                     key=lambda k: -abs(k["veraenderung"]))
        karte_kat = _karte(
            "Veränderung je Kategorie",
            "Liquiditätswirkung im gemeinsamen Zeitraum (aktuell – Vergleich)",
            balken_diagramm([k["name"] for k in kat], [k["veraenderung"] for k in kat], divergierend=True)
            if kat else _leer(PLOT_BREITE, "Keine Veränderungen gegenüber dem Vergleichsstand."),
            [("Liquidität besser", FARBE["blau"], "flaeche"), ("Liquidität schlechter", FARBE["rot"], "flaeche")])
        mv = [x for x in m if x["vergleich_liquiditaet_ende"] is not None]
        karte_mv = _karte(
            "Veränderung der Monatsendliquidität", "aktuell – Vergleich",
            saeulen_diagramm([x["label"] for x in mv], [{
                "name": "Veränderung", "farbe": FARBE["blau"],
                "werte": [x["liquiditaet_ende"] - x["vergleich_liquiditaet_ende"] for x in mv]}],
                divergierend=True, beschriften=0),
            [("höher als geplant", FARBE["blau"], "flaeche"), ("niedriger als geplant", FARBE["rot"], "flaeche")])
        el.append(KeepTogether([Paragraph(_esc(titel), S["h2"]), _kartenzeile(karte_kat, karte_mv)]))

        monate = [x for x in m if any(x["schluessel"] in k["je_monat"] for k in kpi["kategorien"])]
        geaendert = [k for k in kpi["kategorien"] if abs(k["veraenderung"] or 0) >= 0.005
                     or any(abs(v or 0) >= 0.005 for v in k["je_monat"].values())]
        if geaendert:
            name_breite = 170
            rest = (SEITE_BREITE - name_breite) / (len(monate) + 1)
            zeilen = [[Paragraph(_esc(k["name"]), S["zelle"])]
                      + [_zahl_text(k["je_monat"].get(x["schluessel"])) for x in monate]
                      + [_zahl_text(k["veraenderung"])] for k in geaendert]
            unveraendert = len(kpi["kategorien"]) - len(geaendert)
            el.append(KeepTogether([
                Paragraph("Veränderung je Kategorie und Monat", S["karte"]),
                Paragraph("Liquiditätswirkung aktuell – Vergleich in €; positiv = Liquidität besser"
                          + (f" · {unveraendert} Kategorien unverändert (ausgeblendet)" if unveraendert else ""),
                          S["unter"]),
                _wertetabelle(["Kategorie"] + [x["label"] for x in monate] + ["Summe"], zeilen,
                              [name_breite] + [rest] * (len(monate) + 1), fett_letzte=True),
            ]))
    else:
        el.append(Paragraph("Planvergleich", S["h2"]))
        el.append(Paragraph("Noch kein eingefrorener Planstand – über „Plan einfrieren“ auf der "
                            "Plan-Seite anlegen.", S["hinweis"]))

    # ---- Umsatz und Kosten ----
    ein = kpi["einzahlungen_konten"]
    aus = kpi["auszahlungen_gruppen"]
    karte_ein = _karte(
        "Einzahlungen nach Konto", "Summe im Zeitraum, Anteil an allen Einzahlungen",
        balken_diagramm([x["name"] for x in ein], [x["summe"] for x in ein], farbe=FARBE["blau"],
                        zusatz=[f" · {prozent(x['anteil'], 0)}" for x in ein]))
    karte_aus = _karte(
        "Auszahlungen nach Kategorie", "Summe im Zeitraum, Anteil an allen Auszahlungen",
        balken_diagramm([x["name"] + (" *" if x["sondereffekt"] else "") for x in aus],
                        [x["summe"] for x in aus], farbe=FARBE["orange"],
                        zusatz=[f" · {prozent(x['anteil'], 0)}" for x in aus]))
    el.append(KeepTogether([Paragraph("Umsatz und Kosten", S["h2"]), _kartenzeile(karte_ein, karte_aus)]))
    gruppen = kpi["kennzahlen"]["sondereffekt_gruppen"]
    el.append(Paragraph(
        ("Sondereffekte (* – in den Kennzahlen „ohne Sondereffekte“ herausgerechnet): "
         + _esc(", ".join(gruppen)) + ".") if gruppen else
        "Für die Kennzahlen „ohne Sondereffekte“ ist noch keine Kontogruppe als Sondereffekt "
        "gekennzeichnet (Konten, Abschnitt Gruppen, Spalte „Sondereffekt“).", S["hinweis"]))

    # ---- Wertetabellen ----
    el.append(PageBreak())
    el.append(Paragraph("Wertetabellen", S["h2"]))
    halbe = SPALTE
    tab_w = _wertetabelle(
        ["Woche", "Ist / Prognose", "Soll", "Abweichung"],
        [[f"{x['label']} (bis {datum_de(x['bis'])})" + (" – Prognose" if x["prognose"] else ""),
          _zahl_text(x["wert"]), _zahl_text(x["soll"]),
          "–" if x["soll"] is None or x["wert"] is None else _zahl_text(x["wert"] - x["soll"])]
         for x in w],
        [halbe * 0.40, halbe * 0.2, halbe * 0.2, halbe * 0.2])
    kopf_m = ["Monat", "Einzahlungen", "Auszahlungen", "Netto", "Liquidität Monatsende"]
    if mit_vgl:
        kopf_m += ["Vergleich", "Veränderung"]
    zeilen_m = []
    for x in m:
        z = [f"{x['label']} ({x['status']})", _zahl_text(x["einzahlungen"]),
             _zahl_text(-x["auszahlungen"] if x["auszahlungen"] is not None else None),
             _zahl_text(x["netto"]), _zahl_text(x["liquiditaet_ende"])]
        if mit_vgl:
            v = x["vergleich_liquiditaet_ende"]
            z += [_zahl_text(v), "–" if v is None else _zahl_text(x["liquiditaet_ende"] - v)]
        zeilen_m.append(z)
    erste = SEITE_BREITE * 0.2
    el.append(Paragraph("Monatsübersicht", S["karte"]))
    el.append(Spacer(0, 2))
    el.append(_wertetabelle(kopf_m, zeilen_m,
                            [erste] + [(SEITE_BREITE - erste) / (len(kopf_m) - 1)] * (len(kopf_m) - 1)))
    el.append(Spacer(0, 4 * mm))
    tab_ein = _wertetabelle(
        ["Konto", "Summe", "Anteil"],
        [[Paragraph(_esc(x["name"]), S["zelle"]), _zahl_text(x["summe"]), prozent(x["anteil"])] for x in ein],
        [halbe * 0.6, halbe * 0.22, halbe * 0.18])
    tab_aus = _wertetabelle(
        ["Kategorie", "Summe", "Anteil", "ohne Sondereffekte"],
        [[Paragraph(_esc(x["name"] + (" (Sondereffekt)" if x["sondereffekt"] else "")), S["zelle"]),
          _zahl_text(x["summe"]), prozent(x["anteil"]),
          "–" if x["sondereffekt"] else prozent(x["anteil_ohne"])] for x in aus],
        [halbe * 0.46, halbe * 0.16, halbe * 0.14, halbe * 0.24])
    el.append(_kartenzeile(
        [Paragraph("Liquidität je Wochenende", S["karte"]), Spacer(0, 2), tab_w],
        [Paragraph("Einzahlungen nach Konto", S["karte"]), Spacer(0, 2), tab_ein, Spacer(0, 4 * mm),
         Paragraph("Auszahlungen nach Kategorie", S["karte"]), Spacer(0, 2), tab_aus]))

    def fusszeile(canvas, doc):
        canvas.saveState()
        canvas.setFont(SCHRIFT, 7)
        canvas.setFillColor(FARBE["grau"])
        canvas.drawString(10 * mm, 6 * mm, f"KPI-Übersicht · {mandant.name}")
        canvas.drawRightString(landscape(A4)[0] - 10 * mm, 6 * mm, f"Seite {doc.page}")
        canvas.restoreState()

    dokument.build(el, onFirstPage=fusszeile, onLaterPages=fusszeile)
    return puffer.getvalue()
