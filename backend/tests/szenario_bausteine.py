"""Szenario-Bausteine für den eigenschaftsbasierten Prüfer (Orakel) der manuellen
Plan-Verschiebungen und ihrer Wirkung auf Plan, Liquidität, Planstand und
Freigabe-Kennzahlen. Nur synthetische Daten.

Bausteine:
- `Szenario` (unveränderliche Beschreibung: heute, Konten, Posten mit Zahlungen,
  Dauerbuchungen mit bezahlten Raten, Bestellungen, Wochenbudgets,
  PLAN-Verschiebungen, Planstände, Prüffenster),
- `erzeuge(seed)` – zufällig, aber seed-deterministisch,
- `aufbauen(...)` – In-Memory-Datenbank (Kopie einer Vorlage mit Kontenrahmen),
- `Auswertung` – Plan/Kennzahlen je Datenbankvariante (gepuffert),
- Invarianten `INVARIANTEN` (I1 … I6 und Ergänzungen) mit Vorbedingung, Prüfpunkten
  und Einzelprüfung,
- `verkleinere(...)` – lässt Bestandteile weg, solange der Prüfpunkt fehlschlägt,
- `beschreibe(...)`, `als_code(...)`, `vermutete_ursache(...)` – lesbare Ausgabe.

Von `tests/test_verschiebungen_eigenschaften.py` und vom Orakel
(scratchpad/r3/orakel.py) gemeinsam genutzt. Verschiebungen und Bestellungen tragen einen
Anlagetag (die Zuordnung wertet den Zahlstand beim Anlegen aus). Ändert sich der Generator, ändern sich
die Szenarien der Seeds (Liste bekannter Fehlschläge in der Testdatei anpassen)."""

from __future__ import annotations

import contextlib
import random
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base
from app.services import bestellungen as svc
from app.services import liquiditaet as liq
from app.services.feiertage import feiertage, naechster_bankarbeitstag

from test_bestellungen import _benutzer, _zahlung
from test_liquiditaet import _konto, _mandant

NULL = Decimal("0")
CENT = Decimal("0.01")
TOLERANZ = 0.005
KONTEN = ("4920", "4930", "3400", "4210")      # 4920/4930: gleiche Gruppe (A_SONST)
UST = {"4920": Decimal("19"), "4930": Decimal("19"), "3400": Decimal("19"), "4210": None}
BUNDESLAENDER = ("NW", "NW", "BY", "SN")
BETRAEGE = ("500", "750.50", "1000", "1200", "1500", "2000", "2500", "3000", "4999.99")
BANK_ANFANG = Decimal("100000")
# Posten und Termine der Szenarien sind Altbestand (vor allen Verschiebungen angelegt):
# ihr Anlagezeitpunkt wäre sonst die Uhrzeit des Laufs – verglichen mit den angenommenen
# Anlagezeitpunkten der Verschiebungen hinge das Ergebnis vom Kalendertag des Laufs ab
ALTBESTAND = datetime(2020, 1, 1, tzinfo=timezone.utc)


def altbestand(modell, angelegt: datetime | None = None) -> dict:
    """Konstruktor-Argument erstellt_am (ohne Angabe ALTBESTAND), sofern das Modell die
    Spalte hat (Differenzläufe gegen einen älteren Stand ohne Spalte)."""
    return {"erstellt_am": angelegt or ALTBESTAND} if hasattr(modell, "erstellt_am") else {}


def nach_allen_verschiebungen(heute: date) -> datetime:
    """Anlagezeitpunkt eines zusätzlich erfassten Postens/Termins (Monotonie-Prüfung): am Tag
    des Datenstands nach allen Verschiebungen (die höchstens heute angelegt sind)."""
    return datetime.combine(heute, datetime.max.time().replace(microsecond=0)).astimezone()
KW_ANZAHL = 9                                    # Freigabe: laufende KW bis +8
WOCHEN = 13                                      # Standardfenster des Plans


# ---------------------------------------------------------------------------
# Szenario-Beschreibung
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Posten:
    """Kreditor-OP. Status ergibt sich aus den Zahlungen: decken sie den Betrag,
    ist er BEZAHLT (bezahlt_am = letzte Zahlung), sonst OFFEN mit Teilzahlungen."""
    nr: int
    konto: str
    betrag: Decimal
    faellig: date
    zahlungen: tuple[tuple[date, Decimal], ...] = ()
    angelegt: datetime | None = None     # Anlagezeitpunkt (None: Altbestand)

    @property
    def gezahlt(self) -> Decimal:
        return sum((b for _, b in self.zahlungen), NULL)

    @property
    def bezahlt(self) -> bool:
        return self.betrag - self.gezahlt <= CENT

    @property
    def bezahlt_am(self) -> date | None:
        return max(t for t, _ in self.zahlungen) if self.bezahlt and self.zahlungen else None

    @property
    def rest(self) -> Decimal:
        return max(NULL, self.betrag - self.gezahlt)


@dataclass(frozen=True)
class Dauer:
    """Monatliche Dauerbuchung (Kreditor). `bezahlt`: (Plantag der Rate, Zahltag) je
    gebuchter Rate (betragsgleich)."""
    nr: int
    konto: str
    betrag: Decimal
    stichtag: int
    vortragen: bool = True
    bezahlt: tuple[tuple[date, date], ...] = ()


@dataclass(frozen=True)
class Bestellung:
    """Bestellung (netto, USt laut Konto). `zahlungen`: (Tag, Brutto-Betrag,
    abschließend) – Ablösung durch Zahlung bzw. Teilzahlung."""
    nr: int
    konto: str
    netto: Decimal
    zahldatum: date
    status: str = "FREIGEGEBEN"                  # FREIGEGEBEN | OFFEN
    zahlungen: tuple[tuple[date, Decimal, bool], ...] = ()
    angelegt: date | None = None                  # Anlagetag (None: Bestelldatum = Zahldatum − 30)

    @property
    def brutto(self) -> Decimal:
        u = UST[self.konto]
        return (self.netto * (1 + u / 100)).quantize(CENT) if u is not None else self.netto

    @property
    def abgeloest(self) -> bool:
        return any(a for _, _, a in self.zahlungen) or \
            self.brutto - sum((b for _, b, _ in self.zahlungen), NULL) <= CENT

    @property
    def rest(self) -> Decimal:
        if self.abgeloest:
            return NULL
        return self.brutto - sum((b for _, b, _ in self.zahlungen), NULL)


@dataclass(frozen=True)
class Budget:
    konto: str
    montag: date                                  # ISO-Woche des Wochen-Overrides
    netto: Decimal


@dataclass(frozen=True)
class Verschiebung:
    nr: int
    konto: str
    von: date
    nach: date
    betrag: Decimal                               # Vorzeichen wie der Fluss (Auszahlung < 0)
    erstellt: date | None = None                  # Anlagetag; None: min(heute, Vortag des Ursprungs)


# Anlagetag „unbekannt“ (nach dem Ursprung, wie bei direkt angelegten Verschiebungen in
# Tests und Skripten)
UNBEKANNT = date(2099, 1, 1)


@dataclass(frozen=True)
class Szenario:
    seed: int
    heute: date
    bundesland: str = "NW"
    konten: tuple[str, ...] = ("4920",)
    posten: tuple[Posten, ...] = ()
    dauer: tuple[Dauer, ...] = ()
    bestellungen: tuple[Bestellung, ...] = ()
    budgets: tuple[Budget, ...] = ()
    verschiebungen: tuple[Verschiebung, ...] = ()
    planstaende: tuple[int, ...] = ()             # Stichtag = s0 − 7·k (k Wochen zurück)
    referenz: int | None = None                   # k des Planstands mit Soll-Referenz
    fenster: tuple[tuple[int, int], ...] = ((0, WOCHEN),)   # (Startversatz in Wochen, Wochen)

    @property
    def s0(self) -> date:
        return liq.wochen_start(self.heute)

    @property
    def horizont_ende(self) -> date:
        return self.s0 + timedelta(days=WOCHEN * 7 - 1)


# ---------------------------------------------------------------------------
# Hilfen (Plantage, explizite Beträge)
# ---------------------------------------------------------------------------

