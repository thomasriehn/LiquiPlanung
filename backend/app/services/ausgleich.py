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


_TRANSLITERATION = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower().translate(_TRANSLITERATION))


def _tokens(text: str | None) -> set[str]:
    roh = re.split(r"[^a-z0-9]+", (text or "").lower().translate(_TRANSLITERATION))
    return {t for t in roh if len(t) >= 3 and t not in _RECHTSFORMEN}


def restbetrag(posten: models.OffenerPosten) -> Decimal:
    return posten.betrag_brutto - (posten.bezahlt_betrag or Decimal("0"))


def bewerte(
    transaktion: models.BankTransaktion, posten: models.OffenerPosten
) -> tuple[int, list[str], str] | None:
    """Score eines Kandidatenpaars; None = kein sinnvoller Kandidat.

    Liefert (score, gründe, art) mit art VOLL (gleicht den Restbetrag aus, ggf.
    mit Skonto) oder TEIL (Teilzahlung – nur bei starken Signalen wie Belegnummer
    oder eindeutigem Partner). Überzahlungen werden nicht vorgeschlagen.
    """
    eingang = transaktion.betrag > 0
    if eingang != (posten.art == PostenArt.DEBITOR.value):
        return None
    rest = restbetrag(posten)
    if rest <= 0:
        return None
    zahlung = abs(transaktion.betrag)
    if zahlung > rest + Decimal("0.01"):
        return None  # Überzahlung/Sammler -> kein automatischer Vorschlag

    gruende: list[str] = []
    if abs(zahlung - rest) <= Decimal("0.01"):
        score = 50
        art = "VOLL"
        gruende.append("Betrag exakt")
    elif zahlung >= rest * Decimal("0.97"):
        score = 35
        art = "VOLL"
        gruende.append(f"Betrag ähnlich (Differenz {rest - zahlung:.2f} € – Skonto?)")
    else:
        score = 20
        art = "TEIL"
        gruende.append(f"Teilzahlung ({zahlung:.2f} von {rest:.2f} € offen)")

    zweck_norm = _norm((transaktion.verwendungszweck or "") + (transaktion.referenz or ""))
    beleg_norm = _norm(posten.belegnr)
    beleg_treffer = len(beleg_norm) >= 3 and beleg_norm in zweck_norm
    if beleg_treffer:
        score += 40
        gruende.append("Belegnummer im Verwendungszweck")

    posten_tokens = _tokens(posten.partner)
    umsatz_tokens = _tokens((transaktion.partner or "") + " " + (transaktion.verwendungszweck or ""))
    partner_stark = False
    if posten_tokens:
        gemeinsam = posten_tokens & umsatz_tokens
        if len(gemeinsam) / len(posten_tokens) >= 0.5:
            score += 25
            partner_stark = True
            gruende.append("Partner passt")
        elif gemeinsam:
            score += 15
            gruende.append("Partner passt teilweise")

    # Teilzahlungen nur mit starkem Signal (Betrag allein ist kein Indiz)
    if art == "TEIL" and not (beleg_treffer or partner_stark):
        return None

    zahltag = posten.zahlung_geplant_am or posten.faellig_am
    if abs((transaktion.buchungstag - zahltag).days) <= 45:
        score += 10
    if posten.rechnungsdatum and transaktion.buchungstag < posten.rechnungsdatum:
        score -= 30
        gruende.append("Umsatz liegt vor dem Rechnungsdatum")
    return score, gruende, art


