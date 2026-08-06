"""Insolvenzgeld-Assistent.

Im Insolvenzgeldzeitraum (i. d. R. die letzten drei Monate vor dem
Insolvenzereignis, §§ 165 ff. SGB III) entfallen beim Schuldner:

- Nettolohn-Auszahlungen (Insolvenzgeld bzw. dessen Vorfinanzierung nach
  § 170 Abs. 4 SGB III trägt die Löhne),
- Sozialversicherungsbeiträge für Beitragsmonate im Zeitraum (§ 175 SGB III),
- Lohnsteuer (keine Lohnzahlung durch den Arbeitgeber; Insolvenzgeld ist
  steuerfrei).

Die Engine unterdrückt daher im Zeitraum Personal-Budgets und
Personal-Dauerbuchungen und kürzt generierte SV-/LSt-Kalendertermine anteilig
nach der Überlappung ihres Beitrags-/Lohnmonats mit dem Zeitraum. Manuell
angelegte Termine ohne Periode bleiben unangetastet. Offene Posten werden nicht
automatisch unterdrückt – Altlohnansprüche gehören als Insolvenzforderung
klassifiziert.
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .feiertage import naechster_bankarbeitstag
from .zahlungskalender import _monat_plus  # Monatsarithmetik

CENT = Decimal("0.01")


def igeld_fenster(mandant: models.Mandant) -> tuple[date, date] | None:
    if (
        not mandant.insolvenzgeld_aktiv
        or mandant.insolvenzgeld_von is None
        or mandant.insolvenzgeld_bis is None
        or mandant.insolvenzgeld_von > mandant.insolvenzgeld_bis
    ):
        return None
    return mandant.insolvenzgeld_von, mandant.insolvenzgeld_bis


def monatsanteil(jahr: int, monat: int, fenster: tuple[date, date]) -> Decimal:
    """Anteil des Kalendermonats, der im Insolvenzgeldzeitraum liegt (0..1)."""
    von, bis = fenster
    m_von = date(jahr, monat, 1)
    fj, fm = _monat_plus(jahr, monat, 1)
    m_bis = date(fj, fm, 1) - timedelta(days=1)
    ueberlappung_von = max(m_von, von)
    ueberlappung_bis = min(m_bis, bis)
    if ueberlappung_von > ueberlappung_bis:
        return Decimal("0")
    tage = (ueberlappung_bis - ueberlappung_von).days + 1
    return Decimal(tage) / Decimal(m_bis.day)


def termin_entlastung(termin: models.Zahlungstermin, fenster: tuple[date, date]) -> Decimal:
    """Kürzungsbetrag eines SV-/LSt-Termins (0, wenn nicht betroffen)."""
    if termin.typ not in (models.TerminTyp.SV.value, models.TerminTyp.LST.value):
        return Decimal("0")
    periode = termin.periode or ""
    if len(periode) != 7 or periode[4] != "-":
        return Decimal("0")  # keine Monats-Periode (manuell angelegt)
    try:
        jahr, monat = int(periode[:4]), int(periode[5:7])
    except ValueError:
        return Decimal("0")
    anteil = monatsanteil(jahr, monat, fenster)
    if anteil <= 0:
        return Decimal("0")
    return (termin.betrag * anteil).quantize(CENT)


def vorschau(
    db: Session,
    mandant: models.Mandant,
    start: date,
    ende: date,
    von: date | None = None,
    bis: date | None = None,
) -> dict:
    """Entlastungsvorschau für das Planungsfenster [start, ende].

    von/bis überschreiben den gespeicherten Zeitraum (für die UI-Vorschau vor
    dem Speichern); ohne Angabe gilt der Zeitraum des Mandanten.
    """
    if von is not None and bis is not None and von <= bis:
        fenster = (von, bis)
    else:
        fenster = None
        if mandant.insolvenzgeld_von and mandant.insolvenzgeld_bis:
            fenster = (mandant.insolvenzgeld_von, mandant.insolvenzgeld_bis)
    leer = {
        "aktiv": mandant.insolvenzgeld_aktiv,
        "von": None,
        "bis": None,
        "positionen": [],
        "summen": {"personal_budget": 0.0, "personal_dauer": 0.0, "sv": 0.0,
                   "lst": 0.0, "gesamt": 0.0},
    }
    if fenster is None:
        return leer
    f_von, f_bis = fenster
    bl = mandant.bundesland
    positionen: list[dict] = []
    summen = {"personal_budget": Decimal("0"), "personal_dauer": Decimal("0"),
              "sv": Decimal("0"), "lst": Decimal("0")}

    konten = {
        k.id: k
        for k in db.scalars(
            select(models.Konto).where(models.Konto.mandant_id == mandant.id)
        )
    }
    # Personal- und SV-Konten: Nettolöhne über Igeld/Vorfinanzierung,
    # SV-Beiträge nach § 175 SGB III von der BA getragen
    personal_ids = {
        k.id
        for k in konten.values()
        if k.typ in (models.KontoTyp.PERSONAL.value, models.KontoTyp.SV.value)
    }

    # 1) SV-/LSt-Termine im Fenster
    termine = db.scalars(
        select(models.Zahlungstermin).where(
            models.Zahlungstermin.mandant_id == mandant.id,
            models.Zahlungstermin.datum >= start,
            models.Zahlungstermin.datum <= ende,
        )
    )
    for t in termine:
        entlastung = termin_entlastung(t, fenster)
        if entlastung <= 0:
            continue
        schluessel = "sv" if t.typ == models.TerminTyp.SV.value else "lst"
        summen[schluessel] += entlastung
        positionen.append(
            {
                "art": t.typ,
                "text": t.kommentar or t.typ,
                "datum": t.datum.isoformat(),
                "betrag": float(t.betrag),
                "entlastung": float(entlastung),
            }
        )

    # 2) Dauerbuchungen auf Personalkonten (Zahltag im Fenster und im Zeitraum)
    from .liquiditaet import _dauer_termine  # lokale Einbindung vermeidet Zyklen

    dauer = db.scalars(
        select(models.Dauerbuchung).where(
            models.Dauerbuchung.mandant_id == mandant.id,
            models.Dauerbuchung.aktiv.is_(True),
        )
    )
    for d_ in dauer:
        if d_.konto_id not in personal_ids or d_.art != models.PostenArt.KREDITOR.value:
            continue
        for roh in _dauer_termine(d_, start - timedelta(days=31), ende):
            zahltag = naechster_bankarbeitstag(roh, bl)
            if zahltag < start or zahltag > ende:
                continue
            if f_von <= zahltag <= f_bis:
                summen["personal_dauer"] += d_.betrag_brutto
                positionen.append(
                    {
                        "art": "PERSONAL_DAUER",
                        "text": d_.name,
                        "datum": zahltag.isoformat(),
                        "betrag": float(d_.betrag_brutto),
                        "entlastung": float(d_.betrag_brutto),
                    }
                )

    # 3) Budgets auf Personalkonten: Tagesanteile gemäß Verteilungsprofil des
    #    Kontos (z. B. Lohnlauf am Monatsende) im Fenster und Zeitraum
    from .liquiditaet import VERTEILUNGEN, _monats_verteilung

    budgets = db.scalars(
        select(models.Budget).where(models.Budget.mandant_id == mandant.id)
    )
    for b in budgets:
        konto = konten.get(b.konto_id)
        if konto is None or b.konto_id not in personal_ids or not konto.aktiv:
            continue
        profil = konto.verteilung if konto.verteilung in VERTEILUNGEN else "GLEICH"
        verteilung = _monats_verteilung(b.jahr, b.monat, profil, bl)
        anteil = sum(
            (a for d, a in verteilung.items()
             if start <= d <= ende and f_von <= d <= f_bis),
            Decimal("0"),
        )
        if anteil <= 0:
            continue
        brutto = b.betrag_netto  # Personalkonten ohne USt
        if konto.ust_satz is not None:
            brutto = brutto * (Decimal("100") + konto.ust_satz) / Decimal("100")
        entlastung = (abs(brutto) * anteil).quantize(CENT)
        summen["personal_budget"] += entlastung
        positionen.append(
            {
                "art": "PERSONAL_BUDGET",
                "text": f"{konto.nummer} {konto.bezeichnung} – Budget {b.monat:02d}/{b.jahr}",
                "datum": f"{b.jahr}-{b.monat:02d}-01",
                "betrag": float(abs(brutto)),
                "entlastung": float(entlastung),
            }
        )

    gesamt = sum(summen.values(), Decimal("0"))
    positionen.sort(key=lambda p: p["datum"])
    return {
        "aktiv": mandant.insolvenzgeld_aktiv,
        "von": f_von.isoformat(),
        "bis": f_bis.isoformat(),
        "positionen": positionen,
        "summen": {
            "personal_budget": float(summen["personal_budget"]),
            "personal_dauer": float(summen["personal_dauer"]),
            "sv": float(summen["sv"]),
            "lst": float(summen["lst"]),
            "gesamt": float(gesamt),
        },
    }
