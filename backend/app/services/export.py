"""Excel- und PDF-Export der 13-Wochen-Planung und des Soll-/Ist-Vergleichs.

Beide Exporte arbeiten auf dem Ergebnis von `berechne_plan` bzw.
`soll_ist_vergleich`, damit Anzeige und Bericht identisch rechnen.
"""

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .. import models

VERFAHREN_NAMEN = {
    "REGELMANDAT": "Regelmandat",
    "VORLAEUFIG": "vorläufiges Verfahren",
    "EROEFFNET": "eröffnetes Verfahren",
    "EIGENVERWALTUNG": "Eigenverwaltung",
}


def _auto(zeile: dict, tag: str, heute: str) -> float:
    """Anzeige-Logik: Ist bis heute, Plan ab morgen."""
    if tag <= heute:
        return zeile.get("ist", {}).get(tag, 0.0)
    return zeile.get("plan", {}).get(tag, 0.0)


def _agg(kinder: list[dict]) -> dict:
    agg: dict = {"plan": {}, "ist": {}, "basis": {}}
    for kind in kinder:
        for feld in ("plan", "ist", "basis"):
            for tag, wert in kind.get(feld, {}).items():
                agg[feld][tag] = agg[feld].get(tag, 0.0) + wert
    return agg


def _wochenwert(zeile: dict, tage: list[str], heute: str, metrik: str) -> float | None:
    summe, belegt = 0.0, False
    for tag in tage:
        if metrik == "auto":
            wert = _auto(zeile, tag, heute)
        elif metrik == "plan":
            wert = zeile.get("plan", {}).get(tag, 0.0)
        elif metrik == "ist":
            if tag > heute:
                continue
            wert = zeile.get("ist", {}).get(tag, 0.0)
        else:  # delta
            if tag > heute:
                continue
            wert = zeile.get("ist", {}).get(tag, 0.0) - zeile.get("basis", {}).get(tag, 0.0)
        if wert:
            summe += wert
            belegt = True
    return summe if belegt else None


def _letzter_wert(werte: dict, tage: list[str]) -> float | None:
    """Letzter bekannter Wert innerhalb der Woche (Ist-Bestände enden heute)."""
    wert = None
    for tag in tage:
        w = werte.get(tag)
        if w is not None:
            wert = w
    return wert


def _datum_de(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}.{iso[0:4]}"


def _de(wert: float | None) -> str:
    if wert is None or abs(wert) < 0.005:
        return ""
    return f"{wert:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")


# --------------------------------------------------------------------- Excel

_FETT = Font(bold=True)
_GRUPPE_FUELLUNG = PatternFill("solid", fgColor="EDF1F7")
_SUMME_FUELLUNG = PatternFill("solid", fgColor="DCE6F2")
_BESTAND_FUELLUNG = PatternFill("solid", fgColor="F6F2E8")
_KOPF_FUELLUNG = PatternFill("solid", fgColor="C9D8EC")
_ZAHL_FORMAT = "#,##0.00;[Red]-#,##0.00"
_DUENN = Side(style="thin", color="D6DDE6")
_RAHMEN = Border(left=_DUENN, right=_DUENN, top=_DUENN, bottom=_DUENN)


def _zeilen_struktur(plan: dict) -> list[tuple[str, str, dict]]:
    """Liefert (typ, name, zeilen-dict) in Anzeige-Reihenfolge."""
    struktur: list[tuple[str, str, dict]] = []
    for gruppe in plan["zeilen"]:
        struktur.append(("gruppe", gruppe["name"], _agg(gruppe["kinder"])))
        for kind in gruppe["kinder"]:
            name = f"{kind['nummer']} {kind['name']}".strip()
            struktur.append(("konto", name, kind))
    s = plan["summen"]
    struktur.append(("summe", "Einzahlungen gesamt", s["einzahlungen"]))
    struktur.append(("summe", "Auszahlungen gesamt", s["auszahlungen"]))
    struktur.append(("summe", "Netto-Cashflow", s["netto"]))
    return struktur