def _monatstage(j: int, m: int) -> int:
    return ((date(j + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)).day


def rate_tage(d: Dauer, von: date, bis: date, bl: str) -> list[date]:
    """Plantage (Bankarbeitstag) der Raten mit Plantag in [von, bis] – wie die Engine:
    Stichtag (auf Monatsende begrenzt), dann nächster Bankarbeitstag."""
    tage = []
    j, m = von.year, von.month
    j, m = (j - 1, 12) if m == 1 else (j, m - 1)          # Vormonat: Rate kann hineinrutschen
    while True:
        roh = date(j, m, min(d.stichtag, _monatstage(j, m)))
        if roh > bis:
            break
        t = naechster_bankarbeitstag(roh, bl)
        if von <= t <= bis:
            tage.append(t)
        j, m = (j + 1, 1) if m == 12 else (j, m + 1)
    return tage


def explizit_am_tag(sz: Szenario, konto: str, tag: date, bis_index: int | None = None) -> Decimal:
    """Explizit geplanter Betrag (positiv) einer Zeile am Tag, wie er vor dem Tag zu sehen
    war: Posten (abzüglich Zahlungen vor dem Tag), Raten, Bestellungen und frühere
    Verschiebungen dorthin (Ketten). Grundlage der Vorbedingung „Verschiebung betrifft
    nur explizit Geplantes“."""
    s = NULL
    for p in sz.posten:
        if p.konto == konto and p.faellig == tag:
            s += max(NULL, p.betrag - sum((b for t, b in p.zahlungen if t < tag), NULL))
    for d in sz.dauer:
        if d.konto == konto and tag in rate_tage(d, tag, tag, sz.bundesland):
            s += d.betrag
    for b in sz.bestellungen:
        if b.konto == konto and b.zahldatum == tag:
            s += max(NULL, b.brutto - sum((x for t, x, _ in b.zahlungen if t < tag), NULL))
    for i, v in enumerate(sz.verschiebungen):
        if bis_index is not None and i >= bis_index:
            break
        if v.konto == konto and v.nach == tag:
            s += abs(v.betrag)
    return s


def ist_ratentag(sz: Szenario, konto: str, tag: date, nur_ohne_vortrag: bool = False) -> bool:
    return any(d.konto == konto and (not nur_ohne_vortrag or not d.vortragen)
               and tag in rate_tage(d, tag, tag, sz.bundesland) for d in sz.dauer)


def fremdzahlung_auf_dauerkonto(sz: Szenario) -> bool:
    """Zahlungen von Posten/Bestellungen (oder einer anderen Dauerbuchung) auf dem Konto
    einer Dauerbuchung: der
    Vortragsabgleich rechnet sie mangels Zuordnung als Ratenzahlung an (dokumentierte
    Grenze, KONZEPT §5 „Vortrag“) – Invarianten mit Vortrag gelten dort nicht zwingend."""
    dauerkonten = {d.konto for d in sz.dauer}
    if any(p.konto in dauerkonten and p.zahlungen for p in sz.posten) or any(
            b.konto in dauerkonten and b.zahlungen for b in sz.bestellungen):
        return True
    # verschärft nach dem Lauf über 2000 Seeds: auch die Zahlung einer Rate eines anderen
    # Dauerschuldverhältnisses desselben Kontos (Rate kurz vor dem Fenster, also nicht
    # im Abgleich) gilt dem Vortrag als Vorauszahlung – derselbe dokumentierte Fall
    je_konto = defaultdict(list)
    for d in sz.dauer:
        je_konto[d.konto].append(d)
    return any(len(ds) > 1 and any(d.bezahlt for d in ds) for ds in je_konto.values())


def unbezahlte_vergangene_raten_ohne_vortrag(sz: Szenario) -> bool:
    """Eine unbezahlte Rate ohne Vortrag, deren Plantag vergangen ist – auch nach einer
    Verschiebung (korrigiert nach dem Lauf über 3200 Seeds: eine auf einen vergangenen Tag
    verschobene unbezahlte Rate ohne Vortrag gilt ebenso als erledigt, KONZEPT §5)."""
    von, bis = sz.s0 - timedelta(days=21), sz.heute - timedelta(days=1)
    for d in sz.dauer:
        if d.vortragen:
            continue
        bezahlt = {t for t, _ in d.bezahlt}
        for t in rate_tage(d, von, bis, sz.bundesland):
            if t not in bezahlt:
                return True
        # Plantage nach Verschiebung (auch Ketten) unbezahlter Raten
        offen = {t for t in rate_tage(d, sz.heute - timedelta(days=120),
                                      sz.heute + timedelta(days=120), sz.bundesland)
                 if t not in bezahlt}
        erreicht = set()
        while offen:
            t = offen.pop()
            for v in sz.verschiebungen:
                if v.konto == d.konto and v.von == t and v.nach not in erreicht:
                    erreicht.add(v.nach)
                    offen.add(v.nach)
        if any(von <= t <= bis for t in erreicht):
            return True
    return False


def vor_fenster_bezahlt(sz: Szenario, konto: str, start: date) -> bool:
    """Eine Position der Zeile wurde vor dem Fensterbeginn bezahlt (Posten BEZAHLT bzw.
    Bestellung abgelöst vor `start`)."""
    for p in sz.posten:
        if p.konto == konto and p.bezahlt and p.bezahlt_am and p.bezahlt_am < start:
            return True
    for b in sz.bestellungen:
        if b.konto == konto and b.abgeloest and b.zahlungen and \
                max(t for t, _, _ in b.zahlungen) < start:
            return True
    return False


def kw_montage(sz: Szenario) -> list[date]:
    return [sz.s0 + timedelta(days=7 * i) for i in range(KW_ANZAHL)]


def budget_konten(sz: Szenario) -> set[str]:
    return {b.konto for b in sz.budgets}


# ---------------------------------------------------------------------------
# Zufallserzeugung
# ---------------------------------------------------------------------------

def _heute_waehlen(rng: random.Random, bl: str) -> date:
    art = rng.choice(["montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag",
                      "sonntag", "feiertag", "feiertag", "nach_feiertag", "jahreswechsel"])
    if art in ("feiertag", "nach_feiertag"):
        tage = sorted(d for j in (2026, 2027) for d in feiertage(j, bl)
                      if d.weekday() < 5 and date(2026, 4, 1) <= d <= date(2027, 1, 15))
        tage += [date(2026, 12, 24), date(2026, 12, 31)]   # keine Bankarbeitstage
        d = rng.choice(tage)
        return d + timedelta(days=1) if art == "nach_feiertag" else d
    if art == "jahreswechsel":
        return date(2026, 12, 18) + timedelta(days=rng.randint(0, 24))
    montag = liq.wochen_start(date(2026, 4, 6) + timedelta(days=rng.randint(0, 270)))
    wt = ["montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag",
          "sonntag"].index(art)
    return montag + timedelta(days=wt)


def _versatz(rng: random.Random) -> int:
    return rng.randint(-14, 21) if rng.random() < 0.5 else rng.randint(-40, 60)


def _betrag(rng: random.Random) -> Decimal:
    return Decimal(rng.choice(BETRAEGE))


def erzeuge(seed: int) -> Szenario:
    """Zufälliges, seed-deterministisches Szenario."""
    rng = random.Random(seed)
    bl = rng.choice(BUNDESLAENDER)
    heute = _heute_waehlen(rng, bl)
    s0 = liq.wochen_start(heute)
    konten = tuple(sorted(rng.sample(KONTEN, rng.choice([1, 1, 2, 2, 3]))))
    erstes_datum = heute - timedelta(days=60)   # frühester Zahltag (Bankanker davor)

    # Positionen (Zahlungen erst nach den Verschiebungen: „am Zieltag bezahlt“)
    dauer = [Dauer(i + 1, rng.choice(konten), _betrag(rng),
                   rng.randint(1, 28) if rng.random() < 0.9 else rng.randint(29, 31),
                   vortragen=rng.random() < 0.9)
             for i in range(rng.choice([0, 0, 1, 1, 2]))]
    # Posten meist auf Konten ohne Dauerbuchung: fremde Zahlungen auf einem
    # Dauerbuchungskonto rechnet der Vortrag mit an (Grenze, siehe I1)
    ohne_dauer = [k for k in konten if k not in {d.konto for d in dauer}] or list(konten)
    posten = [Posten(i + 1, rng.choice(ohne_dauer if rng.random() < 0.75 else konten),
                     _betrag(rng), heute + timedelta(days=_versatz(rng)))
              for i in range(rng.choice([0, 1, 1, 2, 2, 3, 4, 5]))]
    bestellungen = [Bestellung(i + 1, rng.choice(konten), _betrag(rng),
                               heute + timedelta(days=rng.randint(-20, 45)),
                               status="FREIGEGEBEN" if rng.random() < 0.8 else "OFFEN")
                    for i in range(rng.choice([0, 0, 0, 1, 1, 2]))]

    # Budgets: meist keins (die meisten Invarianten setzen das voraus)
    budgets = []
    r = rng.random()
    if r < 0.25:
        for _ in range(rng.choice([1, 2])):
            budgets.append(Budget(rng.choice(konten), s0 + timedelta(days=7 * rng.randint(-3, 6)),
                                  Decimal(rng.choice(["1000", "2500", "5000", "10000"]))))
    elif r < 0.35:   # nur vergangene Wochen (I1b)
        for _ in range(rng.choice([1, 2])):
            budgets.append(Budget(rng.choice(konten), s0 - timedelta(days=7 * rng.randint(1, 3)),
                                  Decimal(rng.choice(["1000", "2500", "5000", "10000"]))))
    budgets = list({(b.konto, b.montag): b for b in budgets}.values())

    # Verschiebungen
    kandidaten: list[tuple[str, date, Decimal]] = []
    for p in posten:
        kandidaten.append((p.konto, p.faellig, p.betrag))
    for d in dauer:
        for t in rate_tage(d, heute - timedelta(days=45), heute + timedelta(days=60), bl):
            kandidaten.append((d.konto, t, d.betrag))
    for b in bestellungen:
        kandidaten.append((b.konto, b.zahldatum, b.brutto))
    verschiebungen: list[Verschiebung] = []
    for i in range(rng.choice([0, 1, 1, 2, 2, 2, 3, 4])):
        w = rng.random()
        if verschiebungen and w < 0.25:          # Kette: Ziel der vorigen = Ursprung
            vor = rng.choice(verschiebungen)
            konto, von, hinweis = vor.konto, vor.nach, abs(vor.betrag)
        elif verschiebungen and w < 0.35:        # mehrere am selben Ursprungstag
            vor = rng.choice(verschiebungen)
            konto, von, hinweis = vor.konto, vor.von, abs(vor.betrag)
        elif kandidaten and rng.random() < 0.9:
            konto, von, hinweis = rng.choice(kandidaten)
        else:
            konto, von, hinweis = (rng.choice(konten), heute + timedelta(days=rng.randint(-30, 50)),
                                   _betrag(rng))
        z = rng.random()
        if z < 0.6:
            nach = heute + timedelta(days=rng.randint(1, 45))
        elif z < 0.75:
            nach = von - timedelta(days=rng.randint(1, 10))
        elif z < 0.85 and kandidaten:
            nach = rng.choice(kandidaten)[1]
        else:
            nach = heute + timedelta(days=rng.randint(-15, 0))
        if nach == von:
            nach = von + timedelta(days=1)
        a = rng.random()
        if a < 0.55:
            betrag = hinweis
        elif a < 0.75:
            betrag = (hinweis * Decimal(rng.choice(["0.25", "0.5", "0.6"]))).quantize(CENT)
        elif a < 0.88:
            betrag = hinweis + Decimal("500")
        else:
            betrag = _betrag(rng)
        verschiebungen.append(Verschiebung(i + 1, konto, von, nach, -betrag))

    def ziele_ab(konto: str, tag: date) -> list[date]:
        """Zieltage der Verschiebungen (auch über Ketten) ab einem Ursprungstag."""
        ziele, offen = [], [tag]
        while offen:
            t = offen.pop()
            for v in verschiebungen:
                if v.konto == konto and v.von == t and v.nach not in ziele:
                    ziele.append(v.nach)
                    offen.append(v.nach)
        return ziele

    def zahltag(optionen: list[date]) -> date | None:
        optionen = [t for t in optionen if erstes_datum <= t <= heute]
        return rng.choice(optionen) if optionen else None

    # Zahlungen der Posten: am Ursprungs-, Ziel- oder anderem Tag, auch vor dem Fenster
    for idx, p in enumerate(posten):
        w = rng.random()
        if w < 0.45:
            continue
        ziele = [t for t in ziele_ab(p.konto, p.faellig) if t <= heute]
        if w < 0.85:
            r = rng.random()
            if ziele and r < 0.45:
                t = rng.choice(ziele)
            elif r < 0.65:
                t = zahltag([p.faellig])
            elif r < 0.8:
                t = zahltag([s0 - timedelta(days=rng.randint(1, 20))])
            else:
                t = zahltag([p.faellig + timedelta(days=rng.randint(-10, 10)),
                             heute - timedelta(days=rng.randint(0, 5))])
            if t is not None:
                posten[idx] = replace(p, zahlungen=((t, p.betrag),))
        else:
            zahlungen = []
            rest = p.betrag
            for _ in range(rng.choice([1, 1, 2])):
                t = zahltag([p.faellig + timedelta(days=rng.randint(-10, 15)),
                             heute - timedelta(days=rng.randint(0, 10))] + ziele)
                anteil = (rest * Decimal(rng.choice(["0.2", "0.3", "0.5"]))).quantize(CENT)
                if t is not None and anteil > 0:
                    zahlungen.append((t, anteil))
                    rest -= anteil
            posten[idx] = replace(p, zahlungen=tuple(sorted(zahlungen)))

    # Raten: bezahlt am Plantag, am Zieltag einer Verschiebung, vorab oder gar nicht
    for idx, d in enumerate(dauer):
        bezahlt = []
        for t in rate_tage(d, heute - timedelta(days=45), heute + timedelta(days=10), bl):
            w = rng.random()
            ziele = [z for z in ziele_ab(d.konto, t) if z <= heute]
            if t > heute:
                if w < 0.05:
                    bezahlt.append((t, heute - timedelta(days=rng.randint(0, 3))))
                continue
            if w < 0.55:
                bezahlt.append((t, t))
            elif w < 0.8:
                continue
            elif w < 0.93 and ziele:
                bezahlt.append((t, rng.choice(ziele)))
            elif w < 0.97:
                bezahlt.append((t, t))
            else:
                bezahlt.append((t, t - timedelta(days=rng.randint(1, 10))))
        dauer[idx] = replace(d, bezahlt=tuple(b for b in bezahlt if b[1] >= erstes_datum))

    # Bestellungen: Ablösung durch Zahlung bzw. Teilzahlung (nur freigegebene)
    for idx, b in enumerate(bestellungen):
        if b.status != "FREIGEGEBEN":
            continue
        w = rng.random()
        if w < 0.65:
            continue
        ziele = [t for t in ziele_ab(b.konto, b.zahldatum) if t <= heute]
        t = zahltag(([rng.choice(ziele)] if ziele and rng.random() < 0.4 else [])
                    or [b.zahldatum, s0 - timedelta(days=rng.randint(1, 10)),
                        heute - timedelta(days=rng.randint(0, 5))])
        if t is None:
            continue
        if w < 0.9:
            bestellungen[idx] = replace(b, zahlungen=((t, b.brutto, True),))
        else:
            bestellungen[idx] = replace(b, zahlungen=((t, (b.brutto / 2).quantize(CENT), False),))

    planstaende: tuple[int, ...] = ()
    referenz = None
    if rng.random() < 0.35:
        planstaende = tuple(sorted(rng.sample([0, 1, 2], rng.choice([1, 2]))))
        if rng.random() < 0.3:
            referenz = rng.choice(planstaende)
    fenster = ((0, WOCHEN),) + tuple(rng.sample(
        [(-3, 6), (-1, 4), (1, 4), (3, 6), (6, 4), (0, 2), (-6, WOCHEN)], 2))

    sz = Szenario(seed=seed, heute=heute, bundesland=bl, konten=konten,
                  posten=tuple(posten), dauer=tuple(dauer), bestellungen=tuple(bestellungen),
                  budgets=tuple(budgets), verschiebungen=tuple(verschiebungen),
                  planstaende=planstaende, referenz=referenz, fenster=fenster)
    if rng.random() < 0.35:
        sz = _muster(rng, sz)
    # Zusatzmuster (Runde 3) aus eigenem Zufallsstrom: die bisherigen Szenarien der Seeds
    # bleiben erhalten, nur ein Teil erhält zusätzlich ein Muster
    rng2 = random.Random(f"zusatz:{seed}")
    if rng2.random() < 0.3:
        sz = _muster_zusatz(rng2, sz)
    # Runde 3, Iteration 2 (eigener Zufallsstrom): Anlagetage der Verschiebungen und
    # Bestellungen, vollständig bezahlte Posten mit Teilzahlungen
    return _anlage_und_teilzahlungen(random.Random(f"anlage:{seed}"), sz)


def _anlage_und_teilzahlungen(rng: random.Random, sz: Szenario) -> Szenario:
    """Anlagetag je Verschiebung (vor dem Ursprung, höchstens heute; teils unbekannt) und
    je Bestellung; mit Wahrscheinlichkeit 0,2 ein Posten, der in Teilen vollständig bezahlt
    ist (Teilzahlung oft vor dem Wochenbeginn), oft verschoben."""
    heute, s0 = sz.heute, sz.s0
    vs = []
    for v in sz.verschiebungen:
        if v.erstellt is None:
            hi = min(heute, v.von - timedelta(days=1))
            lo = min(hi, v.von - timedelta(days=30))
            r = rng.random()
            t = (UNBEKANNT if r < 0.15 else hi if r < 0.55
                 else lo + timedelta(days=rng.randint(0, (hi - lo).days)))
            v = replace(v, erstellt=t)
        vs.append(v)
    # Anlage höchstens heute (künftig angelegte gäbe es nicht; sie zählten sonst stets als
    # nach den Verschiebungen angelegt) und stets ausdrücklich – ein aus `heute` abgeleiteter
    # Standard verschöbe sich, wenn ein Orakel denselben Fall zu einem früheren Stand rechnet
    best = [replace(b, angelegt=min(heute, b.zahldatum - timedelta(days=rng.randint(1, 45))))
            if rng.random() < 0.5 else
            replace(b, angelegt=b.angelegt or min(heute, b.zahldatum - timedelta(days=30)))
            for b in sz.bestellungen]
    posten = list(sz.posten)
    if rng.random() < 0.2:
        konto = rng.choice(sz.konten)
        betrag = _betrag(rng)
        f = heute + timedelta(days=rng.randint(-25, 5))
        t1 = min(heute, max(heute - timedelta(days=50), s0 - timedelta(days=rng.randint(1, 12))
                            if rng.random() < 0.6 else f - timedelta(days=rng.randint(0, 5))))
        t2 = min(heute, max(t1, f + timedelta(days=rng.randint(-3, 12))))
        teil = (betrag * Decimal(rng.choice(["0.2", "0.3", "0.5"]))).quantize(CENT)
        posten.append(Posten(len(posten) + 1, konto, betrag, f, ((t1, teil), (t2, betrag - teil))))
        if rng.random() < 0.6:
            nach = heute + timedelta(days=rng.randint(-10, 20))
            if nach == f:
                nach = f + timedelta(days=1)
            anteil = betrag if rng.random() < 0.5 else (betrag - teil)
            vs.append(Verschiebung(len(vs) + 1, konto, f, nach, -anteil,
                                   erstellt=min(heute, f - timedelta(days=rng.randint(1, 10)))))
    return replace(sz, verschiebungen=tuple(vs), bestellungen=tuple(best), posten=tuple(posten))


MUSTER = ("teilbezahlt_gerollt", "kette_bezahlt", "am_ziel_bezahlt", "rate_verschoben",
          "zukunftsfenster", "vorziehen", "vom_rolltag")


def _muster(rng: random.Random, sz: Szenario) -> Szenario:
    """Gezielte Erweiterung des Szenario-Raums: fügt ein typisches Verschiebungsmuster
    hinzu (Abläufe aus den Befunden der Korrekturrunden), damit seltene, aber häufig
    genutzte Abläufe ausreichend oft vorkommen."""
    art = rng.choice(MUSTER)
    heute, s0 = sz.heute, sz.s0
    konto = rng.choice(sz.konten)
    p_nr = len(sz.posten) + 1
    v_nr = len(sz.verschiebungen) + 1
    betrag = _betrag(rng)
    posten, vs, budgets, fenster, dauer = (list(sz.posten), list(sz.verschiebungen),
                                           list(sz.budgets), list(sz.fenster), list(sz.dauer))
    if art == "teilbezahlt_gerollt":
        # überfälliger Posten, nach dem Ursprungstag teilbezahlt, voll verschoben (V1);
        # oft mit Budget in der Ursprungswoche
        f = (s0 - timedelta(days=rng.randint(1, 12)) if rng.random() < 0.7
             else heute - timedelta(days=rng.randint(3, 20)))
        t = f + timedelta(days=rng.randint(1, (heute - f).days))
        teil = (betrag * Decimal(rng.choice(["0.2", "0.3", "0.5"]))).quantize(CENT)
        posten.append(Posten(p_nr, konto, betrag, f, ((t, teil),)))
        vs.append(Verschiebung(v_nr, konto, f, heute + timedelta(days=rng.randint(2, 20)), -betrag))
        if rng.random() < 0.6:
            budgets.append(Budget(konto, liq.wochen_start(f), Decimal("10000")))
    elif art == "kette_bezahlt":
        # vorab bezahlter Posten, zweimal verschoben, Zwischenziel vergangen (V4)
        f = heute - timedelta(days=rng.randint(2, 12))
        bezahlt = f - timedelta(days=rng.randint(0, 3))
        posten.append(Posten(p_nr, konto, betrag, f, ((bezahlt, betrag),)))
        t1 = f + timedelta(days=rng.randint(1, max(1, (heute - f).days)))
        t2 = heute + timedelta(days=rng.randint(1, 14))
        vs += [Verschiebung(v_nr, konto, f, t1, -betrag),
               Verschiebung(v_nr + 1, konto, t1, t2, -betrag)]
    elif art == "am_ziel_bezahlt":
        # verschoben und am neuen Termin bezahlt (V2)
        f = heute - timedelta(days=rng.randint(4, 25))
        t = f + timedelta(days=rng.randint(1, (heute - f).days))
        posten.append(Posten(p_nr, konto, betrag, f, ((t, betrag),)))
        vs.append(Verschiebung(v_nr, konto, f, t, -betrag))
    elif art == "rate_verschoben":
        # vergangene, unbezahlte Rate verschoben (V3), ggf. neben überfälligem Posten (V6)
        stichtag = rng.randint(1, 28)
        d = Dauer(len(dauer) + 1, konto, betrag, stichtag)
        raten = [r for r in rate_tage(d, heute - timedelta(days=40), heute - timedelta(days=1),
                                      sz.bundesland)]
        if raten:
            r = raten[-1]
            dauer.append(replace(d, bezahlt=tuple((x, x) for x in raten[:-1]
                                                  if x >= heute - timedelta(days=60))))
            vs.append(Verschiebung(v_nr, konto, r, heute + timedelta(days=rng.randint(1, 14)),
                                   -betrag))
            if rng.random() < 0.4:
                posten.append(Posten(p_nr, konto, _betrag(rng),
                                     heute - timedelta(days=rng.randint(2, 20))))
    elif art == "zukunftsfenster":
        # Fenster in der Zukunft, Ursprung davor, Ziel darin (V5)
        f = heute + timedelta(days=rng.randint(1, 20))
        k = (liq.wochen_start(f) - s0).days // 7 + 1
        start = s0 + timedelta(days=7 * k)
        posten.append(Posten(p_nr, konto, betrag, f))
        vs.append(Verschiebung(v_nr, konto, f, start + timedelta(days=rng.randint(0, 20)), -betrag))
        fenster.append((k, 4))
    elif art == "vorziehen":
        # künftige Zahlung auf einen inzwischen vergangenen Tag vorgezogen, nicht bezahlt
        f = heute + timedelta(days=rng.randint(1, 20))
        posten.append(Posten(p_nr, konto, betrag, f))
        ziel = max(s0, heute - timedelta(days=rng.randint(1, 5)))
        if ziel < heute:
            vs.append(Verschiebung(v_nr, konto, f, ziel, -betrag))
    elif art == "vom_rolltag":
        # überfälliger Posten steht heute (Rolltag) im Plan und wird von dort (teilweise)
        # verschoben – der naheliegende Bedienweg im Zellendialog
        posten.append(Posten(p_nr, konto, betrag, heute - timedelta(days=rng.randint(1, 15))))
        rolltag = naechster_bankarbeitstag(heute, sz.bundesland)
        anteil = betrag if rng.random() < 0.5 else (betrag / 2).quantize(CENT)
        vs.append(Verschiebung(v_nr, konto, rolltag, rolltag + timedelta(days=rng.randint(1, 14)),
                               -anteil, erstellt=heute if rolltag > heute else None))
    return replace(sz, posten=tuple(posten), verschiebungen=tuple(vs), budgets=tuple(
        {(b.konto, b.montag): b for b in budgets}.values()), fenster=tuple(fenster),
        dauer=tuple(dauer))


MUSTER_ZUSATZ = ("vom_vergangenen_rolltag", "bestellung_am_ziel_bezahlt", "rate_vorgezogen",
                 "budget_aus_kuenftiger_woche", "kette_ueber_aussen")


def _muster_zusatz(rng: random.Random, sz: Szenario) -> Szenario:
    """Muster zur Zuordnung der Verschiebungen (Runde 3): Verschiebung vom Rolltag eines
    vergangenen Tages (wirkt nach dem Tag weiter), verschobene und am Ziel abgelöste
    Bestellung, künftige Rate auf einen vergangenen Tag vorgezogen, Budgetanteil aus
    einer Woche außerhalb des Freigabefensters, Kette über einen Tag hinter dem Fenster."""
    art = rng.choice(MUSTER_ZUSATZ)
    heute, s0, bl = sz.heute, sz.s0, sz.bundesland
    konto = rng.choice(sz.konten)
    betrag = _betrag(rng)
    posten, vs, budgets, dauer, best = (list(sz.posten), list(sz.verschiebungen),
                                        list(sz.budgets), list(sz.dauer), list(sz.bestellungen))
    v_nr = len(vs) + 1
    if art == "vom_vergangenen_rolltag":
        # überfälliger Posten, an einem vergangenen Bankarbeitstag (damals Rolltag) ganz oder
        # zur Hälfte verschoben; das Ziel kann inzwischen auch verstrichen sein
        tage = [s0 - timedelta(days=i) for i in range(-6, 8)]
        tage = [t for t in tage if t < heute and naechster_bankarbeitstag(t, bl) == t]
        if tage:
            x = rng.choice(tage)
            posten.append(Posten(len(posten) + 1, konto, betrag, x - timedelta(days=rng.randint(1, 10))))
            anteil = betrag if rng.random() < 0.5 else (betrag / 2).quantize(CENT)
            ziel = x + timedelta(days=rng.randint(1, 20))
            vs.append(Verschiebung(v_nr, konto, x, ziel, -anteil, erstellt=x - timedelta(days=1)))
    elif art == "bestellung_am_ziel_bezahlt":
        # freigegebene Bestellung, verschoben und am neuen Termin (ganz oder teilweise) bezahlt
        z = heute - timedelta(days=rng.randint(3, 20))
        t = z + timedelta(days=rng.randint(1, (heute - z).days))
        b = Bestellung(len(best) + 1, konto, betrag, z)
        teil = rng.random() < 0.3
        b = replace(b, zahlungen=((t, (b.brutto / 2).quantize(CENT) if teil else b.brutto, not teil),))
        best.append(b)
        vs.append(Verschiebung(v_nr, konto, z, t, -b.brutto))
    elif art == "rate_vorgezogen":
        # künftige Rate auf einen inzwischen vergangenen Tag (laufende oder Vorwoche) vorgezogen
        d = Dauer(len(dauer) + 1, konto, betrag, rng.randint(1, 28))
        kuenftig = rate_tage(d, heute + timedelta(days=1), heute + timedelta(days=60), bl)
        ziel = max(s0 - timedelta(days=7 * rng.randint(0, 1)), heute - timedelta(days=rng.randint(1, 9)))
        if kuenftig and ziel < heute:
            dauer.append(d)
            vs.append(Verschiebung(v_nr, konto, rng.choice(kuenftig), ziel, -betrag))
    elif art == "budget_aus_kuenftiger_woche":
        # Budget in einer künftigen Woche; Verschiebung von dort (ohne explizit Geplantes) in eine
        # frühere Woche, daneben ein offener Posten, der in der Wochensicht hineinrollen würde
        w = s0 + timedelta(days=7 * rng.randint(1, 5))
        budgets.append(Budget(konto, w, Decimal(rng.choice(["1000", "2500", "5000"]))))
        posten.append(Posten(len(posten) + 1, konto, betrag, heute - timedelta(days=rng.randint(0, 6))))
        von = w + timedelta(days=rng.randint(0, 4))
        vs.append(Verschiebung(v_nr, konto, von, max(heute + timedelta(days=1),
                                                     von - timedelta(days=rng.randint(3, 20))),
                               -Decimal(rng.choice(["300", "800", "5000"]))))
    elif art == "kette_ueber_aussen":
        # offener Posten, Kette über einen Tag hinter dem Freigabefenster zurück ins Fenster
        f = heute + timedelta(days=rng.randint(1, 20))
        posten.append(Posten(len(posten) + 1, konto, betrag, f))
        aussen = s0 + timedelta(days=7 * KW_ANZAHL + rng.randint(0, 30))
        zurueck = heute + timedelta(days=rng.randint(1, 40))
        vs += [Verschiebung(v_nr, konto, f, aussen, -betrag),
               Verschiebung(v_nr + 1, konto, aussen, zurueck, -betrag)]
    return replace(sz, posten=tuple(posten), verschiebungen=tuple(vs), budgets=tuple(
        {(b.konto, b.montag): b for b in budgets}.values()), dauer=tuple(dauer),
        bestellungen=tuple(best))


# ---------------------------------------------------------------------------
# Datenbank-Aufbau
# ---------------------------------------------------------------------------

_VORLAGE: sqlite3.Connection | None = None


def _sitzung_aus(roh: sqlite3.Connection):
    engine = create_engine("sqlite://", creator=lambda: roh, poolclass=StaticPool)
    return sessionmaker(bind=engine, expire_on_commit=False)(), engine


def _vorlage() -> sqlite3.Connection:
    """Leere Datenbank mit Schema, Mandant (Kontenrahmen SKR03) und Benutzer – je
    Prozess einmal angelegt und für jedes Szenario kopiert (schneller als create_all)."""
    global _VORLAGE
    if _VORLAGE is None:
        roh = sqlite3.connect(":memory:", check_same_thread=False)
        db, engine = _sitzung_aus(roh)
        Base.metadata.create_all(engine)
        _mandant(db)
        _benutzer(db)
        db.commit()
        db.close()
        _VORLAGE = roh
    return _VORLAGE


@dataclass
class Aufbau:
    db: object
    m: models.Mandant
    konto_id: dict[str, int]
    planstand_id: dict[int, int] = field(default_factory=dict)   # k → Snapshot-ID
    frisch_id: int | None = None
    engine: object = None

    def schliessen(self) -> None:
        self.db.close()
        if self.engine is not None:
            self.engine.dispose()


def aufbauen(sz: Szenario, *, mit_verschiebungen: bool = True, planstaende: bool = True,
             frischer_planstand: bool = False) -> Aufbau:
    """Datenbank zum Szenario: SQLite-Kopie der Vorlage – auch bei TEST_DATABASE_URL, weil
    je Szenario mehrere Varianten (mit/ohne Verschiebungen, frischer Planstand)
    gleichzeitig offen sind; die Engine-Logik ist datenbankunabhängig."""
    roh = sqlite3.connect(":memory:", check_same_thread=False)
    _vorlage().backup(roh)
    db, engine = _sitzung_aus(roh)
    m = db.scalar(select(models.Mandant))
    benutzer = db.scalar(select(models.Benutzer))
    m.bundesland = sz.bundesland
    konto_id = {nr: _konto(db, m, nr).id for nr in KONTEN}
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=_konto(db, m, "1200").id,
                          datum=sz.heute - timedelta(days=70), wert=BANK_ANFANG))
    for p in sz.posten:
        op = models.OffenerPosten(
            mandant_id=m.id, art="KREDITOR", partner=f"Lieferant {p.nr}", faellig_am=p.faellig,
            betrag_brutto=p.betrag, konto_id=konto_id[p.konto],
            bezahlt_betrag=min(p.gezahlt, p.betrag),
            status="BEZAHLT" if p.bezahlt else "OFFEN", bezahlt_am=p.bezahlt_am,
            **altbestand(models.OffenerPosten, p.angelegt))
        db.add(op)
        db.flush()
        for t, b in p.zahlungen:          # Ist-Buchung zum Posten (wie die Zahlungserfassung)
            _zahlung(db, m, t, str(b), konto=p.konto, text=f"OP {p.nr}").posten_id = op.id
    for d in sz.dauer:
        db.add(models.Dauerbuchung(
            mandant_id=m.id, name=f"Dauer {d.nr}", art="KREDITOR", konto_id=konto_id[d.konto],
            betrag_brutto=d.betrag, intervall="MONATLICH", stichtag=d.stichtag,
            gueltig_von=date(2025, 1, 1), aktiv=True, vortragen=d.vortragen))
        for _, t in d.bezahlt:
            _zahlung(db, m, t, str(d.betrag), konto=d.konto, text=f"Rate {d.nr}")
    for b in sz.bestellungen:
        best = svc.anlegen(db, m, benutzer, {
            "kreditor": f"Kreditor {b.nr}", "bestellnummer": f"B-{b.nr}",
            "bestelldatum": b.zahldatum - timedelta(days=30), "zahldatum": b.zahldatum,
            "betrag_netto": str(b.netto), "konto_id": konto_id[b.konto]})
        best.erstellt_am = datetime.combine(
            b.angelegt or min(sz.heute, b.zahldatum - timedelta(days=30)), datetime.min.time())
        if b.status == "FREIGEGEBEN":
            svc.entscheiden(db, best, benutzer, "FREIGEBEN", "Orakel", {"orakel": True})
            # freigegeben gleich nach der Anlage (nicht zur Uhrzeit des Laufs)
            best.entschieden_am = best.erstellt_am + timedelta(minutes=30)
        for t, betrag, abschliessend in b.zahlungen:
            bu = _zahlung(db, m, t, str(betrag), konto=b.konto, text=f"Bestellung {b.nr}")
            svc.abloesen(db, best, benutzer, art="ZAHLUNG", buchung_id=bu.id,
                         abschliessend=abschliessend)
    for bw in sz.budgets:
        iso = bw.montag.isocalendar()
        db.add(models.BudgetWoche(mandant_id=m.id, konto_id=konto_id[bw.konto], jahr=iso.year,
                                  kw=iso.week, betrag_netto=bw.netto, aufteilung="TAG"))
    if mit_verschiebungen:
        for v in sz.verschiebungen:
            # Anlage mittags (unbekannt: nach dem Ursprung, wie direkt angelegte in Tests)
            erstellt = v.erstellt or min(sz.heute, v.von - timedelta(days=1))
            db.add(models.PlanVerschiebung(mandant_id=m.id, konto_id=konto_id[v.konto],
                                           quelle="PLAN", von=v.von, nach=v.nach,
                                           betrag=v.betrag,
                                           erstellt_am=datetime.combine(erstellt, datetime.min.time())
                                           + timedelta(hours=12)))
            db.flush()                     # Anlagereihenfolge = ID-Reihenfolge
    db.commit()
    a = Aufbau(db=db, m=m, konto_id=konto_id, engine=engine)
    if planstaende:
        for k in sz.planstaende:
            snap = liq.erstelle_snapshot(db, m, start=sz.s0 - timedelta(days=7 * k),
                                         wochen=WOCHEN, heute=sz.heute)
            if sz.referenz == k:
                snap.referenz = True
                db.commit()
            a.planstand_id[k] = snap.id
    if frischer_planstand:
        a.frisch_id = liq.erstelle_snapshot(db, m, start=sz.s0, wochen=WOCHEN, heute=sz.heute).id
    return a


