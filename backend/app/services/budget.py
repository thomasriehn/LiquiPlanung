"""Budget-Raster für die Budgetseite: wirksames Netto-Budget je Konto in Tages-,
Wochen- oder Monatsspalten samt den auf der jeweiligen Ebene erfassten Werten.

Erfassungsebenen (Vorrang Tag > Woche > Monat):
  - Monat: Netto-Budget, verteilt nach dem Verteilungsprofil des Kontos,
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
from .liquiditaet import VERTEILUNGEN, _konto_richtung, budget_basis_woche, wochen_start

ANSICHTEN = ("tage", "wochen", "monate")
MONATSNAMEN = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def budget_konten(db: Session, mandant_id: int) -> list[models.Konto]:
    """Konten der Budgetplanung: aktive Konten außer Bank/Kasse."""
    return list(db.scalars(
        select(models.Konto)
        .where(models.Konto.mandant_id == mandant_id, models.Konto.aktiv.is_(True),
               models.Konto.typ.not_in((models.KontoTyp.BANK.value, models.KontoTyp.KASSE.value)))
        .order_by(models.Konto.nummer)
    ))


def _erfasste_werte(db: Session, mandant_id: int, von: date, bis: date):
    monat = {(b.konto_id, b.jahr, b.monat): b.betrag_netto for b in db.scalars(
        select(models.Budget).where(models.Budget.mandant_id == mandant_id,
                                    models.Budget.jahr >= von.year - 1,
                                    models.Budget.jahr <= bis.year + 1))}
    woche = {(b.konto_id, b.jahr, b.kw): b.betrag_netto for b in db.scalars(
        select(models.BudgetWoche).where(models.BudgetWoche.mandant_id == mandant_id,
                                         models.BudgetWoche.jahr >= von.year - 1,
                                         models.BudgetWoche.jahr <= bis.year + 1))}
    tag = {(b.konto_id, b.datum): b.betrag_netto for b in db.scalars(
        select(models.BudgetTag).where(models.BudgetTag.mandant_id == mandant_id,
                                       models.BudgetTag.datum >= wochen_start(von),
                                       models.BudgetTag.datum <= bis + timedelta(days=6)))}
    return monat, woche, tag


def budget_netto_tage(db: Session, mandant: models.Mandant, von: date, bis: date,
                      konten: list[models.Konto] | None = None) -> dict[int, dict[date, Decimal]]:
    """Wirksames Netto-Budget je Konto und Tag im Zeitraum [von, bis]."""
    konten = konten if konten is not None else budget_konten(db, mandant.id)
    monat, woche, tag = _erfasste_werte(db, mandant.id, von, bis)
    mit_budget = {k for (k, _, _) in monat} | {k for (k, _, _) in woche} | {k for (k, _) in tag}
    ergebnis: dict[int, dict[date, Decimal]] = defaultdict(dict)
    for konto in konten:
        if konto.id not in mit_budget:
            continue
        profil = konto.verteilung if konto.verteilung in VERTEILUNGEN else "GLEICH"
        w = wochen_start(von)
        while w <= bis:
            for d, (quelle, anteil, teiler) in budget_basis_woche(
                konto.id, profil, w, w + timedelta(days=6), mandant.bundesland, monat, woche, tag
            ).items():
                if von <= d <= bis:
                    ergebnis[konto.id][d] = quelle * anteil / teiler
            w += timedelta(days=7)
    return ergebnis


def _perioden(ansicht: str, jahr: int, start: date, wochen: int, bl: str) -> list[dict]:
    if ansicht == "monate":
        perioden = []
        for m in range(1, 13):
            von = date(jahr, m, 1)
            bis = (date(jahr + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
            perioden.append({"id": f"{jahr}-{m:02d}", "label": MONATSNAMEN[m - 1], "unter": str(jahr),
                             "von": von, "bis": bis})
        return perioden
    start = wochen_start(start)
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
        perioden.append({"id": d.isoformat(), "label": WOCHENTAGE[d.weekday()], "unter": f"{d:%d.%m.}",
                         "von": d, "bis": d, "banktag": ist_bankarbeitstag(d, bl)})
    return perioden


def budget_raster(db: Session, mandant: models.Mandant, ansicht: str, jahr: int,
                  start: date, wochen: int) -> dict:
    """Spalten und Werte der Budgetseite für eine Ansicht."""
    perioden = _perioden(ansicht, jahr, start, wochen, mandant.bundesland)
    von, bis = perioden[0]["von"], perioden[-1]["bis"]
    konten = budget_konten(db, mandant.id)
    netto = budget_netto_tage(db, mandant, von, bis, konten)
    monat, woche, tag = _erfasste_werte(db, mandant.id, von, bis)
    gruppen = {g.id: g for g in db.scalars(
        select(models.KontoGruppe).where(models.KontoGruppe.mandant_id == mandant.id))}

    def erfasst(konto_id: int, p: dict) -> Decimal | None:
        if ansicht == "monate":
            return monat.get((konto_id, p["von"].year, p["von"].month))
        if ansicht == "wochen":
            iso = p["von"].isocalendar()
            return woche.get((konto_id, iso.year, iso.week))
        return tag.get((konto_id, p["von"]))

    summen: dict[str, dict[str, Decimal]] = {"EIN": defaultdict(Decimal), "AUS": defaultdict(Decimal)}
    zeilen = []
    for k in konten:
        tageswerte = netto.get(k.id, {})
        richtung = _konto_richtung(k)
        werte, erfasste = {}, {}
        for p in perioden:
            summe = sum((v for d, v in tageswerte.items() if p["von"] <= d <= p["bis"]), Decimal("0"))
            if summe:
                werte[p["id"]] = float(summe.quantize(Decimal("0.01")))
                if richtung in summen:
                    summen[richtung][p["id"]] += summe
            e = erfasst(k.id, p)
            if e is not None:
                erfasste[p["id"]] = float(e)
        g = gruppen.get(k.gruppe_id)
        zeilen.append({
            "id": k.id, "nummer": k.nummer, "bezeichnung": k.bezeichnung,
            "gruppe": g.name if g else None, "richtung": richtung,
            "ust_satz": float(k.ust_satz) if k.ust_satz is not None else None,
            "verteilung": k.verteilung or "GLEICH",
            "werte": werte, "erfasst": erfasste,
        })
    return {
        "ansicht": ansicht, "von": von.isoformat(), "bis": bis.isoformat(),
        "perioden": [{**p, "von": p["von"].isoformat(), "bis": p["bis"].isoformat()} for p in perioden],
        "konten": zeilen,
        "summen": {r: {pid: float(v.quantize(Decimal("0.01"))) for pid, v in s.items()}
                   for r, s in summen.items()},
    }