def plan_xlsx(plan: dict, sollist: dict | None, mandant: models.Mandant) -> bytes:
    wb = Workbook()
    heute = plan["heute"]

    # ---- Info ----
    info = wb.active
    info.title = "Info"
    zeilen = [
        ("13-Wochen-Liquiditätsplanung", ""),
        ("Mandant", mandant.name),
        ("Aktenzeichen", mandant.aktenzeichen or "–"),
        ("Verfahrensstatus", VERFAHREN_NAMEN.get(mandant.verfahrensstatus, mandant.verfahrensstatus)),
        ("Insolvenz-Stichtag", _datum_de(mandant.insolvenz_stichtag.isoformat()) if mandant.insolvenz_stichtag else "–"),
        ("Zeitraum", f"{_datum_de(plan['start'])} – {_datum_de(plan['ende'])}"),
        ("Stand (heute)", _datum_de(heute)),
        ("Soll-Basis", "eingefrorener Plan vom " + _datum_de(plan["snapshot"]["stichtag"])
         if plan["vergleichsbasis"] == "SNAPSHOT" else "aktueller Plan (kein Snapshot)"),
        ("Gesperrte Insolvenzforderungen (§ 38 InsO)", plan["gesperrte_insolvenzforderungen"]),
        ("Werte", "Ist bis heute, Plan ab morgen (Blatt 'Tage'); Plan/Ist/Δ je Woche (Blatt 'Wochen')"),
    ]
    for i, (a, b) in enumerate(zeilen, start=1):
        info.cell(row=i, column=1, value=a).font = _FETT
        zelle = info.cell(row=i, column=2, value=b)
        if isinstance(b, (int, float)):
            zelle.number_format = _ZAHL_FORMAT
    info.column_dimensions["A"].width = 42
    info.column_dimensions["B"].width = 44

    struktur = _zeilen_struktur(plan)

    # ---- Tage ----
    ws = wb.create_sheet("Tage")
    ws.cell(row=1, column=1, value="Konto / BWA-Zeile").font = _FETT
    spalte = 2
    for woche in plan["wochen"]:
        von = spalte
        for tag in woche["tage"]:
            zelle = ws.cell(row=2, column=spalte, value=f"{tag[8:10]}.{tag[5:7]}.")
            zelle.fill = _KOPF_FUELLUNG
            zelle.alignment = Alignment(horizontal="center")
            if tag == heute:
                zelle.font = _FETT
            ws.column_dimensions[get_column_letter(spalte)].width = 10
            spalte += 1
        ws.merge_cells(start_row=1, start_column=von, end_row=1, end_column=spalte - 1)
        kopf = ws.cell(row=1, column=von, value=woche["label"])
        kopf.fill = _KOPF_FUELLUNG
        kopf.font = _FETT
        kopf.alignment = Alignment(horizontal="center")
    ws.column_dimensions["A"].width = 38

    zeile_nr = 3
    for typ, name, daten in struktur:
        zelle = ws.cell(row=zeile_nr, column=1, value=name)
        spalte = 2
        for woche in plan["wochen"]:
            for tag in woche["tage"]:
                wert = _auto(daten, tag, heute)
                z = ws.cell(row=zeile_nr, column=spalte, value=wert if wert else None)
                z.number_format = _ZAHL_FORMAT
                z.border = _RAHMEN
                if typ == "gruppe":
                    z.fill = _GRUPPE_FUELLUNG
                if typ == "summe":
                    z.fill = _SUMME_FUELLUNG
                spalte += 1
        if typ in ("gruppe", "summe"):
            zelle.font = _FETT
            zelle.fill = _GRUPPE_FUELLUNG if typ == "gruppe" else _SUMME_FUELLUNG
        zeile_nr += 1

    b = plan["bestaende"]
    bestand_zeilen: list[tuple[str, dict[str, float | None]]] = [
        (f"{fk['nummer']} {fk['name']}", fk["bestand"]) for fk in b["finanzkonten"]
    ]
    bestand_zeilen.append(("Liquidität (Bank + Kasse)", b["liquiditaet"]))
    if b["kreditlinien"]:
        bestand_zeilen.append(("Verfügbar (inkl. Kreditlinien)", b["verfuegbar"]))
    bestand_zeilen.append(("Warenbestand (nachrichtlich)", b["waren"]))
    for name, werte in bestand_zeilen:
        zelle = ws.cell(row=zeile_nr, column=1, value=name)
        zelle.fill = _BESTAND_FUELLUNG
        zelle.font = _FETT
        spalte = 2
        for woche in plan["wochen"]:
            for tag in woche["tage"]:
                wert = werte.get(tag)
                z = ws.cell(row=zeile_nr, column=spalte, value=wert)
                z.number_format = _ZAHL_FORMAT
                z.fill = _BESTAND_FUELLUNG
                z.border = _RAHMEN
                spalte += 1
        zeile_nr += 1
    ws.freeze_panes = "B3"

    # ---- Wochen (Plan/Ist/Delta) ----
    ww = wb.create_sheet("Wochen")
    ww.cell(row=1, column=1, value="Konto / BWA-Zeile").font = _FETT
    spalte = 2
    for woche in plan["wochen"]:
        ww.merge_cells(start_row=1, start_column=spalte, end_row=1, end_column=spalte + 2)
        kopf = ww.cell(row=1, column=spalte,
                       value=f"{woche['label']} ({woche['von'][8:10]}.{woche['von'][5:7]}.–{woche['bis'][8:10]}.{woche['bis'][5:7]}.)")
        kopf.fill = _KOPF_FUELLUNG
        kopf.font = _FETT
        kopf.alignment = Alignment(horizontal="center")
        for i, unter in enumerate(("Plan", "Ist", "Δ")):
            z = ww.cell(row=2, column=spalte + i, value=unter)
            z.fill = _KOPF_FUELLUNG
            z.alignment = Alignment(horizontal="center")
            ww.column_dimensions[get_column_letter(spalte + i)].width = 11
        spalte += 3
    ww.column_dimensions["A"].width = 38

    zeile_nr = 3
    for typ, name, daten in struktur:
        zelle = ww.cell(row=zeile_nr, column=1, value=name)
        if typ in ("gruppe", "summe"):
            zelle.font = _FETT
            zelle.fill = _GRUPPE_FUELLUNG if typ == "gruppe" else _SUMME_FUELLUNG
        spalte = 2
        for woche in plan["wochen"]:
            for i, metrik in enumerate(("plan", "ist", "delta")):
                wert = _wochenwert(daten, woche["tage"], heute, metrik)
                z = ww.cell(row=zeile_nr, column=spalte + i, value=wert)
                z.number_format = _ZAHL_FORMAT
                z.border = _RAHMEN
                if typ == "gruppe":
                    z.fill = _GRUPPE_FUELLUNG
                if typ == "summe":
                    z.fill = _SUMME_FUELLUNG
            spalte += 3
        zeile_nr += 1
    # Liquidität je Wochenende
    zelle = ww.cell(row=zeile_nr, column=1, value="Liquidität zum Wochenende")
    zelle.font = _FETT
    zelle.fill = _BESTAND_FUELLUNG
    spalte = 2
    for woche in plan["wochen"]:
        wert = b["liquiditaet"].get(woche["tage"][-1])
        z = ww.cell(row=zeile_nr, column=spalte, value=wert)
        z.number_format = _ZAHL_FORMAT
        z.fill = _BESTAND_FUELLUNG
        spalte += 3
    ww.freeze_panes = "B3"

    # ---- Soll-Ist (falls Snapshot vorhanden) ----
    if sollist and sollist.get("snapshot"):
        ws2 = wb.create_sheet("Soll-Ist")
        ws2.cell(row=1, column=1,
                 value=f"Soll-/Ist-Vergleich – eingefrorener Plan vom {_datum_de(sollist['snapshot']['stichtag'])}"
                 ).font = _FETT
        ws2.cell(row=2, column=1, value="Zeile").font = _FETT
        spalte = 2
        for woche in sollist["wochen"]:
            ws2.merge_cells(start_row=2, start_column=spalte, end_row=2, end_column=spalte + 2)
            kopf = ws2.cell(row=2, column=spalte, value=woche["label"])
            kopf.fill = _KOPF_FUELLUNG
            kopf.font = _FETT
            kopf.alignment = Alignment(horizontal="center")
            for i, unter in enumerate(("Soll", "Ist", "Δ")):
                z = ws2.cell(row=3, column=spalte + i, value=unter)
                z.fill = _KOPF_FUELLUNG
                ws2.column_dimensions[get_column_letter(spalte + i)].width = 11
            spalte += 3
        ws2.column_dimensions["A"].width = 38
        zeile_nr = 4
        heute_iso = sollist["heute"]
        for z_ in sollist["zeilen"]:
            ws2.cell(row=zeile_nr, column=1, value=z_["name"])
            spalte = 2
            for woche in sollist["wochen"]:
                schluessel = f"{woche['jahr']}-{woche['kw']:02d}"
                soll = z_["plan"].get(schluessel)
                vergangen = woche["von"] <= heute_iso
                ist = z_["ist"].get(schluessel) if vergangen else None
                delta = None
                if vergangen and (soll is not None or ist is not None):
                    delta = (ist or 0.0) - (soll or 0.0)
                for i, wert in enumerate((soll, ist, delta)):
                    zc = ws2.cell(row=zeile_nr, column=spalte + i, value=wert)
                    zc.number_format = _ZAHL_FORMAT
                    zc.border = _RAHMEN
                spalte += 3
            zeile_nr += 1
        ws2.freeze_panes = "B4"

    puffer = BytesIO()
    wb.save(puffer)
    return puffer.getvalue()


