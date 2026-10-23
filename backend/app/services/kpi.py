"""Kennzahlen (KPI) für das Management-Fact-Sheet.

Alle Werte stammen aus `berechne_plan` (Ist bis heute, Plan ab morgen), damit
KPI-Seite, 13-Wochen-Plan und Exporte identisch rechnen. Eingefrorene
Planstände (Snapshots) liefern
  - die Soll-Linie der Liquidität (gewählte Soll-Referenz bzw. Plan-Basis) und
  - den Planvergleich („Anpassungen“): aktueller Stand gegen einen frei
    wählbaren früheren Planstand (z. B. Vorwoche oder Plan zum Antrag).
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..models import PostenArt, PostenStatus, Richtung
from .liquiditaet import (
    _lade_konten,
    berechne_plan,
    gruppen_aufloeser,
    liquiditaet_zum,
    liquiditaets_richtung,
    snapshot_fluesse,
    snapshot_liquiditaet,
)

MONATSNAMEN = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
               "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
OHNE_GRUPPE = "OHNE"
MAX_EINZAHLUNGSKONTEN = 7       # darüber hinaus: „Übrige“


def _r(wert) -> float | None:
    return None if wert is None else round(float(wert), 2)


def kpi_daten(
    db: Session,
    mandant: models.Mandant,
    start: date | None = None,
    wochen: int = 13,
    vergleich_id: int | None = None,
    heute: date | None = None,
) -> dict:
    heute = heute or date.today()
    plan = berechne_plan(db, mandant, start=start, wochen=wochen, heute=heute)
    tage = plan["tage"]
    h = plan["heute"]
    liquiditaet = plan["bestaende"]["liquiditaet"]

    konten = _lade_konten(db, mandant.id)
    konto_by_id = {k.id: k for k in konten}
    gruppen = list(db.scalars(
        select(models.KontoGruppe).where(models.KontoGruppe.mandant_id == mandant.id)
    ))
    gruppe_fuer_key = gruppen_aufloeser(konto_by_id, gruppen)

    def auto(zeile: dict, tag: str) -> float:
        quelle = zeile["ist"] if tag <= h else zeile["plan"]
        return quelle.get(tag, 0.0)

    # ---- Kategorien (BWA-Gruppen) des aktuellen Plans je Tag ----
    gruppen_info: dict[str, dict] = {}
    aktuell: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for g in plan["zeilen"]:
        code = g["code"]
        sonder = next((x.sondereffekt for x in gruppen if x.code == code), False)
        gruppen_info[code] = {"name": g["name"], "richtung": g["richtung"], "sondereffekt": sonder}
        for kind in g["kinder"]:
            for tag in tage:
                wert = auto(kind, tag)
                if wert:
                    aktuell[code][tag] += wert

    # ---- Monate des Fensters ----
    monate: list[dict] = []
    for tag in tage:
        if not monate or monate[-1]["schluessel"] != tag[:7]:
            monate.append({
                "schluessel": tag[:7],
                "label": f"{MONATSNAMEN[int(tag[5:7]) - 1]} {tag[2:4]}",
                "tage": [],
            })
        monate[-1]["tage"].append(tag)
    for m in monate:
        vergangen = [t for t in m["tage"] if t <= h]
        m["status"] = ("IST" if len(vergangen) == len(m["tage"])
                       else "PLAN" if not vergangen else "IST/PLAN")

    summen = plan["summen"]

    def summe_auto(reihe: dict, tage_: list[str]) -> float:
        return sum(auto(reihe, t) for t in tage_)

    # ---- Planstände: Soll-Referenz und Vergleich ----
    referenz = db.get(models.PlanSnapshot, plan["snapshot"]["id"]) if plan["snapshot"] else None
    if vergleich_id is not None:
        vergleich = db.get(models.PlanSnapshot, vergleich_id)
        if vergleich is None or vergleich.mandant_id != mandant.id:
            raise ValueError("Planstand nicht gefunden")
    else:
        vergleich = referenz

    def liq_verlauf(snap) -> dict[str, float]:
        if snap is None:
            return {}
        return {d.isoformat(): float(v) for d, v in
                snapshot_liquiditaet(db, mandant, snap, konto_by_id, gruppe_fuer_key, heute).items()}

    soll_liq = liq_verlauf(referenz)
    vgl_liq = soll_liq if vergleich is referenz else liq_verlauf(vergleich)

    # Vergleichsplan je Kategorie und Tag (nur liquiditätswirksame Zeilen)
    vgl_gruppe: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    vgl_tage: set[str] = set()
    if vergleich is not None:
        for key, tageswerte in snapshot_fluesse(vergleich).items():
            richtung = liquiditaets_richtung(key, konto_by_id, gruppe_fuer_key)
            if richtung is None:
                continue
            g = gruppe_fuer_key(key)
            code = g.code if g else OHNE_GRUPPE
            if code not in gruppen_info:
                gruppen_info[code] = {
                    "name": g.name if g else "Nicht zugeordnet",
                    "richtung": g.richtung if g else Richtung.AUS.value,
                    "sondereffekt": g.sondereffekt if g else False,
                }
            for d, v in tageswerte.items():
                vgl_gruppe[code][d.isoformat()] += float(v)
        vgl_tage = {(vergleich.stichtag + timedelta(days=i)).isoformat()
                    for i in range(vergleich.wochen * 7)}
    ueberlappung = [t for t in tage if t in vgl_tage]

    # ---- Liquidität je Wochenende: Soll (Referenz) und Ist/Prognose ----
    liquiditaet_wochen = [
        {
            "label": w["label"],
            "bis": w["bis"],
            "wert": _r(liquiditaet.get(w["bis"])),
            "prognose": w["bis"] > h,
            "soll": _r(soll_liq.get(w["bis"])),
        }
        for w in plan["wochen"]
    ]

    # ---- Monatswerte ----
    monats_liste = []
    for m in monate:
        letzter = m["tage"][-1]
        ueber = [t for t in m["tage"] if t in vgl_tage]
        vgl_netto = (sum(vgl_gruppe[c].get(t, 0.0) for c in vgl_gruppe for t in ueber)
                     if ueber else None)
        monats_liste.append({
            "schluessel": m["schluessel"],
            "label": m["label"],
            "status": m["status"],
            "einzahlungen": _r(summe_auto(summen["einzahlungen"], m["tage"])),
            "auszahlungen": _r(summe_auto(summen["auszahlungen"], m["tage"])),
            "netto": _r(summe_auto(summen["netto"], m["tage"])),
            "liquiditaet_ende": _r(liquiditaet.get(letzter)),
            "vergleich_liquiditaet_ende": _r(vgl_liq.get(letzter)) if vergleich else None,
            "vergleich_netto": _r(vgl_netto),
        })

    # ---- Planvergleich je Kategorie (Liquiditätswirkung aktuell − Vergleich) ----
    kategorien = []
    if vergleich is not None:
        for code, info in gruppen_info.items():
            akt = sum(aktuell[code].get(t, 0.0) for t in ueberlappung)
            vgl = sum(vgl_gruppe[code].get(t, 0.0) for t in ueberlappung)
            if not akt and not vgl:
                continue
            je_monat = {}
            for m in monate:
                tage_m = [t for t in m["tage"] if t in vgl_tage]
                if tage_m:
                    je_monat[m["schluessel"]] = _r(
                        sum(aktuell[code].get(t, 0.0) - vgl_gruppe[code].get(t, 0.0) for t in tage_m))
            kategorien.append({
                "code": code, "name": info["name"], "richtung": info["richtung"],
                "aktuell": _r(akt), "vergleich": _r(vgl), "veraenderung": _r(akt - vgl),
                "je_monat": je_monat,
            })
        kategorien.sort(key=lambda k: (k["richtung"] != Richtung.EIN.value, -abs(k["veraenderung"] or 0)))

    # ---- Verteilungen über das Fenster ----
    einzahlungen_konten: list[dict] = []
    for g in plan["zeilen"]:
        if g["richtung"] != Richtung.EIN.value:
            continue
        for kind in g["kinder"]:
            summe = sum(auto(kind, t) for t in tage)
            if summe > 0:
                einzahlungen_konten.append({
                    "name": f"{kind['nummer']} {kind['name']}".strip(), "summe": summe,
                    "sondereffekt": gruppen_info[g["code"]]["sondereffekt"],
                })
    einzahlungen_konten.sort(key=lambda x: -x["summe"])
    if len(einzahlungen_konten) > MAX_EINZAHLUNGSKONTEN:
        rest = einzahlungen_konten[MAX_EINZAHLUNGSKONTEN - 1:]
        einzahlungen_konten = einzahlungen_konten[:MAX_EINZAHLUNGSKONTEN - 1] + [{
            "name": f"Übrige ({len(rest)} Konten)", "summe": sum(x["summe"] for x in rest),
            "sondereffekt": False,
        }]
    ein_gesamt = sum(x["summe"] for x in einzahlungen_konten)
    for x in einzahlungen_konten:
        x["anteil"] = round(x["summe"] / ein_gesamt, 4) if ein_gesamt else None
        x["summe"] = _r(x["summe"])

    auszahlungen_gruppen = []
    for code, info in gruppen_info.items():
        if info["richtung"] == Richtung.EIN.value:
            continue
        summe = -sum(aktuell[code].get(t, 0.0) for t in tage)
        if summe > 0.005:
            auszahlungen_gruppen.append({"code": code, "name": info["name"], "summe": summe,
                                         "sondereffekt": info["sondereffekt"]})
    auszahlungen_gruppen.sort(key=lambda x: -x["summe"])
    aus_gesamt = sum(x["summe"] for x in auszahlungen_gruppen)
    aus_ohne = sum(x["summe"] for x in auszahlungen_gruppen if not x["sondereffekt"])
    for x in auszahlungen_gruppen:
        x["anteil"] = round(x["summe"] / aus_gesamt, 4) if aus_gesamt else None
        x["anteil_ohne"] = (None if x["sondereffekt"] or not aus_ohne
                            else round(x["summe"] / aus_ohne, 4))
        x["summe"] = _r(x["summe"])

    # ---- Kennzahlen ----
    ein = sum(v for c, tw in aktuell.items() if gruppen_info[c]["richtung"] == Richtung.EIN.value
              for v in tw.values())
    aus = -sum(v for c, tw in aktuell.items() if gruppen_info[c]["richtung"] != Richtung.EIN.value
               for v in tw.values())
    ein_ohne = sum(v for c, tw in aktuell.items()
                   if gruppen_info[c]["richtung"] == Richtung.EIN.value
                   and not gruppen_info[c]["sondereffekt"] for v in tw.values())
    aus_ohne_summe = -sum(v for c, tw in aktuell.items()
                          if gruppen_info[c]["richtung"] != Richtung.EIN.value
                          and not gruppen_info[c]["sondereffekt"] for v in tw.values())

    def marge(e: float, a: float) -> float | None:
        return round((e - a) / e, 4) if e > 0.005 else None

    zukunft = [t for t in tage if t >= h] or tage
    tief_tag = min(zukunft, key=lambda t: liquiditaet[t])
    unterdeckung = next((t for t in zukunft if liquiditaet[t] < 0), None)

    abgeschlossen = [w for w in plan["wochen"] if w["bis"] < h]
    if abgeschlossen:
        kw = abgeschlossen[-1]
        kw_tag, kw_label = kw["bis"], kw["label"]
    elif h in tage:
        kw_tag, kw_label = h, "heute"
    else:
        kw_tag = kw_label = None
    kw_sollist = None
    if kw_tag and soll_liq.get(kw_tag) is not None:
        ist_wert, soll_wert = liquiditaet[kw_tag], soll_liq[kw_tag]
        kw_sollist = {"label": kw_label, "bis": kw_tag, "ist": _r(ist_wert),
                      "soll": _r(soll_wert), "abweichung": _r(ist_wert - soll_wert)}

    offen: dict[str, Decimal] = defaultdict(Decimal)
    for p in db.scalars(select(models.OffenerPosten).where(
        models.OffenerPosten.mandant_id == mandant.id,
        models.OffenerPosten.status == PostenStatus.OFFEN.value,
    )):
        offen[p.art] += max(Decimal("0"), p.betrag_brutto - (p.bezahlt_betrag or Decimal("0")))

    liq_heute = (liquiditaet[h] if h in liquiditaet
                 else float(liquiditaet_zum(db, mandant, heute, heute)))
    ende = tage[-1]

    def snap_info(s):
        return {"id": s.id, "stichtag": s.stichtag.isoformat(), "wochen": s.wochen,
                "kommentar": s.kommentar, "referenz": s.referenz} if s else None

    return {
        "start": plan["start"],
        "ende": plan["ende"],
        "heute": h,
        "referenz": snap_info(referenz),
        "vergleich": snap_info(vergleich),
        "kennzahlen": {
            "liquiditaet_heute": _r(liq_heute),
            "liquiditaet_ende": _r(liquiditaet[ende]),
            "liquiditaet_ende_vergleich": _r(vgl_liq.get(ende)) if vergleich else None,
            "tiefstand": {"wert": _r(liquiditaet[tief_tag]), "datum": tief_tag},
            "unterdeckung_am": unterdeckung,
            "kw_sollist": kw_sollist,
            "netto_fenster": _r(ein - aus),
            "einzahlungen_fenster": _r(ein),
            "auszahlungen_fenster": _r(aus),
            "umsatzrentabilitaet": marge(ein, aus),
            "umsatzrentabilitaet_ohne_effekte": marge(ein_ohne, aus_ohne_summe),
            "forderungen_offen": _r(offen[PostenArt.DEBITOR.value]),
            "verbindlichkeiten_offen": _r(offen[PostenArt.KREDITOR.value]),
            "gesperrte_insolvenzforderungen": plan["gesperrte_insolvenzforderungen"],
            "sondereffekt_gruppen": sorted(i["name"] for i in gruppen_info.values()
                                           if i["sondereffekt"]),
        },
        "liquiditaet_wochen": liquiditaet_wochen,
        "monate": monats_liste,
        "kategorien": kategorien,
        "einzahlungen_konten": einzahlungen_konten,
        "auszahlungen_gruppen": auszahlungen_gruppen,
    }
