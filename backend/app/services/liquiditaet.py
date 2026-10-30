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
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from .. import models
from ..models import PostenArt, PostenStatus, Richtung
from .feiertage import ist_bankarbeitstag, naechster_bankarbeitstag

CENT = Decimal("0.01")

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
    gerollt:   nimmt je Planschlüssel die auf den Rolltag verschobenen
               überfälligen Posten-/Bestellbeträge auf (für manuelle Verschiebungen).
    mit_budget: False = nur explizite Flüsse (ohne Budget/Restbudget).
    bestellungen: None = Mandantenstandard (freigegebene, bei
               `bestellungen_offen_einplanen` auch offene), "FREIGEGEBEN" = nur
               freigegebene, "KEINE" = gar keine; abgelöste stets wie bezahlte Posten.
    ohne_bestellung_id: diese Bestellung auslassen (Freigabeprüfung).
    """
    heute = heute or start
    konten = _lade_konten(db, mandant.id)
    konto_by_id = {k.id: k for k in konten}

    from .insolvenzgeld import igeld_fenster

    igeld = igeld_fenster(mandant)
    bestell_status = bestell_plan_status(mandant, bestellungen)
    # Projektion: was nur_offen auslässt (bezahlte Posten, vergangene Dauerraten/
    # Termine, abgelöste Bestellungen), an seiner Stelle in der vollen Sicht
    ausgelassen: dict[Key, dict[date, Decimal]] | None = (
        defaultdict(lambda: defaultdict(Decimal)) if nur_offen and mit_budget else None
    )
    plan = _explizite_fluesse(
        db, mandant, konto_by_id, start, ende, heute, nur_offen, szenario, gerollt,
        igeld, bestell_status, ohne_bestellung_id, ausgelassen,
    )

    # 4) Budget mit Restbudget-Logik
    if mit_budget:
        verbrauch = None
        if ausgelassen is not None:
            # Restbudget wie in der vollen Sicht: Erledigtes der Woche verbraucht das
            # Budget weiter – sonst bläht es sich in der Projektion wieder auf und
            # zählt neben dem Ist doppelt. Offene Posten verbrauchen es dort, wo die
            # Projektion sie führt (gerollt: nächster Bankarbeitstag nach heute).
            verbrauch = defaultdict(lambda: defaultdict(Decimal))
            for quelle in (plan, ausgelassen):
                for key, tageswerte in quelle.items():
                    for d, v in tageswerte.items():
                        verbrauch[key][d] += v
        _budget_einarbeiten(db, mandant, konto_by_id, plan, start, ende, szenario, igeld,
                            verbrauch=verbrauch)

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
    ausgelassen: dict[Key, dict[date, Decimal]] | None = None,
) -> dict[Key, dict[date, Decimal]]:
    """Explizite Planflüsse (Schritte 1–3b von plan_fluesse, ohne Budget).

    ausgelassen: nimmt bei nur_offen die als erledigt ausgelassenen Flüsse an ihrem
                 Tag der vollen Sicht auf (Restbudget der Projektion)."""
    bl = mandant.bundesland
    plan: dict[Key, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))

    from .insolvenzgeld import termin_entlastung

    szenario_faktor = _szenario_aufloeser(szenario)
    verzoegerung = szenario.debitoren_verzoegerung_tage if szenario else 0

    if nur_offen:
        roll_ziel = naechster_bankarbeitstag(max(start, heute + timedelta(days=1)), bl)
    else:
        roll_ziel = naechster_bankarbeitstag(max(start, heute), bl)

    # 1) Offene Posten (inkl. im Fenster bezahlter, für die Plan-zum-Start-Sicht)
    posten = db.scalars(
        select(models.OffenerPosten).where(
            models.OffenerPosten.mandant_id == mandant.id,
            models.OffenerPosten.status != PostenStatus.STORNIERT.value,
        )
    )
    for p in posten:
        # Zahlungssperre: Insolvenzforderungen (§ 38 InsO) werden nicht bedient
        if (
            p.art == PostenArt.KREDITOR.value
            and p.forderungsklasse == models.Forderungsklasse.INSOLVENZFORDERUNG.value
        ):
            continue
        zahltag = p.zahlung_geplant_am or p.faellig_am
        if p.art == PostenArt.DEBITOR.value and verzoegerung:
            zahltag = zahltag + timedelta(days=verzoegerung)
        if p.status == PostenStatus.BEZAHLT.value:
            if nur_offen and ausgelassen is None:
                continue
            if p.bezahlt_am is None or p.bezahlt_am < start:
                continue
            d = zahltag if zahltag >= start else naechster_bankarbeitstag(start, bl)
            grundbetrag = p.betrag_brutto
        else:  # OFFEN: nur den Restbetrag planen (Teilzahlungen sind abgezogen)
            grundbetrag = p.betrag_brutto - (p.bezahlt_betrag or Decimal("0"))
            if grundbetrag <= 0:
                continue
            grenze = max(start, heute + timedelta(days=1)) if nur_offen else max(start, heute)
            d = zahltag if zahltag >= grenze else roll_ziel
        if d > ende:
            continue
        if p.art == PostenArt.DEBITOR.value:
            f = szenario_faktor(konto_by_id.get(p.konto_id), Richtung.EIN.value)
            betrag = (grundbetrag * f).quantize(CENT)
        else:
            betrag = -grundbetrag
        key: Key = p.konto_id if p.konto_id is not None else f"TERMIN:OP_{p.art}"
        if nur_offen and p.status == PostenStatus.BEZAHLT.value:
            ausgelassen[key][d] += betrag   # bezahlt: nur fürs Restbudget
            continue
        plan[key][d] += betrag
        if gerollt is not None and p.status != PostenStatus.BEZAHLT.value and d == roll_ziel \
                and zahltag < d:
            gerollt[key] = gerollt.get(key, Decimal("0")) + betrag

    # 2) Dauerbuchungen: Raster mit Vorlauf erzeugen, erst verschieben, dann filtern,
    #    damit Raten nicht an der Fenstergrenze verloren gehen (z. B. 01.11. = Sonntag)
    dauer = db.scalars(
        select(models.Dauerbuchung).where(
            models.Dauerbuchung.mandant_id == mandant.id,
            models.Dauerbuchung.aktiv.is_(True),
        )
    )
    for db_ in dauer:
        d_konto = konto_by_id.get(db_.konto_id) if db_.konto_id else None
        for roh in _dauer_termine(db_, start - timedelta(days=31), ende):
            d = naechster_bankarbeitstag(roh, bl)
            if d < start or d > ende:
                continue
            if nur_offen and d <= heute and ausgelassen is None:
                continue
            # Insolvenzgeld: Personal-/SV-Dauerbuchungen im Zeitraum entfallen
            # (Nettolöhne über Igeld/Vorfinanzierung, SV-Beiträge nach § 175 SGB III)
            if (
                igeld
                and db_.art == PostenArt.KREDITOR.value
                and d_konto is not None
                and d_konto.typ in (models.KontoTyp.PERSONAL.value, models.KontoTyp.SV.value)
                and igeld[0] <= d <= igeld[1]
            ):
                continue
            if db_.art == PostenArt.DEBITOR.value:
                f = szenario_faktor(d_konto, Richtung.EIN.value)
                betrag = (db_.betrag_brutto * f).quantize(CENT)
            else:
                betrag = -db_.betrag_brutto
            key = db_.konto_id if db_.konto_id is not None else f"TERMIN:DAUER_{db_.art}"
            if nur_offen and d <= heute:
                ausgelassen[key][d] += betrag   # vergangen: nur fürs Restbudget
                continue
            plan[key][d] += betrag

    # 3) Zahlungstermine (SV/Steuern); betrag > 0 = Auszahlung
    termine = db.scalars(
        select(models.Zahlungstermin).where(
            models.Zahlungstermin.mandant_id == mandant.id,
            models.Zahlungstermin.datum >= start,
            models.Zahlungstermin.datum <= ende,
        )
    )
    for t in termine:
        erledigt = nur_offen and (
            t.datum <= heute or t.status == models.TerminStatus.ERLEDIGT.value
        )
        if erledigt and ausgelassen is None:
            continue
        betrag = t.betrag
        if igeld:
            betrag = betrag - termin_entlastung(t, igeld)
            if betrag <= 0:
                continue
        key = _termin_key(t.typ, t.konto_id)
        if erledigt:
            ausgelassen[key][t.datum] += -betrag   # nur fürs Restbudget
            continue
        plan[key][t.datum] += -betrag

    # 3b) Bestellungen: brutto zum erwarteten Zahldatum, vor dem Budget (verbrauchen
    #     das Restbudget ihres Kontos); kein Szenariofaktor (wie Kreditor-Posten)
    if bestell_status is not None:
        abgeloest = models.BestellStatus.ABGELOEST.value
        bestellungen = db.scalars(
            select(models.Bestellung)
            .options(selectinload(models.Bestellung.abloesungen))
            .where(
                models.Bestellung.mandant_id == mandant.id,
                models.Bestellung.status.in_(sorted(bestell_status | {abgeloest})),
            )
        )
        for b in bestellungen:
            if b.id == ohne_bestellung_id:
                continue
            key = b.konto_id if b.konto_id is not None else BESTELL_KEY
            if b.status == abgeloest:
                # wie ein bezahlter Posten: voller Betrag (Plan − Ist zeigt die
                # Minderlieferung), nie in der Projektion; durch eine Rechnung
                # abgelöst trägt der offene Posten die Zahlung
                if (b.abgeloest_durch == models.AbloeseArt.RECHNUNG.value
                        or b.abgeloest_am is None or b.abgeloest_am < start
                        or (nur_offen and ausgelassen is None)):
                    continue
                tag = min(b.zahldatum, b.abgeloest_am)
                d = tag if tag >= start else naechster_bankarbeitstag(start, bl)
                if d > ende:
                    continue
                (ausgelassen if nur_offen else plan)[key][d] -= _brutto(b.betrag_netto, b.ust_satz)
                continue
            rest = bestellung_rest(b)
            if rest <= 0:
                continue
            grenze = max(start, heute + timedelta(days=1)) if nur_offen else max(start, heute)
            d = b.zahldatum if b.zahldatum >= grenze else roll_ziel
            if d > ende:
                continue
            plan[key][d] -= rest
            if gerollt is not None and d == roll_ziel and b.zahldatum < d:
                gerollt[key] = gerollt.get(key, Decimal("0")) - rest
    return plan


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
# Eine Verschiebung bewegt einen (Teil-)Betrag einer Planzeile vom Tag `von`
# auf den Tag `nach` (Vorzeichen wie der Zahlungsfluss). Sie wirkt dauerhaft,
# ist aber stets auf den dort tatsächlich geplanten Betrag begrenzt – ändert
# sich die Planbasis (Posten bezahlt, Dauerbuchung beendet), entsteht kein
# Phantombetrag. Ist `von` vorbei, der Zieltag aber noch offen, sind
# überfällige Posten inzwischen auf den Rolltag gewandert und werden von dort
# genommen. Vorträge (Quelle VORTRAG) wirken nur bis zu ihrem Zieltag – danach
# ist der noch offene Betrag wieder regulär vorzutragen.

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


def _extern_plan(db: Session, mandant: models.Mandant, heute: date):
    """Planbetrag eines Tages außerhalb des Fensters (wochenweise gecacht)."""
    cache: dict[date, dict] = {}

    def verfuegbar(key: Key, tag: date) -> Decimal:
        w = wochen_start(tag)
        if w not in cache:
            cache[w] = plan_fluesse(db, mandant, w, w + timedelta(days=6), heute=heute)
        return cache[w].get(key, {}).get(tag, Decimal("0"))

    return verfuegbar


def verschiebungen_anwenden(
    plan: dict[Key, dict[date, Decimal]], verschiebungen: list, start: date, ende: date,
    heute: date, roll_ziel: date, gerollt: dict[Key, Decimal], extern,
) -> list[tuple]:
    """Wendet Plan-Verschiebungen auf `plan` an. Liefert die tatsächlichen
    Bewegungen (key, ursprungstag, zieltag, betrag, art, verschiebung_id) mit
    art "fest" (vom Ursprungstag) oder "rollend" (vom Rolltag überfälliger Posten)."""
    bewegungen: list[tuple] = []
    pool = dict(gerollt)
    for v in verschiebungen:
        key = verschiebung_key(v)
        vz = Decimal(1) if v.betrag > 0 else Decimal(-1)
        rest = abs(v.betrag)
        ziel_im_fenster = start <= v.nach <= ende
        im_fenster = start <= v.von <= ende
        verfuegbar = plan.get(key, {}).get(v.von, Decimal(0)) if im_fenster else extern(key, v.von)
        if verfuegbar * vz > 0:
            anteil = min(rest, abs(verfuegbar))
            if im_fenster:
                plan[key][v.von] -= vz * anteil
            if ziel_im_fenster:
                plan[key][v.nach] += vz * anteil
            bewegungen.append((key, v.von, v.nach, vz * anteil, "fest", v.id))
            rest -= anteil
        if (rest > 0 and v.von <= heute < v.nach and v.von != roll_ziel
                and start <= roll_ziel <= ende):
            am_rolltag = plan.get(key, {}).get(roll_ziel, Decimal(0))
            gerollt_rest = pool.get(key, Decimal(0))
            if am_rolltag * vz > 0 and gerollt_rest * vz > 0:
                anteil = min(rest, abs(am_rolltag), abs(gerollt_rest))
                plan[key][roll_ziel] -= vz * anteil
                pool[key] = gerollt_rest - vz * anteil
                if ziel_im_fenster:
                    plan[key][v.nach] += vz * anteil
                bewegungen.append((key, roll_ziel, v.nach, vz * anteil, "rollend", v.id))
    return bewegungen


def verschiebungen_projektion(
    projektion: dict[Key, dict[date, Decimal]], verschiebungen: list, bewegungen: list[tuple],
    start: date, ende: date, heute: date, roll_ziel: date, gerollt: dict[Key, Decimal],
    ist_liquide,
) -> None:
    """Überträgt die Verschiebungen auf die Restplan-Sicht (nur noch offene
    Zahlungen): künftige Ursprungstage werden entlastet, bei vergangenem
    Ursprungstag nur überfällige, auf den Rolltag gewanderte Posten (Dauer-
    buchungen und Termine sind dort ohnehin nicht mehr enthalten). Der Zieltag
    erhält den Betrag, solange er in der Zukunft liegt."""
    bewegt: dict[int, Decimal] = defaultdict(Decimal)
    for _, _, _, betrag, _, vid in bewegungen:
        bewegt[vid] += betrag
    pool = dict(gerollt)
    for v in verschiebungen:
        menge = bewegt.get(v.id, Decimal(0))
        key = verschiebung_key(v)
        if not menge or not ist_liquide(key):
            continue
        vz = Decimal(1) if menge > 0 else Decimal(-1)
        zubuchen = menge
        if v.von > heute:
            if start <= v.von <= ende:
                verfuegbar = projektion.get(key, {}).get(v.von, Decimal(0))
                anteil = min(abs(menge), abs(verfuegbar)) if verfuegbar * vz > 0 else Decimal(0)
                projektion[key][v.von] -= vz * anteil
                zubuchen = vz * anteil
        elif start <= roll_ziel <= ende:
            am_rolltag = projektion.get(key, {}).get(roll_ziel, Decimal(0))
            gerollt_rest = pool.get(key, Decimal(0))
            if am_rolltag * vz > 0 and gerollt_rest * vz > 0:
                anteil = min(abs(menge), abs(am_rolltag), abs(gerollt_rest))
                projektion[key][roll_ziel] -= vz * anteil
                pool[key] = gerollt_rest - vz * anteil
        if zubuchen and v.nach > heute and start <= v.nach <= ende:
            projektion[key][v.nach] += zubuchen


def vortrag_verschiebungen_anwenden(
    vortrag: dict[Key, dict[date, Decimal]], verschiebungen: list, start: date, ende: date,
) -> list[tuple]:
    """Vorträge liegen auf dem aktuellen Vortragstag – von dort wird (begrenzt
    auf den noch offenen Vortrag) auf den Zieltag verschoben."""
    bewegungen: list[tuple] = []
    for v in verschiebungen:
        key = verschiebung_key(v)
        vz = Decimal(1) if v.betrag > 0 else Decimal(-1)
        rest = abs(v.betrag)
        for tag in sorted(vortrag.get(key, {})):
            verfuegbar = vortrag[key][tag]
            if verfuegbar * vz <= 0 or tag == v.nach:
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


def dauer_vortraege(
    db: Session,
    mandant: models.Mandant,
    start: date,
    ende: date,
    heute: date,
    finanz_nrn: set[str],
    szenario: models.Szenario | None = None,
    verschiebungen: list[tuple[int, date, date, Decimal]] | None = None,
) -> list[dict]:
    """Fällige, aber nicht (vollständig) gebuchte Zahlungen aus
    Dauerschuldverhältnissen.

    Betrachtet werden Fälligkeiten im Fenster bis einschließlich heute (nur
    Dauerschuldverhältnisse mit Konto und „vortragen“). Ist-Zahlungen gleicher
    Richtung auf ihrem Konto werden angerechnet, jede höchstens einmal:
      1. betragsgleiche Zahlungen bis 14 Tage um die Fälligkeit (trennt z. B.
         zwei Raten am selben Tag sauber),
      2. danach chronologisch alle übrigen Zahlungen ab 14 Tage vor der
         Fälligkeit – Teilzahlungen mindern den Vortrag, der nicht verbrauchte
         Rest einer angerechneten Zahlung gilt als Vorauszahlung für spätere
         Fälligkeiten.
    Der ungedeckte Rest wird auf den nächsten Bankarbeitstag ab morgen
    vorgetragen (sofern im Fenster). Manuell verschobene Raten
    (`verschiebungen`: Konto, Ursprungstag, Zieltag, Betrag) sind statt am
    Ursprungstag am Zieltag fällig."""
    bl = mandant.bundesland
    ziel = naechster_bankarbeitstag(heute + timedelta(days=1), bl)
    bis = min(heute, ende)
    if ziel > ende or bis < start:
        return []
    weg: dict[tuple[int, date], list[list]] = defaultdict(list)
    for konto_id, von, nach, betrag in verschiebungen or []:
        weg[(konto_id, von)].append([nach, betrag])
    von_min = min([start] + [von for _, von in weg])
    bis_max = max([bis] + [von for _, von in weg])

    from .insolvenzgeld import igeld_fenster

    igeld = igeld_fenster(mandant)
    szenario_faktor = _szenario_aufloeser(szenario)
    faellig: list[tuple[date, models.Dauerbuchung, Decimal]] = []
    for d_ in db.scalars(
        select(models.Dauerbuchung).where(
            models.Dauerbuchung.mandant_id == mandant.id,
            models.Dauerbuchung.aktiv.is_(True),
            models.Dauerbuchung.vortragen.is_(True),
            models.Dauerbuchung.konto_id.is_not(None),
        )
    ):
        for roh in _dauer_termine(d_, von_min - timedelta(days=31), bis_max):
            d = naechster_bankarbeitstag(roh, bl)
            im_fenster = start <= d <= bis
            if not im_fenster and (d_.konto_id, d) not in weg:
                continue
            if (
                igeld
                and d_.art == PostenArt.KREDITOR.value
                and d_.konto is not None
                and d_.konto.typ in (models.KontoTyp.PERSONAL.value, models.KontoTyp.SV.value)
                and igeld[0] <= d <= igeld[1]
            ):
                continue  # im Insolvenzgeldzeitraum gar nicht geplant
            soll = d_.betrag_brutto if d_.art == PostenArt.DEBITOR.value else -d_.betrag_brutto
            vz = Decimal(1) if soll > 0 else Decimal(-1)
            for eintrag in weg.get((d_.konto_id, d), []):
                anteil = min(abs(eintrag[1]), abs(soll)) if eintrag[1] * soll > 0 else Decimal(0)
                if not anteil:
                    continue
                eintrag[1] -= vz * anteil
                soll -= vz * anteil
                if start <= eintrag[0] <= bis:   # verschobener Teil: am Zieltag fällig
                    faellig.append((eintrag[0], d_, vz * anteil))
            if im_fenster and soll:
                faellig.append((d, d_, soll))
    if not faellig:
        return []

    # einzelne Ist-Zahlungen je Sachkonto (nicht Tagessummen: zwei Raten am
    # selben Tag sollen einzeln zählen); [Datum, Betrag, noch verfügbar]
    zahlungen: dict[str, list[list]] = defaultdict(list)
    for b in db.scalars(
        select(models.Buchung)
        .where(
            models.Buchung.mandant_id == mandant.id,
            models.Buchung.datum >= start - timedelta(days=VORTRAG_VORLAUF_TAGE),
            models.Buchung.datum <= heute,
        )
        .order_by(models.Buchung.datum, models.Buchung.id)
    ):
        fluss = _sachfluss(b, finanz_nrn)
        if fluss is not None:
            zahlungen[fluss[0]].append([b.datum, fluss[1], abs(fluss[1])])

    faellig.sort(key=lambda x: (x[0], x[1].id))
    offen = [abs(soll) for _, _, soll in faellig]
    fenster = timedelta(days=VORTRAG_VORLAUF_TAGE)

    # 1. betragsgleiche Zahlung nahe der Fälligkeit
    for i, (d, d_, soll) in enumerate(faellig):
        kandidaten = [
            (abs((z[0] - d).days), j)
            for j, z in enumerate(zahlungen.get(d_.konto.nummer, []))
            if z[2] == abs(z[1]) and z[1] * soll > 0
            and abs(z[0] - d) <= fenster and _betragsgleich(z[1], soll)
        ]
        if kandidaten:
            zahlungen[d_.konto.nummer][min(kandidaten)[1]][2] = Decimal("0")
            offen[i] = Decimal("0")

    # 2. übrige Zahlungen chronologisch anrechnen (Teilzahlungen, Vorauszahlungen)
    for i, (d, d_, soll) in enumerate(faellig):
        for z in zahlungen.get(d_.konto.nummer, []):
            if offen[i] <= 0:
                break
            angebrochen = Decimal("0") < z[2] < abs(z[1])
            if z[2] <= 0 or z[1] * soll <= 0 or (z[0] < d - fenster and not angebrochen):
                continue
            anteil = min(z[2], offen[i])
            z[2] -= anteil
            offen[i] -= anteil

    vortraege: list[dict] = []
    for i, (d, d_, soll) in enumerate(faellig):
        if offen[i] <= 0:
            continue
        rest = offen[i] if soll > 0 else -offen[i]
        if d_.art == PostenArt.DEBITOR.value:
            rest = (rest * szenario_faktor(d_.konto, Richtung.EIN.value)).quantize(CENT)
        vortraege.append({
            "dauer_id": d_.id,
            "name": d_.name,
            "konto_id": d_.konto_id,
            "faellig": d,
            "ziel": ziel,
            "soll": soll,
            "gezahlt": abs(soll) - offen[i],
            "betrag": rest,
        })
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
) -> None:
    """Budget je Konto und Woche als Restbudget in `plan` einarbeiten.

    verbrauch: explizite Flüsse, die das Wochenbudget verbrauchen (Standard: `plan`
               selbst); die Projektion übergibt die volle Sicht."""
    bl = mandant.bundesland
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

    # Wochen des Fensters
    w = wochen_start(start)
    wochen: list[tuple[date, date]] = []
    while w <= ende:
        wochen.append((w, min(w + timedelta(days=6), ende)))
        w += timedelta(days=7)

    szenario_faktor = _szenario_aufloeser(szenario)

    for konto_id in konto_ids:
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
            gesamt = sum(basis.values(), Decimal("0"))
            if gesamt <= 0:
                continue
            # Restbudget: bereits explizit geplante Beträge dieses Kontos in der Woche
            tageswerte = explizit_quelle.get(konto_id, {})
            explizit = sum(
                (abs(tageswerte.get(w_von + timedelta(days=i), Decimal("0"))) for i in range(7)),
                Decimal("0"),
            )
            rest = max(Decimal("0"), gesamt - explizit).quantize(CENT)
            if rest == 0:
                continue
            skalierung = rest / gesamt
            tage_sortiert = sorted(basis)
            verteilt = Decimal("0")
            for i, d in enumerate(tage_sortiert):
                if i == len(tage_sortiert) - 1:
                    betrag = rest - verteilt
                else:
                    betrag = (basis[d] * skalierung).quantize(CENT)
                    verteilt += betrag
                plan[konto_id][d] += vorzeichen * betrag


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


def snapshot_fluesse(snap: models.PlanSnapshot) -> dict[Key, dict[date, Decimal]]:
    werte: dict[Key, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for w in snap.werte:
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

    # ---- Plan (aktuelle Erwartung) und Projektionsplan ----
    gerollt_plan: dict[Key, Decimal] = {}
    gerollt_proj: dict[Key, Decimal] = {}
    plan = plan_fluesse(db, mandant, start, ende, heute=heute, szenario=szenario,
                        gerollt=gerollt_plan)
    plan_projektion = plan_fluesse(
        db, mandant, start, ende, heute=heute, nur_offen=True, szenario=szenario,
        gerollt=gerollt_proj,
    )

    gruppen = list(
        db.scalars(
            select(models.KontoGruppe)
            .where(models.KontoGruppe.mandant_id == mandant.id)
            .order_by(models.KontoGruppe.sortierung, models.KontoGruppe.id)
        )
    )
    gruppe_fuer_key = gruppen_aufloeser(konto_by_id, gruppen)

    # ---- manuelle Verschiebungen (Plan und Restplan-Sicht) ----
    bl = mandant.bundesland
    verschiebungen = plan_verschiebungen(db, mandant.id, "PLAN")
    plan_bewegungen = verschiebungen_anwenden(
        plan, verschiebungen, start, ende, heute,
        naechster_bankarbeitstag(max(start, heute), bl), gerollt_plan,
        _extern_plan(db, mandant, heute),
    )
    verschiebungen_projektion(
        plan_projektion, verschiebungen, plan_bewegungen, start, ende, heute,
        naechster_bankarbeitstag(max(start, heute + timedelta(days=1)), bl), gerollt_proj,
        lambda key: liquiditaets_richtung(key, konto_by_id, gruppe_fuer_key) is not None,
    )

    # ---- Vortrag nicht gebuchter Dauerschuld-Zahlungen (Ansicht „mit Vortrag“) ----
    vortraege = dauer_vortraege(
        db, mandant, start, ende, heute, finanz_nrn, szenario,
        verschiebungen=[(key, tag, nach, betrag) for key, tag, nach, betrag, art, _ in plan_bewegungen
                        if art == "fest" and isinstance(key, int)],
    )
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

    verschoben = markierung(plan_bewegungen)
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
    gerollt: dict[Key, Decimal] = {}
    plan = plan_fluesse(db, mandant, start, ende, heute=start, gerollt=gerollt)
    # manuelle Verschiebungen gehören zum Plan (Sicht zum Stichtag)
    verschiebungen_anwenden(
        plan, plan_verschiebungen(db, mandant.id, "PLAN"), start, ende, start,
        naechster_bankarbeitstag(start, mandant.bundesland), gerollt,
        _extern_plan(db, mandant, start),
    )
    snap = models.PlanSnapshot(
        mandant_id=mandant.id, stichtag=start, wochen=wochen, kommentar=kommentar
    )
    db.add(snap)
    db.flush()
    for key, tageswerte in plan.items():
        for d, v in tageswerte.items():
            if not v:
                continue
            db.add(
                models.PlanSnapshotWert(
                    snapshot_id=snap.id,
                    konto_id=key if isinstance(key, int) else None,
                    termin_typ=None if isinstance(key, int) else str(key).replace("TERMIN:", ""),
                    datum=d,
                    betrag=v,
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

    plan: dict[Key, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for w in snap.werte:
        key: Key = w.konto_id if w.konto_id is not None else f"TERMIN:{w.termin_typ}"
        plan[key][w.datum] += w.betrag

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