# ----------------------------------------------------------------------- PDF

def plan_pdf(plan: dict, mandant: models.Mandant) -> bytes:
    heute = plan["heute"]
    puffer = BytesIO()
    dokument = SimpleDocTemplate(
        puffer,
        pagesize=landscape(A4),
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title=f"13-Wochen-Liquiditätsplanung {mandant.name}",
    )
    titel_stil = ParagraphStyle("titel", fontName="Helvetica-Bold", fontSize=13, spaceAfter=2)
    meta_stil = ParagraphStyle("meta", fontName="Helvetica", fontSize=8, textColor=colors.HexColor("#444444"))

    elemente = [Paragraph("13-Wochen-Liquiditätsplanung", titel_stil)]
    meta = (
        f"{mandant.name}"
        + (f" · {mandant.aktenzeichen}" if mandant.aktenzeichen else "")
        + f" · {VERFAHREN_NAMEN.get(mandant.verfahrensstatus, mandant.verfahrensstatus)}"
        + f" · Zeitraum {_datum_de(plan['start'])} – {_datum_de(plan['ende'])}"
        + f" · Stand {_datum_de(heute)}"
        + (" · Soll-Basis: Snapshot vom " + _datum_de(plan["snapshot"]["stichtag"])
           if plan["vergleichsbasis"] == "SNAPSHOT" else " · Soll-Basis: aktueller Plan")
    )
    elemente.append(Paragraph(meta, meta_stil))
    if plan["gesperrte_insolvenzforderungen"]:
        elemente.append(Paragraph(
            f"Zahlungsgesperrte Insolvenzforderungen (§ 38 InsO), nicht im Plan: "
            f"{_de(plan['gesperrte_insolvenzforderungen'])} €", meta_stil))
    elemente.append(Spacer(0, 4 * mm))

    kopf = ["Konto / BWA-Zeile"] + [
        f"{w['label']}\n{w['von'][8:10]}.{w['von'][5:7]}.–{w['bis'][8:10]}.{w['bis'][5:7]}."
        for w in plan["wochen"]
    ]
    daten: list[list[str]] = [kopf]
    stile = [
        ("FONTSIZE", (0, 0), (-1, -1), 6.5),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#C9D8EC")),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D6DDE6")),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
    ]

    def zeile_hinzu(name: str, werte: list[float | None], typ: str):
        nr = len(daten)
        daten.append([name] + [_de(w) for w in werte])
        if typ == "gruppe":
            stile.append(("BACKGROUND", (0, nr), (-1, nr), colors.HexColor("#EDF1F7")))
            stile.append(("FONTNAME", (0, nr), (-1, nr), "Helvetica-Bold"))
        elif typ == "summe":
            stile.append(("BACKGROUND", (0, nr), (-1, nr), colors.HexColor("#DCE6F2")))
            stile.append(("FONTNAME", (0, nr), (-1, nr), "Helvetica-Bold"))
        elif typ == "bestand":
            stile.append(("BACKGROUND", (0, nr), (-1, nr), colors.HexColor("#F6F2E8")))
            stile.append(("FONTNAME", (0, nr), (0, nr), "Helvetica-Bold"))
        elif typ == "konto":
            daten[nr][0] = "    " + daten[nr][0]
        for spalte, wert in enumerate(werte, start=1):
            if wert is not None and wert < -0.005:
                stile.append(("TEXTCOLOR", (spalte, nr), (spalte, nr), colors.HexColor("#B3362C")))

    for typ, name, zeilen_daten in _zeilen_struktur(plan):
        werte = [_wochenwert(zeilen_daten, w["tage"], heute, "auto") for w in plan["wochen"]]
        if typ == "konto" and not any(w is not None for w in werte):
            continue
        zeile_hinzu(name, werte, typ)

    b = plan["bestaende"]
    for fk in b["finanzkonten"]:
        zeile_hinzu(
            f"{fk['nummer']} {fk['name']}",
            [_letzter_wert(fk["bestand"], w["tage"]) for w in plan["wochen"]],
            "bestand",
        )
    zeile_hinzu("Liquidität (Bank + Kasse)",
                [b["liquiditaet"].get(w["tage"][-1]) for w in plan["wochen"]], "bestand")
    if b["kreditlinien"]:
        zeile_hinzu("Verfügbar (inkl. Kreditlinien)",
                    [b["verfuegbar"].get(w["tage"][-1]) for w in plan["wochen"]], "bestand")
    zeile_hinzu("Warenbestand (nachrichtlich)",
                [b["waren"].get(w["tage"][-1]) for w in plan["wochen"]], "bestand")

    breite = dokument.width
    erste = 62 * mm
    woche_breite = (breite - erste) / len(plan["wochen"])
    tabelle = Table(daten, colWidths=[erste] + [woche_breite] * len(plan["wochen"]),
                    repeatRows=1)
    tabelle.setStyle(TableStyle(stile))
    elemente.append(tabelle)
    elemente.append(Spacer(0, 3 * mm))
    elemente.append(Paragraph(
        "Wochenwerte: Ist bis heute, Plan ab morgen. Bestände jeweils zum Wochenende. "
        "Erstellt mit LiquiPlanung.", meta_stil))
    dokument.build(elemente)
    return puffer.getvalue()