@contextlib.contextmanager
def schnelle_kennzahlen():
    """Die Freigabe-Kennzahlen rechnen zur Gegenprobe die Liquidität (berechne_plan) –
    für die Invarianten unerheblich (geprüft wird nur `ebenen`) und teuer; hier
    vorübergehend ausgeblendet. Ändert keinen Produktcode."""
    original = svc.berechne_plan
    svc.berechne_plan = lambda *a, **k: {"bestaende": {"liquiditaet": {}, "verfuegbar": {}}}
    try:
        yield
    finally:
        svc.berechne_plan = original


# ---------------------------------------------------------------------------
# Auswertung (gepuffert je Datenbankvariante)
# ---------------------------------------------------------------------------

class Auswertung:
    """Varianten: "mit" (Szenario samt Planständen), "ohne" (ohne PLAN-Verschiebungen und
    Planstände), "frisch" (wie "mit", zusätzlich Planstand zum aktuellen Wochenbeginn)."""

    def __init__(self, sz: Szenario):
        self.sz = sz
        self._auf: dict[str, Aufbau] = {}
        self._plan: dict[tuple, dict] = {}
        self._kz: dict[tuple, dict | None] = {}

    def auf(self, variante: str) -> Aufbau:
        if variante not in self._auf:
            self._auf[variante] = aufbauen(
                self.sz, mit_verschiebungen=variante != "ohne", planstaende=variante != "ohne",
                frischer_planstand=variante == "frisch")
        return self._auf[variante]

    def schliessen(self) -> None:
        for a in self._auf.values():
            a.schliessen()
        self._auf.clear()

    def plan(self, start: date, wochen: int, variante: str = "mit",
             heute: date | None = None) -> dict:
        heute = heute or self.sz.heute
        schluessel = (start, wochen, variante, heute)
        if schluessel not in self._plan:
            a = self.auf(variante)
            self._plan[schluessel] = liq.berechne_plan(a.db, a.m, start=start, wochen=wochen,
                                                       heute=heute)
        return self._plan[schluessel]

    def zeile(self, plan: dict, konto: str) -> dict:
        for g in plan["zeilen"]:
            for k in g["kinder"]:
                if k["nummer"] == konto:
                    return k
        return {"plan": {}, "ist": {}, "vortrag": {}}

    def _snapshot_id(self, a: Aufbau, basis):
        if basis == "LIVE":
            return "LIVE"
        if basis == "FRISCH":
            return a.frisch_id
        if isinstance(basis, tuple) and basis[0] == "PLANSTAND":
            return a.planstand_id.get(basis[1])
        raise ValueError(basis)

    def basis_aufloesen(self, montag: date, basis, variante: str = "mit"):
        """AUTO wie die Freigabe (Soll-Referenz, sonst jüngster abdeckender Planstand, sonst
        LIVE) – liefert die gleichwertige ausdrückliche Basis."""
        if basis != "AUTO":
            return basis
        a = self.auf(variante)
        snap, _ = svc._basis_waehlen(a.db, a.m, None, montag, montag + timedelta(days=6),
                                     self.sz.heute)
        if snap is None:
            return "LIVE"
        if snap.id == a.frisch_id:
            return "FRISCH"
        return next(("PLANSTAND", k) for k, i in a.planstand_id.items() if i == snap.id)

    def kz(self, montag: date, basis, konto: str | None, variante: str = "mit",
           bestellung_nr: int | None = None) -> dict | None:
        """Freigabe-Kennzahlen (Ebenen) der KW; None, wenn der Planstand fehlt oder die KW
        nicht abdeckt (dann fehlen dort Planwerte – keine Aussage)."""
        basis = self.basis_aufloesen(montag, basis, variante)
        schluessel = (montag, basis, konto, variante, bestellung_nr)
        if schluessel in self._kz:
            return self._kz[schluessel]
        a = self.auf(variante)
        sid = self._snapshot_id(a, basis)
        ergebnis = None
        if sid is not None:
            if sid != "LIVE":
                s = a.db.get(models.PlanSnapshot, sid)
                bis = s.stichtag + timedelta(days=7 * s.wochen - 1)
                if not (s.stichtag <= montag and bis >= montag + timedelta(days=6)):
                    self._kz[schluessel] = None
                    return None
            iso = montag.isocalendar()
            with schnelle_kennzahlen():
                if bestellung_nr is not None:
                    b = a.db.scalar(select(models.Bestellung).where(
                        models.Bestellung.bestellnummer == f"B-{bestellung_nr}"))
                    r = svc.bestellung_kennzahlen(a.db, a.m, b, jahr=iso.year, kw=iso.week,
                                                  basis=sid, heute=self.sz.heute)
                else:
                    r = svc.freigabe_kennzahlen(
                        a.db, a.m, konto_id=a.konto_id[konto] if konto else None,
                        betrag_brutto=NULL, jahr=iso.year, kw=iso.week, basis=sid,
                        heute=self.sz.heute)
            ergebnis = {e["ebene"]: e for e in r["ebenen"]}
            ergebnis["_basis"] = r["basis"]
        self._kz[schluessel] = ergebnis
        return ergebnis


