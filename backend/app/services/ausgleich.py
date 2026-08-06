"""Automatischer OP-Ausgleich: Bankumsätze ↔ offene Posten.

Ein Scoring-Verfahren bewertet Kandidatenpaare (Richtung, Betrag, Belegnummer im
Verwendungszweck, Partnername, Datumsnähe) und schlägt eindeutige Zuordnungen
vor. Die Übernahme erfolgt immer durch den Benutzer (sichere Treffer sind in
der Oberfläche vorausgewählt); ein bestätigter Ausgleich setzt den Posten auf
BEZAHLT und verknüpft den Bankumsatz. Teilzahlungen werden nicht vorgeschlagen.
"""

import re
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..models import PostenArt, PostenStatus

# ab diesem Score gilt ein Vorschlag als sicher / als möglich
SCHWELLE_SICHER = 85
SCHWELLE_MOEGLICH = 55

_RECHTSFORMEN = {
    "gmbh", "mbh", "ag", "kg", "ohg", "gbr", "ug", "se", "co", "cokg", "e", "k",
    "inh", "und", "der", "die", "das",
}


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _tokens(text: str | None) -> set[str]:
    roh = re.split(r"[^a-zA-Z0-9äöüÄÖÜß]+", (text or "").lower())
    return {t for t in roh if len(t) >= 3 and t not in _RECHTSFORMEN}


def bewerte(
    transaktion: models.BankTransaktion, posten: models.OffenerPosten
) -> tuple[int, list[str]] | None:
    """Score eines Kandidatenpaars; None = kein sinnvoller Kandidat."""
    eingang = transaktion.betrag > 0
    if eingang != (posten.art == PostenArt.DEBITOR.value):
        return None

    diff = abs(abs(transaktion.betrag) - posten.betrag_brutto)
    gruende: list[str] = []
    if diff <= Decimal("0.01"):
        score = 50
        gruende.append("Betrag exakt")
    elif posten.betrag_brutto and diff / posten.betrag_brutto <= Decimal("0.03"):
        score = 35
        gruende.append(f"Betrag ähnlich (Differenz {diff:.2f} € – Skonto?)")
    else:
        return None

    zweck_norm = _norm((transaktion.verwendungszweck or "") + (transaktion.referenz or ""))
    beleg_norm = _norm(posten.belegnr)
    if len(beleg_norm) >= 3 and beleg_norm in zweck_norm:
        score += 40
        gruende.append("Belegnummer im Verwendungszweck")

    posten_tokens = _tokens(posten.partner)
    umsatz_tokens = _tokens((transaktion.partner or "") + " " + (transaktion.verwendungszweck or ""))
    if posten_tokens:
        gemeinsam = posten_tokens & umsatz_tokens
        if len(gemeinsam) / len(posten_tokens) >= 0.5:
            score += 25
            gruende.append("Partner passt")
        elif gemeinsam:
            score += 15
            gruende.append("Partner passt teilweise")

    zahltag = posten.zahlung_geplant_am or posten.faellig_am
    if abs((transaktion.buchungstag - zahltag).days) <= 45:
        score += 10
    if posten.rechnungsdatum and transaktion.buchungstag < posten.rechnungsdatum:
        score -= 30
        gruende.append("Umsatz liegt vor dem Rechnungsdatum")
    return score, gruende


def vorschlaege(db: Session, mandant: models.Mandant) -> list[dict]:
    """Eindeutige Zuordnungsvorschläge (je Umsatz und Posten höchstens einer)."""
    offene = list(
        db.scalars(
            select(models.OffenerPosten).where(
                models.OffenerPosten.mandant_id == mandant.id,
                models.OffenerPosten.status == PostenStatus.OFFEN.value,
            )
        )
    )
    umsaetze = list(
        db.scalars(
            select(models.BankTransaktion).where(
                models.BankTransaktion.mandant_id == mandant.id,
                models.BankTransaktion.posten_id.is_(None),
            )
        )
    )
    kandidaten: list[tuple[int, list[str], models.BankTransaktion, models.OffenerPosten]] = []
    for t in umsaetze:
        for p in offene:
            ergebnis = bewerte(t, p)
            if ergebnis is None or ergebnis[0] < SCHWELLE_MOEGLICH:
                continue
            kandidaten.append((ergebnis[0], ergebnis[1], t, p))

    # eindeutig zuordnen: bester Score zuerst, jedes Element nur einmal
    kandidaten.sort(key=lambda k: -k[0])
    belegt_t: set[int] = set()
    belegt_p: set[int] = set()
    ergebnis: list[dict] = []
    for score, gruende, t, p in kandidaten:
        if t.id in belegt_t or p.id in belegt_p:
            continue
        belegt_t.add(t.id)
        belegt_p.add(p.id)
        ergebnis.append(
            {
                "score": score,
                "konfidenz": "SICHER" if score >= SCHWELLE_SICHER else "MOEGLICH",
                "gruende": gruende,
                "transaktion": {
                    "id": t.id,
                    "buchungstag": t.buchungstag.isoformat(),
                    "betrag": float(t.betrag),
                    "partner": t.partner,
                    "verwendungszweck": t.verwendungszweck,
                },
                "posten": {
                    "id": p.id,
                    "art": p.art,
                    "partner": p.partner,
                    "belegnr": p.belegnr,
                    "betrag_brutto": float(p.betrag_brutto),
                    "faellig_am": p.faellig_am.isoformat(),
                },
            }
        )
    return ergebnis


def gleiche_aus(
    db: Session,
    mandant: models.Mandant,
    paare: list[tuple[int, int]],
) -> int:
    """Bestätigte Zuordnungen übernehmen: Posten -> BEZAHLT, Umsatz verknüpfen."""
    anzahl = 0
    for transaktion_id, posten_id in paare:
        t = db.get(models.BankTransaktion, transaktion_id)
        p = db.get(models.OffenerPosten, posten_id)
        if (
            t is None or p is None
            or t.mandant_id != mandant.id or p.mandant_id != mandant.id
        ):
            raise ValueError("Umsatz oder Posten nicht gefunden")
        if t.posten_id is not None:
            raise ValueError(f"Bankumsatz {transaktion_id} ist bereits abgeglichen")
        if p.status != PostenStatus.OFFEN.value:
            raise ValueError(f"Posten {posten_id} ist nicht offen")
        t.posten_id = p.id
        p.status = PostenStatus.BEZAHLT.value
        p.bezahlt_am = t.buchungstag
        anzahl += 1
    return anzahl


def hebe_auf(db: Session, mandant: models.Mandant, transaktion_id: int) -> None:
    """Ausgleich rückgängig machen: Posten wieder öffnen, Verknüpfung lösen."""
    t = db.get(models.BankTransaktion, transaktion_id)
    if t is None or t.mandant_id != mandant.id:
        raise ValueError("Bankumsatz nicht gefunden")
    if t.posten_id is None:
        raise ValueError("Bankumsatz ist nicht abgeglichen")
    p = db.get(models.OffenerPosten, t.posten_id)
    if p is not None:
        p.status = PostenStatus.OFFEN.value
        p.bezahlt_am = None
    t.posten_id = None
