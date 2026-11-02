"""13-Wochen-Liquiditätsplanung.

Ist:   aus importierten Buchungen – liquiditätswirksam ist ein Satz, wenn genau eine
       Seite ein Finanzkonto (Bank/Kasse) ist; der Zahlungsfluss wird der Sachkonto-
       Seite zugeordnet (+ Einzahlung / − Auszahlung).
Plan:  offene Posten, Dauerbuchungen, Zahlungstermine, Budget (Restbudget-Logik).
       Überfällige offene Posten werden auf den nächsten Bankarbeitstag ab heute
       gerollt ("aktuelle Erwartung"); Snapshots frieren den Plan zum Stichtag ein
       und dienen als Soll-Basis für vergangene Tage.
Projektion: Die Bestandsfortschreibung nutzt eine "Restplan"-Sicht (nur offene
       Posten, nicht erledigte künftige Termine, künftige Dauerraten), damit bereits
       gezahlte Vorgänge nicht doppelt zählen.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from .. import models
from ..models import PostenArt, PostenStatus, Richtung
from .feiertage import ist_bankarbeitstag, naechster_bankarbeitstag

CENT = Decimal("0.01")
NULL_EUR = Decimal("0")

# Schlüssel einer Planzeile: int = konto_id, "TERMIN:<TYP>" = Termin ohne Konto,
# "NR:<nummer>" = Buchung auf unbekanntem Konto.
Key = int | str


def wochen_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _f(x: Decimal | None) -> float:
    return float((x or Decimal("0")).quantize(CENT))


def _brutto(netto: Decimal, ust_satz: Decimal | None) -> Decimal:
    if ust_satz is None:
        return netto
    return (netto * (Decimal("100") + ust_satz) / Decimal("100")).quantize(CENT)


def _lade_konten(db: Session, mandant_id: int) -> list[models.Konto]:
    """Alle Konten (auch inaktive) – Plandaten können auf deaktivierte Konten zeigen."""
    return list(
        db.scalars(
            select(models.Konto)
            .options(joinedload(models.Konto.gruppe))
            .where(models.Konto.mandant_id == mandant_id)
            .order_by(models.Konto.nummer)
        )
    )


def _fluss_auf_finanzkonto(b: models.Buchung, nummer: str) -> Decimal:
    """Bestandsänderung des Finanzkontos `nummer` durch Buchung b (+ = Zugang)."""
    if b.konto_nr == nummer:
        return b.betrag if b.sh == "S" else -b.betrag
    if b.gegenkonto_nr == nummer:
        return -b.betrag if b.sh == "S" else b.betrag
    return Decimal("0")


def ist_zahlungsfluesse(
    db: Session,
    mandant_id: int,
    finanz_nrn: set[str],
    von: date,
    bis: date,
) -> tuple[dict[str, dict[date, Decimal]], dict[str, dict[date, Decimal]]]:
    """Liefert (sachflüsse je Konto-Nr, Bestandsbewegungen je Finanzkonto-Nr)."""
    sach: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    bank: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    if von > bis:
        return sach, bank
    buchungen = db.scalars(
        select(models.Buchung).where(
            models.Buchung.mandant_id == mandant_id,
            models.Buchung.datum >= von,
            models.Buchung.datum <= bis,
        )
    )
    for b in buchungen:
        if b.konto_nr == b.gegenkonto_nr:
            continue  # Saldo Null, keine Aussagekraft
        if b.konto_nr in finanz_nrn:
            bank[b.konto_nr][b.datum] += _fluss_auf_finanzkonto(b, b.konto_nr)
        if b.gegenkonto_nr in finanz_nrn:
            bank[b.gegenkonto_nr][b.datum] += _fluss_auf_finanzkonto(b, b.gegenkonto_nr)
        fluss = _sachfluss(b, finanz_nrn)
        if fluss is not None:
            sach[fluss[0]][b.datum] += fluss[1]
    return sach, bank


def _sachfluss(b: models.Buchung, finanz_nrn: set[str]) -> tuple[str, Decimal] | None:
    """(Sachkonto-Nr, Zahlungsfluss) einer Buchung mit genau einer Finanzseite;
    der Fluss ist die Bestandsänderung der Finanzseite."""
    if b.konto_nr == b.gegenkonto_nr:
        return None
    k_fin = b.konto_nr in finanz_nrn
    g_fin = b.gegenkonto_nr in finanz_nrn
    if k_fin and not g_fin:
        return b.gegenkonto_nr, _fluss_auf_finanzkonto(b, b.konto_nr)
    if g_fin and not k_fin:
        return b.konto_nr, _fluss_auf_finanzkonto(b, b.gegenkonto_nr)
    return None


def _termin_key(t_typ: str, konto_id: int | None) -> Key:
    return konto_id if konto_id is not None else f"TERMIN:{t_typ}"


def _szenario_aufloeser(szenario: models.Szenario | None):
    """Faktor je Konto: Konto-Detailregel > Gruppen-Detailregel > globaler Faktor."""
    if szenario is None:
        eins = Decimal("1")
        return lambda konto, richtung: eins
    konto_regeln = {r.konto_id: r.faktor for r in szenario.regeln if r.konto_id}
    gruppen_regeln = {r.gruppe_id: r.faktor for r in szenario.regeln if r.gruppe_id}
    ein = szenario.ein_faktor / Decimal("100")
    aus = szenario.aus_faktor / Decimal("100")

    def faktor(konto: models.Konto | None, richtung: str) -> Decimal:
        if konto is not None:
            if konto.id in konto_regeln:
                return konto_regeln[konto.id] / Decimal("100")
            if konto.gruppe_id in gruppen_regeln:
                return gruppen_regeln[konto.gruppe_id] / Decimal("100")
        return ein if richtung == Richtung.EIN.value else aus

    return faktor


VERTEILUNGEN = (
    "GLEICH", "MONATSANFANG", "MONATSMITTE", "MONATSENDE",
    "WTAG_MO", "WTAG_DI", "WTAG_MI", "WTAG_DO", "WTAG_FR",
)
_WOCHENTAG = {"WTAG_MO": 0, "WTAG_DI": 1, "WTAG_MI": 2, "WTAG_DO": 3, "WTAG_FR": 4}
_verteilung_cache: dict[tuple[int, int, str, str], dict[date, Decimal]] = {}


def _monats_verteilung(jahr: int, monat: int, profil: str, bl: str) -> dict[date, Decimal]:
    """Anteil je Tag (Summe 1) für das Verteilungsprofil eines Monatsbudgets."""
    key = (jahr, monat, profil, bl)
    if key in _verteilung_cache:
        return _verteilung_cache[key]
    from .feiertage import bankarbeitstage_im_monat

    banktage = bankarbeitstage_im_monat(jahr, monat, bl)
    verteilung: dict[date, Decimal]
    if not banktage:
        verteilung = {}
    elif profil == "MONATSANFANG":
        verteilung = {banktage[0]: Decimal("1")}
    elif profil == "MONATSENDE":
        verteilung = {banktage[-1]: Decimal("1")}
    elif profil == "MONATSMITTE":
        mitte = min(banktage, key=lambda d: (abs(d.day - 15), d.day))
        verteilung = {mitte: Decimal("1")}
    elif profil in _WOCHENTAG:
        ziel = _WOCHENTAG[profil]
        zahltage: list[date] = []
        d = date(jahr, monat, 1)
        while d.month == monat:
            if d.weekday() == ziel:
                zahltag = naechster_bankarbeitstag(d, bl)
                if zahltag.month != monat:
                    zahltag = banktage[-1]  # Monatsende statt Überlauf in den Folgemonat
                if zahltag not in zahltage:
                    zahltage.append(zahltag)
            d += timedelta(days=1)
        anteil = Decimal("1") / len(zahltage) if zahltage else Decimal("0")
        verteilung = {t: anteil for t in zahltage}
    elif profil.startswith("KW"):
        # eine Zahlung je Kalenderwoche: Anteil der Woche = ihre Bankarbeitstage im
        # Monat ÷ alle Bankarbeitstage; Zahltag = Wochentag laut Konto („KW:4“ =
        # freitags, nächster Banktag der Woche) bzw. erster Bankarbeitstag der Woche
        wtag = int(profil[3:]) if ":" in profil else None
        wochen: dict[tuple[int, int], list[date]] = {}
        for t in banktage:
            wochen.setdefault(t.isocalendar()[:2], []).append(t)
        verteilung = {}
        for tage in wochen.values():
            if wtag is None:
                zahltag = tage[0]
            else:
                zahltag = next((t for t in tage if t.weekday() >= wtag), tage[-1])
            verteilung[zahltag] = verteilung.get(zahltag, Decimal("0")) + Decimal(len(tage)) / len(banktage)
    else:  # GLEICH
        anteil = Decimal("1") / len(banktage)
        verteilung = {t: anteil for t in banktage}
    _verteilung_cache[key] = verteilung
    return verteilung


# KW = eine Zahlung je Kalenderwoche, TAG = gleichmäßig auf die Bankarbeitstage,
# KONTO = ausdrücklich das Verteilungsprofil des Kontos; leer (ältere Werte) = Konto
AUFTEILUNGEN = ("KW", "TAG", "KONTO")


def budget_profil(konto: models.Konto, aufteilung: str | None) -> str:
    """Verteilungsprofil eines Budgetwerts: Aufteilung je KW bzw. Tag oder –
    bei KONTO bzw. ohne Angabe – das Verteilungsprofil des Kontos."""
    profil = konto.verteilung if konto.verteilung in VERTEILUNGEN else "GLEICH"
    if aufteilung == "TAG":
        return "GLEICH"
    if aufteilung == "KW":
        return f"KW:{_WOCHENTAG[profil]}" if profil in _WOCHENTAG else "KW"
    return profil


class MonatsBudgets:
    """Wirksame Monatsbudgets je Konto: erfasster Monatswert oder – bis zum
    nächsten erfassten Wert – der fortgeschriebene Wert eines Vormonats."""

    def __init__(self, budgets):
        self._je_konto: dict[int, list[tuple[tuple[int, int], models.Budget]]] = {}
        for b in budgets:
            self._je_konto.setdefault(b.konto_id, []).append(((b.jahr, b.monat), b))
        for liste in self._je_konto.values():
            liste.sort(key=lambda x: x[0])
        self._schluessel = {k: [m for m, _ in v] for k, v in self._je_konto.items()}

    @property
    def konto_ids(self) -> set[int]:
        return set(self._je_konto)

    def quelle(self, konto_id: int, jahr: int, monat: int) -> models.Budget | None:
        """Budgetsatz, der im Monat gilt (erfasst oder fortgeschrieben)."""
        import bisect

        schluessel = self._schluessel.get(konto_id)
        if not schluessel:
            return None
        i = bisect.bisect_right(schluessel, (jahr, monat)) - 1
        if i < 0:
            return None
        m, b = self._je_konto[konto_id][i]
        if m == (jahr, monat) or b.fortschreiben:
            return b
        return None

    def wert(self, konto_id: int, jahr: int, monat: int) -> tuple[Decimal, str | None] | None:
        b = self.quelle(konto_id, jahr, monat)
        return (b.betrag_netto, b.aufteilung) if b is not None else None


BESTELL_KEY = "TERMIN:BESTELLUNG"   # Planzeile für Bestellungen ohne Konto


def bestell_plan_status(mandant: models.Mandant, bestellungen: str | None) -> set[str] | None:
    """Bestellstatus, deren offener Rest geplant wird (ABGELOEST wird gesondert
    behandelt). None = gar keine Bestellungen ("KEINE")."""
    if bestellungen == "KEINE":
        return None
    if bestellungen == "FREIGEGEBEN":
        return {models.BestellStatus.FREIGEGEBEN.value}
    if bestellungen is None:
        status = {models.BestellStatus.FREIGEGEBEN.value}
        # Mandantenschalter (Standard an): auch noch nicht freigegebene einplanen
        if getattr(mandant, "bestellungen_offen_einplanen", True) is not False:
            status.add(models.BestellStatus.OFFEN.value)
        return status
    raise ValueError(f"Unbekannter Bestellfilter: {bestellungen}")


def bestellung_rest(b: models.Bestellung) -> Decimal:
    """Noch offener Bruttobetrag einer Bestellung: Brutto abzüglich der
    Teilzahlungen (nicht abschließende Ablösungen)."""
    teil = sum(
        (a.betrag for a in b.abloesungen if not a.abschliessend), Decimal("0")
    )
    return _brutto(b.betrag_netto, b.ust_satz) - teil


def plan_fluesse(
    db: Session,
    mandant: models.Mandant,
    start: date,
    ende: date,
    heute: date | None = None,
    nur_offen: bool = False,
    szenario: models.Szenario | None = None,
    gerollt: dict[Key, Decimal] | None = None,
    mit_budget: bool = True,
    bestellungen: str | None = None,
    ohne_bestellung_id: int | None = None,
    explizit: dict[Key, dict[date, Decimal]] | None = None,
    zuordnung: "Zuordnung | None" = None,
    verschoben: dict[Key, dict[date, Decimal]] | None = None,
    ueberschuss_ab: date | None = None,
) -> dict[Key, dict[date, Decimal]]:
    """Planzahlungen je Konto/Tag für das Fenster [start, ende].

    heute:     überfällige offene Posten werden auf den nächsten Bankarbeitstag ab
               max(start, heute) gerollt; ohne Angabe gilt start (Snapshot-Sicht).
    nur_offen: Projektionssicht für die Bestandsfortschreibung – nur noch nicht
               erfüllte Zahlungen; Vergangenheitstermine gelten als ausgeführt
               (das Ist bildet sie ab), überfällige offene Posten rollen strikt
               hinter heute.
    szenario:  Was-wäre-wenn-Faktoren (Einzahlungen, budgetbasierte Auszahlungen,
               Debitorenverzögerung); None = Basisplan.
    gerollt:   nimmt je Planschlüssel die auf den Rolltag gerollten überfälligen
               Posten-/Bestellbeträge auf.
    mit_budget: False = nur explizite Flüsse (ohne Budget/Restbudget).
    bestellungen: None = Mandantenstandard (freigegebene, bei
               `bestellungen_offen_einplanen` auch offene), "FREIGEGEBEN" = nur
               freigegebene, "KEINE" = gar keine; abgelöste stets wie bezahlte Posten.
    ohne_bestellung_id: diese Bestellung auslassen (Freigabeprüfung).
    explizit:  nimmt die expliziten Flüsse (ohne Budget, mit Verschiebungen) auf.
    zuordnung: Zuordnung der PLAN-Verschiebungen (None = ohne Verschiebungen): jeder
               verschobene Anteil steht in dieser Sicht, als läge sein Plantag am
               Zieltag; den Budgetanteil nimmt jede Sicht dem Bruttobudget des
               Ursprungstags und bucht ihn am Zieltag (dieselben Beträge in allen Sichten).
    verschoben: nimmt die Markierung verschobener Beträge auf (Änderung des Flusses:
               abgebende Tage gegenläufig, empfangende mit dem Vorzeichen des Flusses).
    ueberschuss_ab: erster Tag, der den Überschuss eines verschobenen Budgetanteils über den
               Tageswert tragen darf (Standard: heute, in der Projektion morgen) – vergangene
               Soll-Tage bleiben unverändert.
    """
    heute = heute or start
    if ueberschuss_ab is None:
        ueberschuss_ab = heute + timedelta(days=1) if nur_offen else heute
    konten = _lade_konten(db, mandant.id)
    konto_by_id = {k.id: k for k in konten}

    from .insolvenzgeld import igeld_fenster

    igeld = igeld_fenster(mandant)
    bestell_status = bestell_plan_status(mandant, bestellungen)
    plan, verbrauch = _explizite_fluesse(
        db, mandant, konto_by_id, start, ende, heute, nur_offen, szenario, gerollt,
        igeld, bestell_status, ohne_bestellung_id, zuordnung, verschoben,
    )
    if explizit is not None:
        for key, tageswerte in plan.items():
            for d, v in tageswerte.items():
                explizit[key][d] += v

    # 4) Budget mit Restbudget-Logik: verbraucht wird es von den expliziten Flüssen an
    #    ihrem Plantag ohne Verschiebungen (in der Projektion zusätzlich vom Erledigten der
    #    Woche, sonst bläht es sich neben dem Ist wieder auf); verschobene Budgetanteile
    #    (Zuordnung) fehlen dem Bruttobudget ihres Ursprungstags und stehen am Zieltag
    if mit_budget:
        budget: dict[Key, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        # ins Fenster verschobenes Budget aus Wochen nach dem Fenster: was diese Wochen in
        # dieser Sicht abgeben können, zeigt ihr Restbudget – dafür die expliziten Beträge bis
        # zum Ende der spätesten Ursprungswoche (gleiche Regeln, gleicher Fensterbeginn)
        budget_ende = ende
        if zuordnung is not None:
            for _, tag, _, _, herkunft in zuordnung.budget_fragmente():
                if start <= tag <= ende and herkunft:
                    budget_ende = max(budget_ende, wochen_start(max(herkunft)) + timedelta(days=6))
        if budget_ende > ende:
            _, verbrauch = _explizite_fluesse(
                db, mandant, konto_by_id, start, budget_ende, heute, nur_offen, szenario, None,
                igeld, bestell_status, ohne_bestellung_id, zuordnung, None)
        _budget_einarbeiten(db, mandant, konto_by_id, budget, start, budget_ende, szenario, igeld,
                            verbrauch=verbrauch, zuordnung=zuordnung, verschoben=verschoben,
                            ueberschuss_ab=ueberschuss_ab, ende_anzeige=ende)
        for key, tageswerte in budget.items():
            for d, v in tageswerte.items():
                if v and d <= ende:
                    plan[key][d] += v

    if nur_offen:
        # Projektionssicht: Finanz-, Info- und nicht liquiditätswirksame Konten raus
        for key in list(plan.keys()):
            if isinstance(key, int):
                k = konto_by_id.get(key)
                if k is None:
                    continue
                if not k.liquiditaetswirksam or k.typ in (
                    models.KontoTyp.BANK.value,
                    models.KontoTyp.KASSE.value,
                ):
                    del plan[key]
    return plan


def _skaliert(fragmente: list[tuple], betrag: Decimal) -> list[tuple]:
    """Fragmente (Tag, Grundbetrag ≥ 0, bewegt, …) einer Zuordnung auf den Betrag der
    Sicht (mit Vorzeichen) umgelegt – gleich, sofern kein Szenariofaktor wirkt."""
    gesamt = sum((f[1] for f in fragmente), Decimal("0"))
    if not gesamt:
        return []
    if gesamt == abs(betrag):
        vz = Decimal(1) if betrag >= 0 else Decimal(-1)
        return [(f[0], vz * f[1]) + tuple(f[2:]) for f in fragmente]
    ergebnis, verteilt = [], Decimal("0")
    for i, f in enumerate(fragmente):
        if i == len(fragmente) - 1:
            teil = betrag - verteilt
        else:
            teil = (betrag * f[1] / gesamt).quantize(CENT)
            verteilt += teil
        ergebnis.append((f[0], teil) + tuple(f[2:]))
    return ergebnis


def _zahlungen_vor(db: Session, mandant_id: int, start: date,
                   nur_konto: int | None = None) -> dict[int, Decimal]:
    """Je bezahltem Posten (bezahlt ab `start`, ggf. nur eines Kontos) die Zahlungen vor
    `start` (Ist-Buchung zum Posten bzw. OP-Ausgleich) – die volle Sicht führt nur den im
    Fenster bezahlten Teil."""
    ergebnis: dict[int, Decimal] = defaultdict(Decimal)
    im_fenster = select(models.OffenerPosten.id).where(
        models.OffenerPosten.mandant_id == mandant_id,
        models.OffenerPosten.status == PostenStatus.BEZAHLT.value,
        models.OffenerPosten.bezahlt_am >= start)
    if nur_konto is not None:
        im_fenster = im_fenster.where(models.OffenerPosten.konto_id == nur_konto)
    for pid, betrag in db.execute(
        select(models.Buchung.posten_id, models.Buchung.betrag).where(
            models.Buchung.mandant_id == mandant_id, models.Buchung.datum < start,
            models.Buchung.posten_id.in_(im_fenster))
    ):
        ergebnis[pid] += abs(betrag)
    for pid, betrag in db.execute(
        select(models.AusgleichZuordnung.posten_id, models.AusgleichZuordnung.betrag)
        .join(models.BankTransaktion,
              models.BankTransaktion.id == models.AusgleichZuordnung.transaktion_id)
        .where(models.AusgleichZuordnung.mandant_id == mandant_id,
               models.BankTransaktion.buchungstag < start,
               models.AusgleichZuordnung.posten_id.in_(im_fenster))
    ):
        ergebnis[pid] += abs(betrag)
    return ergebnis


def _ohne_vorab(fragmente: list[tuple], vorab: Decimal) -> list[tuple]:
    """Fragmente (Tag, Betrag, bewegt, …) ohne den vor dem Fenster gezahlten Teil – zuerst
    vom unverschobenen Rest am Plantag, dann von den verschobenen Anteilen."""
    if vorab <= 0:
        return fragmente
    ergebnis = []
    for f in sorted(fragmente, key=lambda f: f[2]):
        weg = min(vorab, f[1])
        vorab -= weg
        if f[1] - weg > 0:
            ergebnis.append((f[0], f[1] - weg) + tuple(f[2:]))
    return ergebnis


def _explizite_fluesse(
    db: Session,
    mandant: models.Mandant,
    konto_by_id: dict[int, models.Konto],
    start: date,
    ende: date,
    heute: date,
    nur_offen: bool,
    szenario: models.Szenario | None,
    gerollt: dict[Key, Decimal] | None,
    igeld: tuple[date, date] | None,
    bestell_status: set[str] | None,
    ohne_bestellung_id: int | None,
    zuordnung: "Zuordnung | None" = None,
    verschoben: dict[Key, dict[date, Decimal]] | None = None,
    angelegt_bis: datetime | None = None,
    daten_bis: date | None = None,
    posten_angelegt_bis: datetime | None = None,
    nur_konto: int | None = None,
    beitraege: list | None = None,
) -> tuple[dict[Key, dict[date, Decimal]], dict[Key, dict[date, Decimal]]]:
    """Explizite Planflüsse (Schritte 1–3b von plan_fluesse, ohne Budget).

    Liefert (plan, verbrauch): `plan` mit angewendeten Verschiebungen – jeder Bestandteil
    wird je Fragment der Zuordnung nach den Regeln dieser Sicht an seinem (ggf.
    verschobenen) Plantag geführt; `verbrauch` ohne Verschiebungen für das Restbudget,
    in der Projektion einschließlich des als erledigt Ausgelassenen an seinem Tag der
    vollen Sicht. angelegt_bis: Stand zu diesem Zeitpunkt – später angelegte Posten, Termine
    und Bestellungen fehlen (damals noch offene Bestellungen nur, wenn der Mandant offene
    einplant); posten_angelegt_bis: abweichende Grenze für Posten und Termine (wie `v_stand`
    der Zuordnung); daten_bis: Stand der Daten (ein Anlagezeitpunkt eines Postens oder
    Termins danach gilt als unbekannt, wie in der Zuordnung); nur_konto: nur die Flüsse dieses
    Kontos (Restbudget einer Zeile – Abfragen per SQL auf das Konto und das Fenster beschränkt);
    beitraege: nimmt je Objekt seinen Restbudget-Verbrauch auf (Zeile, Tag, Betrag, Objekt –
    None bei Dauerbuchungen –, Bestandteil und Plantag – None bei Raten und Terminen), ohne den
    Plan zu rechnen (nur `verbrauch` ist dann gültig)."""
    bl = mandant.bundesland

    def spaeter(obj) -> bool:
        if isinstance(obj, models.Bestellung):
            grenze, t = angelegt_bis, _zeitpunkt(obj.erstellt_am)
        else:
            grenze = posten_angelegt_bis if posten_angelegt_bis is not None else angelegt_bis
            t = _angelegt_bekannt(obj, daten_bis) if grenze is not None else None
        return grenze is not None and t is not None and t > grenze
    plan: dict[Key, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    verbrauch: dict[Key, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    z = zuordnung or Zuordnung.leer()

    from .insolvenzgeld import termin_entlastung

    szenario_faktor = _szenario_aufloeser(szenario)
    verzoegerung = szenario.debitoren_verzoegerung_tage if szenario else 0

    if nur_offen:
        roll_ziel = naechster_bankarbeitstag(max(start, heute + timedelta(days=1)), bl)
        grenze = max(start, heute + timedelta(days=1))
    else:
        roll_ziel = naechster_bankarbeitstag(max(start, heute), bl)
        grenze = max(start, heute)
    erster = naechster_bankarbeitstag(start, bl)   # Plantag vor dem Fenster: erster Bankarbeitstag

    def eintragen(key: Key, fragmente: list[tuple], ort, ort_natur: date | None) -> None:
        """Fragmente (Tag, Betrag, bewegt) an ihrem Ort dieser Sicht in den Plan; `ort`
        liefert (Tag | None, gerollt). Bewegte Fragmente werden markiert."""
        for tag, betrag, bewegt, *_ in fragmente:
            d, ist_gerollt = ort(tag)
            if d is not None:
                plan[key][d] += betrag
                if ist_gerollt and gerollt is not None:
                    gerollt[key] = gerollt.get(key, Decimal("0")) + betrag
            if bewegt and verschoben is not None and d != ort_natur:
                if ort_natur is not None:
                    verschoben[key][ort_natur] -= betrag
                if d is not None:
                    verschoben[key][d] += betrag

    def offen_ort(verz: int):
        def ort(tag: date) -> tuple[date | None, bool]:
            tag = tag + timedelta(days=verz)
            d = tag if tag >= grenze else roll_ziel
            return (d, d == roll_ziel and tag < d) if d <= ende else (None, False)
        return ort

    # Restbudget offener Posten und Bestellungen: in beiden Sichten am Tag der vollen Sicht –
    # Überfälliges am Rolltag ab max(start, heute); in der Projektion rollt es hinter heute
    # (freitags in die Folgewoche) und verbräuchte sonst deren Budget
    voll_grenze = max(start, heute)
    voll_roll = naechster_bankarbeitstag(voll_grenze, bl)

    def verbrauch_tag(tag: date, verz: int = 0) -> date | None:
        tag = tag + timedelta(days=verz)
        d = tag if tag >= voll_grenze else voll_roll
        return d if d <= ende else None

    def verbrauchend(ref: tuple, plantag: date, grundbetrag: Decimal, verz: int = 0) -> Decimal:
        """Betrag, der das Restbudget im Fenster verbraucht: liegt der Plantag vor dem Fenster,
        ohne die von ihrem Plantag weg verschobenen Anteile – sie haben das Budget ihrer
        vergangenen Ursprungswoche verbraucht (wie in der Sicht ab dem Ursprungstag). Vom
        Rolltag weg Verschobenes (damals schon überfällig) und Zurückverschobenes bleibt."""
        if plantag + timedelta(days=verz) >= start:
            return grundbetrag
        return max(Decimal("0"), grundbetrag - z.vom_plantag_weg(ref, plantag))

    # 1) Offene Posten (inkl. im Fenster bezahlter, für die Plan-zum-Start-Sicht); vor dem
    #    Fenster bezahlte zählen in keiner Sicht (auch nicht fürs Restbudget) – die Historie
    #    bleibt in der Datenbank
    op = models.OffenerPosten
    posten_bedingung = [
        op.mandant_id == mandant.id,
        op.status != PostenStatus.STORNIERT.value,
        (op.status != PostenStatus.BEZAHLT.value) | (op.bezahlt_am >= start),
    ]
    if nur_konto is not None:
        # eine Zeile: Plantag nach dem Fenster verbraucht nichts (Debitoren rücken nur später)
        posten_bedingung += [op.konto_id == nur_konto,
                             func.coalesce(op.zahlung_geplant_am, op.faellig_am) <= ende]
    posten = db.scalars(select(op).where(*posten_bedingung))
    # bezahlte Posten zeigt die volle Sicht nur mit ihren Zahlungen ab Fensterbeginn
    vor_fenster = {} if nur_offen or beitraege is not None else \
        _zahlungen_vor(db, mandant.id, start, nur_konto)
    for p in posten:
        # Zahlungssperre: Insolvenzforderungen (§ 38 InsO) werden nicht bedient
        if (
            p.art == PostenArt.KREDITOR.value
            and p.forderungsklasse == models.Forderungsklasse.INSOLVENZFORDERUNG.value
        ) or spaeter(p):
            continue
        zahltag = p.zahlung_geplant_am or p.faellig_am
        verz = verzoegerung if p.art == PostenArt.DEBITOR.value else 0
        key: Key = p.konto_id if p.konto_id is not None else f"TERMIN:OP_{p.art}"
        bezahlt = p.status == PostenStatus.BEZAHLT.value
        if bezahlt:
            if p.bezahlt_am is None or p.bezahlt_am < start:
                continue
            ref, grundbetrag = ("OP", p.id, "erledigt"), p.betrag_brutto

            def ort(tag: date, verz=verz) -> tuple[date | None, bool]:
                tag = tag + timedelta(days=verz)
                d = tag if tag >= start else erster
                return (d, False) if d <= ende else (None, False)
        else:  # OFFEN: nur den Restbetrag planen (Teilzahlungen sind abgezogen)
            grundbetrag = p.betrag_brutto - (p.bezahlt_betrag or Decimal("0"))
            ref, ort = ("OP", p.id, "offen"), offen_ort(verz)
        if grundbetrag <= 0:
            continue
        f = szenario_faktor(konto_by_id.get(p.konto_id), Richtung.EIN.value) \
            if p.art == PostenArt.DEBITOR.value else Decimal("-1")
        natur, _ = ort(zahltag)
        vtag = natur if bezahlt else verbrauch_tag(zahltag, verz)
        if vtag is not None:
            # Restbudget: bezahlte Posten verbrauchen in beiden Sichten mit ihrem vollen Betrag
            # (auch den vor dem Fenster gezahlten Teil), in der Projektion nur dafür
            anteil = (verbrauchend(ref, zahltag, grundbetrag, verz) * f).quantize(CENT)
            verbrauch[key][vtag] += anteil
            if beitraege is not None:
                beitraege.append((key, vtag, anteil, p, ref, zahltag + timedelta(days=verz)))
        if (nur_offen and bezahlt) or beitraege is not None:
            continue
        if bezahlt:
            # nur die Zahlungen ab Fensterbeginn: je Tranche der Zuordnung an ihrem Tag, ohne
            # Zuordnung vom Betrag am Plantag abgezogen
            fragmente = z.bezahlt_zwischen(ref, ab=start)
            if fragmente is None:
                vorab = min(vor_fenster.get(p.id, Decimal("0")), p.betrag_brutto)
                fragmente = _ohne_vorab(z.verteilung(ref, zahltag, grundbetrag), vorab)
            grundbetrag = sum((fr[1] for fr in fragmente), Decimal("0"))
            if grundbetrag <= 0:
                continue
        else:
            fragmente = z.verteilung(ref, zahltag, grundbetrag)
        eintragen(key, _skaliert(fragmente, (grundbetrag * f).quantize(CENT)), ort, natur)

    # 2) Dauerbuchungen: Raster mit Vorlauf erzeugen, erst verschieben, dann filtern,
    #    damit Raten nicht an der Fenstergrenze verloren gehen (z. B. 01.11. = Sonntag);
    #    dazu Raten außerhalb des Fensters, von denen Anteile verschoben sind
    dauer_bedingung = [models.Dauerbuchung.mandant_id == mandant.id,
                       models.Dauerbuchung.aktiv.is_(True)]
    if nur_konto is not None:
        dauer_bedingung.append(models.Dauerbuchung.konto_id == nur_konto)
    dauer = db.scalars(select(models.Dauerbuchung).where(*dauer_bedingung))
    for db_ in dauer:
        d_konto = konto_by_id.get(db_.konto_id) if db_.konto_id else None
        tage = {naechster_bankarbeitstag(roh, bl)
                for roh in _dauer_termine(db_, start - timedelta(days=31), ende)}
        tage = {d for d in tage if start <= d <= ende} | z.raten.get(db_.id, set())
        key = db_.konto_id if db_.konto_id is not None else f"TERMIN:DAUER_{db_.art}"
        for d in sorted(tage):
            if _rate_entfaellt(db_, d_konto, d, igeld):
                continue
            if db_.art == PostenArt.DEBITOR.value:
                f = szenario_faktor(d_konto, Richtung.EIN.value)
                betrag = (db_.betrag_brutto * f).quantize(CENT)
            else:
                betrag = -db_.betrag_brutto
            natur = d if start <= d <= ende else None
            if natur is not None:
                verbrauch[key][d] += betrag   # vergangen in der Projektion: nur fürs Restbudget
                if beitraege is not None:
                    beitraege.append((key, d, betrag, None, None, None))
            if beitraege is not None:
                continue
            for tag, teil, bewegt, zustand in _skaliert(
                    z.raten_verteilung(db_.id, d, db_.betrag_brutto), betrag):
                # Projektion: nur offene Raten nach heute (vergangene/bezahlte bildet das
                # Ist ab, nicht gebuchte der Vortrag)
                drin = start <= tag <= ende and not (
                    nur_offen and (zustand != "offen" or tag <= heute))
                ziel = tag if drin else None
                if ziel is not None:
                    plan[key][ziel] += teil
                if bewegt and verschoben is not None and ziel != natur and not nur_offen:
                    if natur is not None:
                        verschoben[key][natur] -= teil
                    if ziel is not None:
                        verschoben[key][ziel] += teil

    # 3) Zahlungstermine (SV/Steuern); betrag > 0 = Auszahlung
    bedingung = models.Zahlungstermin.datum.between(start, ende)
    if z.termin_ids:
        bedingung = bedingung | models.Zahlungstermin.id.in_(sorted(z.termin_ids))
    if nur_konto is not None:
        bedingung = bedingung & (models.Zahlungstermin.konto_id == nur_konto)
    termine = db.scalars(
        select(models.Zahlungstermin).where(
            models.Zahlungstermin.mandant_id == mandant.id, bedingung)
    )
    for t in termine:
        if spaeter(t):
            continue
        # erledigt ohne Erledigungsdatum: der ins Fenster verschobene Anteil steht am Zieltag
        # (z. B. gestundet und am neuen Termin bezahlt); ob die Zahlung vor dem Fenster lag,
        # prüft die Freigabe am Ist (`bestellungen._stand_gestern`)
        erledigt = t.status == models.TerminStatus.ERLEDIGT.value
        betrag = t.betrag
        if igeld:
            betrag = betrag - termin_entlastung(t, igeld)
            if betrag <= 0:
                continue
        key = _termin_key(t.typ, t.konto_id)
        natur = t.datum if start <= t.datum <= ende else None
        if natur is not None:
            verbrauch[key][natur] += -betrag   # erledigt in der Projektion: nur fürs Restbudget
            if beitraege is not None:
                beitraege.append((key, natur, -betrag, t, None, None))
        if beitraege is not None:
            continue
        ref = ("TERMIN", t.id, "erledigt" if erledigt else "offen")

        def ort(tag: date, erledigt=erledigt) -> tuple[date | None, bool]:
            if not start <= tag <= ende or (nur_offen and (erledigt or tag <= heute)):
                return None, False
            return tag, False

        eintragen(key, _skaliert(z.verteilung(ref, t.datum, abs(betrag)), -betrag),
                  ort, None if nur_offen else natur)

    # 3b) Bestellungen: brutto zum erwarteten Zahldatum, vor dem Budget (verbrauchen
    #     das Restbudget ihres Kontos); kein Szenariofaktor (wie Kreditor-Posten)
    if bestell_status is not None:
        abgeloest = models.BestellStatus.ABGELOEST.value
        best_bedingung = [models.Bestellung.mandant_id == mandant.id,
                          models.Bestellung.status.in_(sorted(bestell_status | {abgeloest}))]
        if nur_konto is not None:
            best_bedingung += [models.Bestellung.konto_id == nur_konto,
                               models.Bestellung.zahldatum <= ende]
        bestellungen = db.scalars(
            select(models.Bestellung)
            .options(selectinload(models.Bestellung.abloesungen))
            .where(*best_bedingung)
        )
        einplanen = models.BestellStatus.OFFEN.value in bestell_status
        for b in bestellungen:
            if b.id == ohne_bestellung_id or spaeter(b) or (
                    angelegt_bis is not None and not einplanen and b.entschieden_am is not None
                    and _zeitpunkt(b.entschieden_am) > angelegt_bis):
                continue
            key = b.konto_id if b.konto_id is not None else BESTELL_KEY
            if b.status == abgeloest:
                # wie ein bezahlter Posten: voller Betrag am Zahldatum (Plan − Ist zeigt
                # die Minderlieferung; früh gezahlt verbraucht sie weiter das Budget der
                # Zahlwoche), nie in der Projektion; durch eine Rechnung abgelöst trägt
                # der offene Posten die Zahlung
                if b.abgeloest_durch == models.AbloeseArt.RECHNUNG.value or b.abgeloest_am is None:
                    continue
                grundbetrag, ref = _brutto(b.betrag_netto, b.ust_satz), ("BEST", b.id, "erledigt")

                def ort(tag: date, b=b) -> tuple[date | None, bool]:
                    if b.abgeloest_am < start and tag < start:
                        return None, False
                    d = tag if tag >= start else erster
                    return (d, False) if d <= ende else (None, False)

                natur, _ = ort(b.zahldatum)
                if natur is not None:   # Projektion: nur fürs Restbudget
                    anteil = verbrauchend(ref, b.zahldatum, grundbetrag)
                    verbrauch[key][natur] -= anteil
                    if beitraege is not None:
                        beitraege.append((key, natur, -anteil, b, ref, b.zahldatum))
                if nur_offen or beitraege is not None:
                    continue
            else:
                grundbetrag = bestellung_rest(b)
                if grundbetrag <= 0:
                    continue
                ref, ort = ("BEST", b.id, "offen"), offen_ort(0)
                natur, _ = ort(b.zahldatum)
                vtag = verbrauch_tag(b.zahldatum)
                if vtag is not None:
                    anteil = verbrauchend(ref, b.zahldatum, grundbetrag)
                    verbrauch[key][vtag] -= anteil
                    if beitraege is not None:
                        beitraege.append((key, vtag, -anteil, b, ref, b.zahldatum))
                if beitraege is not None:
                    continue
            eintragen(key, _skaliert(z.verteilung(ref, b.zahldatum, grundbetrag), -grundbetrag),
                      ort, natur)
    return plan, verbrauch


def _dauer_termine(d: models.Dauerbuchung, start: date, ende: date) -> list[date]:
    von = max(start, d.gueltig_von)
    bis = min(ende, d.gueltig_bis) if d.gueltig_bis else ende
    if von > bis:
        return []
    termine: list[date] = []
    if d.intervall == models.Intervall.WOECHENTLICH.value:
        wt = max(0, min(6, d.stichtag))
        t = von + timedelta(days=(wt - von.weekday()) % 7)
        while t <= bis:
            termine.append(t)
            t += timedelta(days=7)
    else:
        schritt = {"MONATLICH": 1, "QUARTAL": 3, "JAEHRLICH": 12}.get(d.intervall, 1)
        # Ankermonat aus gueltig_von, damit Quartal/Jahr stabil rastert
        j, m = d.gueltig_von.year, d.gueltig_von.month
        while True:
            tag = min(max(1, d.stichtag), _monatstage(j, m))
            t = date(j, m, tag)
            if t > bis:
                break
            if t >= von:
                termine.append(t)
            idx = j * 12 + (m - 1) + schritt
            j, m = idx // 12, idx % 12 + 1
    return termine


# ---- Manuelle Verschiebungen von Planbeträgen ----
# Eine Verschiebung bewegt einen (Teil-)Betrag einer Planzeile vom Tag `von` auf den Tag
# `nach` (Vorzeichen wie der Zahlungsfluss). Sie wird einmal und unabhängig von der Sicht
# den Bestandteilen zugeordnet, die am Ursprungstag liegen (`Zuordnung`). Bezahlte Beträge
# zerfallen dabei in Tranchen je Zahlung (Ist-Buchung bzw. OP-Ausgleich zum Posten,
# Ablösungen einer Bestellung, angerechnete Zahlungen einer Rate), so dass für jede Tranche
# feststeht, ob sie beim Anlegen der Verschiebung (Anlagetag t) schon bezahlt war. Kandidat
# ist nur, was beim Anlegen eine Sicht in der Zelle zeigte (`_Teil.sichtbar_bei`): offene
# Reste, Teilzahlungen erst nach t, bezahlte Posten mit Zahlung ab dem Wochenbeginn von t,
# Raten, Termine, abgelöste Bestellungen (bis t gezahlte Teilzahlungen und vor dem Fenster
# bezahlte Posten nicht – die Höhe-Regel vergleicht nur Sichtbares; eine Teilzahlung ohne
# bekannten Zahltag nur, wenn erst der Bestandteil samt ihr der Verschiebung entspricht –
# dann war sie beim Anlegen offen; eine am Anlagetag selbst gezahlte gilt als vor der
# Verschiebung gezahlt, außer der Betrag übersteigt, was die Zelle dann gezeigt hätte, und
# passt erst mit ihr – der Zellendialog verschiebt höchstens den Zellwert, sie wurde also erst
# danach erfasst). Ein erst nach t ganz bezahlter Posten bzw. abgelöste
# Bestellung war beim Anlegen offen: seine bis t gezahlten Tranchen zählen wie Teilzahlungen
# (so ändert die spätere Schlusszahlung die Zuordnung nicht). Reihenfolge:
#   1. sichtbare, beim Anlegen noch offene Bestandteile: Posten mit Plantag `von`,
#      Dauerraten, Zahlungstermine und freigegebene Bestellungen mit Zahldatum `von` (auch
#      dorthin verschobene Anteile), je mit dem offenen Rest und den erst ab t bezahlten
#      Tranchen; bei einer Verschiebung vom Rolltag ihres Anlagetags (angelegt am Wochenende
#      oder Feiertag, Ursprung = nächster Bankarbeitstag) auch die damals überfälligen, nach t
#      noch offenen Posten und Bestellungen, die auf diesen Tag gerollt waren (am Anlagetag
#      schon Bezahltes stand bezahlt an seinem Plantag, nicht gerollt in der Zelle).
#      Ein Bestandteil, dessen beim Anlegen sichtbarer offener Betrag der Verschiebung
#      entspricht, zuerst (Teilbeträge mit gleicher Zeile, gleichem Ursprung und Ziel zählen
#      mit ihrem Gesamtbetrag und setzen beim schon gewählten Bestandteil fort), sonst
#      Überfälliges, dann Posten, Raten, Termine, Bestellungen; innerhalb eines Bestandteils
#      der offene Rest und die zuletzt bezahlten Tranchen zuerst,
#   2. dieselben Bestandteile, soweit sie beim Anlegen schon bezahlt (und sichtbar) waren,
#   3. Budget des Ursprungstags – zuerst dorthin verschobenes (zuletzt Gebrachtes zuerst), nur
#      soweit es beim Anlegen angezeigt war: was seine Herkunftswoche in der vollen Sicht am
#      Anlagetag nicht abgeben konnte (kein Restbudget, `_gedeckt`), stand nicht in der Zelle
#      und ist kein Kandidat; dann das Bruttobudget des Tages (vor dem Restbudget-Abzug,
#      gerundet wie der Plan); jeder Budgetanteil merkt sich seine Ursprungstage (Herkunft) und
#      jede Entnahme ihre Stelle in der Folge der Verschiebungen; zurück an einen Tag
#      verschobenes Budget gilt dort wieder als dessen Bruttobudget (nur der von dort stammende
#      Anteil – Budget anderer Herkunft füllt die Lücke nicht, sonst änderte es Herkunft und
#      Wirkung der Verschiebung, die dem Tag Budget entnommen hat). Lag beim Anlegen eine
#      noch nicht entschiedene Bestellung in der Zelle und übersteigt der Betrag das
#      Bruttobudget des Tages oder das, was die Woche beim Anlegen abgeben konnte (angezeigter
#      Tageswert und die übrigen Tage ab dem Anlagetag, `_angezeigtes_budget`), hat der Nutzer
#      (auch) sie verschoben: dann zuerst das damals angezeigte Budget (Restbudget der Woche im
#      Plan am Anlagetag, ohne später Angelegtes, nach den Entnahmen früherer Verschiebungen),
#      dann die Bestellung, erst ein Überschuss über die Zelle hinaus aus dem übrigen
#      Bruttobudget. Das Restbudget am Anlagetag rechnet die Zuordnung je Zeile und Anlagetag
#      einmal (`_beitraege`, nur diese Zeile) und filtert je Verschiebung später Angelegtes;
#      von einem Plantag vor dem Fenster weg Verschobenes (frühere Verschiebungen) verbraucht
#      wie in jeder Sicht nichts,
#   4. noch nicht entschiedene Bestellungen (wenn der Mandant sie einplant) sowie erst nach
#      der Verschiebung angelegte Posten, Termine (`erstellt_am`; unbekannt oder nach dem
#      Datenstand: alt) und Bestellungen bzw. erst nach ihr freigegebene: sie schaffen sich
#      keinen Rahmen und nehmen der Verschiebung kein Budget weg – außer Anteilen, die eine
#      frühere Verschiebung bewusst an den Ursprungstag gelegt hat (Hin- und Rückverschiebung).
#      Erst nach der Verschiebung Angelegtes nimmt sie nur, wenn ihr eigener Anlagezeitpunkt
#      unzuverlässig ist – sonst stand es beim Anlegen nicht in der Zelle (verliert sie ihren
#      Bestandteil, übernimmt ihr Rest keine später erfasste Rechnung).
#      Ist der Anlagezeitpunkt der Verschiebung unzuverlässig (nach heute), vergleichen Posten,
#      Termine und Bestellungen mit verlässlichen Zeitstempeln (nicht nach heute) mit dem Mittag
#      ihres angenommenen Anlagetags.
# Spätere Zahlungen ändern die Zuordnung nicht. Ist ein Bestandteil auf mehrere Tage verteilt,
# gehen seine beim Anlegen offenen Tranchen nach ihrer Zahlung chronologisch an die Teile in
# der Reihenfolge ihres Tags (früheste Fälligkeit zuerst, § 366 Abs. 2 BGB), der offene Rest
# an die spätesten – die Beträge je Tag bleiben, gleich in welcher Reihenfolge die
# Verschiebungen angelegt sind. Eine später angelegte Verschiebung sieht einen schon
# aufgeteilten Bestandteil so geordnet, wie der Plan ihn bei ihrer Anlage zeigte (Ordnung zum
# Anlagetag der früheren). Ist der Anlagetag unbekannt (nicht vor dem Ursprungstag oder
# nach heute), gilt der Vortag des Ursprungs (höchstens heute) als Anlagetag; Überfälliges
# zählt dann nur an seinem Plantag. Was eine Verschiebung auf einen Tag bringt, liegt dort für
# Folgeverschiebungen (Ketten); die Zuordnung merkt sich, welche Verschiebung von welcher
# Gebrachtes weiterverschoben hat (`folgen_von`) – die Rücknahme der ersten kürzt die Folgen
# um diesen Anteil (API). So ist jede Verschiebung auf den tatsächlich geplanten Betrag
# begrenzt – ändert sich die Planbasis (Posten storniert, Dauerbuchung beendet), entsteht
# kein Phantombetrag; der nicht zuordenbare Rest bleibt wirkungslos.
#
# Jede Sicht führt einen verschobenen Anteil nach ihren eigenen Regeln, als läge sein
# Plantag am Zieltag: offen – am Zieltag, ist der vorbei, auf dem Rolltag der Sicht
# (überfällig, in der Freigabe Überfälliges); erledigt – in der vollen Sicht am Zieltag
# (bezahlte Posten nur mit ihren Tranchen ab Fensterbeginn), nie in der Projektion;
# Teilzahlungen stehen in keiner Sicht. Die Freigabe („Stand gestern“) führt die Tranchen,
# die erst ab heute bezahlt sind, an ihrem Tag als noch offen, die vor s0 bezahlten
# abgelösten Bestellungen als fehlendes Ist – jeweils dieselben Tranchen wie der Plan; ein
# vor der Verschiebung eingefrorener Planstand führt den Anteil noch an seinem damaligen Tag
# (lag der vor dem Stichtag – überfällig –, am ersten Bankarbeitstag ab dem Stichtag) – ist
# der vorbei und der Anteil offen, zählt er dort wie Überfälliges (`nach_stand_verschoben`;
# begrenzt durch die Minderausgabe seines Tags und die Summe der Tage mit Minderausgabe
# seiner Zeile – eine Mehrausgabe an anderen Tagen erklärt sie nicht).
# Verschobene Raten sind mit ihrem offenen Anteil am Zieltag fällig (Vortrag), bezahlte nie.
# Budget: jede Sicht nimmt die bewegten Beträge je Entnahme in der Folge der Verschiebungen
# (`_wochen_abgabe`) zuerst vom angezeigten Tageswert des Ursprungstags (jede Entnahme sieht
# die Zelle wie bei ihrer Anlage – eine ganz verschobene Zelle ist leer, die übrigen Tage
# bleiben), einen Überschuss anteilig von den übrigen Tagen der Woche, die noch Budget zeigen,
# ab heute (`ueberschuss_ab`; vergangene Soll-Tage bleiben). Was die Woche in dieser Sicht nicht
# abgeben kann (kein Restbudget mehr), fehlt auch am Zieltag – je Herkunft gekürzt, am
# ältesten Anteil zuerst; die Summe jeder Sicht bleibt (keine Phantom-Ausgabe); liegt die
# Ursprungswoche nach dem Fenster,
# rechnet die Sicht deren Restbudget mit. Am Zieltag wird unabhängig vom dortigen Restbudget
# gebucht. Das Restbudget verbrauchen die expliziten Beträge an ihrem Tag ohne
# Verschiebungen, bezahlte Posten in beiden Sichten mit ihrem vollen Betrag, Überfälliges in
# beiden Sichten am Rolltag der vollen Sicht (ab heute, auch in der Projektion – sie
# unterscheidet sich darin nicht nach dem Wochentag); von ihrem Plantag vor dem Fenster
# weg verschobene Anteile eines Bestandteils verbrauchen im Fenster nichts (sie verbrauchten
# das Budget ihrer vergangenen Ursprungswoche).
# Zahlstatus der Raten (`_raten_zahlstatus`): einmal je Berechnung und unabhängig von den
# Verschiebungen – Raten ab Fensterbeginn bis heute (Zahlungen bis zum Zahlstand der
# Sicht), danach, nur mit den übrigen Zahlungen, Raten vor dem Fenster an einem
# Ursprungstag; zuletzt die auf einen Tag bis zum Zahlstand vorgezogenen Anteile künftiger
# Raten mit den dann noch freien Zahlungen ab ihrem Zieltag (der bezahlte Teil ist
# erledigt, der Rest offen). Raten nach dem Zahlstand (in der Freigabe auch die heute
# fällige) gelten sonst als offen. Zuordnung, Vortrag und Freigabe verwenden denselben
# Zahlstatus; der Zellendialog gleicht nur das Konto seiner Zeile ab (`nur_konten`).
# Die Zuordnung lädt nur Bestandteile der Zeilen mit Verschiebungen, die an einem
# Ursprungstag liegen oder auf einen Rolltag gerollt sein können (Auswahl per SQL, Zahlungen
# über eine Unterabfrage – keine ID-Listen, die Historie bleibt in der Datenbank).
# Vorträge (Quelle VORTRAG) wirken nur bis zu ihrem Zieltag – danach ist der noch
# offene Betrag wieder regulär vorzutragen; eine Folgeverschiebung vom Zieltag einer früheren
# nimmt nur, was dorthin verschoben liegt.

def verschiebung_key(v: models.PlanVerschiebung) -> Key:
    return v.konto_id if v.konto_id is not None else f"TERMIN:{v.termin_typ}"


def plan_verschiebungen(db: Session, mandant_id: int, quelle: str,
                        offen_ab: date | None = None) -> list:
    q = select(models.PlanVerschiebung).where(
        models.PlanVerschiebung.mandant_id == mandant_id,
        models.PlanVerschiebung.quelle == quelle,
    )
    if offen_ab is not None:
        q = q.where(models.PlanVerschiebung.nach > offen_ab)
    return list(db.scalars(q.order_by(models.PlanVerschiebung.id)))


def _zeitpunkt(t) -> datetime | None:
    """Zeitstempel mit Zeitzone (ohne Angabe: UTC, wie gespeichert)."""
    if not isinstance(t, datetime):
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def _zeitpunkt_verlaesslich(t: datetime | None, heute: date | None) -> bool:
    """Ein gespeicherter Zeitstempel ist mit dem Datenstand `heute` vergleichbar: nicht danach
    (sonst stammt er aus einem späteren Stand als die Rechnung)."""
    return t is not None and (heute is None or t.astimezone().date() <= heute)


def _angelegt_bekannt(obj, heute: date | None) -> datetime | None:
    """Anlagezeitpunkt eines Postens bzw. Termins, sofern verlässlich (`_zeitpunkt_verlaesslich`)
    – sonst, wie ohne Angabe, unbekannt (gilt als alt)."""
    t = _zeitpunkt(getattr(obj, "erstellt_am", None))
    return t if _zeitpunkt_verlaesslich(t, heute) else None


def _anlagetag(v) -> date | None:
    """Kalendertag der Anlage einer Verschiebung (Ortszeit des Servers wie date.today())."""
    t = getattr(v, "erstellt_am", None)
    if isinstance(t, datetime):
        return _zeitpunkt(t).astimezone().date()
    return t


def _rate_entfaellt(d_: models.Dauerbuchung, konto: models.Konto | None, tag: date,
                    igeld: tuple[date, date] | None) -> bool:
    """Insolvenzgeld: Personal-/SV-Dauerbuchungen (Kreditor) entfallen im Zeitraum
    (Nettolöhne über Igeld/Vorfinanzierung, SV-Beiträge nach § 175 SGB III)."""
    return bool(
        igeld
        and d_.art == PostenArt.KREDITOR.value
        and konto is not None
        and konto.typ in (models.KontoTyp.PERSONAL.value, models.KontoTyp.SV.value)
        and igeld[0] <= tag <= igeld[1]
    )


class _BudgetBrutto:
    """Bruttobudget je Konto und Tag im Basisplan, vor dem Restbudget-Abzug (wie
    `_budget_einarbeiten` je Kalenderwoche, Insolvenzgeld berücksichtigt) – Obergrenze des
    Budgetanteils einer Verschiebung."""

    def __init__(self, db: Session, mandant: models.Mandant,
                 konto_by_id: dict[int, models.Konto], igeld: tuple[date, date] | None):
        self._bl = mandant.bundesland
        self._konten = konto_by_id
        self._igeld = igeld
        self._monat = MonatsBudgets(
            db.scalars(select(models.Budget).where(models.Budget.mandant_id == mandant.id)))
        self._woche = {
            (bw.konto_id, bw.jahr, bw.kw): (bw.betrag_netto, bw.aufteilung)
            for bw in db.scalars(
                select(models.BudgetWoche).where(models.BudgetWoche.mandant_id == mandant.id))
        }
        self._tag = {
            (bt.konto_id, bt.datum): bt.betrag_netto
            for bt in db.scalars(
                select(models.BudgetTag).where(models.BudgetTag.mandant_id == mandant.id))
        }
        self.konto_ids = (self._monat.konto_ids | {k for (k, _, _) in self._woche}
                          | {k for (k, _) in self._tag})
        self._cache: dict[tuple[int, date], dict[date, Decimal]] = {}

    def richtung(self, konto_id: int) -> int | None:
        """Flussvorzeichen des Budgets eines Kontos (None: kein liquiditätswirksames Budget)."""
        konto = self._konten.get(konto_id)
        if (konto_id not in self.konto_ids or konto is None or not konto.aktiv
                or not konto.liquiditaetswirksam):
            return None
        richtung = _konto_richtung(konto)
        if richtung == Richtung.INFO.value:
            return None
        return 1 if richtung == Richtung.EIN.value else -1

    def tag(self, konto_id: int, d: date) -> tuple[Decimal, int] | None:
        """(Bruttobetrag ≥ 0, Flussvorzeichen) am Tag oder None."""
        vz = self.richtung(konto_id)
        if vz is None:
            return None
        b = self.woche(konto_id, wochen_start(d)).get(d)
        if not b or b <= 0:
            return None
        return b, vz

    def woche(self, konto_id: int, w: date) -> dict[date, Decimal]:
        """Bruttobudget je Tag der Woche ab `w` (gerundet wie der Plan)."""
        konto = self._konten.get(konto_id)
        if self.richtung(konto_id) is None:
            return {}
        if (konto_id, w) not in self._cache:
            basis = {
                t: _brutto(quelle, konto.ust_satz) * anteil / teiler
                for t, (quelle, anteil, teiler) in budget_basis_woche(
                    konto, w, w + timedelta(days=6), self._bl, self._monat, self._woche,
                    self._tag).items()
            }
            if self._igeld and konto.typ in (models.KontoTyp.PERSONAL.value,
                                             models.KontoTyp.SV.value):
                basis = {t: b for t, b in basis.items()
                         if not self._igeld[0] <= t <= self._igeld[1]}
            # Tageswerte mit derselben Rundung wie der Plan (Rest auf den letzten Tag)
            self._cache[(konto_id, w)] = _verteile(
                basis, sum(basis.values(), Decimal("0")).quantize(CENT))
        return self._cache[(konto_id, w)]


def _posten_zahlungen(db: Session, mandant_id: int,
                      posten_ids) -> dict[int, list[tuple[date, Decimal]]]:
    """Einzelne Zahlungen (Tag, Betrag ≥ 0) je Posten: Ist-Buchungen zum Posten und
    OP-Ausgleich mit Bankumsätzen. `posten_ids`: Auswahl der Posten als Unterabfrage (keine
    literale ID-Liste – die Zahl der Bind-Parameter ist begrenzt) oder kurze Liste."""
    ergebnis: dict[int, list[tuple[date, Decimal]]] = defaultdict(list)
    if isinstance(posten_ids, (list, tuple, set)) and not posten_ids:
        return ergebnis
    for pid, tag, betrag in db.execute(
        select(models.Buchung.posten_id, models.Buchung.datum, models.Buchung.betrag)
        .where(models.Buchung.mandant_id == mandant_id, models.Buchung.posten_id.in_(posten_ids))
    ):
        ergebnis[pid].append((tag, abs(betrag)))
    for pid, tag, betrag in db.execute(
        select(models.AusgleichZuordnung.posten_id, models.BankTransaktion.buchungstag,
               models.AusgleichZuordnung.betrag)
        .join(models.BankTransaktion,
              models.BankTransaktion.id == models.AusgleichZuordnung.transaktion_id)
        .where(models.AusgleichZuordnung.mandant_id == mandant_id,
               models.AusgleichZuordnung.posten_id.in_(posten_ids))
    ):
        ergebnis[pid].append((tag, abs(betrag)))
    return ergebnis


def _tranchen(zahlungen, gesamt: Decimal, rest_tag: date | None) -> list[tuple[date | None, Decimal]]:
    """Bezahlter Betrag `gesamt` in Tranchen je Zahlung (chronologisch, bis `gesamt`); ein
    nicht durch Zahlungen belegter Rest mit `rest_tag` (None = unbekannt)."""
    ergebnis: list[tuple[date | None, Decimal]] = []
    summe = Decimal("0")
    for tag, betrag in sorted(zahlungen, key=lambda z: z[0]):
        teil = min(betrag, gesamt - summe)
        if teil <= 0:
            break
        ergebnis.append((tag, teil))
        summe += teil
    if gesamt > summe:
        ergebnis.append((rest_tag, gesamt - summe))
    return ergebnis


_RANG = {"OP": 0, "DAUER": 1, "TERMIN": 2, "BEST": 3}
_ZUSTAND_FOLGE = {"offen": 0, "teil": 1, "erledigt": 2}
_OHNE_DATUM = date.max          # Zahltag unbekannt, gilt als nach der Anlage (erledigte Termine)
_TAGE_LITERAL = 200             # bis zu so vielen Ursprungstagen als Liste, sonst Unterabfrage


class _Teil:
    """Tranche eines Bestandteils an seinem natürlichen Plantag. zustand: offen | erledigt |
    teil (Teilzahlung – in keiner Sicht geführt); zahltag: Tag der Zahlung dieser Tranche
    (None = unbekannt, gilt als vor der Anlage und vor jedem Fenster); rollbar: war überfällig
    auf den Rolltag gerollt (Posten, Bestellungen); spaet: erst nach dem Budget (nicht
    entschiedene Bestellung); angelegt/entschieden: Anlage des Postens, Termins bzw. der
    Bestellung und Freigabe der Bestellung (nach der Verschiebung: spät; unbekannt: alt);
    ordnung: Art, Bestandteil, Zustand, später Gezahltes vor früher Gezahltem (so liegen die
    beim Anlegen offenen Beträge vorn – auch nachdem sie bezahlt sind); abschluss: Tag, an dem
    ein bezahlter Posten bzw. eine abgelöste Bestellung ganz bezahlt war (vorher war er offen,
    seine bis dahin gezahlten Tranchen waren Teilzahlungen)."""
    __slots__ = ("ref", "key", "tag", "vz", "zustand", "zahltag", "rollbar", "spaet", "ordnung",
                 "angelegt", "entschieden", "abschluss", "verlaesslich")

    def __init__(self, ref, key, tag, vz, zustand, zahltag=None, rollbar=False, spaet=False,
                 angelegt=None, entschieden=None, abschluss=None, verlaesslich=False):
        self.ref, self.key, self.tag, self.vz = ref, key, tag, vz
        self.zustand, self.zahltag, self.rollbar, self.spaet = zustand, zahltag, rollbar, spaet
        self.angelegt = angelegt          # Anlage (nach der Verschiebung: spät)
        self.entschieden = entschieden    # Freigabe der Bestellung (nach der Verschiebung: spät)
        self.abschluss = abschluss        # ganz bezahlt bzw. abgelöst am (nur erledigt)
        # Anlage und Freigabe einer Bestellung liegen nicht nach dem Datenstand (vergleichbar mit
        # dem angenommenen Anlagezeitpunkt einer Verschiebung)
        self.verlaesslich = verlaesslich
        self.ordnung = (_RANG[ref[0]], ref[1:-1], _ZUSTAND_FOLGE[zustand],
                        -zahltag.toordinal() if zahltag is not None else 1)

    def spaet_fuer(self, v_angelegt: datetime | None, v_stand: datetime | None = None) -> bool:
        """Erst nach dem Budget: nicht entschiedene Bestellung sowie nach der Verschiebung
        angelegte Posten, Termine und Bestellungen bzw. erst nach ihr freigegebene Bestellungen
        (beim Anlegen gab es sie nicht oder sie war offen). Posten und Termine vergleichen mit
        `v_stand` (der Anlage, ist sie unzuverlässig, mittags am angenommenen Anlagetag),
        Bestellungen ebenso, sofern ihre eigenen Zeitstempel verlässlich sind (sonst stammen
        beide aus einem späteren Stand und werden unmittelbar verglichen)."""
        if self.spaet:
            return True
        if v_stand is not None and (self.ref[0] in ("OP", "TERMIN") or self.verlaesslich):
            v_angelegt = v_stand
        if v_angelegt is None:
            return False
        return any(t is not None and t > v_angelegt for t in (self.angelegt, self.entschieden))

    def da_bei(self, v_angelegt: datetime | None, einplanen: bool,
               v_stand: datetime | None = None) -> bool:
        """Stand beim Anlegen der Verschiebung schon in der Zelle: bis dahin angelegt; eine
        damals noch nicht entschiedene Bestellung nur, wenn der Mandant offene einplant."""
        if v_stand is not None and (self.ref[0] in ("OP", "TERMIN") or self.verlaesslich):
            v_angelegt = v_stand
        if v_angelegt is not None and self.angelegt is not None and self.angelegt > v_angelegt:
            return False
        if self.ref[0] == "BEST" and (self.spaet or (
                v_angelegt is not None and self.entschieden is not None
                and self.entschieden > v_angelegt)):
            return einplanen
        return True

    def offen_bei(self, t: date) -> bool:
        """Beim Anlegen am Tag t noch offen (erst ab t bezahlt)."""
        return self.zustand == "offen" or (self.zahltag is not None and self.zahltag >= t)

    def sichtbar_bei(self, t: date, w0: date) -> bool:
        """Beim Anlegen am Tag t in einer Sicht der Zelle zu sehen (Fenster ab w0): offene
        Reste; Teilzahlungen nur, wenn sie erst nach dem Anlagetag gezahlt sind (bis dahin sind
        sie vom offenen Rest abgezogen) – ebenso die Tranchen eines Postens bzw. einer
        Bestellung, die erst nach dem Anlagetag ganz bezahlt bzw. abgelöst war (beim Anlegen
        offen: vor bzw. am Anlagetag Gezahltes war Teilzahlung); bezahlte Posten mit einer
        Zahlung ab Fensterbeginn (die volle Sicht führt nur diese); Raten, Termine und
        abgelöste Bestellungen stets."""
        if self.zustand == "offen":
            return True
        if self.zustand == "teil" or (self.abschluss is not None and self.abschluss > t):
            return self.zahltag is not None and self.zahltag > t
        if self.ref[0] == "OP":
            return self.zahltag is not None and self.zahltag >= w0
        return True


class Zuordnung:
    """Sichtunabhängige Zuordnung der PLAN-Verschiebungen zu Planbestandteilen (siehe
    „Manuelle Verschiebungen“). Je Berechnung einmal erstellt und allen Sichten übergeben,
    damit angezeigter Plan, Projektion, Planstand, Vortrag und Freigabe-Kennzahlen dieselben
    Bestandteile bewegen und denselben Zahlstatus der Raten verwenden.

    heute: Stand der Daten; zahlungen_bis: bis hierhin zählen Ist-Zahlungen für den
    Zahlstatus der Raten (Freigabe: Stand gestern); abgleich_ab: Beginn des Ratenabgleichs
    (Fensterbeginn der Berechnung, Standard: Montag der laufenden Woche); nur_konten: der
    Ratenabgleich nur für diese Konten (Zellendialog einer Zeile – er rechnet je Sachkonto)."""

    def __init__(self, db: Session | None, mandant: models.Mandant | None,
                 heute: date | None, verschiebungen: list | None = None,
                 zahlungen_bis: date | None = None, abgleich_ab: date | None = None,
                 nur_konten: set[int] | None = None):
        self.heute = heute
        self.zahlungen_bis = zahlungen_bis or heute
        self.abgleich_ab = abgleich_ab or (wochen_start(heute) if heute else None)
        self.nur_konten = nur_konten
        self._db, self._mandant = db, mandant
        if verschiebungen is None:
            verschiebungen = plan_verschiebungen(db, mandant.id, "PLAN") if db is not None else []
        self.verschiebungen = [v for v in verschiebungen if v.betrag and v.von != v.nach]
        # Bestandteil → Fragmente [Tag, Betrag (≥ 0), bewegt] (je Tranche eines am Plantag)
        self._fragmente: dict[tuple, list[list]] = {}
        self._zahltag: dict[int, date | None] = {}        # id(Fragment) → Zahltag der Tranche
        self.budget_anspruch: dict[int, Decimal] = {}     # Verschiebung → bewegtes Budget
        self.rest: dict[int, Decimal] = {}                # Verschiebung → nicht zuordenbarer Rest
        # je (Konto, Tag) dem Bruttobudget entnommen (netto nach Rückverschiebungen)
        self.budget_genommen: dict[tuple[int, date], Decimal] = defaultdict(Decimal)
        # dieselben Entnahmen [Konto, Tag, Betrag] in der Folge der Verschiebungen (jede Sicht
        # nimmt sie in dieser Folge vom angezeigten Tageswert, `_wochen_abgabe`)
        self.budget_entnahmen: list[list] = []
        # verschobenes Budget [Konto, Tag, Betrag ≥ 0, vz, Herkunft {Ursprungstag: Betrag}]
        self._budget_frag: list[list] = []
        self.budget_bewegungen: list[tuple[int, date, date, Decimal]] = []
        # (Verschiebung, frühere Verschiebung) → von dieser dorthin gebrachter, weiter
        # verschobener Betrag (Ketten; Rücknahme der früheren kürzt die Folgeverschiebung)
        self.entnommen: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
        self._kette: dict[int, tuple[int, ...]] = {}      # id(Fragment) → bringende Verschiebungen
        # id(Fragment) → Weg ((Verschiebung, Tag, an dem das Fragment lag, Ursprungstag der
        # Verschiebung), …) – vom Rolltag genommenes Überfälliges lag an seinem Plantag
        self._weg: dict[int, tuple[tuple[int, date, date], ...]] = {}
        self._ref_info: dict[tuple, tuple[Key, int, bool]] = {}   # Zeile, vz, im Soll der Freigabe
        self._v_zeit = {v.id: _zeitpunkt(getattr(v, "erstellt_am", None))
                        for v in self.verschiebungen}
        self.raten: dict[int, set[date]] = {}             # Dauerbuchung → Plantage verschobener Raten
        self.termin_ids: set[int] = set()                 # Termine mit verschobenen Anteilen
        self._status: dict | None = None
        self._status_zusatz: dict[int, set[date]] = {}    # Raten vor dem Abgleichsbeginn
        self._vorgezogen: dict[tuple[int, date], list[tuple[date, Decimal]]] = {}
        # (Konto, Anlagetag) → Woche → Restbudget-Verbrauch je Objekt (Tag, Betrag, Zeitprüfung)
        self._beitraege_cache: dict[tuple, dict[date, list]] = {}
        self._ersetzt: list[list] = []                    # umgeordnete Fragmente (ids belegt)
        if self.verschiebungen:
            self._zuordnen()

    @classmethod
    def leer(cls) -> "Zuordnung":
        return cls(None, None, None, [])

    # -- Abfragen der Sichten --

    def verteilung(self, ref: tuple, tag: date, betrag: Decimal) -> list[tuple[date, Decimal, bool]]:
        """Fragmente (Tag, Betrag ≥ 0, bewegt) eines Bestandteils; ohne Verschiebung
        der ganze Betrag am Plantag."""
        frag = self._fragmente.get(ref)
        if frag is None:
            return [(tag, abs(betrag), False)]
        return [(f[0], f[1], f[2]) for f in frag if f[1] > 0]

    def bezahlt_zwischen(self, ref: tuple, ab: date | None = None,
                         vor: date | None = None) -> list[tuple[date, Decimal, bool]] | None:
        """Fragmente (Tag, Betrag, bewegt) der Tranchen eines bezahlten Bestandteils, deren
        Zahlung in [ab, vor) liegt (unbekannter Zahltag: vor jedem Tag); None = der Bestandteil
        ist nicht zugeordnet (dann gilt der ganze Betrag am Plantag)."""
        frag = self._fragmente.get(ref)
        if frag is None:
            return None
        ergebnis = []
        for f in frag:
            t = self._zahltag.get(id(f)) or date.min
            if f[1] > 0 and (ab is None or t >= ab) and (vor is None or t < vor):
                ergebnis.append((f[0], f[1], f[2]))
        return ergebnis

    def raten_status(self) -> dict[tuple[int, date], tuple[Decimal, list]]:
        """Zahlstatus der Raten (mit Konto) laut `_raten_zahlstatus`: je (Dauerbuchung,
        Plantag) (bezahlter Betrag, [(Zahltag, Betrag)])."""
        if self._status is None:
            self._status = {} if self._db is None or self.abgleich_ab is None else \
                _raten_zahlstatus(self._db, self._mandant, self.abgleich_ab, self.heute,
                                  self._status_zusatz, zahlungen_bis=self.zahlungen_bis,
                                  vorgezogen=self._vorgezogen, nur_konten=self.nur_konten)
        return self._status

    def _bezahlt(self, dauer_id: int, tag: date) -> tuple[Decimal, list[tuple[date, Decimal]]]:
        """(bezahlter Betrag, [(Zahltag, Betrag)]) einer Rate laut Zahlstatus; Raten nach dem
        Zahlstand der Sicht (Freigabe: auch die heute fällige) gelten an ihrem Plantag als offen."""
        if self._db is None or tag > self.zahlungen_bis:
            return Decimal("0"), []
        return self.raten_status().get((dauer_id, tag), (Decimal("0"), []))

    def raten_verteilung(self, dauer_id: int, tag: date,
                         betrag: Decimal) -> list[tuple[date, Decimal, bool, str]]:
        """Fragmente (Tag, Betrag ≥ 0, bewegt, zustand) einer Rate mit Plantag `tag`; ohne
        Verschiebung offener und bezahlter Anteil (Zahlstatus) am Plantag."""
        teile = [(z, self._fragmente.get(("DAUER", dauer_id, tag, z))) for z in ("offen", "erledigt")]
        if all(f is None for _, f in teile):
            soll = abs(betrag)
            bezahlt = min(self._bezahlt(dauer_id, tag)[0], soll)
            if not bezahlt:
                return [(tag, soll, False, "offen")]
            return ([(tag, soll - bezahlt, False, "offen")] if soll > bezahlt else []) + [
                (tag, bezahlt, False, "erledigt")]
        return [(f[0], f[1], f[2], z) for z, frag in teile if frag for f in frag if f[1] > 0]

    def budget_fragmente(self) -> list[tuple[int, date, Decimal, int, dict[date, Decimal]]]:
        """Verschobene Budgetbeträge an ihrem (letzten) Zieltag: (Konto, Tag, Betrag ≥ 0, vz,
        Herkunft {Ursprungstag: Betrag})."""
        return [(k, t, b, vz, q) for k, t, b, vz, q, _ in self._budget_frag if b > 0]

    def nach_stand_verschoben(self, zeitpunkt: datetime | None, s0: date, bis: date,
                              heute: date) -> list[tuple[Key, date, date, Decimal]]:
        """Auszahlungsanteile, die erst nach `zeitpunkt` (Anlage eines Planstands) verschoben
        wurden: der Planstand führt sie noch an ihrem damaligen Tag in [s0, bis], jetzt liegen
        sie an einem Tag ab heute und sind beim Stand gestern noch offen. (Zeile, damaliger
        Tag, Tag, Betrag). Lag ein offener Posten bzw. eine Bestellung damals vor s0 (beim
        Stichtag überfällig, später vom Rolltag verschoben), führt ihn der Planstand – wie
        jede Sicht zum Stichtag – am ersten Bankarbeitstag ab s0."""
        if zeitpunkt is None:
            return []
        s0_roll = naechster_bankarbeitstag(s0, self._mandant.bundesland)
        ergebnis = []
        for ref, frags in self._fragmente.items():
            info = self._ref_info.get(ref)
            if info is None or info[1] > 0 or not info[2]:
                continue
            zustand = ref[-1]
            if ref[0] == "TERMIN" and zustand == "erledigt":
                continue      # ohne Zahltag – die Termin-Prüfung der Freigabe sieht das Ist
            for f in frags:
                if not f[2] or f[1] <= 0 or f[0] < heute:
                    continue
                zahltag = self._zahltag.get(id(f))
                if zustand != "offen" and (zahltag is None or zahltag < heute):
                    continue
                damals = None
                for vid, tag, _ in self._weg.get(id(f), ()):
                    t = self._v_zeit.get(vid)
                    if t is not None and t > zeitpunkt:
                        damals = tag
                        break
                if damals is not None and damals < s0 and ref[0] in ("OP", "BEST"):
                    damals = s0_roll      # im Planstand überfällig auf den Rolltag gerollt
                if damals is not None and s0 <= damals <= bis:
                    ergebnis.append((info[0], damals, f[0], f[1]))
        return ergebnis

    def vom_plantag_weg(self, ref: tuple, plantag: date) -> Decimal:
        """Betrag der Fragmente eines Bestandteils, die eine Verschiebung von seinem Plantag
        weg auf einen anderen Tag gelegt hat (nicht vom Rolltag, nicht zurück)."""
        summe = Decimal("0")
        for f in self._fragmente.get(ref, ()):
            weg = self._weg.get(id(f))
            if f[2] and f[1] > 0 and f[0] != plantag and weg and weg[0][2] == plantag:
                summe += f[1]
        return summe

    def folgen_von(self, verschiebung_id: int) -> dict[int, Decimal]:
        """Folgeverschiebungen, die von `verschiebung_id` Gebrachtes weiterverschoben haben:
        Verschiebung → Betrag."""
        return {w: b for (w, u), b in self.entnommen.items() if u == verschiebung_id and b > 0}

    # -- Zuordnung --

    def _anlage(self, v) -> date | None:
        """Anlagetag, sofern verlässlich (vor dem Ursprungstag und nicht nach heute)."""
        t = _anlagetag(v)
        return t if t is not None and t < v.von and (self.heute is None or t <= self.heute) else None

    def _zuordnen(self) -> None:
        db, m = self._db, self._mandant
        bl = m.bundesland
        eintag = timedelta(days=1)
        einplanen = getattr(m, "bestellungen_offen_einplanen", True) is not False
        urspruenge: dict[Key, set[date]] = defaultdict(set)
        # Verschiebungen vom Rolltag ihrer Anlage: Überfälliges vor diesem Tag war dorthin gerollt
        rolltag_schwelle: dict[int, date] = {}
        roll_grenze: dict[Key, date] = {}
        # Anlagetag je Verschiebung; unbekannt: spätestmöglich (Vortag des Ursprungs, höchstens heute)
        anlage = {v.id: self._anlage(v) for v in self.verschiebungen}
        stand = {v.id: anlage[v.id] or min(v.von - eintag, self.heute) for v in self.verschiebungen}
        for v in self.verschiebungen:
            key = verschiebung_key(v)
            urspruenge[key].add(v.von)
            t = stand[v.id]
            if t < v.von and naechster_bankarbeitstag(t, bl) == v.von:
                rolltag_schwelle[v.id] = t
                roll_grenze[key] = max(roll_grenze.get(key, t), t)
        teile: list[tuple[_Teil, Decimal]] = []

        def neu(teil: _Teil, betrag: Decimal) -> None:
            if betrag > 0:
                teile.append((teil, betrag))

        def relevant(key: Key, tag: date) -> bool:
            return tag in urspruenge.get(key, ()) or (key in roll_grenze and tag < roll_grenze[key])

        # nur Zeilen mit Verschiebungen laden (Konten per SQL, Zeilen ohne Konto gesondert) und
        # nur Bestandteile, die an einem Ursprungstag liegen oder auf einen Rolltag gerollt sein
        # können – per SQL, damit eine lange Historie in der Datenbank bleibt
        konto_ids = sorted(k for k in urspruenge if isinstance(k, int))
        alle_tage = sorted({t for tage in urspruenge.values() for t in tage})
        if len(alle_tage) <= _TAGE_LITERAL:
            tage_auswahl = alle_tage
        else:
            tage_auswahl = select(models.PlanVerschiebung.von).where(
                models.PlanVerschiebung.mandant_id == m.id,
                models.PlanVerschiebung.quelle == "PLAN").distinct()
        roll_max = max(roll_grenze.values()) if roll_grenze else None
        schwelle_min = min(rolltag_schwelle.values()) if rolltag_schwelle else None

        def nur_zeilen(spalte, ohne_konto: bool):
            bedingung = spalte.in_(konto_ids)
            return (bedingung | spalte.is_(None)) if ohne_konto else bedingung

        op = models.OffenerPosten
        plantag = func.coalesce(op.zahlung_geplant_am, op.faellig_am)
        lage = plantag.in_(tage_auswahl)
        if roll_max is not None:
            # vor dem Rolltag: nur, was beim Anlegen noch offen gewesen sein kann
            lage = lage | ((plantag < roll_max) & (
                (op.status != PostenStatus.BEZAHLT.value) | op.bezahlt_am.is_(None)
                | (op.bezahlt_am >= schwelle_min)))
        posten_bedingung = (op.mandant_id == m.id, op.status != PostenStatus.STORNIERT.value,
                            nur_zeilen(op.konto_id,
                                       any(str(k).startswith("TERMIN:OP_") for k in urspruenge)),
                            lage)
        posten = [p for p in db.scalars(select(op).where(*posten_bedingung))
                  if not (p.art == PostenArt.KREDITOR.value and p.forderungsklasse
                          == models.Forderungsklasse.INSOLVENZFORDERUNG.value)
                  and relevant(p.konto_id if p.konto_id is not None else f"TERMIN:OP_{p.art}",
                               p.zahlung_geplant_am or p.faellig_am)]
        zahlungen = _posten_zahlungen(db, m.id, select(op.id).where(
            *posten_bedingung, (op.status == PostenStatus.BEZAHLT.value) | (op.bezahlt_betrag != 0)))
        for p in posten:
            key: Key = p.konto_id if p.konto_id is not None else f"TERMIN:OP_{p.art}"
            tag = p.zahlung_geplant_am or p.faellig_am
            vz = 1 if p.art == PostenArt.DEBITOR.value else -1
            angelegt = _angelegt_bekannt(p, self.heute)
            if p.status == PostenStatus.BEZAHLT.value:
                tranchen = _tranchen(zahlungen.get(p.id, ()), p.betrag_brutto, p.bezahlt_am)
                abschluss = p.bezahlt_am or max((z for z, _ in tranchen if z is not None),
                                                default=None)
                for zahltag, betrag in tranchen:
                    neu(_Teil(("OP", p.id, "erledigt"), key, tag, vz, "erledigt", zahltag, True,
                              angelegt=angelegt, abschluss=abschluss), betrag)
            else:
                gezahlt = min(p.bezahlt_betrag or Decimal("0"), p.betrag_brutto)
                neu(_Teil(("OP", p.id, "offen"), key, tag, vz, "offen", rollbar=True,
                          angelegt=angelegt), p.betrag_brutto - gezahlt)
                for zahltag, betrag in _tranchen(zahlungen.get(p.id, ()), gezahlt, None):
                    neu(_Teil(("OP", p.id, "teil"), key, tag, vz, "teil", zahltag, True,
                              angelegt=angelegt), betrag)

        abgeloest = models.BestellStatus.ABGELOEST.value
        status = (bestell_plan_status(m, None) or set()) | {abgeloest}
        best = models.Bestellung
        b_lage = best.zahldatum.in_(tage_auswahl)
        if roll_max is not None:
            b_lage = b_lage | (best.zahldatum < roll_max)
        for b in db.scalars(select(best)
                            .options(selectinload(best.abloesungen))
                            .where(best.mandant_id == m.id, best.status.in_(sorted(status)),
                                   nur_zeilen(best.konto_id, BESTELL_KEY in urspruenge), b_lage)):
            key = b.konto_id if b.konto_id is not None else BESTELL_KEY
            if not relevant(key, b.zahldatum):
                continue
            brutto = _brutto(b.betrag_netto, b.ust_satz)
            zeiten = {"angelegt": _zeitpunkt(b.erstellt_am),
                      "entschieden": _zeitpunkt(b.entschieden_am)}
            zeiten["verlaesslich"] = all(z is None or _zeitpunkt_verlaesslich(z, self.heute)
                                         for z in zeiten.values())
            if b.status == abgeloest:
                if b.abgeloest_durch == models.AbloeseArt.RECHNUNG.value or b.abgeloest_am is None:
                    continue
                for zahltag, betrag in _tranchen([(a.datum, a.betrag) for a in b.abloesungen],
                                                 brutto, b.abgeloest_am):
                    neu(_Teil(("BEST", b.id, "erledigt"), key, b.zahldatum, -1, "erledigt",
                              zahltag, True, abschluss=b.abgeloest_am, **zeiten), betrag)
            else:
                spaet = b.status != models.BestellStatus.FREIGEGEBEN.value
                rest = bestellung_rest(b)
                neu(_Teil(("BEST", b.id, "offen"), key, b.zahldatum, -1, "offen",
                          rollbar=True, spaet=spaet, **zeiten), rest)
                for zahltag, betrag in _tranchen(
                        [(a.datum, a.betrag) for a in b.abloesungen if not a.abschliessend],
                        min(brutto, brutto - rest), None):
                    neu(_Teil(("BEST", b.id, "teil"), key, b.zahldatum, -1, "teil", zahltag,
                              True, spaet, **zeiten), betrag)

        from .insolvenzgeld import igeld_fenster, termin_entlastung

        igeld = igeld_fenster(m)
        for t in db.scalars(select(models.Zahlungstermin).where(
                models.Zahlungstermin.mandant_id == m.id,
                models.Zahlungstermin.datum.in_(tage_auswahl))):
            key = _termin_key(t.typ, t.konto_id)
            if t.datum not in urspruenge.get(key, ()):
                continue
            betrag = t.betrag
            if igeld:
                betrag = betrag - termin_entlastung(t, igeld)
                if betrag <= 0:
                    continue
            erledigt = t.status == models.TerminStatus.ERLEDIGT.value
            zustand = "erledigt" if erledigt else "offen"
            neu(_Teil(("TERMIN", t.id, zustand), key, t.datum, -1 if betrag > 0 else 1, zustand,
                      _OHNE_DATUM if erledigt else None,
                      angelegt=_angelegt_bekannt(t, self.heute)), abs(betrag))

        konto_by_id = {k.id: k for k in _lade_konten(db, m.id)}
        raten: list[tuple[models.Dauerbuchung, date, Key]] = []
        mit_konto: set[int] = set()
        for d_ in db.scalars(select(models.Dauerbuchung).where(
                models.Dauerbuchung.mandant_id == m.id, models.Dauerbuchung.aktiv.is_(True))):
            if d_.konto_id is not None:
                mit_konto.add(d_.id)
            key = d_.konto_id if d_.konto_id is not None else f"TERMIN:DAUER_{d_.art}"
            tage = urspruenge.get(key)
            if not tage:
                continue
            konto = konto_by_id.get(d_.konto_id) if d_.konto_id else None
            for roh in _dauer_termine(d_, min(tage) - timedelta(days=31), max(tage)):
                d = naechster_bankarbeitstag(roh, bl)
                if d in tage and not _rate_entfaellt(d_, konto, d, igeld):
                    raten.append((d_, d, key))
                    if d_.konto_id is not None and d < self.abgleich_ab:
                        self._status_zusatz.setdefault(d_.id, set()).add(d)
        for d_, d, key in raten:
            vz = 1 if d_.art == PostenArt.DEBITOR.value else -1
            bezahlt, gezahlt = self._bezahlt(d_.id, d) if d_.konto_id is not None \
                else (Decimal("0"), [])
            bezahlt = min(bezahlt, d_.betrag_brutto)
            neu(_Teil(("DAUER", d_.id, d, "offen"), key, d, vz, "offen"), d_.betrag_brutto - bezahlt)
            for zahltag, betrag in _tranchen(gezahlt, bezahlt, None):
                neu(_Teil(("DAUER", d_.id, d, "erledigt"), key, d, vz, "erledigt", zahltag), betrag)

        # Fragmente und Lage je (Zeile, Tag)
        liegt: dict[tuple[Key, date], list[list]] = defaultdict(list)
        ueber: dict[Key, dict[tuple, None]] = defaultdict(dict)   # rollbare Bestandteile
        teil_von: dict[int, _Teil] = {}
        for teil, betrag in teile:
            frag = [teil.tag, betrag, False]
            self._ref_info.setdefault(teil.ref, (teil.key, teil.vz, not teil.spaet))
            self._fragmente.setdefault(teil.ref, []).append(frag)
            self._zahltag[id(frag)] = teil.zahltag
            teil_von[id(frag)] = teil
            liegt[(teil.key, teil.tag)].append(frag)
            if teil.rollbar and teil.key in roll_grenze:
                ueber[teil.key][teil.ref] = None
        budget: list[_BudgetBrutto] = []                 # erst bei Bedarf geladen
        budget_liegt: dict[tuple[int, date], list[list]] = defaultdict(list)
        # Bestandteil → spätester Anlagetag der Verschiebungen, die von ihm genommen haben (nur
        # danach Bezahltes war beim Anlegen jeder dieser Verschiebungen offen)
        anlage_max: dict[tuple, date] = {}
        # seit der letzten Ordnung nach Fälligkeit bewegte Bestandteile → Zeile
        ungeordnet: dict[tuple, Key] = {}

        def nehmen(v, key: Key, kandidaten: list[list], rest: Decimal) -> Decimal:
            for f in kandidaten:
                if rest <= 0:
                    break
                anteil = min(rest, f[1])
                if anteil <= 0:
                    continue
                f[1] -= anteil
                rest -= anteil
                teil = teil_von[id(f)]
                neu_frag = [v.nach, anteil, True]
                self._fragmente[teil.ref].append(neu_frag)
                self._zahltag[id(neu_frag)] = self._zahltag.get(id(f))
                teil_von[id(neu_frag)] = teil
                kette = self._kette.get(id(f), ())
                for u in kette:
                    self.entnommen[(v.id, u)] += anteil
                self._kette[id(neu_frag)] = kette + (v.id,)
                self._weg[id(neu_frag)] = self._weg.get(id(f), ()) + ((v.id, f[0], v.von),)
                liegt[(key, v.nach)].append(neu_frag)
                ident = teil.ref[:-1]
                anlage_max[ident] = max(anlage_max.get(ident, stand[v.id]), stand[v.id])
                ungeordnet[ident] = key
                if teil.ref[0] == "DAUER":
                    self.raten.setdefault(teil.ref[1], set()).add(teil.ref[2])
                elif teil.ref[0] == "TERMIN":
                    self.termin_ids.add(teil.ref[1])
            return rest

        # Teilbeträge (gleiche Zeile, gleicher Ursprung und Ziel) wirken wie das Ganze: „in
        # Höhe“ zählt der Gesamtbetrag der Gruppe, Folgeteile setzen beim gewählten Bestandteil fort
        gruppen_ziel: dict[tuple, Decimal] = defaultdict(Decimal)
        for v in self.verschiebungen:
            gruppen_ziel[(verschiebung_key(v), v.von, v.nach, 1 if v.betrag > 0 else -1)] += \
                abs(v.betrag)
        gruppen_wahl: dict[tuple, set] = defaultdict(set)
        for v in self.verschiebungen:
            key = verschiebung_key(v)
            vz = 1 if v.betrag > 0 else -1
            rest = abs(v.betrag)
            t = stand[v.id]
            w0 = wochen_start(t)
            # von früher angelegten Verschiebungen aufgeteilte Bestandteile der Zeile zuerst nach
            # Fälligkeit ordnen – so lagen ihre Tranchen, als diese Verschiebung angelegt wurde
            # (eine seitdem gezahlte Tranche liegt am frühesten Teil, nicht dort, wo die
            # Anlagefolge sie hingelegt hat)
            for ident in [i for i, k in ungeordnet.items() if k == key and anlage_max[i] < t]:
                del ungeordnet[ident]
                self._faellig_ordnen(ident, anlage_max[ident], teil_von, liegt)
            v_angelegt = _zeitpunkt(getattr(v, "erstellt_am", None))
            # Vergleich mit Posten/Terminen: unzuverlässige Anlage (nach dem Datenstand) wie
            # der angenommene Anlagetag, mittags
            v_stand = v_angelegt if _zeitpunkt_verlaesslich(v_angelegt, self.heute) else \
                datetime.combine(t, datetime.min.time().replace(hour=12)).astimezone()
            # (Fragment, Klasse 0 = beim Anlegen offen / 1 = schon bezahlt, überfällig)
            kandidaten: list[tuple[list, int, bool]] = []
            unsichtbar: list[list] = []      # beim Anlegen in keiner Sicht der Zelle
            unbekannt: list[tuple[list, bool]] = []   # Teilzahlung ohne bekannten Zahltag
            am_anlagetag: list[tuple[list, bool]] = []   # Teilzahlung am Anlagetag selbst
            spaet_da: list[list] = []        # spät eingestuft, beim Anlegen in der Zelle
            spaet: list[list] = []           # erst nach der Verschiebung angelegt/freigegeben

            def einordnen(f: list, ueberfaellig: bool) -> None:
                teil = teil_von[id(f)]
                sichtbar = teil.sichtbar_bei(t, w0)
                # dorthin verschobene Anteile hat der Nutzer bewusst an den Tag gelegt: nie spät
                if teil.spaet_fuer(v_angelegt, v_stand) and not f[2]:
                    (spaet_da if sichtbar and teil.da_bei(v_angelegt, einplanen, v_stand)
                     else spaet).append(f)
                elif not sichtbar:
                    if teil.zustand == "teil" and teil.zahltag is None:
                        unbekannt.append((f, ueberfaellig))
                    elif teil.zahltag == t:
                        am_anlagetag.append((f, ueberfaellig))
                    else:
                        unsichtbar.append(f)
                else:
                    kandidaten.append((f, 0 if teil.offen_bei(t) else 1, ueberfaellig))

            for f in liegt.get((key, v.von), ()):
                if f[1] > 0 and teil_von[id(f)].vz == vz:
                    einordnen(f, False)
            schwelle = rolltag_schwelle.get(v.id)
            if schwelle is not None:
                # vom Rolltag der Anlage: die damals überfälligen, noch offenen Posten und
                # Bestellungen standen beim Anlegen in der Zelle (ohne bekannten Anlagetag nur an
                # ihrem Plantag, nicht dorthin Verschobenes – ob dessen Ziel beim Anlegen schon
                # vorbei war, ist unbekannt)
                for ref in ueber.get(key, ()):
                    for f in self._fragmente[ref]:
                        teil = teil_von[id(f)]
                        # schon am Zieltag Liegendes (z. B. ein früherer Teilbetrag dieser
                        # Gruppe) bliebe, wo es ist – es verbrauchte die Verschiebung nur
                        # am Anlagetag schon Bezahltes stand nicht gerollt in der Zelle (die
                        # volle Sicht zeigt es bezahlt an seinem Plantag)
                        offen = teil.zustand == "offen" or (
                            teil.zahltag is not None and teil.zahltag > schwelle)
                        if (f[1] > 0 and teil.vz == vz and f[0] < schwelle and f[0] != v.nach
                                and (anlage[v.id] or not f[2]) and offen):
                            einordnen(f, True)
            budget_kontext: list = []

            def kontext() -> tuple | None:
                """(Bruttobudget, Sicht am Anlagetag, gedeckter Teil des dorthin verschobenen
                Budgets) – je Verschiebung einmal, nur für Budgetzeilen."""
                if not isinstance(key, int):
                    return None
                if not budget_kontext:
                    if not budget:
                        budget.append(_BudgetBrutto(db, m, konto_by_id, igeld))
                    bb = budget[0]
                    # dorthin verschobenes Budget nur, soweit es beim Anlegen angezeigt war
                    # (gedeckt vom Restbudget seiner Herkunftswoche in der vollen Sicht am
                    # Anlagetag) – ein ungedeckter Anteil stand nicht in der Zelle
                    sicht = (t, v_angelegt, v_stand, konto_by_id, igeld, {})
                    gedeckt = {id(f): self._gedeckt(f, key, bb, sicht)
                               for f in budget_liegt.get((key, v.von), ())
                               if f[3] == vz and f[2] > 0}
                    budget_kontext.append((bb, sicht, gedeckt))
                return budget_kontext[0]

            if am_anlagetag:
                # am Anlagetag gezahlte Teilzahlung: gilt als vor der Verschiebung gezahlt (vom
                # offenen Rest abgezogen, in keiner Sicht) – außer der Betrag übersteigt, was die
                # Zelle dann zeigte (der Zellendialog verschiebt höchstens den Zellwert), und
                # passt erst mit ihr: dann wurde sie erst nach der Verschiebung erfasst
                zelle = sum((f[1] for f, _, _ in kandidaten), Decimal("0")) + \
                    sum((f[1] for f in spaet_da), Decimal("0"))
                kt = kontext()
                if kt is not None:
                    zelle += self._angezeigtes_budget(v, key, vz, kt[0], kt[1], kt[2])[0]
                heute_teil = sum((f[1] for f, _ in am_anlagetag), Decimal("0"))
                if zelle < abs(v.betrag) <= zelle + heute_teil:
                    kandidaten.extend((f, 0, ueberfaellig) for f, ueberfaellig in am_anlagetag)
            gruppe = (key, v.von, v.nach, vz)
            ziel_betrag, wahl = gruppen_ziel[gruppe], gruppen_wahl[gruppe]
            if unbekannt:
                # Teilzahlung ohne bekannten Zahltag (kein Beleg): ob sie beim Anlegen schon
                # gezahlt war, ist offen – entspricht erst der Bestandteil samt ihr der
                # Verschiebung (Höhe-Regel), war sie es nicht: sie zählt als beim Anlegen offen
                offen_summe: dict[tuple, Decimal] = defaultdict(Decimal)
                for f, k, _ in kandidaten:
                    if k == 0:
                        offen_summe[teil_von[id(f)].ordnung[:2]] += f[1]
                dazu: dict[tuple, Decimal] = defaultdict(Decimal)
                for f, _ in unbekannt:
                    dazu[teil_von[id(f)].ordnung[:2]] += f[1]
                for f, ueberfaellig in unbekannt:
                    o = teil_von[id(f)].ordnung[:2]
                    if offen_summe[o] != ziel_betrag and offen_summe[o] + dazu[o] == ziel_betrag:
                        kandidaten.append((f, 0, ueberfaellig))
            # beim Anlegen Offenes vor Bezahltem; darin ein Bestandteil in Höhe der Verschiebung
            # zuerst (verglichen mit seinem beim Anlegen sichtbaren offenen bzw. bezahlten
            # Betrag), sonst Überfälliges, dann Posten, Raten, Termine, Bestellungen
            summe: dict[tuple, Decimal] = defaultdict(Decimal)
            for f, k, _ in kandidaten:
                summe[(k,) + teil_von[id(f)].ordnung[:2]] += f[1]

            def ordnung(e: tuple) -> tuple:
                f, k, ueberfaellig = e
                teil = teil_von[id(f)]
                return (k, teil.ordnung[:2] not in wahl,
                        summe[(k,) + teil.ordnung[:2]] != ziel_betrag,
                        not ueberfaellig, f[0]) + teil.ordnung

            def folge(liste: list[list]) -> list[list]:
                return sorted(liste, key=lambda f: (f[0],) + teil_von[id(f)].ordnung)

            reihe = [f for f, _, _ in sorted(kandidaten, key=ordnung)]
            vorher = [f[1] for f in reihe]
            rest = nehmen(v, key, reihe, rest)
            wahl.update(teil_von[id(f)].ordnung[:2] for f, b in zip(reihe, vorher) if f[1] < b)
            if rest > 0 and isinstance(key, int):
                bb, anlage_sicht, gedeckt = kontext()
                if spaet_da:
                    # lag eine noch nicht entschiedene Bestellung in der Zelle und übersteigt der
                    # Betrag das Bruttobudget des Tages oder das, was die Woche beim Anlegen
                    # abgeben konnte (angezeigter Tageswert, Überschuss von den übrigen Tagen ab
                    # dem Anlagetag), hat der Nutzer (auch) sie verschoben – zuerst das angezeigte
                    # Budget, dann die Bestellung, erst ein Überschuss über die Zelle hinaus aus
                    # dem übrigen Bruttobudget (nicht abgebbares Budget nie vor einem in der Zelle
                    # gezeigten Bestandteil)
                    angezeigt, abgebbar = self._angezeigtes_budget(v, key, vz, bb, anlage_sicht,
                                                                   gedeckt)
                    if rest > abgebbar or rest > self._budget_frei(v, key, vz, bb, gedeckt):
                        rest = self._budget_nehmen(v, key, vz, rest, bb, budget_liegt, gedeckt,
                                                   angezeigt)
                        rest = nehmen(v, key, folge(spaet_da), rest)
                        spaet_da = []
                if rest > 0:
                    rest = self._budget_nehmen(v, key, vz, rest, bb, budget_liegt, gedeckt)
            # beim Anlegen in keiner Sicht der Zelle Gezeigtes (`unsichtbar`) ist kein Kandidat –
            # ein Betrag darüber hinaus bleibt wirkungslos; ebenso erst nach der Verschiebung
            # Angelegtes, wenn ihr Anlagezeitpunkt verlässlich ist (verliert sie ihren
            # Bestandteil – Storno, geänderte Fälligkeit –, übernimmt ihr Rest keine später
            # erfasste Rechnung am Ursprungstag)
            for liste in (spaet_da, [] if _zeitpunkt_verlaesslich(v_angelegt, self.heute)
                          else spaet):
                if rest > 0 and liste:
                    rest = nehmen(v, key, folge(liste), rest)
            if rest > 0:
                self.rest[v.id] = rest      # über dem geplanten Betrag: wirkungslos
        self._nach_faelligkeit(anlage_max, teil_von)
        self._vorziehen_abgleichen(mit_konto)

    def _nach_faelligkeit(self, anlage_max: dict[tuple, date], teil_von: dict[int, "_Teil"]) -> None:
        """Ein auf mehrere Tage verteilter Bestandteil: die beim Anlegen offenen Tranchen gehen
        nach ihrer Zahlung chronologisch an die Fragmente in der Reihenfolge ihres Tags (früheste
        Fälligkeit zuerst, wie § 366 Abs. 2 BGB), der offene Rest an die spätesten. Die Beträge je
        Tag bleiben gleich (dieselbe Zuordnung, gleich in welcher Reihenfolge die Verschiebungen
        angelegt sind); nur welche Tranche dort liegt, folgt der Fälligkeit. Beim Anlegen schon
        Bezahltes bleibt, wo es ist."""
        for ident, t in anlage_max.items():
            self._faellig_ordnen(ident, t, teil_von)

    def _faellig_ordnen(self, ident: tuple, t: date, teil_von: dict[int, "_Teil"],
                        liegt: dict | None = None) -> None:
        """Ordnung nach Fälligkeit (`_nach_faelligkeit`) für einen Bestandteil, beim Anlegen am
        Tag t (spätester Anlagetag der Verschiebungen, die von ihm genommen haben); `liegt`
        (Fragmente je Zeile und Tag der laufenden Zuordnung) wird nachgeführt."""
        w0 = wochen_start(t)
        refs = [ident + (z,) for z in ("offen", "teil", "erledigt")
                if ident + (z,) in self._fragmente]
        plaetze = [f for r in refs for f in self._fragmente[r] if f[1] > 0
                   and teil_von[id(f)].offen_bei(t) and teil_von[id(f)].sichtbar_bei(t, w0)]
        if len(plaetze) < 2 or len({f[0] for f in plaetze}) < 2:
            return
        tranchen: dict[tuple, list] = {}
        for f in plaetze:
            teil, zahltag = teil_von[id(f)], self._zahltag.get(id(f))
            schluessel = (teil.zustand == "offen", zahltag or date.max, teil.ref)
            eintrag = tranchen.setdefault(schluessel, [teil, zahltag, Decimal("0")])
            eintrag[2] += f[1]
        if len(tranchen) < 2:
            return
        reihe = [tranchen[s] for s in sorted(tranchen)]
        weg = {id(f) for f in plaetze}
        for r in refs:
            self._fragmente[r] = [f for f in self._fragmente[r] if id(f) not in weg]
        if liegt is not None:
            for ort in {(teil_von[id(f)].key, f[0]) for f in plaetze}:
                liegt[ort] = [f for f in liegt.get(ort, ()) if id(f) not in weg]
        self._ersetzt.extend(plaetze)      # ids der ersetzten Fragmente bleiben vergeben
        i = 0
        for f in sorted(plaetze, key=lambda f: (f[0], f[2])):
            bedarf = f[1]
            while bedarf > 0 and i < len(reihe):
                teil, zahltag, verfuegbar = reihe[i]
                anteil = min(bedarf, verfuegbar)
                neu_frag = [f[0], anteil, f[2]]
                self._fragmente[teil.ref].append(neu_frag)
                self._zahltag[id(neu_frag)] = zahltag
                teil_von[id(neu_frag)] = teil
                self._kette[id(neu_frag)] = self._kette.get(id(f), ())
                self._weg[id(neu_frag)] = self._weg.get(id(f), ())
                if liegt is not None:
                    liegt[(teil.key, f[0])].append(neu_frag)
                bedarf -= anteil
                reihe[i][2] -= anteil
                if reihe[i][2] <= 0:
                    i += 1

    def _vorziehen_abgleichen(self, mit_konto: set[int]) -> None:
        """Künftige Raten, deren Anteile auf einen Tag bis zum Zahlstand vorgezogen sind:
        diese Anteile gleicht `_raten_zahlstatus` nach allen übrigen Raten mit den verbliebenen
        Zahlungen ab ihrem Zieltag ab; der bezahlte Teil wechselt am Zieltag in den erledigten
        Anteil der Rate (kein Vortrag, nicht in der Projektion)."""
        vorgezogen: dict[tuple[int, date], list[list]] = {}
        for ref, frags in self._fragmente.items():
            if ref[0] != "DAUER" or ref[3] != "offen" or ref[2] <= self.heute \
                    or ref[1] not in mit_konto:
                continue
            ziele = [f for f in frags if f[2] and f[1] > 0 and f[0] <= self.zahlungen_bis]
            if ziele:
                vorgezogen[(ref[1], ref[2])] = sorted(ziele, key=lambda f: f[0])
        if not vorgezogen:
            return
        self._vorgezogen = {k: [(f[0], f[1]) for f in v] for k, v in vorgezogen.items()}
        self._status = None
        status = self.raten_status()
        for (d_id, tag), ziele in vorgezogen.items():
            _, gezahlt = status.get((d_id, tag), (Decimal("0"), []))
            zahl = [list(z) for z in sorted(gezahlt)]
            erledigt = self._fragmente.setdefault(("DAUER", d_id, tag, "erledigt"), [])
            for f in ziele:
                for z in zahl:
                    if f[1] <= 0:
                        break
                    if z[1] <= 0:
                        continue
                    anteil = min(f[1], z[1])
                    f[1] -= anteil
                    z[1] -= anteil
                    neu_frag = [f[0], anteil, True]
                    erledigt.append(neu_frag)
                    self._zahltag[id(neu_frag)] = z[0]
                    self._weg[id(neu_frag)] = self._weg.get(id(f), ())

    def _beitraege(self, key: int, t: date, konto_by_id: dict, igeld) -> dict[date, list]:
        """Restbudget-Verbrauch der Zeile im Plan am Anlagetag t (Fenster ab dessen Wochenbeginn,
        Mandantenstandard) je Objekt, nach Wochen: [(Tag, Betrag, Art, Anlage, Freigabe,
        Bestandteil, Plantag)] –
        einmal je Zeile und Anlagetag bis zur spätesten Ursprungswoche der Zeile geladen; was
        erst nach einer Verschiebung angelegt ist, filtert `_wochen_rest` (wie
        `_explizite_fluesse` mit angelegt_bis)."""
        schluessel = (key, t)
        if schluessel not in self._beitraege_cache:
            s = wochen_start(t)
            ende = max(wochen_start(v.von) for v in self.verschiebungen
                       if verschiebung_key(v) == key) + timedelta(days=6)
            liste: list = []
            if ende >= s:
                _explizite_fluesse(
                    self._db, self._mandant, konto_by_id, s, ende, t, False, None, None, igeld,
                    bestell_plan_status(self._mandant, None), None, nur_konto=key,
                    beitraege=liste)
            einplanen = getattr(self._mandant, "bestellungen_offen_einplanen", True) is not False
            je_woche: dict[date, list] = defaultdict(list)
            for k, tag, betrag, obj, ref, plantag in liste:
                if k != key:
                    continue
                if obj is None:
                    zeit = ("D", None, None)
                elif isinstance(obj, models.Bestellung):
                    zeit = ("B", _zeitpunkt(obj.erstellt_am),
                            None if einplanen else _zeitpunkt(obj.entschieden_am))
                else:
                    zeit = ("P", _angelegt_bekannt(obj, self.heute), None)
                # ein Plantag vor dem Fenster: von dort weg Verschobenes verbraucht im Fenster
                # nichts (wie `verbrauchend`) – das rechnet `_wochen_rest` mit dem Stand der
                # Zuordnung beim Anlegen
                vor = (ref, plantag) if ref is not None and plantag < s else (None, None)
                je_woche[wochen_start(tag)].append((tag, betrag) + zeit + vor)
            self._beitraege_cache[schluessel] = je_woche
        return self._beitraege_cache[schluessel]

    def _wochen_rest(self, key: int, w: date, brutto: dict[date, Decimal], anlage_sicht) -> Decimal:
        """Restbudget der Woche ab `w` im Plan am Anlagetag t (Fenster ab dessen Wochenbeginn,
        Mandantenstandard, ohne später Angelegtes – Posten und Termine nach `v_stand`,
        Bestellungen nach der Anlage der Verschiebung bzw. erst danach freigegebene, wenn der
        Mandant offene nicht einplant): Bruttobudget nach den expliziten Beträgen der Zeile an
        ihrem Tag; von einem Plantag vor dem Fenster weg Verschobenes (durch die früher
        angelegten Verschiebungen) verbraucht nichts. Je Verschiebung und Woche einmal gerechnet
        (`_woche_bei`) – der Stand der Zuordnung ändert sich mit jeder Verschiebung."""
        t, v_angelegt, v_stand, konto_by_id, igeld, _ = anlage_sicht
        grenze = v_stand if v_stand is not None else v_angelegt
        tage: dict[date, Decimal] = defaultdict(Decimal)
        for tag, betrag, art, angelegt, entschieden, ref, plantag in \
                self._beitraege(key, t, konto_by_id, igeld).get(w, ()):
            if art == "P" and grenze is not None and angelegt is not None and angelegt > grenze:
                continue
            if art == "B" and v_angelegt is not None and any(
                    z is not None and z > v_angelegt for z in (angelegt, entschieden)):
                continue
            if ref is not None:
                weg = self.vom_plantag_weg(ref, plantag)
                if weg:
                    vz = Decimal("1") if betrag > 0 else Decimal("-1")
                    betrag = vz * max(Decimal("0"), abs(betrag) - weg)
            tage[tag] += betrag
        explizit = sum((abs(b) for b in tage.values()), Decimal("0"))
        return max(Decimal("0"), sum(brutto.values(), Decimal("0")) - explizit).quantize(CENT)

    def _woche_bei(self, key: int, w: date, budget: _BudgetBrutto,
                   anlage_sicht) -> tuple[dict[date, Decimal], dict[date, Decimal]] | None:
        """Budget der Woche ab `w` in der vollen Sicht am Anlagetag t nach den Entnahmen der
        früher angelegten Verschiebungen (wie `_budget_einarbeiten`): (angezeigter Tageswert,
        je Ursprungstag nicht Abgebbares). None: die Woche liegt vor diesem Fenster (dort kürzt
        keine Sicht) oder hat kein Budget. Je Verschiebung einmal je Woche gerechnet."""
        t, wochen = anlage_sicht[0], anlage_sicht[5]
        if w in wochen:
            return wochen[w]
        brutto = budget.woche(key, w)
        ergebnis = None
        if w >= wochen_start(t) and brutto:
            rest = self._wochen_rest(key, w, brutto, anlage_sicht)
            bis = w + timedelta(days=6)
            weg = [(d, b) for k, d, b in self.budget_entnahmen if k == key and w <= d <= bis and b > 0]
            if rest > 0:
                werte = _verteile(brutto, rest)
                fehlt = _wochen_abgabe(werte, weg, t)
            else:
                werte = dict.fromkeys(brutto, Decimal("0"))
                fehlt = defaultdict(Decimal)
                for d, b in weg:
                    fehlt[d] += b
            ergebnis = (werte, fehlt)
        wochen[w] = ergebnis
        return ergebnis

    def _gedeckt(self, f: list, key: int, budget: _BudgetBrutto, anlage_sicht) -> dict[date, Decimal]:
        """Beim Anlegen angezeigter Teil eines an den Ursprungstag verschobenen Budgetanteils je
        Herkunftstag: was die Herkunftswoche in der vollen Sicht am Anlagetag nicht abgeben
        konnte, kürzt die dorthin verschobenen Anteile – den früher verschobenen zuerst, wie
        `_budget_einarbeiten`; der Rest war in der Zelle zu sehen."""
        gedeckt: dict[date, Decimal] = {}
        for d, b in f[4].items():
            if b <= 0:
                continue
            sicht = self._woche_bei(key, wochen_start(d), budget, anlage_sicht)
            fehlt = sicht[1].get(d, Decimal("0")) if sicht is not None else Decimal("0")
            for g in self._budget_frag:
                if fehlt <= 0:
                    break
                if g[0] != key:
                    continue
                anteil = min(fehlt, g[4].get(d, Decimal("0")))
                if g is f:
                    b -= anteil
                    break
                fehlt -= anteil
            if b > 0:
                gedeckt[d] = b
        return gedeckt

    def _budget_frei(self, v, key: int, vz: int, budget: _BudgetBrutto,
                     gedeckt: dict[int, dict[date, Decimal]]) -> Decimal:
        """Am Ursprungstag für die Verschiebung verfügbares Budget: der gedeckte Teil des
        dorthin verschobenen und das noch nicht entnommene Bruttobudget des Tages."""
        frei = sum((b for g in gedeckt.values() for b in g.values()), Decimal("0"))
        natur = budget.tag(key, v.von)
        if natur is not None and natur[1] == vz:
            frei += max(Decimal("0"), natur[0] - self.budget_genommen.get((key, v.von), Decimal("0")))
        return frei

    def _angezeigtes_budget(self, v, key: int, vz: int, budget: _BudgetBrutto, anlage_sicht,
                            gedeckt: dict[int, dict[date, Decimal]]) -> tuple[Decimal, Decimal]:
        """Budget der Zelle des Ursprungstags beim Anlegen: (angezeigt, abgebbar). Angezeigt: der
        Tageswert (Restbudget der Woche im Plan am Anlagetag nach den Entnahmen früherer
        Verschiebungen) und der gedeckte Teil des dorthin verschobenen Budgets; abgebbar dazu,
        was die übrigen Tage der Woche ab dem Anlagetag noch zeigten (ein Überschuss fällt
        dort an, `_wochen_abgabe`)."""
        dorthin = sum((b for frei in gedeckt.values() for b in frei.values()), Decimal("0"))
        if budget.richtung(key) != vz:
            return dorthin, dorthin
        sicht = self._woche_bei(key, wochen_start(v.von), budget, anlage_sicht)
        if sicht is None:
            natur = budget.tag(key, v.von)
            eigen = max(Decimal("0"), natur[0] - self.budget_genommen.get((key, v.von), Decimal("0"))) \
                if natur is not None else Decimal("0")
            return dorthin + eigen, dorthin + eigen
        t = anlage_sicht[0]
        eigen = sicht[0].get(v.von, Decimal("0"))
        uebrige = sum((b for x, b in sicht[0].items() if x != v.von and x >= t and b > 0),
                      Decimal("0"))
        return dorthin + eigen, dorthin + eigen + uebrige

    def _entnehmen(self, key: int, tag: date, betrag: Decimal) -> None:
        """Entnahme aus dem Bruttobudget eines Tags (in der Folge der Verschiebungen)."""
        self.budget_genommen[(key, tag)] += betrag
        letzte = self.budget_entnahmen[-1] if self.budget_entnahmen else None
        if letzte is not None and letzte[0] == key and letzte[1] == tag:
            letzte[2] += betrag
        else:
            self.budget_entnahmen.append([key, tag, betrag])

    def _zurueckgeben(self, key: int, tag: date, betrag: Decimal) -> None:
        """Zurückverschobenes Budget gilt wieder als Bruttobudget des Tags – die zuletzt
        angelegten Entnahmen dort werden zuerst zurückgenommen."""
        self.budget_genommen[(key, tag)] -= betrag
        for e in reversed(self.budget_entnahmen):
            if betrag <= 0:
                break
            if e[0] == key and e[1] == tag and e[2] > 0:
                anteil = min(betrag, e[2])
                e[2] -= anteil
                betrag -= anteil

    def _budget_nehmen(self, v, key: int, vz: int, rest: Decimal, budget: _BudgetBrutto,
                       budget_liegt: dict, gedeckt: dict[int, dict[date, Decimal]],
                       hoechstens: Decimal | None = None) -> Decimal:
        """Budgetanteil einer Verschiebung (höchstens `hoechstens`): zuerst der beim Anlegen
        angezeigte (gedeckte) Teil des auf den Ursprungstag verschobenen Budgets (zuletzt
        Gebrachtes zuerst; `gedeckt` wird fortgeschrieben), dann das Bruttobudget des Tages
        (abzüglich dessen, was frühere Verschiebungen ihm entnommen haben); am Zieltag liegt der
        Betrag für Folgeverschiebungen bereit. Ungedecktes verschobenes Budget stand nicht in der
        Zelle und bleibt, wo es ist. Je verschobenem Betrag wird seine Herkunft (Ursprungstag des
        Bruttobudgets) mitgeführt – eine Sicht, in der die Woche dort weniger Restbudget hat,
        bucht am Ziel nur, was sie abgeben konnte."""
        grenze = rest if hoechstens is None else min(rest, hoechstens)
        bewegt = Decimal("0")
        herkunft: dict[date, Decimal] = defaultdict(Decimal)
        kette: set[int] = set()
        for f in reversed(budget_liegt.get((key, v.von), ())):
            if bewegt >= grenze:
                break
            frei = gedeckt.get(id(f))
            if f[3] != vz or f[2] <= 0 or not frei:
                continue
            anteil = min(grenze - bewegt, f[2], sum(frei.values(), Decimal("0")))
            if anteil <= 0:
                continue
            f[2] -= anteil
            bewegt += anteil
            for tag, b in _herkunft_nehmen(f[4], anteil, frei).items():
                herkunft[tag] += b
            for u in f[5]:
                self.entnommen[(v.id, u)] += anteil
            kette.update(f[5])
        natur = budget.tag(key, v.von) if bewegt < grenze else None
        if natur is not None and natur[1] == vz:
            frei = natur[0] - self.budget_genommen.get((key, v.von), Decimal("0"))
            anteil = min(grenze - bewegt, max(Decimal("0"), frei))
            if anteil > 0:
                self._entnehmen(key, v.von, anteil)
                herkunft[v.von] += anteil
                bewegt += anteil
        if bewegt <= 0:
            return rest
        self.budget_bewegungen.append((key, v.von, v.nach, vz * bewegt))
        self.budget_anspruch[v.id] = self.budget_anspruch.get(v.id, Decimal("0")) + bewegt
        # zurück an einen Tag, dem Budget entnommen wurde: der von dort stammende Anteil gilt
        # wieder als dessen Budget (Hin- und Rückverschiebung ohne Wirkung), der Rest liegt am
        # Zieltag. Budget anderer Herkunft füllt die Lücke nicht – sonst änderte es Herkunft und
        # Wirkung der früheren Verschiebung, die dem Tag Budget entnommen hat
        zurueck = min(bewegt, self.budget_genommen.get((key, v.nach), Decimal("0")),
                      herkunft.get(v.nach, Decimal("0")))
        if zurueck > 0:
            self._zurueckgeben(key, v.nach, zurueck)
            herkunft[v.nach] -= zurueck
        bleibt = bewegt - zurueck
        if bleibt > 0:
            frag = [key, v.nach, bleibt, vz, {d: b for d, b in herkunft.items() if b > 0},
                    tuple(sorted(kette)) + (v.id,)]
            self._budget_frag.append(frag)
            budget_liegt[(key, v.nach)].append(frag)
        return rest - bewegt


def _herkunft_nehmen(herkunft: dict[date, Decimal], betrag: Decimal,
                     erlaubt: dict[date, Decimal] | None = None) -> dict[date, Decimal]:
    """`betrag` aus der Herkunft {Ursprungstag: Betrag} entnehmen (früheste zuerst), je Tag
    höchstens `erlaubt` (wird fortgeschrieben; None = unbeschränkt)."""
    genommen: dict[date, Decimal] = {}
    for tag in sorted(herkunft):
        if betrag <= 0:
            break
        grenze = herkunft[tag] if erlaubt is None else min(herkunft[tag],
                                                            erlaubt.get(tag, Decimal("0")))
        teil = min(betrag, grenze)
        if teil > 0:
            herkunft[tag] -= teil
            if erlaubt is not None:
                erlaubt[tag] -= teil
            genommen[tag] = teil
            betrag -= teil
    return genommen


def vortrag_verschiebungen_anwenden(
    vortrag: dict[Key, dict[date, Decimal]], verschiebungen: list, start: date, ende: date,
) -> list[tuple]:
    """Vorträge liegen auf dem aktuellen Vortragstag – von dort wird (begrenzt
    auf den noch offenen Vortrag) auf den Zieltag verschoben. Eine Folgeverschiebung vom
    Zieltag einer früheren (Kette) nimmt nur, was dorthin verschoben liegt – nicht den übrigen,
    mit dem Rolltag gewanderten Vortrag."""
    bewegungen: list[tuple] = []
    ziele: set[tuple[Key, date]] = set()
    for v in verschiebungen:
        key = verschiebung_key(v)
        vz = Decimal(1) if v.betrag > 0 else Decimal(-1)
        rest = abs(v.betrag)
        kette = (key, v.von) in ziele
        ziele.add((key, v.nach))
        for tag in sorted(vortrag.get(key, {})):
            verfuegbar = vortrag[key][tag]
            if verfuegbar * vz <= 0 or tag == v.nach or (kette and tag != v.von):
                continue
            anteil = min(rest, abs(verfuegbar))
            vortrag[key][tag] -= vz * anteil
            if start <= v.nach <= ende:
                vortrag[key][v.nach] += vz * anteil
            bewegungen.append((key, tag, v.nach, vz * anteil, "vortrag", v.id))
            rest -= anteil
            if rest <= 0:
                break
    return bewegungen


VORTRAG_VORLAUF_TAGE = 14      # früher gebuchte Zahlungen zählen noch als erfolgt


def _betragsgleich(ist: Decimal, soll: Decimal) -> bool:
    """Betrag bis 1 % (mindestens 1 €) abweichend."""
    return abs(abs(ist) - abs(soll)) <= max(Decimal("1"), abs(soll) / 100)


def _ist_zahlungen(db: Session, mandant_id: int, finanz_nrn: set[str], von: date,
                   bis: date, ohne_zugeordnete: bool = False,
                   nur_nrn: set[str] | None = None) -> dict[str, list[list]]:
    """Einzelne Ist-Zahlungen je Sachkonto (nicht Tagessummen: zwei Raten am selben Tag
    sollen einzeln zählen); [Datum, Betrag, noch verfügbar]. ohne_zugeordnete: ohne
    Zahlungen, die einem Posten oder einer Bestellung zugeordnet sind; nur_nrn: nur diese
    Sachkonten."""
    zahlungen: dict[str, list[list]] = defaultdict(list)
    q = select(models.Buchung).where(
        models.Buchung.mandant_id == mandant_id,
        models.Buchung.datum >= von,
        models.Buchung.datum <= bis,
    )
    if nur_nrn is not None:
        nrn = sorted(nur_nrn)
        q = q.where(models.Buchung.konto_nr.in_(nrn) | models.Buchung.gegenkonto_nr.in_(nrn))
    if ohne_zugeordnete:
        q = q.where(models.Buchung.posten_id.is_(None), models.Buchung.id.not_in(
            select(models.BestellAbloesung.buchung_id).where(
                models.BestellAbloesung.mandant_id == mandant_id,
                models.BestellAbloesung.buchung_id.is_not(None))))
    for b in db.scalars(q.order_by(models.Buchung.datum, models.Buchung.id)):
        fluss = _sachfluss(b, finanz_nrn)
        if fluss is not None and (nur_nrn is None or fluss[0] in nur_nrn):
            zahlungen[fluss[0]].append([b.datum, fluss[1], abs(fluss[1])])
    return zahlungen


def _raten_abgleich(faellig: list[tuple], zahlungen: dict[str, list[list]],
                    konto_nr: dict[int, str]) -> list[tuple[Decimal, list[tuple[date, Decimal]]]]:
    """Rechnet Ist-Zahlungen gleicher Richtung auf ihrem Konto an Fälligkeiten
    (Tag, Dauerbuchung, Soll, …; nach Tag sortiert) an, jede höchstens einmal:
      1. betragsgleiche Zahlungen bis 14 Tage um die Fälligkeit (trennt z. B. zwei
         Raten am selben Tag sauber),
      2. danach chronologisch alle übrigen Zahlungen ab 14 Tage vor der Fälligkeit –
         Teilzahlungen mindern den offenen Rest, der nicht verbrauchte Rest einer
         angerechneten Zahlung gilt als Vorauszahlung für spätere Fälligkeiten.
    Liefert je Fälligkeit (offener Rest, [(Zahltag, angerechneter Betrag)])."""
    offen = [abs(e[2]) for e in faellig]
    angerechnet: list[list[tuple[date, Decimal]]] = [[] for _ in faellig]
    fenster = timedelta(days=VORTRAG_VORLAUF_TAGE)

    # 1. betragsgleiche Zahlung nahe der Fälligkeit
    for i, (d, d_, soll, *_) in enumerate(faellig):
        liste = zahlungen.get(konto_nr.get(d_.konto_id), [])
        kandidaten = [
            (abs((z[0] - d).days), j)
            for j, z in enumerate(liste)
            if z[2] == abs(z[1]) and z[1] * soll > 0
            and abs(z[0] - d) <= fenster and _betragsgleich(z[1], soll)
        ]
        if kandidaten and offen[i] > 0:
            z = liste[min(kandidaten)[1]]
            angerechnet[i].append((z[0], z[2]))
            z[2] = Decimal("0")
            offen[i] = Decimal("0")

    # 2. übrige Zahlungen chronologisch anrechnen (Teilzahlungen, Vorauszahlungen)
    for i, (d, d_, soll, *_) in enumerate(faellig):
        for z in zahlungen.get(konto_nr.get(d_.konto_id), []):
            if offen[i] <= 0:
                break
            angebrochen = Decimal("0") < z[2] < abs(z[1])
            if z[2] <= 0 or z[1] * soll <= 0 or (z[0] < d - fenster and not angebrochen):
                continue
            anteil = min(z[2], offen[i])
            z[2] -= anteil
            offen[i] -= anteil
            angerechnet[i].append((z[0], anteil))
    return list(zip(offen, angerechnet))


def _vorgezogen_abgleich(eintraege: list[tuple], zahlungen: dict[str, list[list]],
                         konto_nr: dict[int, str], bis: date) -> dict[tuple, tuple[Decimal, list]]:
    """Dritter Abgleichsdurchgang: vorgezogene Anteile künftiger Raten (Zieltag, Dauerbuchung,
    Soll mit Vorzeichen, Schlüssel) mit den nach den übrigen Raten verbliebenen Zahlungen ab
    ihrem Zieltag (bis `bis`) – zuerst eine betragsgleiche bis 14 Tage danach, dann
    chronologisch. Liefert je Schlüssel (bezahlt, [(Zahltag, Betrag)])."""
    ergebnis: dict[tuple, tuple[Decimal, list]] = {}
    fenster = timedelta(days=VORTRAG_VORLAUF_TAGE)
    for ziel, d_, soll, schluessel in sorted(eintraege, key=lambda e: (e[0], e[1].id)):
        liste = zahlungen.get(konto_nr.get(d_.konto_id), [])
        offen = abs(soll)
        angerechnet: list[tuple[date, Decimal]] = []
        gleich = [((z[0] - ziel).days, j) for j, z in enumerate(liste)
                  if z[2] == abs(z[1]) and z[1] * soll > 0 and ziel <= z[0] <= min(bis, ziel + fenster)
                  and _betragsgleich(z[1], soll)]
        if gleich:
            z = liste[min(gleich)[1]]
            angerechnet.append((z[0], min(z[2], offen)))
            z[2], offen = Decimal("0"), Decimal("0")
        for z in liste:
            if offen <= 0:
                break
            if z[2] <= 0 or z[1] * soll <= 0 or not ziel <= z[0] <= bis:
                continue
            anteil = min(z[2], offen)
            z[2] -= anteil
            offen -= anteil
            angerechnet.append((z[0], anteil))
        bezahlt, alt = ergebnis.get(schluessel, (Decimal("0"), []))
        ergebnis[schluessel] = (bezahlt + abs(soll) - offen, alt + angerechnet)
    return ergebnis


def _raten_zahlstatus(db: Session, mandant: models.Mandant, ab: date, bis: date,
                      zusatz: dict[int, set[date]] | None = None,
                      zahlungen_bis: date | None = None,
                      vorgezogen: dict[tuple[int, date], list[tuple[date, Decimal]]] | None = None,
                      nur_konten: set[int] | None = None,
                      ) -> dict[tuple[int, date], tuple[Decimal, list]]:
    """Zahlstatus der Raten aller Dauerbuchungen mit Konto (auch ohne „vortragen“; im
    Insolvenzgeldzeitraum entfallende nicht) – Abgleich (`_raten_abgleich`) mit den
    Ist-Zahlungen ihres Kontos bis `zahlungen_bis` (Standard `bis`; die Freigabe zählt
    Zahlungen bis gestern, Raten wie der Plan bis heute), ohne Zahlungen zu Posten und
    Bestellungen:
      1. Raten mit Plantag in [ab, bis] – unabhängig von Verschiebungen, damit eine
         Verschiebung (auch einer anderen Rate oder Zeile) den Abgleich nicht ändert,
      2. danach, nur mit den übrigen Zahlungen, die Raten vor `ab` aus `zusatz`
         (Dauerbuchung → Plantage; Raten an einem Ursprungstag einer Verschiebung),
      3. zuletzt die auf einen Tag bis `zahlungen_bis` vorgezogenen Anteile künftiger Raten
         aus `vorgezogen` ((Dauerbuchung, Plantag) → [(Zieltag, Betrag)]) mit den dann noch
         verbliebenen Zahlungen ab ihrem Zieltag (`_vorgezogen_abgleich`).
    Künftige Raten (Plantag nach `bis`) gelten sonst als offen. nur_konten: nur die Raten
    dieser Konten (der Abgleich rechnet je Sachkonto, das Ergebnis ist für sie gleich). Liefert
    je (Dauerbuchung, Plantag) (bezahlt, [(Zahltag, angerechneter Betrag)])."""
    bl = mandant.bundesland
    konten = _lade_konten(db, mandant.id)
    konto_by_id = {k.id: k for k in konten}
    konto_nr = {k.id: k.nummer for k in konten}
    finanz_nrn = {k.nummer for k in konten
                  if k.typ in (models.KontoTyp.BANK.value, models.KontoTyp.KASSE.value)}
    from .insolvenzgeld import igeld_fenster

    igeld = igeld_fenster(mandant)
    zusatz = zusatz or {}
    vorgezogen = vorgezogen or {}
    haupt: list[tuple] = []
    neben: list[tuple] = []
    dritte: list[tuple] = []
    nur_nrn = None
    bedingung = [models.Dauerbuchung.mandant_id == mandant.id, models.Dauerbuchung.aktiv.is_(True),
                 models.Dauerbuchung.konto_id.is_not(None)]
    if nur_konten is not None:
        bedingung.append(models.Dauerbuchung.konto_id.in_(sorted(nur_konten)))
        nur_nrn = {konto_nr[k] for k in nur_konten if k in konto_nr}
    for d_ in db.scalars(select(models.Dauerbuchung).where(*bedingung)):
        konto = konto_by_id.get(d_.konto_id)
        vz = Decimal(1) if d_.art == PostenArt.DEBITOR.value else Decimal(-1)
        soll = vz * d_.betrag_brutto
        if ab <= bis:
            for roh in _dauer_termine(d_, ab - timedelta(days=31), bis):
                d = naechster_bankarbeitstag(roh, bl)
                if ab <= d <= bis and not _rate_entfaellt(d_, konto, d, igeld):
                    haupt.append((d, d_, soll))
        for d in zusatz.get(d_.id, ()):
            if d < ab and d <= bis and not _rate_entfaellt(d_, konto, d, igeld):
                neben.append((d, d_, soll))
        for (d_id, plantag), ziele in vorgezogen.items():
            if d_id == d_.id and plantag > bis and not _rate_entfaellt(d_, konto, plantag, igeld):
                dritte += [(ziel, d_, vz * betrag, (d_id, plantag)) for ziel, betrag in ziele]
    if not haupt and not neben and not dritte:
        return {}
    bis_zahlung = zahlungen_bis or bis
    frueh = min([ab] + [d for d, _, _ in neben]) - timedelta(days=VORTRAG_VORLAUF_TAGE)
    zahlungen = _ist_zahlungen(db, mandant.id, finanz_nrn, frueh, bis_zahlung,
                               ohne_zugeordnete=True, nur_nrn=nur_nrn)
    ergebnis: dict[tuple[int, date], tuple[Decimal, list]] = {}
    for liste in (haupt, neben):
        liste.sort(key=lambda x: (x[0], x[1].id))
        abgleich = _raten_abgleich(liste, zahlungen, konto_nr)
        for (d, d_, soll), (offen, angerechnet) in zip(liste, abgleich):
            ergebnis[(d_.id, d)] = (abs(soll) - offen, angerechnet)
    if dritte:
        # Zahlungen ab einem noch früheren Zieltag nur für den dritten Durchgang nachladen –
        # die ersten beiden bleiben davon unberührt
        ziel_frueh = min(e[0] for e in dritte)
        if ziel_frueh < frueh:
            for nr, liste in _ist_zahlungen(db, mandant.id, finanz_nrn, ziel_frueh,
                                            frueh - timedelta(days=1),
                                            ohne_zugeordnete=True, nur_nrn=nur_nrn).items():
                zahlungen[nr] = liste + zahlungen.get(nr, [])
        ergebnis.update(_vorgezogen_abgleich(dritte, zahlungen, konto_nr, bis_zahlung))
    return ergebnis


def dauer_vortraege(
    db: Session,
    mandant: models.Mandant,
    start: date,
    ende: date,
    heute: date,
    finanz_nrn: set[str],
    szenario: models.Szenario | None = None,
    zuordnung: Zuordnung | None = None,
) -> list[dict]:
    """Fällige, aber nicht (vollständig) gebuchte Zahlungen aus
    Dauerschuldverhältnissen (nur mit Konto und „vortragen“).

    Der offene Anteil jeder Rate laut Zahlstatus (`_raten_zahlstatus`, über die Zuordnung)
    an seinem – ggf. verschobenen – Plantag im Fenster bis einschließlich heute wird auf
    den nächsten Bankarbeitstag ab morgen vorgetragen (sofern im Fenster). Bezahlte
    Anteile werden nie vorgetragen; künftige Raten, die auf einen vergangenen Tag
    vorgezogen sind, gelten als offen. `zuordnung` muss den Abgleich ab spätestens `start`
    mit Zahlstand `heute` rechnen (ohne Angabe: eigene ohne Verschiebungen)."""
    bl = mandant.bundesland
    ziel = naechster_bankarbeitstag(heute + timedelta(days=1), bl)
    bis = min(heute, ende)
    if ziel > ende or bis < start:
        return []
    z = zuordnung if zuordnung is not None else Zuordnung(db, mandant, heute, [], abgleich_ab=start)

    from .insolvenzgeld import igeld_fenster

    igeld = igeld_fenster(mandant)
    szenario_faktor = _szenario_aufloeser(szenario)
    vortraege: list[dict] = []
    for d_ in db.scalars(
        select(models.Dauerbuchung).where(
            models.Dauerbuchung.mandant_id == mandant.id,
            models.Dauerbuchung.aktiv.is_(True),
            models.Dauerbuchung.vortragen.is_(True),
            models.Dauerbuchung.konto_id.is_not(None),
        )
    ):
        tage = {naechster_bankarbeitstag(roh, bl)
                for roh in _dauer_termine(d_, start - timedelta(days=31), bis)}
        # dazu Raten mit verschobenen Anteilen (auch vor dem Fenster und künftige)
        tage = {d for d in tage if start <= d <= bis} | z.raten.get(d_.id, set())
        je_tag: dict[date, list[Decimal]] = defaultdict(lambda: [Decimal("0"), Decimal("0")])
        for d in tage:
            if _rate_entfaellt(d_, d_.konto, d, igeld):
                continue  # im Insolvenzgeldzeitraum gar nicht geplant
            for tag, teil, _, zustand in z.raten_verteilung(d_.id, d, d_.betrag_brutto):
                if start <= tag <= bis:
                    je_tag[tag][0 if zustand == "offen" else 1] += teil
        vz = Decimal(1) if d_.art == PostenArt.DEBITOR.value else Decimal(-1)
        for tag in sorted(je_tag):
            offen, bezahlt = je_tag[tag]
            if offen <= 0:
                continue
            rest = vz * offen
            if d_.art == PostenArt.DEBITOR.value:
                rest = (rest * szenario_faktor(d_.konto, Richtung.EIN.value)).quantize(CENT)
            vortraege.append({
                "dauer_id": d_.id,
                "name": d_.name,
                "konto_id": d_.konto_id,
                "faellig": tag,
                "ziel": ziel,
                "soll": vz * (offen + bezahlt),
                "gezahlt": bezahlt,
                "betrag": rest,
            })
    vortraege.sort(key=lambda v: (v["faellig"], v["dauer_id"]))
    return vortraege


def _monatstage(jahr: int, monat: int) -> int:
    if monat == 12:
        return 31
    return (date(jahr, monat + 1, 1) - timedelta(days=1)).day


def budget_basis_woche(
    konto: models.Konto, w_von: date, w_bis: date, bl: str,
    monat_budget: "MonatsBudgets",
    wochen_budget: dict[tuple[int, int, int], tuple[Decimal, str | None]],
    tag_budget: dict[tuple[int, date], Decimal],
) -> dict[date, tuple[Decimal, Decimal, int]]:
    """Budgetbasis eines Kontos in der Woche ab `w_von` (bis `w_bis`): je Tag
    (Netto-Quellbetrag, Anteil, Teiler) – der Tageswert ist Quelle × Anteil ÷ Teiler.
    Vorrang: Tageswert > Wochenwert > Monatsbudget (ggf. fortgeschrieben), verteilt
    nach seiner Aufteilung (KW/Tag) bzw. dem Verteilungsprofil des Kontos."""
    konto_id = konto.id
    profil = konto.verteilung if konto.verteilung in VERTEILUNGEN else "GLEICH"
    iso = w_von.isocalendar()
    tage = [w_von + timedelta(days=i) for i in range(7) if w_von + timedelta(days=i) <= w_bis]
    banktage = [d for d in tage if ist_bankarbeitstag(d, bl)] or [w_von]
    basis: dict[date, tuple[Decimal, Decimal, int]] = {}
    if (konto_id, iso.year, iso.week) in wochen_budget:
        betrag, aufteilung = wochen_budget[(konto_id, iso.year, iso.week)]
        netto = abs(betrag)
        if netto:
            # Wochenwert: je KW auf einen Zahltag (Wochentag laut Konto bzw. erster
            # Bankarbeitstag), je Tag gleichmäßig auf die Banktage; ohne Angabe
            # wöchentliche Profile auf den Zahltag, sonst auf die Banktage
            wtag = _WOCHENTAG.get(profil)
            if aufteilung == "TAG" or (aufteilung is None and wtag is None):
                zieltage = banktage
            elif wtag is not None:
                ziel = naechster_bankarbeitstag(w_von + timedelta(days=wtag), bl)
                zieltage = [ziel if ziel <= w_bis else banktage[-1]]
            else:
                zieltage = [banktage[0]]
            for d in zieltage:
                basis[d] = (netto, Decimal("1"), len(zieltage))
    else:
        for d in tage:
            satz = monat_budget.wert(konto_id, d.year, d.month)
            if satz is None:
                continue
            anteil = _monats_verteilung(d.year, d.month, budget_profil(konto, satz[1]), bl).get(d)
            if anteil:
                basis[d] = (abs(satz[0]), anteil, 1)
    for d in tage:
        if (konto_id, d) in tag_budget:
            netto = abs(tag_budget[(konto_id, d)])
            if netto:
                basis[d] = (netto, Decimal("1"), 1)
            else:
                basis.pop(d, None)
    return basis


def _verteile(gewichte: dict[date, Decimal], summe: Decimal) -> dict[date, Decimal]:
    """`summe` (auf Cent) anteilig nach `gewichte` auf die Tage, der Rundungsrest auf den
    letzten Tag mit Gewicht – wie das Restbudget der Woche."""
    tage = sorted(gewichte)
    gesamt = sum(gewichte.values(), Decimal("0"))
    werte = dict.fromkeys(tage, Decimal("0"))
    if gesamt <= 0:
        return werte
    letzter = max(d for d in tage if gewichte[d] > 0)
    skalierung = summe / gesamt
    verteilt = Decimal("0")
    for d in tage:
        if d != letzter:
            werte[d] = (gewichte[d] * skalierung).quantize(CENT)
            verteilt += werte[d]
    werte[letzter] = summe - verteilt
    return werte


def _wochen_abgabe(werte: dict[date, Decimal], weg_tage: list[tuple[date, Decimal]],
                   ueberschuss_ab: date | None) -> dict[date, Decimal]:
    """Verschobene Budgetanteile einer Woche aus den angezeigten Tageswerten `werte`
    (Restbudget je Tag, wird fortgeschrieben) nehmen – je Entnahme in der Folge der
    Verschiebungen: zuerst vom Tageswert ihres Ursprungstags, ein Überschuss anteilig von den
    übrigen Tagen der Woche, die noch Budget zeigen, ab `ueberschuss_ab` (vergangene Soll-Tage
    bleiben). Jede Entnahme sieht die Zelle so, wie sie bei ihrer Anlage angezeigt war – eine
    ganz verschobene Zelle ist leer, die übrigen Tage bleiben. Liefert je Ursprungstag, was die
    Woche nicht abgeben kann (kein Restbudget mehr)."""
    fehlt: dict[date, Decimal] = defaultdict(Decimal)
    for d, weg in weg_tage:
        if weg <= 0:
            continue
        teil = min(weg, werte.get(d, NULL_EUR))
        if teil > 0:
            werte[d] -= teil
        ueber = weg - teil
        if ueber <= 0:
            continue
        frei = {x: b for x, b in werte.items()
                if x != d and b > 0 and (ueberschuss_ab is None or x >= ueberschuss_ab)}
        summe_frei = sum(frei.values(), NULL_EUR)
        if summe_frei > ueber:
            werte.update(_verteile(frei, summe_frei - ueber))
        else:
            werte.update(dict.fromkeys(frei, NULL_EUR))
            fehlt[d] += ueber - summe_frei
    return fehlt


def _budget_einarbeiten(
    db: Session,
    mandant: models.Mandant,
    konto_by_id: dict[int, models.Konto],
    plan: dict[Key, dict[date, Decimal]],
    start: date,
    ende: date,
    szenario: models.Szenario | None = None,
    igeld: tuple[date, date] | None = None,
    verbrauch: dict[Key, dict[date, Decimal]] | None = None,
    zuordnung: "Zuordnung | None" = None,
    verschoben: dict[Key, dict[date, Decimal]] | None = None,
    ueberschuss_ab: date | None = None,
    ende_anzeige: date | None = None,
) -> set[int]:
    """Budget je Konto und Woche als Restbudget in `plan` einarbeiten. Liefert die
    Konten mit einer Budgetquelle.

    verbrauch: explizite Flüsse, die das Wochenbudget verbrauchen (Standard: `plan`
               selbst); plan_fluesse übergibt sie ohne Verschiebungen (Projektion:
               einschließlich des Erledigten an seinem Tag der vollen Sicht).
    zuordnung: verschobene Budgetanteile – je Entnahme (in der Folge der Verschiebungen)
               vom angezeigten Tageswert ihres Ursprungstags genommen, ein Überschuss anteilig
               von den übrigen Tagen der Woche ab `ueberschuss_ab` (`_wochen_abgabe`,
               vergangene Soll-Tage bleiben); was die Woche in dieser Sicht nicht abgeben kann
               (kein Restbudget mehr), fehlt auch am Zieltag – sonst entstünde eine
               Phantom-Ausgabe. Am
               Zieltag unabhängig vom dortigen Restbudget gebucht; `verschoben` nimmt die
               Markierung auf (bis `ende_anzeige`, Standard `ende`)."""
    bl = mandant.bundesland
    ende_anzeige = ende_anzeige or ende
    zugang: dict[int, list[tuple[date, Decimal, int, dict]]] = defaultdict(list)
    bewegungen: dict[int, list[tuple[date, date, Decimal]]] = defaultdict(list)
    # Entnahmen (Ursprungstag, Betrag) je Konto in der Folge der Verschiebungen
    entnahme: dict[int, list[tuple[date, Decimal]]] = defaultdict(list)
    if zuordnung is not None:
        for k, tag, betrag, vz, herkunft in zuordnung.budget_fragmente():
            zugang[k].append((tag, betrag, vz, herkunft))
        for k, von, nach, betrag in zuordnung.budget_bewegungen:
            bewegungen[k].append((von, nach, betrag))
        for k, d, betrag in zuordnung.budget_entnahmen:
            if betrag > 0:
                entnahme[k].append((d, betrag))
    explizit_quelle = plan if verbrauch is None else verbrauch
    monat_budget = MonatsBudgets(
        db.scalars(select(models.Budget).where(models.Budget.mandant_id == mandant.id))
    )
    overrides = {
        (bw.konto_id, bw.jahr, bw.kw): (bw.betrag_netto, bw.aufteilung)
        for bw in db.scalars(
            select(models.BudgetWoche).where(models.BudgetWoche.mandant_id == mandant.id)
        )
    }
    tag_budget = {
        (bt.konto_id, bt.datum): bt.betrag_netto
        for bt in db.scalars(select(models.BudgetTag).where(
            models.BudgetTag.mandant_id == mandant.id,
            models.BudgetTag.datum >= wochen_start(start),
            models.BudgetTag.datum <= ende,
        ))
    }
    konto_ids = (monat_budget.konto_ids | {k for (k, _, _) in overrides}
                 | {k for (k, _) in tag_budget})
    # verschobenes Budget auch dann buchen, wenn das Konto im Fenster selbst kein Budget hat
    # (z. B. nur ein Tageswert vor dem Fenster)
    bearbeiten = konto_ids | set(zugang) | set(bewegungen)

    # Wochen des Fensters
    w = wochen_start(start)
    wochen: list[tuple[date, date]] = []
    while w <= ende:
        wochen.append((w, min(w + timedelta(days=6), ende)))
        w += timedelta(days=7)

    szenario_faktor = _szenario_aufloeser(szenario)

    for konto_id in sorted(bearbeiten):
        konto = konto_by_id.get(konto_id)
        if konto is None or not konto.aktiv or not konto.liquiditaetswirksam:
            continue
        richtung = _konto_richtung(konto)
        if richtung == Richtung.INFO.value:
            continue
        vorzeichen = Decimal("1") if richtung == Richtung.EIN.value else Decimal("-1")
        faktor = szenario_faktor(konto, richtung)
        # Insolvenzgeld: Personal-/SV-Budgets im Zeitraum entfallen (tagesgenau)
        igeld_konto = (
            igeld
            if igeld
            and konto.typ in (models.KontoTyp.PERSONAL.value, models.KontoTyp.SV.value)
            else None
        )
        fehlt: dict[date, Decimal] = {}     # je Ursprungstag: in dieser Sicht nicht abgebbar
        for w_von, w_bis in wochen:
            # Tagesgenaue Basisverteilung (brutto) gemäß Verteilungsprofil bzw.
            # Wochen-/Tageswerten
            basis: dict[date, Decimal] = {
                d: _brutto(quelle, konto.ust_satz) * faktor * anteil / teiler
                for d, (quelle, anteil, teiler) in budget_basis_woche(
                    konto, w_von, w_bis, bl, monat_budget, overrides, tag_budget
                ).items()
            }
            if igeld_konto:
                for d in list(basis):
                    if igeld_konto[0] <= d <= igeld_konto[1]:
                        del basis[d]
            woche_bis = w_von + timedelta(days=6)
            weg_tage = [(d, (betrag * faktor).quantize(CENT))
                        for d, betrag in entnahme.get(konto_id, ()) if w_von <= d <= woche_bis]
            gesamt = sum(basis.values(), NULL_EUR)
            # Restbudget: bereits explizit geplante Beträge dieses Kontos in der Woche
            tageswerte = explizit_quelle.get(konto_id, {})
            explizit = sum(
                (abs(tageswerte.get(w_von + timedelta(days=i), NULL_EUR)) for i in range(7)),
                NULL_EUR,
            )
            rest = max(NULL_EUR, gesamt - explizit).quantize(CENT) if gesamt > 0 else NULL_EUR
            if rest == 0:
                for d, weg in weg_tage:
                    if weg > 0:
                        fehlt[d] = fehlt.get(d, NULL_EUR) + weg
                continue
            werte = _verteile(basis, rest)
            # verschobene Budgetanteile (Zuordnung) je Entnahme in der Folge der Verschiebungen
            # (`_wochen_abgabe`) – eine ganz verschobene Zelle ist leer, die übrigen Tage zeigen,
            # was danach übrig ist
            for d, b in _wochen_abgabe(werte, weg_tage, ueberschuss_ab).items():
                fehlt[d] = fehlt.get(d, NULL_EUR) + b
            for d, betrag in werte.items():
                if betrag:
                    plan[konto_id][d] += vorzeichen * betrag
        # verschobenes Budget am Zieltag, gekürzt um das in dieser Sicht nicht Abgebbare seiner
        # Herkunft (die Summe jeder Sicht bleibt erhalten) – zuerst beim früher Verschobenen
        # (es lag schon in der Zelle, als später verschoben wurde)
        frags = zugang.get(konto_id, [])
        kuerzung = [NULL_EUR] * len(frags)
        for d, betrag_fehlt in fehlt.items():
            for i, f in enumerate(frags):
                if betrag_fehlt <= 0:
                    break
                anteil = min(betrag_fehlt, (f[3].get(d, NULL_EUR) * faktor).quantize(CENT))
                if anteil > 0:
                    kuerzung[i] += anteil
                    betrag_fehlt -= anteil
        for (tag, betrag, vz, _), minus in zip(frags, kuerzung):
            if start <= tag <= ende:
                b = max(NULL_EUR, (betrag * faktor).quantize(CENT) - minus)
                if b:
                    plan[konto_id][tag] += vz * b
        if verschoben is not None:
            for von, nach, betrag in bewegungen.get(konto_id, ()):
                b = (betrag * faktor).quantize(CENT)
                if start <= von <= ende_anzeige:
                    verschoben[konto_id][von] -= b
                if start <= nach <= ende_anzeige:
                    verschoben[konto_id][nach] += b
    return konto_ids


_banktage_cache: dict[tuple[int, int, str], int] = {}


def bankarbeitstage_anzahl(jahr: int, monat: int, bl: str) -> int:
    key = (jahr, monat, bl)
    if key not in _banktage_cache:
        from .feiertage import bankarbeitstage_im_monat

        _banktage_cache[key] = len(bankarbeitstage_im_monat(jahr, monat, bl))
    return _banktage_cache[key]


def _konto_richtung(konto: models.Konto) -> str:
    if konto.gruppe is not None:
        return konto.gruppe.richtung
    if konto.typ == models.KontoTyp.ERLOES.value:
        return Richtung.EIN.value
    if konto.typ in (models.KontoTyp.BANK.value, models.KontoTyp.KASSE.value, models.KontoTyp.INFO.value):
        return Richtung.INFO.value
    return Richtung.AUS.value


def referenz_snapshot(db: Session, mandant_id: int) -> models.PlanSnapshot | None:
    """Vom Anwender gewählte Soll-Referenz des Mandanten (oder None)."""
    return db.scalars(
        select(models.PlanSnapshot).where(
            models.PlanSnapshot.mandant_id == mandant_id,
            models.PlanSnapshot.referenz.is_(True),
        )
    ).first()


def gruppen_aufloeser(konto_by_id: dict[int, models.Konto], gruppen: list[models.KontoGruppe]):
    """Liefert eine Funktion Planschlüssel → BWA-Gruppe (Konto: seine Gruppe;
    Termine/Posten/Dauerbuchungen ohne Konto: feste Standardgruppen)."""
    gruppe_by_code = {g.code: g for g in gruppen}

    def gruppe_fuer_key(key: Key) -> models.KontoGruppe | None:
        if isinstance(key, int):
            k = konto_by_id.get(key)
            return k.gruppe if k else None
        if key.startswith("TERMIN:SV"):
            return gruppe_by_code.get("A_SV") or gruppe_by_code.get("A_STEUER")
        if key.startswith("TERMIN:OP_DEBITOR") or key.startswith("TERMIN:DAUER_DEBITOR"):
            return gruppe_by_code.get("E_SONST")
        if key.startswith("TERMIN:OP_") or key.startswith("TERMIN:DAUER_"):
            return gruppe_by_code.get("A_SONST")
        if key == BESTELL_KEY:
            return gruppe_by_code.get("A_SONST")
        if key.startswith("TERMIN:"):
            return gruppe_by_code.get("A_STEUER")
        return None

    return gruppe_fuer_key


def liquiditaets_richtung(key: Key, konto_by_id: dict[int, models.Konto], gruppe_fuer_key) -> str | None:
    """EIN/AUS einer Planzeile oder None, wenn sie nicht in die Liquidität
    eingeht (Finanzkonten, nicht liquiditätswirksame und nachrichtliche Konten)."""
    if isinstance(key, int):
        k = konto_by_id.get(key)
        if k is None or not k.liquiditaetswirksam or k.typ in (
            models.KontoTyp.BANK.value, models.KontoTyp.KASSE.value
        ):
            return None
        richtung = _konto_richtung(k)
    else:
        g = gruppe_fuer_key(key)
        richtung = g.richtung if g else Richtung.AUS.value
    return None if richtung == Richtung.INFO.value else richtung


def liquiditaet_zum(db: Session, mandant: models.Mandant, tag: date,
                    heute: date | None = None) -> Decimal:
    """Ist-Liquidität (aktive Bank- und Kassenkonten) zum Ende des Tages `tag`.

    Wie im Plan zählen alle Bestandsanker bis heute – liegt der erste Anker
    nach `tag` (z. B. Kontoauszug von heute), wird von ihm zurückgerechnet."""
    heute = heute or date.today()
    konten = _lade_konten(db, mandant.id)
    finanz_typen = (models.KontoTyp.BANK.value, models.KontoTyp.KASSE.value)
    finanz_nrn = {k.nummer for k in konten if k.typ in finanz_typen}
    finanzkonten = [k for k in konten if k.typ in finanz_typen and k.aktiv]
    werte = _bestaende(db, mandant, finanzkonten, finanz_nrn, [tag], max(tag, heute), {})
    return Decimal(str(werte["liquiditaet"][tag.isoformat()]))


def snapshot_fluesse(
    snap: models.PlanSnapshot, ohne_offene_bestellungen: bool = False,
) -> dict[Key, dict[date, Decimal]]:
    """Eingefrorene Planflüsse je Key/Tag: der zum Stichtag angezeigte Plan
    (Soll/Ist, Basis-Spalte, Exporte, KPI). ohne_offene_bestellungen: ohne den
    gekennzeichneten Anteil noch nicht entschiedener Bestellungen – Soll-Basis der
    Bestellfreigabe, damit sich eine offene Bestellung keinen eigenen Rahmen schafft."""
    werte: dict[Key, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for w in snap.werte:
        if ohne_offene_bestellungen and w.bestellung_offen:
            continue
        key: Key = w.konto_id if w.konto_id is not None else f"TERMIN:{w.termin_typ}"
        werte[key][w.datum] += w.betrag
    return werte


def snapshot_liquiditaet(
    db: Session, mandant: models.Mandant, snap: models.PlanSnapshot,
    konto_by_id: dict[int, models.Konto], gruppe_fuer_key, heute: date | None = None,
) -> dict[date, Decimal]:
    """Geplante Liquidität eines eingefrorenen Planstands je Tag seines
    Fensters: Ist-Liquidität am Vortag des Stichtags + kumulierte Planflüsse."""
    netto: dict[date, Decimal] = defaultdict(Decimal)
    for key, tageswerte in snapshot_fluesse(snap).items():
        if liquiditaets_richtung(key, konto_by_id, gruppe_fuer_key) is None:
            continue
        for d, v in tageswerte.items():
            netto[d] += v
    stand = liquiditaet_zum(db, mandant, snap.stichtag - timedelta(days=1), heute)
    verlauf: dict[date, Decimal] = {}
    for i in range(snap.wochen * 7):
        d = snap.stichtag + timedelta(days=i)
        stand += netto.get(d, Decimal("0"))
        verlauf[d] = stand
    return verlauf


def _snapshot_plan(
    db: Session, mandant_id: int, start: date, heute: date
) -> tuple[dict[Key, dict[date, Decimal]] | None, models.PlanSnapshot | None]:
    """Gewählte Soll-Referenz, sonst der jüngste Snapshot, dessen Fenster das
    angezeigte Fenster überlappt."""
    snap = referenz_snapshot(db, mandant_id)
    if snap is None:
        snaps = db.scalars(
            select(models.PlanSnapshot)
            .where(
                models.PlanSnapshot.mandant_id == mandant_id,
                models.PlanSnapshot.stichtag <= heute,
            )
            .order_by(models.PlanSnapshot.stichtag.desc(), models.PlanSnapshot.id.desc())
        )
        for s in snaps:
            if s.stichtag + timedelta(days=s.wochen * 7) > start:
                snap = s
                break
    if snap is None:
        return None, None
    return snapshot_fluesse(snap), snap


def berechne_plan(
    db: Session,
    mandant: models.Mandant,
    start: date | None = None,
    wochen: int = 13,
    heute: date | None = None,
    szenario: models.Szenario | None = None,
) -> dict:
    heute = heute or date.today()
    start = wochen_start(start or heute)
    ende = start + timedelta(days=wochen * 7 - 1)
    tage = [start + timedelta(days=i) for i in range(wochen * 7)]

    konten = _lade_konten(db, mandant.id)
    konto_by_id = {k.id: k for k in konten}
    konto_by_nr = {k.nummer: k for k in konten}
    finanz_typen = (models.KontoTyp.BANK.value, models.KontoTyp.KASSE.value)
    # Flusserkennung über alle Bank-/Kassenkonten, Bestandszeilen nur für aktive
    finanz_nrn = {k.nummer for k in konten if k.typ in finanz_typen}
    finanzkonten = [k for k in konten if k.typ in finanz_typen and k.aktiv]

    # ---- Ist ----
    ist_bis = min(heute, ende)
    sach_fluesse, _ = ist_zahlungsfluesse(db, mandant.id, finanz_nrn, start, ist_bis)
    ist: dict[Key, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    unbekannte: set[str] = set()
    for nr, tageswerte in sach_fluesse.items():
        konto = konto_by_nr.get(nr)
        if konto is not None and not konto.liquiditaetswirksam:
            continue
        key: Key = konto.id if konto is not None else f"NR:{nr}"
        if konto is None and nr:
            unbekannte.add(nr)
        for d, betrag in tageswerte.items():
            ist[key][d] += betrag

    # ---- Plan (aktuelle Erwartung) und Projektionsplan, mit manuellen Verschiebungen ----
    # (eine Zuordnung für alle Sichten: Plan, Restplan-Sicht und Vortrag bewegen dieselben
    # Bestandteile und Budgetanteile, mit demselben Zahlstatus der Raten)
    zuordnung = Zuordnung(db, mandant, heute, abgleich_ab=min(start, wochen_start(heute)))
    verschoben: dict[Key, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    plan = plan_fluesse(db, mandant, start, ende, heute=heute, szenario=szenario,
                        zuordnung=zuordnung, verschoben=verschoben)
    plan_projektion = plan_fluesse(
        db, mandant, start, ende, heute=heute, nur_offen=True, szenario=szenario,
        zuordnung=zuordnung,
    )

    gruppen = list(
        db.scalars(
            select(models.KontoGruppe)
            .where(models.KontoGruppe.mandant_id == mandant.id)
            .order_by(models.KontoGruppe.sortierung, models.KontoGruppe.id)
        )
    )
    gruppe_fuer_key = gruppen_aufloeser(konto_by_id, gruppen)

    # ---- Vortrag nicht gebuchter Dauerschuld-Zahlungen (Ansicht „mit Vortrag“) ----
    vortraege = dauer_vortraege(db, mandant, start, ende, heute, finanz_nrn, szenario,
                                zuordnung=zuordnung)
    vortrag: dict[Key, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for v in vortraege:
        vortrag[v["konto_id"]][v["ziel"]] += v["betrag"]
    vortrag_bewegungen = vortrag_verschiebungen_anwenden(
        vortrag, plan_verschiebungen(db, mandant.id, "VORTRAG", offen_ab=heute), start, ende)

    # Markierung verschobener Zellen: abgebende Tage negativ, empfangende positiv
    def markierung(bewegungen: list[tuple]) -> dict[Key, dict[date, Decimal]]:
        m: dict[Key, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        for key, tag, nach, betrag, _, _ in bewegungen:
            if start <= tag <= ende:
                m[key][tag] -= betrag
            if start <= nach <= ende:
                m[key][nach] += betrag
        return m

    verschoben_vortrag = markierung(vortrag_bewegungen)
    vortrag_verschoben: dict[tuple[Key, date], Decimal] = defaultdict(Decimal)
    for key, _, nach, betrag, _, _ in vortrag_bewegungen:
        vortrag_verschoben[(key, nach)] += betrag

    # ---- Vergleichsbasis für vergangene Tage ----
    snap_plan, snap = _snapshot_plan(db, mandant.id, start, heute)
    vergleichsbasis = "SNAPSHOT" if snap_plan is not None else "LIVE"
    basis = snap_plan if snap_plan is not None else plan

    # ---- Zeilenstruktur ----
    alle_keys = set(plan.keys()) | set(ist.keys()) | set(vortrag.keys())
    zeilen_je_gruppe: dict[int | None, list[Key]] = defaultdict(list)
    for key in alle_keys:
        g = gruppe_fuer_key(key)
        if isinstance(key, int):
            k = konto_by_id.get(key)
            if k is None:
                continue
            if k.typ in finanz_typen:
                continue  # Finanzkonten erscheinen im Bestandsblock
            if not k.liquiditaetswirksam:
                continue
        zeilen_je_gruppe[g.id if g else None].append(key)

    def key_sort(key: Key):
        if isinstance(key, int):
            return (0, konto_by_id[key].nummer)
        return (1, str(key))

    def zeile_fuer_key(key: Key) -> dict:
        if isinstance(key, int):
            k = konto_by_id[key]
            name, nummer, ust = k.bezeichnung, k.nummer, k.ust_satz
            if not k.aktiv:
                name += " (inaktiv)"
        elif key.startswith("NR:"):
            nummer, name, ust = key[3:], f"Konto {key[3:]} (nicht angelegt)", None
        else:
            benennung = {
                "TERMIN:SV": "SV-Beiträge (Kalender)",
                "TERMIN:UST_VA": "USt-Voranmeldung (Kalender)",
                "TERMIN:LST": "Lohnsteuer (Kalender)",
                "TERMIN:GEWST": "Gewerbesteuer (Kalender)",
                "TERMIN:KST": "Körperschaftsteuer (Kalender)",
                "TERMIN:SONSTIG": "Sonstige Termine",
                "TERMIN:OP_KREDITOR": "Offene Posten (ohne Konto)",
                "TERMIN:OP_DEBITOR": "Forderungen (ohne Konto)",
                "TERMIN:DAUER_KREDITOR": "Dauerbuchungen (ohne Konto)",
                "TERMIN:DAUER_DEBITOR": "Dauererlöse (ohne Konto)",
                BESTELL_KEY: "Bestellungen (ohne Konto)",
            }
            nummer, name, ust = "", benennung.get(key, str(key)), None
        return {
            "key": str(key),
            "typ": "konto",
            "nummer": nummer,
            "name": name,
            "ust_satz": _f(ust) if ust is not None else None,
            "plan": {d.isoformat(): _f(v) for d, v in plan.get(key, {}).items() if v},
            "ist": {d.isoformat(): _f(v) for d, v in ist.get(key, {}).items() if v},
            "basis": {d.isoformat(): _f(v) for d, v in basis.get(key, {}).items() if v},
            "vortrag": {d.isoformat(): _f(v) for d, v in vortrag.get(key, {}).items() if v},
            "verschoben": {d.isoformat(): _f(v) for d, v in verschoben.get(key, {}).items() if v},
            "verschoben_vortrag": {
                d.isoformat(): _f(v) for d, v in verschoben_vortrag.get(key, {}).items() if v},
        }

    zeilen: list[dict] = []
    for g in gruppen:
        if g.richtung == Richtung.INFO.value:
            continue
        kinder = [zeile_fuer_key(k) for k in sorted(zeilen_je_gruppe.get(g.id, []), key=key_sort)]
        zeilen.append(
            {
                "key": f"G:{g.code}",
                "typ": "gruppe",
                "code": g.code,
                "name": g.name,
                "richtung": g.richtung,
                "kinder": kinder,
            }
        )
    ohne = [zeile_fuer_key(k) for k in sorted(zeilen_je_gruppe.get(None, []), key=key_sort)]
    if ohne:
        zeilen.append(
            {
                "key": "G:OHNE",
                "typ": "gruppe",
                "code": "OHNE",
                "name": "Nicht zugeordnet",
                "richtung": Richtung.AUS.value,
                "kinder": ohne,
            }
        )

    # ---- Summen je Tag ----
    def summe_je_tag(quelle: dict[Key, dict[date, Decimal]], nur: str | None) -> dict[str, float]:
        s: dict[date, Decimal] = defaultdict(Decimal)
        for key, tageswerte in quelle.items():
            if liquiditaets_richtung(key, konto_by_id, gruppe_fuer_key) is None:
                continue
            for d, v in tageswerte.items():
                if nur == "EIN" and v <= 0:
                    continue
                if nur == "AUS" and v >= 0:
                    continue
                s[d] += v
        return {d.isoformat(): _f(v) for d, v in s.items() if v}

    summen = {
        name: {
            "plan": summe_je_tag(plan, nur),
            "ist": summe_je_tag(ist, nur),
            "basis": summe_je_tag(basis, nur),
            "vortrag": summe_je_tag(vortrag, nur),
        }
        for name, nur in (("einzahlungen", "EIN"), ("auszahlungen", "AUS"), ("netto", None))
    }

    # ---- Bestände ----
    bestaende = _bestaende(db, mandant, finanzkonten, finanz_nrn, tage, heute, plan_projektion)
    if vortraege:
        # Vorträge liegen nach heute: fortgeschriebene Liquidität um ihre
        # kumulierte Summe verschieben (Ansicht „mit Vortrag“)
        vortrag_netto = summen["netto"]["vortrag"]
        kumuliert = 0.0
        bestaende["liquiditaet_vortrag"] = {}
        bestaende["verfuegbar_vortrag"] = {}
        for d in tage:
            iso = d.isoformat()
            kumuliert += vortrag_netto.get(iso, 0.0)
            bestaende["liquiditaet_vortrag"][iso] = round(bestaende["liquiditaet"][iso] + kumuliert, 2)
            bestaende["verfuegbar_vortrag"][iso] = round(bestaende["verfuegbar"][iso] + kumuliert, 2)

    # ---- Zahlungsgesperrte Insolvenzforderungen (nachrichtlich) ----
    gesperrt = Decimal("0")
    for p in db.scalars(
        select(models.OffenerPosten).where(
            models.OffenerPosten.mandant_id == mandant.id,
            models.OffenerPosten.status == PostenStatus.OFFEN.value,
            models.OffenerPosten.art == PostenArt.KREDITOR.value,
            models.OffenerPosten.forderungsklasse
            == models.Forderungsklasse.INSOLVENZFORDERUNG.value,
        )
    ):
        gesperrt += max(Decimal("0"), p.betrag_brutto - (p.bezahlt_betrag or Decimal("0")))

    # ---- Insolvenzgeld-Entlastung im Fenster (nachrichtlich) ----
    from .insolvenzgeld import igeld_fenster, vorschau as igeld_vorschau

    insolvenzgeld_info = None
    if igeld_fenster(mandant) is not None:
        v = igeld_vorschau(db, mandant, start, ende)
        insolvenzgeld_info = {
            "aktiv": True,
            "von": v["von"],
            "bis": v["bis"],
            "entlastung_fenster": v["summen"]["gesamt"],
        }

    return {
        "gesperrte_insolvenzforderungen": _f(gesperrt),
        "szenario": (
            {
                "id": szenario.id,
                "name": szenario.name,
                "ein_faktor": _f(szenario.ein_faktor),
                "aus_faktor": _f(szenario.aus_faktor),
                "debitoren_verzoegerung_tage": szenario.debitoren_verzoegerung_tage,
                "regeln_anzahl": len(szenario.regeln),
            }
            if szenario else None
        ),
        "insolvenzgeld": insolvenzgeld_info,
        "mandant_id": mandant.id,
        "start": start.isoformat(),
        "ende": ende.isoformat(),
        "heute": heute.isoformat(),
        "wochen": _wochen_struktur(start, wochen),
        "tage": [d.isoformat() for d in tage],
        "zeilen": zeilen,
        "summen": summen,
        "bestaende": bestaende,
        "vergleichsbasis": vergleichsbasis,
        "snapshot": (
            {"id": snap.id, "stichtag": snap.stichtag.isoformat(),
             "erstellt_am": snap.erstellt_am.isoformat(), "referenz": snap.referenz}
            if snap else None
        ),
        "vortraege": [
            {
                "name": v["name"],
                "konto": (f"{konto_by_id[v['konto_id']].nummer} "
                          f"{konto_by_id[v['konto_id']].bezeichnung}"),
                "faellig": v["faellig"].isoformat(),
                "ziel": v["ziel"].isoformat(),
                "soll": _f(v["soll"]),
                "gezahlt": _f(v["gezahlt"]),
                "betrag": _f(v["betrag"]),
            }
            for v in vortraege
        ],
        "vortrag_verschoben": [
            {
                "konto": (f"{konto_by_id[key].nummer} {konto_by_id[key].bezeichnung}"
                          if isinstance(key, int) and key in konto_by_id else str(key)),
                "nach": nach.isoformat(),
                "betrag": _f(betrag),
            }
            for (key, nach), betrag in sorted(vortrag_verschoben.items(), key=lambda x: (x[0][1], str(x[0][0])))
            if betrag
        ],
        "unbekannte_konten": sorted(unbekannte),
    }


def _wochen_struktur(start: date, wochen: int) -> list[dict]:
    struktur = []
    for i in range(wochen):
        w_von = start + timedelta(days=i * 7)
        iso = w_von.isocalendar()
        struktur.append(
            {
                "jahr": iso.year,
                "kw": iso.week,
                "label": f"KW {iso.week}",
                "von": w_von.isoformat(),
                "bis": (w_von + timedelta(days=6)).isoformat(),
                "tage": [(w_von + timedelta(days=t)).isoformat() for t in range(7)],
            }
        )
    return struktur


def _bestaende(
    db: Session,
    mandant: models.Mandant,
    finanzkonten: list[models.Konto],
    finanz_nrn: set[str],
    tage: list[date],
    heute: date,
    plan_projektion: dict[Key, dict[date, Decimal]],
) -> dict:
    start, ende = tage[0], tage[-1]
    # Alle Anker je Konto (z. B. tägliche Kontoauszugssalden): der Verlauf wird
    # stückweise verankert – zwischen zwei Ankern zählen die Buchungsbewegungen,
    # am Ankertag gilt der Auszugssaldo als maßgeblich.
    anker: dict[int, list[tuple[date, Decimal]]] = {}
    for k in finanzkonten:
        zeilen = list(
            db.scalars(
                select(models.Bestand)
                .where(
                    models.Bestand.mandant_id == mandant.id,
                    models.Bestand.konto_id == k.id,
                    models.Bestand.datum <= heute,
                )
                .order_by(models.Bestand.datum, models.Bestand.id)
            )
        )
        # Bestand.wert = Kontostand zum Ende des Tages `datum`; letzter je Tag gilt
        je_tag: dict[date, Decimal] = {b.datum: b.wert for b in zeilen}
        anker[k.id] = sorted(je_tag.items()) or [(start - timedelta(days=1), Decimal("0"))]

    # Bewegungen ab frühestem Anker bis heute (deckt auch Anker nach Fensterende ab)
    frueh = min([a[0][0] for a in anker.values()] + [start]) + timedelta(days=1)
    _, bank_bewegungen = ist_zahlungsfluesse(db, mandant.id, finanz_nrn, frueh, heute)

    konto_bestaende: list[dict] = []
    summe_je_tag: dict[date, Decimal] = defaultdict(Decimal)
    basis_heute = Decimal("0")
    linien = Decimal("0")
    for k in finanzkonten:
        anker_liste = anker[k.id]
        bew = bank_bewegungen.get(k.nummer, {})
        werte: dict[str, float | None] = {}
        erster_datum, erster_wert = anker_liste[0]
        laufend: dict[date, Decimal] = {erster_datum: erster_wert}
        # rückwärts vom ersten Anker bis zum Fensterbeginn
        stand = erster_wert
        d = erster_datum
        while d > start:
            stand -= bew.get(d, Decimal("0"))
            d -= timedelta(days=1)
            laufend[d] = stand
        # vorwärts vom ersten Anker bis heute; spätere Anker setzen den Stand neu
        folgende = anker_liste[1:]
        idx = 0
        stand = erster_wert
        d = erster_datum + timedelta(days=1)
        while d <= heute:
            stand += bew.get(d, Decimal("0"))
            while idx < len(folgende) and folgende[idx][0] == d:
                stand = folgende[idx][1]
                idx += 1
            laufend[d] = stand
            d += timedelta(days=1)
        basis_heute += laufend.get(heute, anker_liste[-1][1])
        for d in tage:
            if d <= heute and d in laufend:
                werte[d.isoformat()] = _f(laufend[d])
                summe_je_tag[d] += laufend[d]
            else:
                werte[d.isoformat()] = None
        linien += k.kreditlinie or Decimal("0")
        konto_bestaende.append(
            {
                "konto_id": k.id,
                "nummer": k.nummer,
                "name": k.bezeichnung,
                "typ": k.typ,
                "kreditlinie": _f(k.kreditlinie),
                "anker": {
                    "datum": anker_liste[-1][0].isoformat(),
                    "wert": _f(anker_liste[-1][1]),
                },
                "bestand": werte,
            }
        )

    # Gesamtliquidität: Ist bis heute, danach Fortschreibung über den Restplan
    plan_netto: dict[date, Decimal] = defaultdict(Decimal)
    for tageswerte in plan_projektion.values():
        for d, v in tageswerte.items():
            plan_netto[d] += v

    liquiditaet: dict[str, float] = {}
    verfuegbar: dict[str, float] = {}
    letzter = None
    for d in tage:
        if d <= heute:
            wert = summe_je_tag.get(d, Decimal("0"))
            letzter = wert
        else:
            if letzter is None:
                letzter = basis_heute  # Fenster liegt komplett in der Zukunft
            letzter = letzter + plan_netto.get(d, Decimal("0"))
        liquiditaet[d.isoformat()] = _f(letzter)
        verfuegbar[d.isoformat()] = _f(letzter + linien)

    # Warenbestand nachrichtlich
    waren_anker = list(
        db.scalars(
            select(models.Bestand)
            .where(
                models.Bestand.mandant_id == mandant.id,
                models.Bestand.typ == models.BestandTyp.WAREN.value,
            )
            .order_by(models.Bestand.datum)
        )
    )
    waren: dict[str, float | None] = {}
    for d in tage:
        wert = None
        for w in waren_anker:
            if w.datum <= d:
                wert = w.wert
            else:
                break
        waren[d.isoformat()] = _f(wert) if wert is not None else None

    return {
        "finanzkonten": konto_bestaende,
        "liquiditaet": liquiditaet,
        "verfuegbar": verfuegbar,
        "kreditlinien": _f(linien),
        "waren": waren,
    }


def szenarien_vergleich(
    db: Session,
    mandant: models.Mandant,
    start: date | None = None,
    wochen: int = 13,
    heute: date | None = None,
) -> dict:
    """Basisplan und alle Szenarien nebeneinander (Wochenwerte + Liquidität).

    Wochenwerte folgen der Anzeige-Logik "Ist bis heute, Plan ab morgen"; das Ist
    ist für alle Szenarien identisch, die Unterschiede entstehen im Planteil.
    """
    heute = heute or date.today()
    szenarien = list(
        db.scalars(
            select(models.Szenario)
            .where(models.Szenario.mandant_id == mandant.id)
            .order_by(models.Szenario.name)
        )
    )
    ergebnis: dict = {"szenarien": []}
    for szenario in [None, *szenarien]:
        plan = berechne_plan(db, mandant, start=start, wochen=wochen, heute=heute,
                             szenario=szenario)
        ergebnis.setdefault("start", plan["start"])
        ergebnis.setdefault("ende", plan["ende"])
        ergebnis.setdefault("heute", plan["heute"])
        ergebnis.setdefault("wochen", plan["wochen"])

        def wochenwerte(quelle: dict) -> list[float]:
            werte = []
            for w in plan["wochen"]:
                summe = 0.0
                for tag in w["tage"]:
                    if tag <= plan["heute"]:
                        summe += quelle["ist"].get(tag, 0.0)
                    else:
                        summe += quelle["plan"].get(tag, 0.0)
                werte.append(round(summe, 2))
            return werte

        liquiditaet = plan["bestaende"]["liquiditaet"]
        verfuegbar = plan["bestaende"]["verfuegbar"]
        min_tag = min(liquiditaet, key=lambda t: liquiditaet[t])
        ergebnis["szenarien"].append(
            {
                "id": szenario.id if szenario else None,
                "name": szenario.name if szenario else "Basisplan",
                "kommentar": szenario.kommentar if szenario else None,
                "ein_faktor": _f(szenario.ein_faktor) if szenario else 100.0,
                "aus_faktor": _f(szenario.aus_faktor) if szenario else 100.0,
                "debitoren_verzoegerung_tage": szenario.debitoren_verzoegerung_tage if szenario else 0,
                "einzahlungen": wochenwerte(plan["summen"]["einzahlungen"]),
                "auszahlungen": wochenwerte(plan["summen"]["auszahlungen"]),
                "netto": wochenwerte(plan["summen"]["netto"]),
                "liquiditaet_wochenende": [
                    liquiditaet[w["tage"][-1]] for w in plan["wochen"]
                ],
                "min_liquiditaet": {"datum": min_tag, "wert": liquiditaet[min_tag]},
                "min_verfuegbar": min(verfuegbar.values()),
                "endbestand": liquiditaet[plan["tage"][-1]],
            }
        )
    return ergebnis


def erstelle_snapshot(
    db: Session,
    mandant: models.Mandant,
    start: date | None = None,
    wochen: int = 13,
    heute: date | None = None,
    kommentar: str | None = None,
) -> models.PlanSnapshot:
    heute = heute or date.today()
    start = wochen_start(start or heute)
    ende = start + timedelta(days=wochen * 7 - 1)
    # manuelle Verschiebungen gehören zum Plan (Sicht zum Stichtag, Zuordnung wie im Plan)
    zuordnung = Zuordnung(db, mandant, heute, abgleich_ab=min(start, wochen_start(heute)))

    def sicht(bestellungen: str | None) -> dict[Key, dict[date, Decimal]]:
        # Überschuss verschobener Budgetanteile erst ab dem Anlagetag (wie die LIVE-Basis der
        # Freigabe): vergangene Soll-Tage bleiben, ein frischer Planstand gleicht dem LIVE-Soll
        return plan_fluesse(db, mandant, start, ende, heute=start, bestellungen=bestellungen,
                            zuordnung=zuordnung, ueberschuss_ab=heute)

    # eingefroren wird der angezeigte Plan (Mandantenstandard, ggf. mit noch offenen
    # Bestellungen); deren Anteil – Differenz zum Plan nur mit entschiedenen
    # Bestellungen, Verschiebungen gleich angewendet – ist gekennzeichnet: als
    # Soll-Basis der Freigabe schüfe sich eine offene Bestellung sonst ihren Rahmen
    plan = sicht(None)
    entschieden = plan
    if models.BestellStatus.OFFEN.value in (bestell_plan_status(mandant, None) or set()) and \
            db.scalar(select(models.Bestellung.id).where(
                models.Bestellung.mandant_id == mandant.id,
                models.Bestellung.status == models.BestellStatus.OFFEN.value).limit(1)):
        entschieden = sicht("FREIGEGEBEN")
    snap = models.PlanSnapshot(
        mandant_id=mandant.id, stichtag=start, wochen=wochen, kommentar=kommentar
    )
    db.add(snap)
    db.flush()
    for key in sorted(set(plan) | set(entschieden), key=str):
        tage = set(plan.get(key, {})) | set(entschieden.get(key, {}))
        for d in sorted(tage):
            fest = entschieden.get(key, {}).get(d, Decimal("0"))
            offen = plan.get(key, {}).get(d, Decimal("0")) - fest
            for betrag, bestellung_offen in ((fest, False), (offen, True)):
                if not betrag:
                    continue
                db.add(
                    models.PlanSnapshotWert(
                        snapshot_id=snap.id,
                        konto_id=key if isinstance(key, int) else None,
                        termin_typ=None if isinstance(key, int) else str(key).replace("TERMIN:", ""),
                        datum=d,
                        betrag=betrag,
                        bestellung_offen=bestellung_offen,
                    )
                )
    db.commit()
    return snap


def soll_ist_vergleich(
    db: Session,
    mandant: models.Mandant,
    snapshot_id: int | None = None,
    heute: date | None = None,
) -> dict:
    """Wochenweiser Soll-/Ist-Vergleich gegen einen eingefrorenen Plan
    (ohne Angabe: gewählte Soll-Referenz, sonst der jüngste Snapshot)."""
    heute = heute or date.today()
    if snapshot_id is not None:
        snap = db.get(models.PlanSnapshot, snapshot_id)
        if snap is None or snap.mandant_id != mandant.id:
            raise ValueError("Snapshot nicht gefunden")
    else:
        snap = referenz_snapshot(db, mandant.id) or db.scalars(
            select(models.PlanSnapshot)
            .where(models.PlanSnapshot.mandant_id == mandant.id)
            .order_by(models.PlanSnapshot.stichtag.desc(), models.PlanSnapshot.id.desc())
        ).first()
    if snap is None:
        return {"snapshot": None, "wochen": [], "zeilen": []}

    start = snap.stichtag
    ende = start + timedelta(days=snap.wochen * 7 - 1)
    konten = _lade_konten(db, mandant.id)
    konto_by_id = {k.id: k for k in konten}
    konto_by_nr = {k.nummer: k for k in konten}
    finanz_nrn = {
        k.nummer for k in konten
        if k.typ in (models.KontoTyp.BANK.value, models.KontoTyp.KASSE.value)
    }

    plan = snapshot_fluesse(snap)   # angezeigter Plan zum Stichtag (inkl. offener Bestellungen)

    sach, _ = ist_zahlungsfluesse(db, mandant.id, finanz_nrn, start, min(heute, ende))
    ist: dict[Key, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for nr, tageswerte in sach.items():
        konto = konto_by_nr.get(nr)
        if konto is not None and (
            not konto.liquiditaetswirksam
            or konto.typ in (models.KontoTyp.BANK.value, models.KontoTyp.KASSE.value)
        ):
            continue
        key = konto.id if konto is not None else f"NR:{nr}"
        for d, v in tageswerte.items():
            ist[key][d] += v

    wochen_liste = _wochen_struktur(start, snap.wochen)

    def wochensummen(quelle: dict[date, Decimal]) -> dict[str, float]:
        s: dict[str, Decimal] = defaultdict(Decimal)
        for d, v in quelle.items():
            iso = d.isocalendar()
            s[f"{iso.year}-{iso.week:02d}"] += v
        return {k: _f(v) for k, v in s.items()}

    zeilen = []
    for key in sorted(set(plan.keys()) | set(ist.keys()), key=lambda x: str(x)):
        if isinstance(key, int):
            k = konto_by_id.get(key)
            if k is None:
                continue
            name = f"{k.nummer} {k.bezeichnung}"
        else:
            name = str(key)
        zeilen.append(
            {
                "key": str(key),
                "name": name,
                "plan": wochensummen(plan.get(key, {})),
                "ist": wochensummen(ist.get(key, {})),
            }
        )
    return {
        "snapshot": {
            "id": snap.id,
            "stichtag": snap.stichtag.isoformat(),
            "wochen": snap.wochen,
            "kommentar": snap.kommentar,
            "erstellt_am": snap.erstellt_am.isoformat(),
            "referenz": snap.referenz,
        },
        "heute": heute.isoformat(),
        "wochen": wochen_liste,
        "zeilen": zeilen,
    }
