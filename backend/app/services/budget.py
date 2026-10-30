"""Budgetplanung: Raster in Tages-, Wochen- oder Monatsspalten (Aufbau wie der
13-Wochen-Plan) und Erfassung einzelner Zellen über den Eingabedialog.

Erfassungsebenen (Vorrang Tag > Woche > Monat):
  - Monat: Netto-Budget mit Aufteilung je KW (eine Zahlung je Kalenderwoche),
    je Tag (gleichmäßig auf die Bankarbeitstage) oder nach Verteilungsprofil des
    Kontos; optional in die Folgemonate fortgeschrieben (bis zum nächsten
    erfassten Monatswert),
  - Woche (ISO-KW): ersetzt die anteiligen Monatswerte dieser Woche,
  - Tag: ersetzt den verteilten Wert dieses Tages (0 = kein Budget).
Die Verteilung entspricht der Planungs-Engine (budget_basis_woche) – ohne
Restbudget, Szenario und Insolvenzgeld, die erst im Plan wirken.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .feiertage import ist_bankarbeitstag
from .liquiditaet import (
    AUFTEILUNGEN, MonatsBudgets, _konto_richtung, budget_basis_woche, wochen_start,
)

ANSICHTEN = ("tage", "wochen", "monate")
EBENEN = ("monat", "woche", "tag")
MONATSNAMEN = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
CENT = Decimal("0.01")


def budget_konten(db: Session, mandant_id: int) -> list[models.Konto]:
    """Konten der Budgetplanung: aktive Konten außer Bank/Kasse."""
    return list(db.scalars(
        select(models.Konto)
        .where(models.Konto.mandant_id == mandant_id, models.Konto.aktiv.is_(True),
               models.Konto.typ.not_in((models.KontoTyp.BANK.value, models.KontoTyp.KASSE.value)))
        .order_by(models.Konto.nummer)
    ))


def _monatsende(jahr: int, monat: int) -> date:
    return date(jahr + (monat == 12), monat % 12 + 1, 1) - timedelta(days=1)


def _erfasste_werte(db: Session, mandant_id: int, von: date, bis: date):
    monate = MonatsBudgets(db.scalars(
        select(models.Budget).where(models.Budget.mandant_id == mandant_id)))
    woche = {(b.konto_id, b.jahr, b.kw): (b.betrag_netto, b.aufteilung) for b in db.scalars(
        select(models.BudgetWoche).where(models.BudgetWoche.mandant_id == mandant_id,
                                         models.BudgetWoche.jahr >= von.year - 1,
                                         models.BudgetWoche.jahr <= bis.year + 1))}
    tag = {(b.konto_id, b.datum): b.betrag_netto for b in db.scalars(
        select(models.BudgetTag).where(models.BudgetTag.mandant_id == mandant_id,
                                       models.BudgetTag.datum >= wochen_start(von),
                                       models.BudgetTag.datum <= bis + timedelta(days=6)))}
    return monate, woche, tag


def budget_netto_tage(db: Session, mandant: models.Mandant, von: date, bis: date,
                      konten: list[models.Konto] | None = None) -> dict[int, dict[date, Decimal]]:
    """Wirksames Netto-Budget je Konto und Tag im Zeitraum [von, bis]."""
    konten = konten if konten is not None else budget_konten(db, mandant.id)
    monate, woche, tag = _erfasste_werte(db, mandant.id, von, bis)
    mit_budget = monate.konto_ids | {k for (k, _, _) in woche} | {k for (k, _) in tag}
    ergebnis: dict[int, dict[date, Decimal]] = defaultdict(dict)
    for konto in konten:
        if konto.id not in mit_budget:
            continue
        w = wochen_start(von)
        while w <= bis:
            for d, (quelle, anteil, teiler) in budget_basis_woche(
                konto, w, w + timedelta(days=6), mandant.bundesland, monate, woche, tag
            ).items():
                if von <= d <= bis:
                    ergebnis[konto.id][d] = quelle * anteil / teiler
            w += timedelta(days=7)
    return ergebnis


def _perioden(ansicht: str, jahr: int | None, start: date, wochen: int, bl: str) -> list[dict]:
    start = wochen_start(start)
    ende = start + timedelta(days=7 * wochen - 1)
    if ansicht == "monate":
        if jahr is not None:                 # ganzes Kalenderjahr
            monate = [(jahr, m) for m in range(1, 13)]
        else:                                # alle Monate, die das Fenster berührt
            monate, j, m = [], start.year, start.month
            while (j, m) <= (ende.year, ende.month):
                monate.append((j, m))
                j, m = (j + 1, 1) if m == 12 else (j, m + 1)
        return [{"id": f"{j}-{m:02d}", "label": f"{MONATSNAMEN[m - 1]} {j}", "unter": "",
                 "von": date(j, m, 1), "bis": _monatsende(j, m)} for j, m in monate]
    if ansicht == "wochen":
        perioden = []
        for i in range(wochen):
            von = start + timedelta(days=7 * i)
            iso = von.isocalendar()
            perioden.append({"id": f"{iso.year}-W{iso.week:02d}", "label": f"KW {iso.week}",
                             "unter": f"{von:%d.%m.}–{von + timedelta(days=6):%d.%m.}",
                             "von": von, "bis": von + timedelta(days=6)})
        return perioden
    perioden = []
    for i in range(wochen * 7):
        d = start + timedelta(days=i)
        iso = d.isocalendar()
        perioden.append({"id": d.isoformat(), "label": WOCHENTAGE[d.weekday()], "unter": f"{d:%d.%m.}",
                         "von": d, "bis": d, "banktag": ist_bankarbeitstag(d, bl),
                         "woche": f"KW {iso.week}"})
    return perioden


def _erfasst(ansicht: str, monate: MonatsBudgets, woche: dict, tag: dict, konto_id: int, p: dict):
    """Auf der Ebene der Ansicht erfasster Wert und Herkunft des Monatswerts."""
    if ansicht == "monate":
        quelle = monate.quelle(konto_id, p["von"].year, p["von"].month)
        if quelle is None:
            return None, None
        eigen = (quelle.jahr, quelle.monat) == (p["von"].year, p["von"].month)
        return (quelle.betrag_netto if eigen else None,
                None if eigen else f"{MONATSNAMEN[quelle.monat - 1]} {quelle.jahr}")
    if ansicht == "wochen":
        iso = p["von"].isocalendar()
        satz = woche.get((konto_id, iso.year, iso.week))
        return (satz[0] if satz else None), None
    return tag.get((konto_id, p["von"])), None


def budget_raster(db: Session, mandant: models.Mandant, ansicht: str, jahr: int | None,
                  start: date, wochen: int) -> dict:
    """Spalten und Werte der Budgetseite, gegliedert nach BWA-Gruppen."""
    perioden = _perioden(ansicht, jahr, start, wochen, mandant.bundesland)
    von, bis = perioden[0]["von"], perioden[-1]["bis"]
    konten = budget_konten(db, mandant.id)
    netto = budget_netto_tage(db, mandant, von, bis, konten)
    monate, woche, tag = _erfasste_werte(db, mandant.id, von, bis)
    gruppen = list(db.scalars(select(models.KontoGruppe)
                              .where(models.KontoGruppe.mandant_id == mandant.id)
                              .order_by(models.KontoGruppe.sortierung, models.KontoGruppe.id)))

    summen: dict[str, dict[str, Decimal]] = {"EIN": defaultdict(Decimal), "AUS": defaultdict(Decimal)}
    zeilen_je_gruppe: dict[int | None, list[dict]] = defaultdict(list)
    for k in konten:
        tageswerte = netto.get(k.id, {})
        richtung = _konto_richtung(k)
        werte, erfasste, fortgeschrieben = {}, {}, {}
        for p in perioden:
            summe = sum((v for d, v in tageswerte.items() if p["von"] <= d <= p["bis"]), Decimal("0"))
            if summe:
                werte[p["id"]] = float(summe.quantize(CENT))
                if richtung in summen:          # aus gerundeten Werten wie die Gruppensummen
                    summen[richtung][p["id"]] += summe.quantize(CENT)
            e, herkunft = _erfasst(ansicht, monate, woche, tag, k.id, p)
            if e is not None:
                erfasste[p["id"]] = float(e)
            if herkunft:
                fortgeschrieben[p["id"]] = herkunft
        zeilen_je_gruppe[k.gruppe_id].append({
            "id": k.id, "nummer": k.nummer, "bezeichnung": k.bezeichnung, "richtung": richtung,
            "ust_satz": float(k.ust_satz) if k.ust_satz is not None else None,
            "verteilung": k.verteilung or "GLEICH",
            "werte": werte, "erfasst": erfasste, "fortgeschrieben": fortgeschrieben,
        })
    gruppen_liste = [{"id": g.id, "name": g.name, "richtung": g.richtung,
                      "konten": zeilen_je_gruppe.get(g.id, [])}
                     for g in gruppen if zeilen_je_gruppe.get(g.id)]
    if zeilen_je_gruppe.get(None):
        gruppen_liste.append({"id": None, "name": "Ohne Gruppe", "richtung": "AUS",
                              "konten": zeilen_je_gruppe[None]})
    return {
        "ansicht": ansicht, "von": von.isoformat(), "bis": bis.isoformat(),
        "heute": date.today().isoformat(),
        "perioden": [{**p, "von": p["von"].isoformat(), "bis": p["bis"].isoformat()} for p in perioden],
        "gruppen": gruppen_liste,
        # flache Liste für einfache Auswertungen (Reihenfolge wie die Gruppen)
        "konten": [k for g in gruppen_liste for k in g["konten"]],
        "summen": {r: {pid: float(v.quantize(CENT)) for pid, v in s.items()}
                   for r, s in summen.items()},
    }


# ------------------------------------------------------------ Eingabedialog

def _periode_tag(ebene: str, periode: str) -> date:
    """Erster Tag einer Periode (JJJJ-MM | JJJJ-Www | JJJJ-MM-TT)."""
    if ebene == "monat":
        j, m = (int(x) for x in periode.split("-"))
        return date(j, m, 1)
    if ebene == "woche":
        j, w = periode.split("-W")
        return date.fromisocalendar(int(j), int(w), 1)
    return date.fromisoformat(periode)


def budget_zelle(db: Session, mandant: models.Mandant, konto: models.Konto, tag: date) -> dict:
    """Erfasste Werte aller Ebenen, die einen Tag betreffen (für den Dialog)."""
    iso = tag.isocalendar()
    monate = MonatsBudgets(db.scalars(select(models.Budget).where(
        models.Budget.mandant_id == mandant.id, models.Budget.konto_id == konto.id)))
    quelle = monate.quelle(konto.id, tag.year, tag.month)
    eigen = quelle is not None and (quelle.jahr, quelle.monat) == (tag.year, tag.month)
    spaeter = sorted(
        f"{b.jahr}-{b.monat:02d}" for b in db.scalars(select(models.Budget).where(
            models.Budget.mandant_id == mandant.id, models.Budget.konto_id == konto.id))
        if (b.jahr, b.monat) > (tag.year, tag.month))
    w = db.scalar(select(models.BudgetWoche).filter_by(
        mandant_id=mandant.id, konto_id=konto.id, jahr=iso.year, kw=iso.week))
    t = db.scalar(select(models.BudgetTag).filter_by(mandant_id=mandant.id, konto_id=konto.id, datum=tag))
    wochen_von = date.fromisocalendar(iso.year, iso.week, 1)
    netto = budget_netto_tage(db, mandant, min(date(tag.year, tag.month, 1), wochen_von),
                              max(_monatsende(tag.year, tag.month), wochen_von + timedelta(days=6)), [konto])
    tage = netto.get(konto.id, {})

    def summe(von: date, bis: date) -> float:
        return float(sum((v for d, v in tage.items() if von <= d <= bis), Decimal("0")).quantize(CENT))

    return {
        "konto": {"id": konto.id, "nummer": konto.nummer, "bezeichnung": konto.bezeichnung,
                  "verteilung": konto.verteilung or "GLEICH",
                  "ust_satz": float(konto.ust_satz) if konto.ust_satz is not None else None},
        "monat": {
            "periode": f"{tag.year}-{tag.month:02d}", "label": f"{MONATSNAMEN[tag.month - 1]} {tag.year}",
            "erfasst": ({"betrag": float(quelle.betrag_netto), "aufteilung": quelle.aufteilung,
                         "fortschreiben": quelle.fortschreiben} if eigen else None),
            "fortgeschrieben_aus": (None if eigen or quelle is None else {
                "periode": f"{quelle.jahr}-{quelle.monat:02d}",
                "label": f"{MONATSNAMEN[quelle.monat - 1]} {quelle.jahr}",
                "betrag": float(quelle.betrag_netto), "aufteilung": quelle.aufteilung}),
            "spaetere": spaeter,
            "wirksam": summe(date(tag.year, tag.month, 1), _monatsende(tag.year, tag.month)),
        },
        "woche": {
            "periode": f"{iso.year}-W{iso.week:02d}", "label": f"KW {iso.week}",
            "von": wochen_von.isoformat(), "bis": (wochen_von + timedelta(days=6)).isoformat(),
            "erfasst": {"betrag": float(w.betrag_netto), "aufteilung": w.aufteilung} if w else None,
            "wirksam": summe(wochen_von, wochen_von + timedelta(days=6)),
        },
        "tag": {
            "periode": tag.isoformat(), "banktag": ist_bankarbeitstag(tag, mandant.bundesland),
            "erfasst": {"betrag": float(t.betrag_netto)} if t else None,
            "wirksam": summe(tag, tag),
        },
    }


def budget_zelle_speichern(db: Session, mandant: models.Mandant, konto: models.Konto, ebene: str,
                           periode: str, betrag: Decimal | None, aufteilung: str | None,
                           fortschreiben: bool, spaetere_entfernen: bool) -> str:
    """Wert einer Ebene speichern (betrag None = Eintrag entfernen). Liefert eine
    Beschreibung für das Audit-Protokoll."""
    if ebene not in EBENEN:
        raise ValueError("Ebene: monat, woche oder tag")
    if aufteilung is not None and aufteilung not in AUFTEILUNGEN:
        raise ValueError("Aufteilung: KW, TAG oder KONTO (Verteilungsprofil des Kontos)")
    if betrag is not None and betrag < 0:
        raise ValueError("Budgetbeträge bitte positiv (netto) erfassen")
    tag = _periode_tag(ebene, periode)
    if ebene == "monat":
        modell, schluessel = models.Budget, {"jahr": tag.year, "monat": tag.month}
        felder = {"aufteilung": aufteilung, "fortschreiben": fortschreiben}
    elif ebene == "woche":
        iso = tag.isocalendar()
        modell, schluessel = models.BudgetWoche, {"jahr": iso.year, "kw": iso.week}
        felder = {"aufteilung": aufteilung}
    else:
        modell, schluessel = models.BudgetTag, {"datum": tag}
        felder = {}
    vorhanden = db.scalar(select(modell).filter_by(mandant_id=mandant.id, konto_id=konto.id, **schluessel))
    if betrag is None:
        if vorhanden:
            db.delete(vorhanden)
        beschreibung = f"{konto.nummer} {ebene} {periode} entfernt"
    else:
        if vorhanden is None:
            vorhanden = modell(mandant_id=mandant.id, konto_id=konto.id, **schluessel)
            db.add(vorhanden)
        vorhanden.betrag_netto = betrag
        for feld, wert in felder.items():
            setattr(vorhanden, feld, wert)
        beschreibung = (f"{konto.nummer} {ebene} {periode}: {betrag} € netto"
                        + (f", Aufteilung {aufteilung or 'Konto'}" if ebene != "tag" else "")
                        + (", fortgeschrieben" if ebene == "monat" and fortschreiben else ""))
    if ebene == "monat" and spaetere_entfernen:
        spaetere = [b for b in db.scalars(select(models.Budget).where(
            models.Budget.mandant_id == mandant.id, models.Budget.konto_id == konto.id))
            if (b.jahr, b.monat) > (tag.year, tag.month)]
        for b in spaetere:
            db.delete(b)
        if spaetere:
            beschreibung += f", {len(spaetere)} spätere Monatswerte entfernt"
    return beschreibung