def _kurz(e: dict) -> str:
    return (f"soll {e['soll_kw']:.2f} (ab heute {e['soll_kw_rest']:.2f}), gebunden "
            f"{e['gebunden']['summe']:.2f}, C1 {e['abweichung']['ist_minus_plan']:.2f}, "
            f"C2 {e['abweichung']['vortraege']:.2f}, C3 {e['abweichung']['ueberfaellig']:.2f}"
            f" → verfügbar {e['verfuegbar']:.2f}")


def _datum_basis(basis) -> str:
    if isinstance(basis, tuple):
        return f"Planstand −{basis[1]} Wo."
    return str(basis)


# ---------------------------------------------------------------------------
# Invarianten
# ---------------------------------------------------------------------------

class Invariante:
    name = ""
    titel = ""
    vorbedingung_text = ""

    def vorbedingung(self, sz: Szenario) -> str | None:
        """None = anwendbar, sonst Grund."""
        return None

    def punkte(self, sz: Szenario) -> list[tuple]:
        return []

    def punkt_gueltig(self, sz: Szenario, punkt: tuple) -> bool:
        return punkt in self.punkte(sz)

    def pruefe(self, aw: Auswertung, punkt: tuple) -> str | None:
        """None = erfüllt, sonst Beschreibung der Verletzung."""
        raise NotImplementedError