def _sammel_vorschlag(
    transaktion: models.BankTransaktion, offene: list[models.OffenerPosten]
) -> dict | None:
    """Sammelüberweisung: Teilmenge offener Posten, deren Restbeträge exakt die
    Zahlung ergeben. Kandidaten brauchen ein Signal (Belegnummer im
    Verwendungszweck oder eindeutiger Partner); bei mehreren Lösungen gewinnen
    mehr Belegtreffer, dann weniger Posten."""
    eingang = transaktion.betrag > 0
    zahlung = abs(transaktion.betrag)
    zweck_norm = _norm((transaktion.verwendungszweck or "") + (transaktion.referenz or ""))
    umsatz_tokens = _tokens((transaktion.partner or "") + " " + (transaktion.verwendungszweck or ""))

    pool: list[tuple[models.OffenerPosten, Decimal, bool]] = []
    for p in offene:
        if eingang != (p.art == PostenArt.DEBITOR.value):
            continue
        rest = restbetrag(p)
        if rest <= 0 or rest > zahlung + Decimal("0.01"):
            continue
        beleg_norm = _norm(p.belegnr)
        beleg = len(beleg_norm) >= 3 and beleg_norm in zweck_norm
        posten_tokens = _tokens(p.partner)
        partner_stark = bool(posten_tokens) and (
            len(posten_tokens & umsatz_tokens) / len(posten_tokens) >= 0.5
        )
        if beleg or partner_stark:
            pool.append((p, rest, beleg))
    if len(pool) < 2:
        return None
    pool = pool[:15]  # Suchraum begrenzen

    beste: tuple[int, int, list[tuple[models.OffenerPosten, Decimal, bool]]] | None = None

    def suche(idx: int, aktuell: list, summe: Decimal) -> None:
        nonlocal beste
        if len(aktuell) >= 2 and abs(summe - zahlung) <= Decimal("0.01"):
            belege = sum(1 for _, _, b in aktuell if b)
            kandidat = (belege, -len(aktuell), list(aktuell))
            if beste is None or kandidat[:2] > beste[:2]:
                beste = kandidat
            return
        if idx >= len(pool) or len(aktuell) >= 6 or summe > zahlung + Decimal("0.01"):
            return
        suche(idx + 1, aktuell + [pool[idx]], summe + pool[idx][1])
        suche(idx + 1, aktuell, summe)

    suche(0, [], Decimal("0"))
    if beste is None:
        return None
    belege, _, teilmenge = beste
    alle_belege = belege == len(teilmenge)
    score = 90 if alle_belege else 75
    gruende = [f"Summe aus {len(teilmenge)} Posten exakt"]
    if belege:
        gruende.append(f"{belege} Belegnummer(n) im Verwendungszweck")
    if any(not b for _, _, b in teilmenge):
        gruende.append("Partner passt")
    return {
        "score": score,
        "konfidenz": "SICHER" if alle_belege else "MOEGLICH",
        "art": "SAMMEL",
        "gruende": gruende,
        "transaktion": {
            "id": transaktion.id,
            "buchungstag": transaktion.buchungstag.isoformat(),
            "betrag": float(transaktion.betrag),
            "partner": transaktion.partner,
            "verwendungszweck": transaktion.verwendungszweck,
        },
        "posten_liste": [
            {
                "id": p.id,
                "art": p.art,
                "partner": p.partner,
                "belegnr": p.belegnr,
                "restbetrag": float(rest),
                "faellig_am": p.faellig_am.isoformat(),
            }
            for p, rest, _ in teilmenge
        ],
    }


def vorschlaege(db: Session, mandant: models.Mandant) -> list[dict]:
    """Eindeutige Zuordnungsvorschläge (je Umsatz und Posten höchstens einer).

    Zuerst 1:1 (voll/Skonto/Teilzahlung), danach Sammelüberweisungen (1:n) für
    Umsätze und Posten, die noch keinem 1:1-Vorschlag zugeteilt sind."""
    offene = list(
        db.scalars(
            select(models.OffenerPosten).where(
                models.OffenerPosten.mandant_id == mandant.id,
                models.OffenerPosten.status == PostenStatus.OFFEN.value,
            )
        )
    )
    zugeordnete = set(
        db.scalars(select(models.AusgleichZuordnung.transaktion_id).where(
            models.AusgleichZuordnung.mandant_id == mandant.id))
    )
    umsaetze = [
        t
        for t in db.scalars(
            select(models.BankTransaktion).where(
                models.BankTransaktion.mandant_id == mandant.id,
            )
        )
        if t.id not in zugeordnete
    ]
    kandidaten: list[
        tuple[int, list[str], str, models.BankTransaktion, models.OffenerPosten]
    ] = []
    for t in umsaetze:
        for p in offene:
            ergebnis = bewerte(t, p)
            if ergebnis is None or ergebnis[0] < SCHWELLE_MOEGLICH:
                continue
            kandidaten.append((ergebnis[0], ergebnis[1], ergebnis[2], t, p))

    # eindeutig zuordnen: bester Score zuerst, jedes Element nur einmal je Runde
    # (nach Übernahme einer Teilzahlung liefert die nächste Runde neue Vorschläge
    # auf Basis des reduzierten Restbetrags)
    kandidaten.sort(key=lambda k: -k[0])
    belegt_t: set[int] = set()
    belegt_p: set[int] = set()
    ergebnis: list[dict] = []
    for score, gruende, art, t, p in kandidaten:
        if t.id in belegt_t or p.id in belegt_p:
            continue
        belegt_t.add(t.id)
        belegt_p.add(p.id)
        rest = restbetrag(p)
        ergebnis.append(
            {
                "score": score,
                # Teilzahlungen nie automatisch vorauswählen
                "konfidenz": "SICHER" if score >= SCHWELLE_SICHER and art == "VOLL" else "MOEGLICH",
                "art": art,
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
                    "restbetrag": float(rest),
                    "rest_nach_zahlung": float(rest - abs(t.betrag)),
                    "faellig_am": p.faellig_am.isoformat(),
                },
            }
        )

    # Sammelüberweisungen für verbleibende Umsätze/Posten
    for t in umsaetze:
        if t.id in belegt_t:
            continue
        verfuegbar = [p for p in offene if p.id not in belegt_p]
        sammel = _sammel_vorschlag(t, verfuegbar)
        if sammel is None:
            continue
        belegt_t.add(t.id)
        for eintrag in sammel["posten_liste"]:
            belegt_p.add(eintrag["id"])
        ergebnis.append(sammel)

    ergebnis.sort(key=lambda v: -v["score"])
    return ergebnis


