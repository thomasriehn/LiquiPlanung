"""Kennzahlen (KPI) für das Management-Fact-Sheet.

Alle Werte stammen aus `berechne_plan` (Ist bis heute, Plan ab morgen), damit
KPI-Seite, 13-Wochen-Plan und Exporte identisch rechnen. Eingefrorene
Planstände (Snapshots) liefern
  - die Soll-Linie der Liquidität (gewählte Soll-Referenz bzw. Plan-Basis) und
  - den Planvergleich („Anpassungen“): aktueller Stand gegen einen frei
    wählbaren früheren Planstand (z. B. Plan zum Antrag) und
  - die Vergleiche zur Vorwoche und zum Antrag (Fact-Sheet-Blöcke
    „Entwicklung KW“ und „Entwicklung Antrag“): Soll/Ist der letzten
    abgeschlossenen Woche gegen den Planstand (gesamt und je Kategorie),
    Veränderung des Ergebnisses (Liquidität zum Ende des gemeinsamen
    Zeitraums), Abweichung Woche gegen Ergebnis, Veränderungen je Kategorie
    und Monat sowie die Ein-/Auszahlungen der kommenden Wochen.
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
    wochen_start,
    liquiditaet_zum,
    liquiditaets_richtung,
    snapshot_fluesse,
    snapshot_liquiditaet,
)

MONATSNAMEN = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
               "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
OHNE_GRUPPE = "OHNE"
MAX_EINZAHLUNGSKONTEN = 7       # darüber hinaus: „Übrige“
FORECAST_WOCHEN = 4             # Ein-/Auszahlungen der kommenden Wochen


def _r(wert) -> float | None:
    return None if wert is None else round(float(wert), 2)


def kpi_daten(
    db: Session,
    mandant: models.Mandant,
    start: date | None = None,
    wochen: int = 13,
    vergleich_id: int | None = None,
    heute: date | None = None,
    vorwoche_id: int | None = None,
    antrag_id: int | None = None,
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

    def vergleichs_fluesse(snap):
        """Planflüsse eines Planstands je Kategorie und Tag (nur liquiditäts-
        wirksame Zeilen), dazu Ein-/Auszahlungen je Tag und seine Tage."""
        je_gruppe: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        ein_aus: dict[str, dict[str, float]] = {"ein": defaultdict(float), "aus": defaultdict(float)}
        if snap is None:
            return je_gruppe, ein_aus, set()
        for key, tageswerte in snapshot_fluesse(snap).items():
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
                je_gruppe[code][d.isoformat()] += float(v)
                ein_aus["ein" if v > 0 else "aus"][d.isoformat()] += float(v)
        snap_tage = {(snap.stichtag + timedelta(days=i)).isoformat() for i in range(snap.wochen * 7)}
        return je_gruppe, ein_aus, snap_tage

    def kategorien_vergleich(vgl_gruppe, vgl_tage) -> list[dict]:
        """Liquiditätswirkung aktuell − Planstand je Kategorie im gemeinsamen
        Zeitraum, je Monat aufgeschlüsselt."""
        gemeinsam = [t for t in tage if t in vgl_tage]
        ergebnis = []
        for code, info in gruppen_info.items():
            akt = sum(aktuell[code].get(t, 0.0) for t in gemeinsam)
            vgl = sum(vgl_gruppe[code].get(t, 0.0) for t in gemeinsam)
            if not akt and not vgl:
                continue
            je_monat = {}
            for m in monate:
                tage_m = [t for t in m["tage"] if t in vgl_tage]
                if tage_m:
                    je_monat[m["schluessel"]] = _r(
                        sum(aktuell[code].get(t, 0.0) - vgl_gruppe[code].get(t, 0.0) for t in tage_m))
            ergebnis.append({
                "code": code, "name": info["name"], "richtung": info["richtung"],
                "aktuell": _r(akt), "vergleich": _r(vgl), "veraenderung": _r(akt - vgl),
                "je_monat": je_monat,
            })
        ergebnis.sort(key=lambda k: (k["richtung"] != Richtung.EIN.value, -abs(k["veraenderung"] or 0)))
        return ergebnis

    vgl_gruppe, _, vgl_tage = vergleichs_fluesse(vergleich)

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
    kategorien = kategorien_vergleich(vgl_gruppe, vgl_tage) if vergleich is not None else []

    # ---- Vergleiche zur Vorwoche und zum Antrag ----
    def planstand(snap_id, automatisch):
        if snap_id is None:
            return automatisch
        snap = db.get(models.PlanSnapshot, snap_id)
        if snap is None or snap.mandant_id != mandant.id:
            raise ValueError("Planstand nicht gefunden")
        return snap

    def block(snap, auto: bool):
        if snap is None:
            return None
        erg = _planstand_vergleich(
            db, mandant, heute, snap, plan, tage, monate, liquiditaet, liq_verlauf(snap),
            vergleichs_fluesse(snap), kategorien_vergleich, gruppen_info, summe_auto, summen)
        erg["automatisch"] = auto
        return erg

    vorwoche = planstand(vorwoche_id, vorwoche_planstand(db, mandant.id, heute))
    antrag = planstand(antrag_id, antrag_planstand(db, mandant.id))
    vorwoche_block = block(vorwoche, vorwoche_id is None)
    antrag_block = block(antrag, antrag_id is None)

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

    return {
        "start": plan["start"],
        "ende": plan["ende"],
        "heute": h,
        "referenz": _snap_info(referenz),
        "vergleich": _snap_info(vergleich),
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
        "vorwoche": vorwoche_block,
        "antrag": antrag_block,
    }


def _snap_info(s: models.PlanSnapshot | None) -> dict | None:
    return {"id": s.id, "stichtag": s.stichtag.isoformat(), "wochen": s.wochen,
            "kommentar": s.kommentar, "referenz": s.referenz} if s else None


def vorwoche_planstand(db: Session, mandant_id: int, heute: date) -> models.PlanSnapshot | None:
    """Jüngster vor der laufenden Woche eingefrorene Planstand (bei wöchentlichem
    Einfrieren der Plan der Vorwoche)."""
    return db.scalars(
        select(models.PlanSnapshot)
        .where(models.PlanSnapshot.mandant_id == mandant_id,
               models.PlanSnapshot.stichtag < wochen_start(heute))
        .order_by(models.PlanSnapshot.stichtag.desc(), models.PlanSnapshot.id.desc())
    ).first()


def antrag_planstand(db: Session, mandant_id: int) -> models.PlanSnapshot | None:
    """Plan zum Antrag: jüngster Planstand mit „Antrag“ im Kommentar, sonst
    der älteste eingefrorene Planstand."""
    snaps = list(db.scalars(
        select(models.PlanSnapshot)
        .where(models.PlanSnapshot.mandant_id == mandant_id)
        .order_by(models.PlanSnapshot.stichtag, models.PlanSnapshot.id)
    ))
    mit_antrag = [s for s in snaps if s.kommentar and "antrag" in s.kommentar.lower()]
    if mit_antrag:
        return mit_antrag[-1]
    return snaps[0] if snaps else None


def _planstand_vergleich(db, mandant, heute, snap, plan, tage, monate, liquiditaet, vw_liq,
                         fluesse, kategorien_vergleich, gruppen_info, summe_auto, summen) -> dict:
    """Aktueller Stand gegen einen Planstand (Fact-Sheet-Blöcke „Entwicklung KW“
    für die Vorwoche bzw. „Entwicklung Antrag“)."""
    vw_gruppe, vw_ein_aus, vw_tage = fluesse
    h = plan["heute"]

    # ---- letzte abgeschlossene Woche: Ist gegen Vorwochenplan ----
    w_von = wochen_start(heute) - timedelta(days=7)
    w_bis = w_von + timedelta(days=6)
    woche = None
    if w_bis.isoformat() in vw_tage and w_bis.isoformat() in vw_liq:
        plan_w = berechne_plan(db, mandant, start=w_von, wochen=1, heute=heute)
        w_tage = plan_w["tage"]
        ist_je: dict[str, float] = defaultdict(float)
        for g in plan_w["zeilen"]:
            gruppen_info.setdefault(g["code"], {"name": g["name"], "richtung": g["richtung"],
                                                "sondereffekt": False})
            for kind in g["kinder"]:
                ist_je[g["code"]] += sum(kind["ist"].get(t, 0.0) for t in w_tage)
        kategorien_w = []
        for code, info in gruppen_info.items():
            soll = sum(vw_gruppe[code].get(t, 0.0) for t in w_tage)
            ist = ist_je.get(code, 0.0)
            if abs(soll) < 0.005 and abs(ist) < 0.005:
                continue
            kategorien_w.append({"code": code, "name": info["name"], "richtung": info["richtung"],
                                 "soll": _r(soll), "ist": _r(ist), "differenz": _r(ist - soll)})
        kategorien_w.sort(key=lambda k: (k["richtung"] != Richtung.EIN.value, -abs(k["differenz"] or 0)))
        ist_liq = plan_w["bestaende"]["liquiditaet"][w_bis.isoformat()]
        soll_liq = vw_liq[w_bis.isoformat()]
        soll_netto = sum(k["soll"] for k in kategorien_w)
        ist_netto = sum(k["ist"] for k in kategorien_w)
        woche = {
            "label": plan_w["wochen"][0]["label"], "von": w_von.isoformat(), "bis": w_bis.isoformat(),
            "soll": _r(soll_liq), "ist": _r(ist_liq), "abweichung": _r(ist_liq - soll_liq),
            "netto": {"soll": _r(soll_netto), "ist": _r(ist_netto), "differenz": _r(ist_netto - soll_netto)},
            "kategorien": kategorien_w,
        }

    # ---- Ergebnis: Liquidität am Ende des gemeinsamen Zeitraums ----
    gemeinsam = [t for t in tage if t in vw_tage and t in vw_liq]
    ergebnis = None
    if gemeinsam:
        tag = gemeinsam[-1]
        ergebnis = {"datum": tag, "planstand": _r(vw_liq[tag]), "aktuell": _r(liquiditaet[tag]),
                    "differenz": _r(liquiditaet[tag] - vw_liq[tag])}
    bruecke = None
    if woche and ergebnis:
        bruecke = {"woche": woche["abweichung"], "ergebnis": ergebnis["differenz"],
                   "differenz": _r(ergebnis["differenz"] - woche["abweichung"])}

    # ---- Ein-/Auszahlungen der kommenden Wochen: aktuell und Vorwochenplan ----
    forecast = []
    for w in plan["wochen"]:
        if w["bis"] < h or len(forecast) >= FORECAST_WOCHEN:
            continue
        w_t = [t for t in w["tage"] if t in tage]
        im_vw = [t for t in w_t if t in vw_tage]
        forecast.append({
            "label": w["label"], "von": w["von"], "bis": w["bis"],
            "einzahlungen": _r(summe_auto(summen["einzahlungen"], w_t)),
            "auszahlungen": _r(summe_auto(summen["auszahlungen"], w_t)),
            "planstand_einzahlungen": _r(sum(vw_ein_aus["ein"].get(t, 0.0) for t in im_vw)) if im_vw else None,
            "planstand_auszahlungen": _r(sum(vw_ein_aus["aus"].get(t, 0.0) for t in im_vw)) if im_vw else None,
        })

    return {
        "planstand": _snap_info(snap),
        "woche": woche,
        "ergebnis": ergebnis,
        "bruecke": bruecke,
        "liquiditaet_wochen": [
            {"label": w["label"], "bis": w["bis"], "wert": _r(liquiditaet.get(w["bis"])),
             "prognose": w["bis"] > h, "planstand": _r(vw_liq.get(w["bis"]))}
            for w in plan["wochen"]
        ],
        "monate": [
            {"schluessel": m["schluessel"], "label": m["label"], "status": m["status"],
             "liquiditaet_ende": _r(liquiditaet.get(m["tage"][-1])),
             "planstand_liquiditaet_ende": _r(vw_liq.get(m["tage"][-1]))}
            for m in monate
        ],
        "kategorien": kategorien_vergleich(vw_gruppe, vw_tage),
        "forecast_wochen": forecast,
    }