def _i1_basis_vorbedingung(sz: Szenario) -> str | None:
    if fremdzahlung_auf_dauerkonto(sz):
        return "Zahlung eines Postens/einer Bestellung auf dem Konto einer Dauerbuchung"
    if unbezahlte_vergangene_raten_ohne_vortrag(sz):
        return "unbezahlte vergangene Rate ohne Vortrag (gilt als erledigt)"
    return None


class I1(Invariante):
    """Ohne Budget ist jeder Planbetrag explizit: was das Soll der KW (Basis) noch
    enthält, ist gebunden (B), bereits gezahlt (C1) oder überfällig/vorzutragen (C2/C3).
    Bei betragsgenauen Zahlungen bleibt kein Spielraum – verfügbar > 0 wäre ein Rahmen
    für eine Bestellung, den kein Budget deckt. Unbezahlte vergangene Raten ohne Vortrag
    gelten fachlich als erledigt (Minderausgabe = Rahmen) und fremde Zahlungen auf
    einem Dauerbuchungskonto rechnet der Vortragsabgleich mangels Zuordnung an
    (dokumentierte Grenze) – beides ausgeschlossen."""
    name = "I1"
    titel = "Kein Phantom-Spielraum ohne Budget"
    vorbedingung_text = ("kein Budget; Zahlungen betragsgenau (Posten inkl. Teilzahlungen, Raten, "
                         "Bestellungen); keine unbezahlte vergangene Rate ohne Vortrag; keine "
                         "fremde Zahlung auf einem Dauerbuchungskonto")

    def vorbedingung(self, sz):
        if sz.budgets:
            return "Budget vorhanden"
        return _i1_basis_vorbedingung(sz)

    def basen(self, sz: Szenario) -> list:
        return ["LIVE", "AUTO"] + [("PLANSTAND", k) for k in sz.planstaende]

    def punkte(self, sz):
        p = [("KZ", montag, basis, konto) for montag in kw_montage(sz)
             for basis in self.basen(sz) for konto in sz.konten]
        for b in sz.bestellungen:
            if b.status == "OFFEN":
                montag = liq.wochen_start(max(b.zahldatum, sz.heute))
                if montag <= sz.s0 + timedelta(days=7 * (KW_ANZAHL - 1)):
                    p += [("BEST", montag, basis, b.nr) for basis in ("LIVE", "AUTO")]
        return p

    def pruefe(self, aw, punkt):
        art, montag, basis, x = punkt
        r = aw.kz(montag, basis, x if art == "KZ" else None,
                  bestellung_nr=x if art == "BEST" else None)
        if r is None:
            return None
        kw = montag.isocalendar().week
        fehler = [f"KW {kw} Basis {_datum_basis(aw.basis_aufloesen(montag, basis))} "
                  f"{'Konto ' + x if art == 'KZ' else 'Bestellung ' + str(x)} Ebene {name}: "
                  f"{_kurz(e)}"
                  for name, e in r.items() if not name.startswith("_")
                  and e["verfuegbar"] > TOLERANZ]
        return "; ".join(fehler) or None


class I1b(I1):
    """Budget nur in Wochen vor dem aktuellen Wochenbeginn: Das Soll der LIVE-Basis und
    eines Planstands zum Wochenbeginn beginnt bei s0 und enthält dieses Budget nicht –
    vergangenes Budget ist mit seiner Woche vergangen. Betrifft jede Verschiebung nur
    explizit Geplantes (Betrag ≤ zum Ursprungstag geplante Posten/Raten/Bestellungen),
    darf sie auch kein vergangenes Budget in die Zukunft tragen. Ältere Planstände
    enthalten die Budgetwochen im Vergleichszeitraum (Minderausgabe = Rahmen) und sind
    ausgenommen."""
    name = "I1b"
    titel = "Vergangenes Budget gibt keinen Spielraum frei"
    vorbedingung_text = ("Budget nur in Wochen vor s0, mindestens eins auf einer verschobenen "
                         "Zeile; Verschiebungen je Ursprungstag zusammen ≤ dort explizit "
                         "Geplantes; sonst wie I1; Basis LIVE und Planstand zum Wochenbeginn")

    def vorbedingung(self, sz):
        if not sz.budgets:
            return "kein Budget"
        if any(b.montag + timedelta(days=6) >= sz.s0 for b in sz.budgets):
            return "Budget ab der laufenden Woche"
        if not budget_konten(sz) & {v.konto for v in sz.verschiebungen}:
            return "kein Budget auf einer verschobenen Zeile (I1 deckt das ab)"
        genommen: dict[tuple[str, date], Decimal] = defaultdict(Decimal)
        for i, v in enumerate(sz.verschiebungen):
            # mehrere Verschiebungen vom selben Tag zusammen höchstens das Geplante
            genommen[(v.konto, v.von)] += abs(v.betrag)
            if genommen[(v.konto, v.von)] > explizit_am_tag(sz, v.konto, v.von, bis_index=i) + CENT:
                return f"Verschiebung V{v.nr} größer als das explizit Geplante"
        return _i1_basis_vorbedingung(sz)

    def basen(self, sz):
        return ["LIVE"] + ([("PLANSTAND", 0)] if 0 in sz.planstaende else [])

    def punkte(self, sz):
        return [p for p in super().punkte(sz) if p[0] == "KZ"]


