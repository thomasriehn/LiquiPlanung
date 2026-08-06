"""Zahlungskalender: SV-Beiträge und Steuertermine.

Regeln:
- SV-Beiträge: fällig am drittletzten Bankarbeitstag des Monats.
- USt-Voranmeldung: 10. des Folgemonats (Monatszahler) bzw. 10. nach Quartalsende;
  mit Dauerfristverlängerung jeweils einen Monat später. Fällt der Termin auf
  Sa/So/Feiertag, verschiebt er sich auf den nächsten Werktag (§ 108 Abs. 3 AO).
- Lohnsteuer: 10. des Folgemonats (Verschiebung wie oben).
- Gewerbesteuer-Vorauszahlung: 15.02. / 15.05. / 15.08. / 15.11.
- Körperschaftsteuer-Vorauszahlung: 10.03. / 10.06. / 10.09. / 10.12.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..models import TerminStatus, TerminTyp, UStZeitraum
from .feiertage import bankarbeitstage_im_monat, naechster_werktag


@dataclass(frozen=True)
class TerminVorschlag:
    typ: str
    datum: date
    beschreibung: str
    periode: str  # z. B. "2026-05" oder "2026-Q3"


def sv_faelligkeit(jahr: int, monat: int, bundesland: str = "NW") -> date:
    tage = bankarbeitstage_im_monat(jahr, monat, bundesland)
    return tage[-3]


def _monat_plus(jahr: int, monat: int, plus: int) -> tuple[int, int]:
    idx = (jahr * 12 + (monat - 1)) + plus
    return idx // 12, idx % 12 + 1


def steuertermine(
    von: date,
    bis: date,
    bundesland: str,
    ust_zeitraum: str = UStZeitraum.MONAT.value,
    dauerfrist: bool = False,
) -> list[TerminVorschlag]:
    """Alle Steuer-/SV-Termine, deren (verschobenes) Datum in [von, bis] liegt."""
    ergebnisse: list[TerminVorschlag] = []

    # großzügiger Monatsbereich, Verschiebungen werden danach gefiltert
    start_j, start_m = _monat_plus(von.year, von.month, -3)
    ende_j, ende_m = _monat_plus(bis.year, bis.month, 1)

    j, m = start_j, start_m
    while (j, m) <= (ende_j, ende_m):
        monat_periode = f"{j}-{m:02d}"
        # SV: drittletzter Bankarbeitstag des Monats
        ergebnisse.append(
            TerminVorschlag(TerminTyp.SV.value, sv_faelligkeit(j, m, bundesland),
                            f"SV-Beiträge {m:02d}/{j}", monat_periode)
        )
        # Lohnsteuer: 10. des Folgemonats für Monat m
        fj, fm = _monat_plus(j, m, 1)
        lst = naechster_werktag(date(fj, fm, 10), bundesland)
        ergebnisse.append(
            TerminVorschlag(TerminTyp.LST.value, lst, f"Lohnsteuer {m:02d}/{j}", monat_periode)
        )
        # USt-VA
        if ust_zeitraum == UStZeitraum.MONAT.value:
            plus = 2 if dauerfrist else 1
            uj, um = _monat_plus(j, m, plus)
            ust = naechster_werktag(date(uj, um, 10), bundesland)
            ergebnisse.append(
                TerminVorschlag(TerminTyp.UST_VA.value, ust, f"USt-VA {m:02d}/{j}", monat_periode)
            )
        else:
            if m in (3, 6, 9, 12):  # Quartalsende
                plus = 2 if dauerfrist else 1
                uj, um = _monat_plus(j, m, plus)
                ust = naechster_werktag(date(uj, um, 10), bundesland)
                q = m // 3
                ergebnisse.append(
                    TerminVorschlag(TerminTyp.UST_VA.value, ust, f"USt-VA Q{q}/{j}", f"{j}-Q{q}")
                )
        # GewSt-VZ
        if m in (2, 5, 8, 11):
            gewst = naechster_werktag(date(j, m, 15), bundesland)
            ergebnisse.append(
                TerminVorschlag(TerminTyp.GEWST.value, gewst, f"GewSt-VZ {m:02d}/{j}", monat_periode)
            )
        # KSt-VZ
        if m in (3, 6, 9, 12):
            kst = naechster_werktag(date(j, m, 10), bundesland)
            ergebnisse.append(
                TerminVorschlag(TerminTyp.KST.value, kst, f"KSt-VZ {m:02d}/{j}", monat_periode)
            )
        j, m = _monat_plus(j, m, 1)

    return sorted(
        (t for t in ergebnisse if von <= t.datum <= bis),
        key=lambda t: (t.datum, t.typ),
    )


def _periode_monate(periode: str) -> list[tuple[int, int]]:
    """'2026-05' -> [(2026, 5)]; '2026-Q3' -> [(2026, 7), (2026, 8), (2026, 9)]."""
    try:
        jahr_teil, rest = periode.split("-")
        jahr = int(jahr_teil)
        if rest.startswith("Q"):
            q = int(rest[1:])
            return [(jahr, (q - 1) * 3 + i) for i in (1, 2, 3)]
        return [(jahr, int(rest))]
    except (ValueError, AttributeError):
        return []


def ust_zahllast_aus_budget(
    db: Session, mandant: models.Mandant, monate: list[tuple[int, int]]
) -> Decimal | None:
    """Erwartete USt-Zahllast des Zeitraums aus der Budgetplanung.

    Umsatzsteuer auf Erlösbudgets minus Vorsteuer auf Aufwands-/Investitions-
    budgets (jeweils Netto-Budget × USt-Satz des Kontos). Negativ = erwartete
    Erstattung. None, wenn keine umsatzsteuerrelevanten Budgets vorliegen.
    """
    from .liquiditaet import _konto_richtung

    if not monate:
        return None
    konten = {
        k.id: k
        for k in db.scalars(
            select(models.Konto).where(
                models.Konto.mandant_id == mandant.id, models.Konto.aktiv.is_(True)
            )
        )
    }
    zahllast = Decimal("0")
    gefunden = False
    for b in db.scalars(
        select(models.Budget).where(models.Budget.mandant_id == mandant.id)
    ):
        if (b.jahr, b.monat) not in monate:
            continue
        konto = konten.get(b.konto_id)
        if konto is None or konto.ust_satz is None or konto.ust_satz <= 0:
            continue
        richtung = _konto_richtung(konto)
        steuer = abs(b.betrag_netto) * konto.ust_satz / Decimal("100")
        if richtung == models.Richtung.EIN.value:
            zahllast += steuer
            gefunden = True
        elif richtung == models.Richtung.AUS.value:
            zahllast -= steuer
            gefunden = True
    return zahllast.quantize(Decimal("0.01")) if gefunden else None


def _historien_schaetzung(
    db: Session, mandant: models.Mandant, konto: models.Konto | None, heute: date
) -> Decimal:
    """Ø der monatlichen Ist-Auszahlungen der letzten 3 Monate auf dem Konto."""
    if konto is None:
        return Decimal("0")
    von = heute - timedelta(days=92)
    finanz_nrn = {
        k.nummer
        for k in db.scalars(
            select(models.Konto).where(
                models.Konto.mandant_id == mandant.id,
                models.Konto.typ.in_(["BANK", "KASSE"]),
            )
        )
    }
    buchungen = db.scalars(
        select(models.Buchung).where(
            models.Buchung.mandant_id == mandant.id,
            models.Buchung.datum >= von,
            models.Buchung.datum < heute,
        )
    )
    summe = Decimal("0")
    monate: set[tuple[int, int]] = set()
    for b in buchungen:
        if b.konto_nr == konto.nummer and b.gegenkonto_nr in finanz_nrn:
            fluss = -b.betrag if b.sh == "S" else b.betrag
        elif b.gegenkonto_nr == konto.nummer and b.konto_nr in finanz_nrn:
            fluss = b.betrag if b.sh == "S" else -b.betrag
        else:
            continue
        if fluss < 0:  # nur Auszahlungen
            summe += -fluss
            monate.add((b.datum.year, b.datum.month))
    if not monate:
        return Decimal("0")
    return (summe / len(monate)).quantize(Decimal("0.01"))


def generiere_termine(
    db: Session, mandant: models.Mandant, von: date, bis: date, heute: date | None = None
) -> dict:
    """Erzeugt Zahlungstermine gemäß Regeln. Bestehende Termine gleicher Art und
    gleichen Datums (auch manuell angepasste) werden nicht überschrieben."""
    heute = heute or date.today()
    regeln = {
        r.typ: r
        for r in db.scalars(
            select(models.TerminRegel).where(models.TerminRegel.mandant_id == mandant.id)
        )
    }
    # Dedup über fachliche Periode (überlebt manuelle Terminverschiebungen);
    # (typ, datum) als Rückfallebene für manuell angelegte Termine ohne Periode
    alle_termine = list(
        db.scalars(
            select(models.Zahlungstermin).where(
                models.Zahlungstermin.mandant_id == mandant.id
            )
        )
    )
    vorhandene_perioden = {(t.typ, t.periode) for t in alle_termine if t.periode}
    vorhandene_daten = {(t.typ, t.datum) for t in alle_termine}
    angelegt, uebersprungen = 0, 0
    for v in steuertermine(von, bis, mandant.bundesland, mandant.ust_zeitraum, mandant.dauerfrist):
        regel = regeln.get(v.typ)
        if regel is None or not regel.aktiv:
            continue
        if (v.typ, v.periode) in vorhandene_perioden or (v.typ, v.datum) in vorhandene_daten:
            uebersprungen += 1
            continue
        if regel.betrag_modus == "BUDGET" and v.typ == TerminTyp.UST_VA.value:
            zahllast = ust_zahllast_aus_budget(db, mandant, _periode_monate(v.periode))
            betrag = zahllast if zahllast is not None else (regel.betrag_fix or Decimal("0"))
        elif regel.betrag_modus == "HISTORIE":
            betrag = _historien_schaetzung(db, mandant, regel.konto, heute)
            if betrag == 0:
                betrag = regel.betrag_fix or Decimal("0")
        else:
            betrag = regel.betrag_fix or Decimal("0")
        db.add(
            models.Zahlungstermin(
                mandant_id=mandant.id,
                typ=v.typ,
                periode=v.periode,
                datum=v.datum,
                betrag=betrag,
                status=TerminStatus.GEPLANT.value,
                konto_id=regel.konto_id,
                kommentar=v.beschreibung,
                generiert=True,
            )
        )
        angelegt += 1
    db.commit()
    return {"angelegt": angelegt, "uebersprungen": uebersprungen}