def gleiche_aus(
    db: Session,
    mandant: models.Mandant,
    paare: list[tuple[int, list[int]]],
) -> int:
    """Bestätigte Zuordnungen übernehmen (je Paar: ein Umsatz, ein oder mehrere Posten).

    1:1 – deckt die Zahlung den Restbetrag (Skonto-Toleranz 3 %), wird der Posten
    BEZAHLT; sonst bleibt er als Teilzahlung OFFEN und nur `bezahlt_betrag` steigt.
    Sammelüberweisung (mehrere Posten) – die Restbeträge müssen die Zahlung exakt
    ergeben; jeder Posten wird voll ausgeglichen. Überzahlungen werden abgelehnt.
    """
    anzahl = 0
    for transaktion_id, posten_ids in paare:
        t = db.get(models.BankTransaktion, transaktion_id)
        if t is None or t.mandant_id != mandant.id:
            raise ValueError("Umsatz nicht gefunden")
        if t.zuordnungen:
            raise ValueError(f"Bankumsatz {transaktion_id} ist bereits abgeglichen")
        if not posten_ids:
            raise ValueError("Keine Posten angegeben")
        posten: list[models.OffenerPosten] = []
        for pid in posten_ids:
            p = db.get(models.OffenerPosten, pid)
            if p is None or p.mandant_id != mandant.id:
                raise ValueError(f"Posten {pid} nicht gefunden")
            if p.status != PostenStatus.OFFEN.value:
                raise ValueError(f"Posten {pid} ist nicht offen")
            posten.append(p)
        zahlung = abs(t.betrag)

        if len(posten) == 1:
            p = posten[0]
            rest = restbetrag(p)
            if zahlung > rest + Decimal("0.01"):
                raise ValueError(
                    f"Zahlung ({zahlung:.2f} €) übersteigt den Restbetrag "
                    f"({rest:.2f} €) von Posten {p.id}"
                )
            db.add(models.AusgleichZuordnung(
                mandant_id=mandant.id, transaktion=t, posten=p, betrag=zahlung
            ))
            p.bezahlt_betrag = (p.bezahlt_betrag or Decimal("0")) + zahlung
            if zahlung >= rest * Decimal("0.97"):
                p.status = PostenStatus.BEZAHLT.value
                p.bezahlt_am = t.buchungstag
        else:
            # Sammelüberweisung: Summe der Restbeträge muss exakt passen
            summe = sum((restbetrag(p) for p in posten), Decimal("0"))
            if abs(summe - zahlung) > Decimal("0.01"):
                raise ValueError(
                    f"Restbeträge ({summe:.2f} €) ergeben nicht die Zahlung "
                    f"({zahlung:.2f} €) – Sammelüberweisung nicht übernommen"
                )
            for p in posten:
                rest = restbetrag(p)
                db.add(models.AusgleichZuordnung(
                    mandant_id=mandant.id, transaktion=t, posten=p, betrag=rest
                ))
                p.bezahlt_betrag = (p.bezahlt_betrag or Decimal("0")) + rest
                p.status = PostenStatus.BEZAHLT.value
                p.bezahlt_am = t.buchungstag
        anzahl += 1
    return anzahl


def hebe_auf(db: Session, mandant: models.Mandant, transaktion_id: int) -> None:
    """Ausgleich rückgängig machen: alle Zuordnungen des Umsatzes lösen und die
    Zahlungsanteile von den Posten abziehen."""
    t = db.get(models.BankTransaktion, transaktion_id)
    if t is None or t.mandant_id != mandant.id:
        raise ValueError("Bankumsatz nicht gefunden")
    if not t.zuordnungen:
        raise ValueError("Bankumsatz ist nicht abgeglichen")
    for zuordnung in list(t.zuordnungen):
        p = zuordnung.posten
        if p is not None:
            p.bezahlt_betrag = max(
                Decimal("0"), (p.bezahlt_betrag or Decimal("0")) - zuordnung.betrag
            )
            p.status = PostenStatus.OFFEN.value
            p.bezahlt_am = None
        db.delete(zuordnung)
    t.zuordnungen.clear()