class I2(Invariante):
    """Eine Verschiebung bewegt Geld nur in der Zeit: liegen alle Ziele im Horizont und
    nach heute und kein Ursprung hinter dem Horizont, ist die Liquidität am Horizontende
    unverändert. Verglichen wird die vollständige Sicht („mit Vortrag“, sonst ohne): eine
    verschobene unbezahlte vergangene Rate wandert aus dem Vortrag in den Plan. Ohne
    Budget auf den Zeilen, weil das Restbudget je Woche vom Expliziten abhängt. Raten
    ohne Vortrag gelten nach ihrem Tag als erledigt – ihre Verschiebung aus der
    Vergangenheit plant sie fachlich neu (ausgeschlossen)."""
    name = "I2"
    titel = "Verschiebungen erhalten die Liquidität"
    vorbedingung_text = ("Verschiebung vorhanden; kein Budget auf verschobenen Zeilen; Ziele in "
                         "(heute, Horizontende]; Ursprünge ≤ Horizontende; kein vergangener "
                         "Ursprung auf einer Rate ohne Vortrag und kein Ursprung vor s0 auf "
                         "einer Rate (verschärft); keine fremde Zahlung auf Dauerbuchungskonto")
    ziele_vergangen = False

    def vorbedingung(self, sz):
        if not sz.verschiebungen:
            return "keine Verschiebung"
        if {v.konto for v in sz.verschiebungen} & budget_konten(sz):
            return "Budget auf verschobener Zeile"
        ende = sz.horizont_ende
        for v in sz.verschiebungen:
            if v.von > ende:
                return "Ursprung hinter dem Horizont"
            if self.ziele_vergangen:
                if not sz.s0 <= v.nach <= ende:
                    return "Ziel außerhalb des Horizonts"
            elif not sz.heute < v.nach <= ende:
                return "Ziel nicht in (heute, Horizontende]"
            if v.von <= sz.heute and ist_ratentag(sz, v.konto, v.von, nur_ohne_vortrag=True):
                return "vergangener Ursprung auf Rate ohne Vortrag"
            if v.von < sz.s0 and ist_ratentag(sz, v.konto, v.von):
                # verschärft nach dem ersten Lauf: eine Rate vor dem Fenster ist weder im
                # Plan noch im Vortrag (der nur Fälligkeiten im Fenster vorträgt) – ins
                # Fenster verschoben wird sie zu Recht wieder gebucht
                return "Ursprung vor dem Fenster auf einer Rate"
        if self.ziele_vergangen and all(v.nach > sz.heute for v in sz.verschiebungen):
            return "kein Ziel bis heute (I2 deckt das ab)"
        if fremdzahlung_auf_dauerkonto(sz):
            return "fremde Zahlung auf Dauerbuchungskonto"
        return None

    def punkte(self, sz):
        return [("LIQ", sz.horizont_ende)]

    def pruefe(self, aw, punkt):
        sz = aw.sz
        tag = punkt[1].isoformat()

        def wert(variante):
            b = aw.plan(sz.s0, WOCHEN, variante)["bestaende"]
            return (b.get("liquiditaet_vortrag") or b["liquiditaet"])[tag]

        mit, ohne = wert("mit"), wert("ohne")
        if abs(mit - ohne) > TOLERANZ:
            return (f"Liquidität (mit Vortrag) am {punkt[1]:%d.%m.%Y}: mit Verschiebungen "
                    f"{mit:.2f}, ohne {ohne:.2f} (Differenz {mit - ohne:+.2f})")
        return None


class I2b(I2):
    """Wie I2, aber Ziele dürfen auch bis heute (im Fenster) liegen: ein offener Posten,
    dessen Zieltag ungezahlt verstrichen ist, ist weiter offen (rollt), ein bezahlter
    bleibt bezahlt – die Liquidität ändert sich nicht. (Ergänzung; deckt Ketten über
    vergangene Zwischenziele ab.)"""
    name = "I2b"
    titel = "Liquidität erhalten, auch bei Zielen bis heute"
    vorbedingung_text = "wie I2, Ziele in [s0, Horizontende], mindestens ein Ziel ≤ heute"
    ziele_vergangen = True


def _fenster(sz: Szenario, versatz: int, wochen: int) -> tuple[date, date]:
    start = sz.s0 + timedelta(days=7 * versatz)
    return start, start + timedelta(days=7 * wochen - 1)


def _summen_vorbedingung(sz: Szenario, konto: str, start: date, ende: date) -> str | None:
    """Vorbedingung des Summenvergleichs einer Zeile über [start, ende]."""
    for v in sz.verschiebungen:
        if v.konto != konto:
            continue
        if not start <= v.nach <= ende:
            return "Ziel außerhalb des Fensters"
        if v.von > ende:
            return "Ursprung hinter dem Fenster"
        if v.von < start and ist_ratentag(sz, konto, v.von):
            # eine Rate vor dem Fenster steht dort nicht – ins Fenster verschoben
            # erscheint sie zu Recht (anders als ein offener Posten, der ohnehin rollt)
            return "Ursprung vor dem Fenster auf einer Rate"
    if vor_fenster_bezahlt(sz, konto, start):
        return "Position vor dem Fenster bezahlt"
    return None


def obergrenze(sz: Szenario, konto: str, start: date, ende: date) -> Decimal:
    """Höchstbetrag, den die Positionen einer Zeile ohne Budget zur Zeilensumme über
    [start, ende] beitragen können – jede höchstens mit ihrem Planbetrag (offen: Rest,
    bezahlt/abgelöst: voll), wenn sie im Fenster liegt, hineinrollt, im Fenster bezahlt
    wurde oder an einem Ursprungstag einer Verschiebung steht (großzügig)."""
    urspruenge = {v.von for v in sz.verschiebungen if v.konto == konto}
    s = NULL
    for p in sz.posten:
        if p.konto != konto:
            continue
        im = start <= p.faellig <= ende or p.faellig in urspruenge
        if p.bezahlt:
            im = im or (p.bezahlt_am is not None and p.bezahlt_am >= start)
            s += p.betrag if im else NULL
        else:
            s += p.rest if (im or p.faellig < start) else NULL
    for d in sz.dauer:
        if d.konto != konto:
            continue
        tage = set(rate_tage(d, start, ende, sz.bundesland))
        tage |= {t for t in urspruenge if t in rate_tage(d, t, t, sz.bundesland)}
        s += d.betrag * len(tage)
    for b in sz.bestellungen:
        if b.konto != konto:
            continue
        im = start <= b.zahldatum <= ende or b.zahldatum in urspruenge
        if b.abgeloest:
            s += b.brutto if (im or b.zahldatum < start) else NULL
        else:
            s += b.rest if (im or b.zahldatum < start) else NULL
    return s


class I3(Invariante):
    """Angezeigter Plan (volle Sicht) einer Zeile ohne Budget: Verschiebungen bewegen
    Beträge innerhalb des Fensters, die Zeilensumme bleibt – sofern alle Ziele im Fenster
    liegen, kein Ursprung hinter dem Fenster bzw. vor dem Fenster auf einer Rate liegt
    (die stünde ohne Verschiebung nicht im Fenster) und keine Position vor dem Fenster
    bezahlt wurde (bezahlte Posten zeigt der Plan an ihrem verschobenen Tag, P1).
    Unabhängig davon trägt keine Position mehr als ihren Betrag bei (Obergrenze)."""
    name = "I3"
    titel = "Nichts doppelt im angezeigten Plan"
    vorbedingung_text = ("kein Budget auf der Zeile; Summe: Ziele im Fenster, Ursprünge ≤ "
                         "Fensterende und nicht vor dem Fenster auf einer Rate, keine Position "
                         "vor dem Fenster bezahlt; Obergrenze: immer")

    def punkte(self, sz):
        p = []
        for versatz, wochen in sz.fenster:
            start, ende = _fenster(sz, versatz, wochen)
            for konto in sz.konten:
                if konto in budget_konten(sz):
                    continue
                p.append(("OBERGRENZE", versatz, wochen, konto))
                if _summen_vorbedingung(sz, konto, start, ende) is None:
                    p.append(("SUMME", versatz, wochen, konto))
        return p

    def punkt_gueltig(self, sz, punkt):
        art, versatz, wochen, konto = punkt
        if konto not in sz.konten or konto in budget_konten(sz):
            return False
        start, ende = _fenster(sz, versatz, wochen)
        return art == "OBERGRENZE" or _summen_vorbedingung(sz, konto, start, ende) is None

    def pruefe(self, aw, punkt):
        sz = aw.sz
        art, versatz, wochen, konto = punkt
        start, ende = _fenster(sz, versatz, wochen)

        def summe(variante):
            z = aw.zeile(aw.plan(start, wochen, variante), konto)
            return round(sum(z["plan"].values()), 2), z["plan"]

        mit, plan_mit = summe("mit")
        fenster = f"Fenster {start:%d.%m.}–{ende:%d.%m.%Y} (heute {sz.heute:%d.%m.})"
        if art == "SUMME":
            ohne, plan_ohne = summe("ohne")
            if abs(mit - ohne) > TOLERANZ:
                return (f"{fenster} Zeile {konto}: Summe mit Verschiebungen {mit:.2f}, ohne "
                        f"{ohne:.2f}; Plan mit {_kompakt(plan_mit)}, ohne {_kompakt(plan_ohne)}")
            return None
        grenze = obergrenze(sz, konto, start, ende)
        if -mit > float(grenze) + TOLERANZ:
            return (f"{fenster} Zeile {konto}: Plansumme {mit:.2f} übersteigt die Positionen "
                    f"({grenze:.2f}); Plan {_kompakt(plan_mit)}")
        return None


def _kompakt(werte: dict) -> str:
    return "{" + ", ".join(f"{t[8:10]}.{t[5:7]}.: {v:.2f}" for t, v in sorted(werte.items())) + "}"


class I4(Invariante):
    """LIVE-Soll der Freigabe = Plan ab Montag der laufenden Woche „wie ein Planstand zu
    diesem Tag“: ohne Budget verschieben Verschiebungen nur zwischen KWs; die Summe über
    die KWs ab s0 bleibt (Bedingungen wie I3 für das Fenster s0 … Ende KW+8)."""
    name = "I4"
    titel = "Soll-Konsistenz der LIVE-Basis"
    vorbedingung_text = "kein Budget; für [s0, Ende KW+8] wie die Summenbedingung von I3"

    def vorbedingung(self, sz):
        if sz.budgets:
            return "Budget vorhanden"
        ende = sz.s0 + timedelta(days=7 * KW_ANZAHL - 1)
        for konto in {v.konto for v in sz.verschiebungen}:
            grund = _summen_vorbedingung(sz, konto, sz.s0, ende)
            if grund:
                return grund
        return None

    def punkte(self, sz):
        return [("SOLL",)]

    def pruefe(self, aw, punkt):
        def summe(variante):
            werte = []
            for montag in kw_montage(aw.sz):
                r = aw.kz(montag, "LIVE", aw.sz.konten[0], variante)
                werte.append(r["gesamt"]["soll_kw"])
            return round(sum(werte), 2), werte

        mit, w_mit = summe("mit")
        ohne, w_ohne = summe("ohne")
        if abs(mit - ohne) > TOLERANZ:
            return (f"LIVE-Soll gesamt über KW {aw.sz.s0.isocalendar().week}+8: mit "
                    f"Verschiebungen {mit:.2f}, ohne {ohne:.2f}; je KW mit {w_mit}, ohne {w_ohne}")
        return None


def _bankfrei_bis_heute(sz: Szenario) -> bool:
    """Zwischen Wochenbeginn und heute liegt kein Bankarbeitstag (heute = erster
    Bankarbeitstag der Woche oder davor)."""
    return sz.heute <= naechster_bankarbeitstag(sz.s0, sz.bundesland)


def _plantage_vor_heute(sz: Szenario) -> bool:
    """Ein Plantag (Position oder Verschiebung) liegt in [s0, heute)."""
    tage = {p.faellig for p in sz.posten} | {b.zahldatum for b in sz.bestellungen}
    tage |= {v.von for v in sz.verschiebungen} | {v.nach for v in sz.verschiebungen}
    for d in sz.dauer:
        tage |= set(rate_tage(d, sz.s0, sz.heute, sz.bundesland))
    return any(sz.s0 <= t < sz.heute for t in tage)


