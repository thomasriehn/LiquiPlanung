"""13-Wochen-Liquiditätsplanung.

Ist:   aus importierten Buchungen – liquiditätswirksam ist ein Satz, wenn genau eine
       Seite ein Finanzkonto (Bank/Kasse) ist; der Zahlungsfluss wird der Sachkonto-
       Seite zugeordnet (+ Einzahlung / − Auszahlung).
Plan:  offene Posten, Dauerbuchungen, Zahlungstermine, Budget (Restbudget-Logik).
       Der Plan wird für das gesamte Fenster berechnet ("Plan zum Fensterbeginn");
       für vergangene Tage wird – falls vorhanden – der eingefrorene Snapshot als
       Vergleichsbasis verwendet.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

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
    return list(
        db.scalars(
            select(models.Konto)
            .options(joinedload(models.Konto.gruppe))
            .where(models.Konto.mandant_id == mandant_id, models.Konto.aktiv.is_(True))
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
        k_fin = b.konto_nr in finanz_nrn
        g_fin = b.gegenkonto_nr in finanz_nrn
        if k_fin:
            bank[b.konto_nr][b.datum] += _fluss_auf_finanzkonto(b, b.konto_nr)
        if g_fin:
            bank[b.gegenkonto_nr][b.datum] += _fluss_auf_finanzkonto(b, b.gegenkonto_nr)
        if k_fin and not g_fin:
            # Fluss = Bestandsänderung der Finanzseite, zugeordnet dem Gegenkonto
            sach[b.gegenkonto_nr][b.datum] += _fluss_auf_finanzkonto(b, b.konto_nr)
        elif g_fin and not k_fin:
            sach[b.konto_nr][b.datum] += _fluss_auf_finanzkonto(b, b.gegenkonto_nr)
    return sach, bank


def _termin_key(t_typ: str, konto_id: int | None) -> Key:
    return konto_id if konto_id is not None else f"TERMIN:{t_typ}"


def plan_fluesse(
    db: Session,
    mandant: models.Mandant,
    start: date,
    ende: date,
) -> dict[Key, dict[date, Decimal]]:
    """Planzahlungen je Konto/Tag für das Fenster [start, ende]."""
    bl = mandant.bundesland
    plan: dict[Key, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    konten = _lade_konten(db, mandant.id)
    konto_by_id = {k.id: k for k in konten}

    def clip(d: date) -> date | None:
        if d < start:
            d = naechster_bankarbeitstag(start, bl)
        return d if d <= ende else None

    # 1) Offene Posten (inkl. im Fenster bezahlter, für die Plan-zum-Start-Sicht)
    posten = db.scalars(
        select(models.OffenerPosten).where(
            models.OffenerPosten.mandant_id == mandant.id,
            models.OffenerPosten.status != PostenStatus.STORNIERT.value,
        )
    )
    for p in posten:
        if p.status == PostenStatus.BEZAHLT.value and (
            p.bezahlt_am is None or p.bezahlt_am < start
        ):
            continue
        zahltag = p.zahlung_geplant_am or p.faellig_am
        d = clip(zahltag)
        if d is None:
            continue
        betrag = p.betrag_brutto if p.art == PostenArt.DEBITOR.value else -p.betrag_brutto
        key: Key = p.konto_id if p.konto_id is not None else f"TERMIN:OP_{p.art}"
        plan[key][d] += betrag

    # 2) Dauerbuchungen
    dauer = db.scalars(
        select(models.Dauerbuchung).where(
            models.Dauerbuchung.mandant_id == mandant.id,
            models.Dauerbuchung.aktiv.is_(True),
        )
    )
    for db_ in dauer:
        for d in _dauer_termine(db_, start, ende):
            d = naechster_bankarbeitstag(d, bl)
            if d > ende:
                continue
            betrag = db_.betrag_brutto if db_.art == PostenArt.DEBITOR.value else -db_.betrag_brutto
            key = db_.konto_id if db_.konto_id is not None else f"TERMIN:DAUER_{db_.art}"
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
        key = _termin_key(t.typ, t.konto_id)
        plan[key][t.datum] += -t.betrag

    # 4) Budget mit Restbudget-Logik
    _budget_einarbeiten(db, mandant, konto_by_id, plan, start, ende)
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


def _monatstage(jahr: int, monat: int) -> int:
    if monat == 12:
        return 31
    return (date(jahr, monat + 1, 1) - timedelta(days=1)).day


def _budget_einarbeiten(
    db: Session,
    mandant: models.Mandant,
    konto_by_id: dict[int, models.Konto],
    plan: dict[Key, dict[date, Decimal]],
    start: date,
    ende: date,
) -> None:
    bl = mandant.bundesland
    budgets = list(
        db.scalars(select(models.Budget).where(models.Budget.mandant_id == mandant.id))
    )
    overrides = {
        (bw.konto_id, bw.jahr, bw.kw): bw.betrag_netto
        for bw in db.scalars(
            select(models.BudgetWoche).where(models.BudgetWoche.mandant_id == mandant.id)
        )
    }
    monat_budget: dict[tuple[int, int, int], Decimal] = {}
    for b in budgets:
        monat_budget[(b.konto_id, b.jahr, b.monat)] = b.betrag_netto
    konto_ids = {b.konto_id for b in budgets} | {k for (k, _, _) in overrides}

    # Wochen des Fensters
    w = wochen_start(start)
    wochen: list[tuple[date, date]] = []
    while w <= ende:
        wochen.append((w, min(w + timedelta(days=6), ende)))
        w += timedelta(days=7)

    for konto_id in konto_ids:
        konto = konto_by_id.get(konto_id)
        if konto is None or not konto.liquiditaetswirksam:
            continue
        richtung = _konto_richtung(konto)
        if richtung == Richtung.INFO.value:
            continue
        vorzeichen = Decimal("1") if richtung == Richtung.EIN.value else Decimal("-1")
        for w_von, w_bis in wochen:
            iso = w_von.isocalendar()
            banktage = [
                w_von + timedelta(days=i)
                for i in range(7)
                if w_von + timedelta(days=i) <= w_bis
                and ist_bankarbeitstag(w_von + timedelta(days=i), bl)
            ]
            if not banktage:
                banktage = [w_von]
            # Tagesgenaue Basisverteilung (brutto): Wochen-Override gleichmäßig,
            # sonst Monatsbudget je Bankarbeitstag des jeweiligen Monats
            basis: dict[date, Decimal] = {}
            if (konto_id, iso.year, iso.week) in overrides:
                netto = abs(overrides[(konto_id, iso.year, iso.week)])
                if netto:
                    anteil = _brutto(netto, konto.ust_satz) / len(banktage)
                    for d in banktage:
                        basis[d] = anteil
            else:
                for d in banktage:
                    mb = monat_budget.get((konto_id, d.year, d.month))
                    if mb is None:
                        continue
                    bt = bankarbeitstage_anzahl(d.year, d.month, bl)
                    if bt:
                        basis[d] = _brutto(abs(mb), konto.ust_satz) / bt
            gesamt = sum(basis.values(), Decimal("0"))
            if gesamt <= 0:
                continue
            # Restbudget: bereits explizit geplante Beträge dieses Kontos in der Woche
            tageswerte = plan.get(konto_id, {})
            explizit = sum(
                (abs(tageswerte.get(w_von + timedelta(days=i), Decimal("0"))) for i in range(7)),
                Decimal("0"),
            )
            rest = max(Decimal("0"), gesamt - explizit).quantize(CENT)
            if rest == 0:
                continue
            faktor = rest / gesamt
            tage_sortiert = sorted(basis)
            verteilt = Decimal("0")
            for i, d in enumerate(tage_sortiert):
                if i == len(tage_sortiert) - 1:
                    betrag = rest - verteilt
                else:
                    betrag = (basis[d] * faktor).quantize(CENT)
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


def _snapshot_plan(
    db: Session, mandant_id: int, start: date, heute: date
) -> tuple[dict[Key, dict[date, Decimal]] | None, models.PlanSnapshot | None]:
    snap = db.scalars(
        select(models.PlanSnapshot)
        .where(
            models.PlanSnapshot.mandant_id == mandant_id,
            models.PlanSnapshot.stichtag <= heute,
        )
        .order_by(models.PlanSnapshot.stichtag.desc(), models.PlanSnapshot.id.desc())
    ).first()
    if snap is None:
        return None, None
    werte: dict[Key, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for w in snap.werte:
        key: Key = w.konto_id if w.konto_id is not None else f"TERMIN:{w.termin_typ}"
        werte[key][w.datum] += w.betrag
    return werte, snap


def berechne_plan(
    db: Session,
    mandant: models.Mandant,
    start: date | None = None,
    wochen: int = 13,
    heute: date | None = None,
) -> dict:
    heute = heute or date.today()
    start = wochen_start(start or heute)
    ende = start + timedelta(days=wochen * 7 - 1)
    tage = [start + timedelta(days=i) for i in range(wochen * 7)]

    konten = _lade_konten(db, mandant.id)
    konto_by_id = {k.id: k for k in konten}
    konto_by_nr = {k.nummer: k for k in konten}
    finanzkonten = [
        k for k in konten if k.typ in (models.KontoTyp.BANK.value, models.KontoTyp.KASSE.value)
    ]
    finanz_nrn = {k.nummer for k in finanzkonten}

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

    # ---- Plan (zum Fensterbeginn) ----
    plan = plan_fluesse(db, mandant, start, ende)

    # ---- Vergleichsbasis für vergangene Tage ----
    snap_plan, snap = _snapshot_plan(db, mandant.id, start, heute)
    vergleichsbasis = "SNAPSHOT" if snap_plan is not None else "LIVE"
    basis = snap_plan if snap_plan is not None else plan

    # ---- Zeilenstruktur ----
    gruppen = list(
        db.scalars(
            select(models.KontoGruppe)
            .where(models.KontoGruppe.mandant_id == mandant.id)
            .order_by(models.KontoGruppe.sortierung, models.KontoGruppe.id)
        )
    )
    gruppe_by_code = {g.code: g for g in gruppen}

    def gruppe_fuer_key(key: Key) -> models.KontoGruppe | None:
        if isinstance(key, int):
            k = konto_by_id.get(key)
            return k.gruppe if k else None
        if key.startswith("TERMIN:SV"):
            return gruppe_by_code.get("A_SV") or gruppe_by_code.get("A_STEUER")
        if key.startswith("TERMIN:OP_DEBITOR") or key.startswith("TERMIN:DAUER_DEBITOR"):
            return gruppe_by_code.get("E_SONST")
        if key.startswith("TERMIN:"):
            return gruppe_by_code.get("A_STEUER")
        return None

    alle_keys = set(plan.keys()) | set(ist.keys())
    zeilen_je_gruppe: dict[int | None, list[Key]] = defaultdict(list)
    for key in alle_keys:
        g = gruppe_fuer_key(key)
        if isinstance(key, int):
            k = konto_by_id[key]
            if k.typ in (models.KontoTyp.BANK.value, models.KontoTyp.KASSE.value):
                continue  # Finanzkonten erscheinen im Bestandsblock
            if not k.liquiditaetswirksam:
                continue
        zeilen_je_gruppe[g.id if g else None].append(key)

    def key_sort(key: Key):
        if isinstance(key, int):
            return (0, konto_by_id[key].nummer)
        return (1, key)

    def zeile_fuer_key(key: Key) -> dict:
        if isinstance(key, int):
            k = konto_by_id[key]
            name, nummer, ust = k.bezeichnung, k.nummer, k.ust_satz
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
            }
            nummer, name, ust = "", benennung.get(key, key), None
        return {
            "key": str(key),
            "typ": "konto",
            "nummer": nummer,
            "name": name,
            "ust_satz": _f(ust) if ust is not None else None,
            "plan": {d.isoformat(): _f(v) for d, v in plan.get(key, {}).items() if v},
            "ist": {d.isoformat(): _f(v) for d, v in ist.get(key, {}).items() if v},
            "basis": {d.isoformat(): _f(v) for d, v in basis.get(key, {}).items() if v},
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
            g = gruppe_fuer_key(key)
            richtung = g.richtung if g else (
                _konto_richtung(konto_by_id[key]) if isinstance(key, int) else Richtung.AUS.value
            )
            if richtung == Richtung.INFO.value:
                continue
            if isinstance(key, int):
                k = konto_by_id[key]
                if k.typ in (models.KontoTyp.BANK.value, models.KontoTyp.KASSE.value):
                    continue
                if not k.liquiditaetswirksam:
                    continue
            for d, v in tageswerte.items():
                if nur == "EIN" and v <= 0:
                    continue
                if nur == "AUS" and v >= 0:
                    continue
                s[d] += v
        return {d.isoformat(): _f(v) for d, v in s.items() if v}

    summen = {
        "einzahlungen": {"plan": summe_je_tag(plan, "EIN"), "ist": summe_je_tag(ist, "EIN"),
                         "basis": summe_je_tag(basis, "EIN")},
        "auszahlungen": {"plan": summe_je_tag(plan, "AUS"), "ist": summe_je_tag(ist, "AUS"),
                         "basis": summe_je_tag(basis, "AUS")},
        "netto": {"plan": summe_je_tag(plan, None), "ist": summe_je_tag(ist, None),
                  "basis": summe_je_tag(basis, None)},
    }

    # ---- Bestände ----
    bestaende = _bestaende(db, mandant, finanzkonten, finanz_nrn, tage, heute, plan)

    return {
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
             "erstellt_am": snap.erstellt_am.isoformat()}
            if snap else None
        ),
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
    plan: dict[Key, dict[date, Decimal]],
) -> dict:
    start, ende = tage[0], tage[-1]
    anker: dict[int, tuple[date, Decimal]] = {}
    for k in finanzkonten:
        b = db.scalars(
            select(models.Bestand)
            .where(
                models.Bestand.mandant_id == mandant.id,
                models.Bestand.konto_id == k.id,
                models.Bestand.datum <= heute,
            )
            .order_by(models.Bestand.datum.desc(), models.Bestand.id.desc())
        ).first()
        # Bestand.wert = Kontostand zum Ende des Tages `datum`
        anker[k.id] = (b.datum, b.wert) if b else (start - timedelta(days=1), Decimal("0"))

    # Bewegungen ab frühestem Anker bis heute
    frueh = min([a[0] for a in anker.values()] + [start]) + timedelta(days=1)
    _, bank_bewegungen = ist_zahlungsfluesse(db, mandant.id, finanz_nrn, frueh, min(heute, ende))

    konto_bestaende: list[dict] = []
    summe_je_tag: dict[date, Decimal] = defaultdict(Decimal)
    linien = Decimal("0")
    for k in finanzkonten:
        a_datum, a_wert = anker[k.id]
        bew = bank_bewegungen.get(k.nummer, {})
        werte: dict[str, float | None] = {}
        # vorwärts vom Anker
        stand = a_wert
        laufend: dict[date, Decimal] = {a_datum: a_wert}
        d = a_datum + timedelta(days=1)
        while d <= min(heute, ende):
            stand += bew.get(d, Decimal("0"))
            laufend[d] = stand
            d += timedelta(days=1)
        # rückwärts vom Anker (falls Anker im Fenster liegt)
        stand = a_wert
        d = a_datum
        while d > start:
            stand -= bew.get(d, Decimal("0"))
            d -= timedelta(days=1)
            laufend[d] = stand
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
                "anker": {"datum": anker[k.id][0].isoformat(), "wert": _f(anker[k.id][1])},
                "bestand": werte,
            }
        )

    # Gesamtliquidität: Ist bis heute, danach Fortschreibung über Plan-Netto
    plan_netto: dict[date, Decimal] = defaultdict(Decimal)
    for tageswerte in plan.values():
        for d, v in tageswerte.items():
            plan_netto[d] += v

    liquiditaet: dict[str, float] = {}
    verfuegbar: dict[str, float] = {}
    letzter_ist = None
    for d in tage:
        if d <= heute:
            wert = summe_je_tag.get(d)
            if wert is None:
                wert = Decimal("0")
            letzter_ist = wert
            liquiditaet[d.isoformat()] = _f(wert)
        else:
            basiswert = letzter_ist if letzter_ist is not None else Decimal("0")
            basiswert += plan_netto.get(d, Decimal("0"))
            letzter_ist = basiswert
            liquiditaet[d.isoformat()] = _f(basiswert)
        verfuegbar[d.isoformat()] = _f(Decimal(str(liquiditaet[d.isoformat()])) + linien)

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
    plan = plan_fluesse(db, mandant, start, ende)
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
    """Wochenweiser Soll-/Ist-Vergleich gegen einen eingefrorenen Plan."""
    heute = heute or date.today()
    if snapshot_id is not None:
        snap = db.get(models.PlanSnapshot, snapshot_id)
        if snap is None or snap.mandant_id != mandant.id:
            raise ValueError("Snapshot nicht gefunden")
    else:
        snap = db.scalars(
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
        },
        "heute": heute.isoformat(),
        "wochen": wochen_liste,
        "zeilen": zeilen,
    }