class I5(Invariante):
    """Ein Planstand friert den angezeigten Plan ein (KONZEPT §7b „Planstände“). Er
    zeigt die Sicht zum Stichtag (heute = s0): Überfälliges am ersten Bankarbeitstag ab
    s0. Der angezeigte Plan rollt Überfälliges auf den nächsten Bankarbeitstag ab heute –
    tageweise gleich sind beide daher nur, solange zwischen s0 und heute kein
    Bankarbeitstag und kein Plantag liegt (verschärfte Vorbedingung)."""
    name = "I5"
    titel = "Planstand = angezeigter Plan (je Zeile und Tag)"
    vorbedingung_text = ("Planstand zum aktuellen Wochenbeginn; kein Bankarbeitstag und kein "
                         "Plantag/Verschiebungstag in [s0, heute)")

    def vorbedingung(self, sz):
        if not _bankfrei_bis_heute(sz):
            return "Bankarbeitstag zwischen Wochenbeginn und heute"
        if _plantage_vor_heute(sz):
            return "Plantag zwischen Wochenbeginn und heute"
        return None

    def punkte(self, sz):
        return [("TAG", konto) for konto in sz.konten]

    def pruefe(self, aw, punkt):
        sz = aw.sz
        konto = punkt[1]
        a = aw.auf("frisch")
        snap = a.db.get(models.PlanSnapshot, a.frisch_id)
        eingefroren = {t: round(float(v), 2)
                       for t, v in liq.snapshot_fluesse(snap).get(a.konto_id[konto], {}).items()
                       if v}
        angezeigt = {date.fromisoformat(t): v
                     for t, v in aw.zeile(aw.plan(sz.s0, WOCHEN, "mit"), konto)["plan"].items()}
        abweichung = {t: (eingefroren.get(t, 0.0), angezeigt.get(t, 0.0))
                      for t in set(eingefroren) | set(angezeigt)
                      if abs(eingefroren.get(t, 0.0) - angezeigt.get(t, 0.0)) > TOLERANZ}
        if abweichung:
            return (f"Zeile {konto}: Planstand ≠ Plan an "
                    + ", ".join(f"{t:%d.%m.} ({e:.2f} statt {p:.2f})"
                                for t, (e, p) in sorted(abweichung.items())))
        return None


class I5w(I5):
    """Ergänzung: auch wochentags – solange der nächste Bankarbeitstag ab heute noch in
    der laufenden Woche liegt – stimmen Planstand und Plan je Zeile und Woche überein
    (Rollen verschiebt nur innerhalb der laufenden Woche)."""
    name = "I5w"
    titel = "Planstand = angezeigter Plan (je Zeile und Woche)"
    vorbedingung_text = ("Planstand zum aktuellen Wochenbeginn; nächster Bankarbeitstag ab heute "
                         "in der laufenden Woche")

    def vorbedingung(self, sz):
        if naechster_bankarbeitstag(sz.heute, sz.bundesland) > sz.s0 + timedelta(days=6):
            return "nächster Bankarbeitstag erst in der Folgewoche"
        return None

    def pruefe(self, aw, punkt):
        sz = aw.sz
        konto = punkt[1]
        a = aw.auf("frisch")
        snap = a.db.get(models.PlanSnapshot, a.frisch_id)

        def wochen(werte):
            s = defaultdict(float)
            for t, v in werte:
                s[liq.wochen_start(t)] += float(v)
            return {w: round(v, 2) for w, v in s.items() if abs(v) > TOLERANZ}

        e = wochen(liq.snapshot_fluesse(snap).get(a.konto_id[konto], {}).items())
        p = wochen((date.fromisoformat(t), v) for t, v in
                   aw.zeile(aw.plan(sz.s0, WOCHEN, "mit"), konto)["plan"].items())
        abw = {w: (e.get(w, 0.0), p.get(w, 0.0)) for w in set(e) | set(p)
               if abs(e.get(w, 0.0) - p.get(w, 0.0)) > TOLERANZ}
        if abw:
            return (f"Zeile {konto}: Planstand ≠ Plan in "
                    + ", ".join(f"KW {w.isocalendar().week} ({x:.2f} statt {y:.2f})"
                                for w, (x, y) in sorted(abw.items())))
        return None


class I6(Invariante):
    """Die LIVE-Basis ist „ab Montag der laufenden Woche ungerollt wie ein Planstand zu
    diesem Tag“ (KONZEPT §7b) und liest wie ein Planstand nur entschiedene Bestellungen –
    ein gerade zum Wochenbeginn eingefrorener Planstand muss dieselben Kennzahlen
    liefern. Offene Bestellungen ausgeschlossen (gekennzeichneter Anteil)."""
    name = "I6"
    titel = "LIVE = frischer Planstand"
    vorbedingung_text = "keine offene (nicht entschiedene) Bestellung"

    def vorbedingung(self, sz):
        if any(b.status == "OFFEN" for b in sz.bestellungen):
            return "offene Bestellung"
        return None

    def punkte(self, sz):
        return [("KZ", montag, konto) for montag in kw_montage(sz) for konto in sz.konten]

    def pruefe(self, aw, punkt):
        _, montag, konto = punkt
        live = aw.kz(montag, "LIVE", konto, "mit")
        frisch = aw.kz(montag, "FRISCH", konto, "frisch")
        kw = montag.isocalendar().week
        fehler = [f"KW {kw} Konto {konto} Ebene {name}: LIVE {_kurz(live[name])} | Planstand "
                  f"{_kurz(frisch[name])}"
                  for name in live if not name.startswith("_")
                  and abs(live[name]["verfuegbar"] - frisch[name]["verfuegbar"]) > 0.01]
        return "; ".join(fehler) or None


INVARIANTEN: dict[str, Invariante] = {
    i.name: i for i in (I1(), I1b(), I2(), I2b(), I3(), I4(), I5(), I5w(), I6())}


# ---------------------------------------------------------------------------
# Prüfen und Verkleinern
# ---------------------------------------------------------------------------

@dataclass
class Ergebnis:
    invariante: str
    anwendbar: bool
    grund: str | None = None
    punkte: int = 0
    verletzungen: list[tuple[tuple, str]] = field(default_factory=list)


def pruefen(sz: Szenario, namen=None, aw: Auswertung | None = None,
            erster_fehler: bool = False) -> dict[str, Ergebnis]:
    eigene = aw is None
    aw = aw or Auswertung(sz)
    ergebnisse = {}
    try:
        for name in namen or INVARIANTEN:
            inv = INVARIANTEN[name]
            grund = inv.vorbedingung(sz)
            if grund is not None:
                ergebnisse[name] = Ergebnis(name, False, grund)
                continue
            e = Ergebnis(name, True)
            for punkt in inv.punkte(sz):
                e.punkte += 1
                text = inv.pruefe(aw, punkt)
                if text:
                    e.verletzungen.append((punkt, text))
                    if erster_fehler:
                        break
            ergebnisse[name] = e
    finally:
        if eigene:
            aw.schliessen()
    return ergebnisse


def punkt_verletzt(sz: Szenario, name: str, punkt: tuple) -> str | None:
    inv = INVARIANTEN[name]
    if inv.vorbedingung(sz) is not None or not inv.punkt_gueltig(sz, punkt):
        return None
    aw = Auswertung(sz)
    try:
        return inv.pruefe(aw, punkt)
    except Exception:                            # Absturz zählt nicht als derselbe Befund
        return None
    finally:
        aw.schliessen()


def _kandidaten(sz: Szenario, punkt: tuple):
    """Verkleinerungsschritte: je ein Bestandteil weniger (zuerst die großen)."""
    for i in range(len(sz.posten)):
        yield replace(sz, posten=sz.posten[:i] + sz.posten[i + 1:])
    for i in range(len(sz.dauer)):
        yield replace(sz, dauer=sz.dauer[:i] + sz.dauer[i + 1:])
    for i in range(len(sz.bestellungen)):
        yield replace(sz, bestellungen=sz.bestellungen[:i] + sz.bestellungen[i + 1:])
    for i in range(len(sz.verschiebungen)):
        yield replace(sz, verschiebungen=sz.verschiebungen[:i] + sz.verschiebungen[i + 1:])
    for i in range(len(sz.budgets)):
        yield replace(sz, budgets=sz.budgets[:i] + sz.budgets[i + 1:])
    for k in sz.planstaende:
        yield replace(sz, planstaende=tuple(x for x in sz.planstaende if x != k),
                      referenz=None if sz.referenz == k else sz.referenz)
    if sz.referenz is not None:
        yield replace(sz, referenz=None)
    for i, p in enumerate(sz.posten):            # einzelne Zahlungen weglassen
        for j in range(len(p.zahlungen)):
            q = replace(p, zahlungen=p.zahlungen[:j] + p.zahlungen[j + 1:])
            yield replace(sz, posten=sz.posten[:i] + (q,) + sz.posten[i + 1:])
    for i, d in enumerate(sz.dauer):
        for j in range(len(d.bezahlt)):
            q = replace(d, bezahlt=d.bezahlt[:j] + d.bezahlt[j + 1:])
            yield replace(sz, dauer=sz.dauer[:i] + (q,) + sz.dauer[i + 1:])
        if not d.vortragen:
            yield replace(sz, dauer=sz.dauer[:i] + (replace(d, vortragen=True),) + sz.dauer[i + 1:])
    for i, b in enumerate(sz.bestellungen):
        if b.zahlungen:
            yield replace(sz, bestellungen=sz.bestellungen[:i] + (replace(b, zahlungen=()),)
                          + sz.bestellungen[i + 1:])
    benutzt = ({p.konto for p in sz.posten} | {d.konto for d in sz.dauer}
               | {b.konto for b in sz.bestellungen} | {v.konto for v in sz.verschiebungen}
               | {b.konto for b in sz.budgets} | {x for x in punkt if x in KONTEN})
    for k in sz.konten:
        if k not in benutzt and len(sz.konten) > 1:
            yield replace(sz, konten=tuple(x for x in sz.konten if x != k))
    if len(sz.fenster) > 1:
        yield replace(sz, fenster=sz.fenster[:1])
    for i, v in enumerate(sz.verschiebungen):    # Anlagetag auf den Standard
        if v.erstellt is not None:
            yield replace(sz, verschiebungen=sz.verschiebungen[:i] + (replace(v, erstellt=None),)
                          + sz.verschiebungen[i + 1:])
    for i, b in enumerate(sz.bestellungen):
        if b.angelegt is not None:
            yield replace(sz, bestellungen=sz.bestellungen[:i] + (replace(b, angelegt=None),)
                          + sz.bestellungen[i + 1:])
    for i, v in enumerate(sz.verschiebungen):    # Beträge vereinfachen
        for neu in _einfachere_betraege(sz, v):
            yield replace(sz, verschiebungen=sz.verschiebungen[:i] + (replace(v, betrag=neu),)
                          + sz.verschiebungen[i + 1:])


def _einfachere_betraege(sz: Szenario, v: Verschiebung):
    explizit = explizit_am_tag(sz, v.konto, v.von)
    if explizit and -explizit != v.betrag:
        yield -explizit


def verkleinere(sz: Szenario, name: str, punkt: tuple,
                max_schritte: int = 200) -> tuple[Szenario, str]:
    """Lässt Bestandteile weg, solange derselbe Prüfpunkt fehlschlägt."""
    text = punkt_verletzt(sz, name, punkt)
    if text is None:
        return sz, ""
    schritte = 0
    geaendert = True
    while geaendert and schritte < max_schritte:
        geaendert = False
        for kandidat in _kandidaten(sz, punkt):
            schritte += 1
            neu = punkt_verletzt(kandidat, name, punkt)
            if neu is not None:
                sz, text, geaendert = kandidat, neu, True
                break
            if schritte >= max_schritte:
                break
    return sz, text


# ---------------------------------------------------------------------------
# Lesbare Ausgabe und vermutete Ursache
# ---------------------------------------------------------------------------

WT = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def _d(t: date) -> str:
    return f"{WT[t.weekday()]} {t:%d.%m.%Y}"


def _rel(sz: Szenario, t: date) -> str:
    if t < sz.s0:
        return "vor Wochenbeginn"
    if t < sz.heute:
        return "laufende Woche, vergangen"
    if t == sz.heute:
        return "heute"
    return "künftig"


def beschreibe(sz: Szenario, punkt: tuple | None = None) -> str:
    z = [f"Seed {sz.seed}: heute {_d(sz.heute)} (KW {sz.heute.isocalendar().week}, s0 "
         f"{sz.s0:%d.%m.}), Bundesland {sz.bundesland}, Konten {', '.join(sz.konten)}, Bank "
         f"{BANK_ANFANG} am {sz.heute - timedelta(days=70):%d.%m.}"]
    for p in sz.posten:
        zahl = ", ".join(f"{b} am {_d(t)}" for t, b in p.zahlungen) or "keine Zahlung"
        status = "BEZAHLT" if p.bezahlt else ("OFFEN teilbezahlt" if p.zahlungen else "OFFEN")
        z.append(f"  OP{p.nr} {p.konto} {p.betrag} fällig {_d(p.faellig)} ({_rel(sz, p.faellig)})"
                 f" – {status}: {zahl}")
    for d in sz.dauer:
        bez = (", ".join(f"Rate {r:%d.%m.} bezahlt {_d(t)}" for r, t in d.bezahlt)
               or "keine Rate bezahlt")
        raten = ", ".join(f"{t:%d.%m.}" for t in rate_tage(
            d, sz.heute - timedelta(days=45), sz.heute + timedelta(days=30), sz.bundesland))
        z.append(f"  Dauer{d.nr} {d.konto} {d.betrag} monatlich am {d.stichtag}. "
                 f"(Raten {raten}){'' if d.vortragen else ', ohne Vortrag'} – {bez}")
    for b in sz.bestellungen:
        zahl = ", ".join(f"{x} am {_d(t)}{' abschließend' if a else ''}"
                         for t, x, a in b.zahlungen) or "keine Zahlung"
        z.append(f"  Best{b.nr} {b.konto} {b.brutto} brutto Zahldatum {_d(b.zahldatum)} "
                 f"{b.status} – {zahl}"
                 + (f" (angelegt {_d(b.angelegt)})" if b.angelegt else ""))
    for bw in sz.budgets:
        z.append(f"  Budget {bw.konto} KW {bw.montag.isocalendar().week} netto {bw.netto}")
    for v in sz.verschiebungen:
        anlage = ("unbekannt" if v.erstellt == UNBEKANNT
                  else _d(v.erstellt) if v.erstellt else "Vortag/heute")
        z.append(f"  V{v.nr} {v.konto} {_d(v.von)} ({_rel(sz, v.von)}) → {_d(v.nach)} "
                 f"({_rel(sz, v.nach)}) {v.betrag}, angelegt {anlage}")
    if sz.planstaende:
        z.append("  Planstände zum " + ", ".join(
            f"{sz.s0 - timedelta(days=7 * k):%d.%m.}{' (Referenz)' if sz.referenz == k else ''}"
            for k in sz.planstaende))
    if punkt:
        z.append(f"  Prüfpunkt {punkt}")
    return "\n".join(z)


def als_code(sz: Szenario) -> str:
    """Szenario als Python-Ausdruck (für benannte Tests)."""
    return repr(sz).replace("datetime.date", "date")


def _bezugsfenster(name: str, sz: Szenario, punkt: tuple) -> tuple[date, date]:
    """Fenster, in dem der Prüfpunkt rechnet (Freigabe: LIVE-Soll s0 … KW-Ende)."""
    if name == "I3":
        return _fenster(sz, punkt[1], punkt[2])
    if name in ("I1", "I1b", "I6") and punkt and isinstance(punkt[1], date):
        return sz.s0, punkt[1] + timedelta(days=6)
    if name == "I4":
        return sz.s0, sz.s0 + timedelta(days=6)
    return sz.s0, sz.horizont_ende


def _offene_plantage(sz: Szenario, konto: str) -> list[date]:
    tage = [p.faellig for p in sz.posten if p.konto == konto and not p.bezahlt]
    return tage + [b.zahldatum for b in sz.bestellungen if b.konto == konto and b.rest > 0]


def _bezahlt_an(sz: Szenario, konto: str, tag: date) -> list[date]:
    """Zahltage bezahlter Positionen mit Plantag `tag` (Posten, abgelöste Bestellungen)."""
    z = [p.bezahlt_am for p in sz.posten if p.konto == konto and p.faellig == tag and p.bezahlt]
    return z + [max(t for t, _, _ in b.zahlungen) for b in sz.bestellungen
                if b.konto == konto and b.zahldatum == tag and b.abgeloest and b.zahlungen]


URSACHEN = {
    "V1": ("V1 teilbezahlter überfälliger Posten: Rest der Verschiebung nimmt Budget des "
           "Ursprungstags"),
    "V1+": "V1-Variante: Verschiebung aus vergangener Woche trägt deren Budget in die Zukunft",
    "V2": "V2 verschobener Posten danach bezahlt: Plan führt ihn am Fensterbeginn und am Zieltag",
    "V2b": ("V2-Variante: Wochensicht des Ursprungstags legt einen dort bezahlten, früher "
            "fälligen Posten auf ihren ersten Bankarbeitstag – die Verschiebung nimmt ihn mit"),
    "V3": "V3 verschobene vergangene unbezahlte Rate zählt nicht als gebunden",
    "V4": "V4 Kette eines bezahlten Postens: Erledigtes am Zwischenziel nicht fortgeschrieben",
    "V5": "V5 Fenster in der Zukunft: Ursprung vor dem Fenster (nach heute) zählt doppelt",
    "V5b": ("neu (wie V5): Ursprung in künftiger Woche außerhalb des Fensters – deren "
            "Wochensicht rollt früher Fälliges auf ihren ersten Bankarbeitstag, die "
            "Verschiebung nimmt es doppelt mit"),
    "V6": ("V6 Pool je Konto: Verschiebung aus vergangenem Tag nimmt anderen überfälligen "
           "Posten mit"),
    "N2": ("neu: mehrere Verschiebungen vom selben Ursprungstag außerhalb des Fensters – der "
           "externe Planbetrag wird nicht vermindert, jede nimmt ihn voll"),
    "N3": ("neu: Kette über einen Tag außerhalb des Fensters – die Folgeverschiebung findet den "
           "dorthin verschobenen Betrag extern nicht (Betrag geht verloren)"),
    "N4": ("neu: Ziel bis heute verstrichen (z. B. vorgezogen, nicht bezahlt) – der offene Betrag "
           "fällt aus Restplan/gebunden/Überfälligem heraus statt zu rollen"),
    "N5": ("neu: Basis Planstand zieht vor s0 Überfälliges, solange der erste Bankarbeitstag "
           "der Woche bevorsteht, in späteren KWs als C3 ab – LIVE nicht (Korrektur nur LIVE)"),
    "N5b": ("neu: Verschiebung eines vor s0 Überfälligen auf den ersten Bankarbeitstag der "
            "Woche (= heute) – die C3-Korrektur der LIVE-Basis zieht ihn für spätere KWs "
            "trotzdem ab (C3 negativ)"),
    "N6": "neu: verschobene, trotzdem am Ursprungstag bezahlte Rate wird am Zieltag weiter geplant",
    "N8": ("neu: Verschiebung vom heutigen Rolltag eines überfälligen Postens – „fest“ nimmt "
           "den gerollten Betrag, der Überfälligen-Pool bleibt voll (gebunden negativ); die "
           "Montags-Sicht (Planstand, LIVE-Soll) findet am Rolltag nichts"),
    "N7": ("neu: Rate vor dem Wochenbeginn vorab bezahlt – LIVE-Soll führt sie in der laufenden "
           "Woche, Ist und Vortrag nicht (Minderausgabe als Spielraum)"),
}


def vermutete_ursache(name: str, sz: Szenario, punkt: tuple) -> str:
    """Zuordnung eines (minimalen) Szenarios zu den bekannten bzw. neuen Befunden –
    Heuristik über die Struktur, Reihenfolge = Vorrang; „unbekannt: …“ = kein Muster."""
    vs = sz.verschiebungen
    start, ende = _bezugsfenster(name, sz, punkt)
    aussen = lambda t: t < start or t > ende                                    # noqa: E731
    ketten = [(a, b) for a in vs for b in vs
              if a is not b and a.nach == b.von and a.konto == b.konto]
    if name == "I6" and not vs:
        return URSACHEN["N5"]
    if name == "I3" and start > sz.heute and any(sz.heute <= v.von < start <= v.nach for v in vs):
        return URSACHEN["V5"]
    s0_roll = naechster_bankarbeitstag(sz.s0, sz.bundesland)
    if name == "I1" and sz.heute <= s0_roll and any(
            v.nach == s0_roll and v.von < sz.s0 for v in vs):
        return URSACHEN["N5b"]
    ursprung = defaultdict(int)
    for v in vs:
        ursprung[(v.konto, v.von)] += 1
    if any(n > 1 and aussen(t) for (k, t), n in ursprung.items()):
        return URSACHEN["N2"]
    if any(aussen(a.nach) for a, b in ketten):
        return URSACHEN["N3"]
    for v in vs:
        if v.von > max(ende, sz.heute) and any(t < liq.wochen_start(v.von)
                                               for t in _offene_plantage(sz, v.konto)):
            return URSACHEN["V5b"]
    rolltag = naechster_bankarbeitstag(sz.heute, sz.bundesland)
    for v in vs:
        if v.von == rolltag and any(t < sz.heute for t in _offene_plantage(sz, v.konto)):
            return URSACHEN["N8"]
    if name in ("I2", "I2b"):                     # V4 betrifft die Restplan-Sicht
        for a, b in ketten:
            if _bezahlt_an(sz, a.konto, a.von):
                return URSACHEN["V4"]
    for v in vs:
        if v.nach <= sz.heute and (v.von > sz.heute or (
                v.von > v.nach and v.von in _offene_plantage(sz, v.konto))):
            return URSACHEN["N4"]
    for v in vs:
        if _bezahlt_an(sz, v.konto, v.von) and not ist_ratentag(sz, v.konto, v.von):
            return URSACHEN["V2"]
    for v in vs:
        w = liq.wochen_start(v.von)
        if v.von == naechster_bankarbeitstag(w, sz.bundesland) and any(
                p.konto == v.konto and p.bezahlt and p.faellig < w <= p.bezahlt_am
                for p in sz.posten):
            return URSACHEN["V2b"]
    for v in vs:
        if v.von <= sz.heute and ist_ratentag(sz, v.konto, v.von):
            if any(v.von in {r for r, _ in d.bezahlt} for d in sz.dauer if d.konto == v.konto):
                return URSACHEN["N6"]
            if any(t < sz.heute for t in _offene_plantage(sz, v.konto)):
                return URSACHEN["V6"]
            return URSACHEN["V3"]
    for v in vs:
        if v.von < sz.heute and any(t < sz.heute and t != v.von
                                    for t in _offene_plantage(sz, v.konto)):
            return URSACHEN["V6"]
    if sz.budgets and any(p.zahlungen and not p.bezahlt for p in sz.posten) and any(
            v.von < sz.heute for v in vs):
        return URSACHEN["V1"]
    if sz.budgets and any(v.von < sz.s0 for v in vs):
        return URSACHEN["V1+"]
    if any(t < r for d in sz.dauer for r, t in d.bezahlt):
        return URSACHEN["N7"]
    teile = []
    if not vs:
        teile.append("ohne Verschiebung")
    if sz.budgets:
        teile.append("Budget")
    if any(p.zahlungen and not p.bezahlt for p in sz.posten):
        teile.append("Teilzahlung")
    if any(b.zahlungen for b in sz.bestellungen):
        teile.append("Bestellung bezahlt")
    if any(v.nach <= sz.heute for v in vs):
        teile.append("Ziel bis heute")
    if any(v.von > sz.heute for v in vs):
        teile.append("künftiger Ursprung")
    return "unbekannt: " + (", ".join(teile) or "sonstiges")
