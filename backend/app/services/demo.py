"""Beispielmandant mit realistischen Daten für die erste Sichtprüfung."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..models import BestandTyp, Intervall, PostenArt
from .kontenrahmen import lege_kontenrahmen_an
from .liquiditaet import wochen_start
from .zahlungskalender import generiere_termine


def lege_demo_mandant_an(db: Session, heute: date | None = None) -> models.Mandant | None:
    if db.scalar(select(models.Mandant.id).where(models.Mandant.kurzname == "demo")):
        return None
    heute = heute or date.today()
    start = wochen_start(heute)

    mandant = models.Mandant(
        name="Muster GmbH (Demo)",
        kurzname="demo",
        kontenrahmen="SKR03",
        bundesland="NW",
        ust_zeitraum="MONAT",
        dauerfrist=True,
        verfahrensstatus="VORLAEUFIG",
        aktenzeichen="70 IN 123/26",
        insolvenz_stichtag=start - timedelta(days=21),
    )
    db.add(mandant)
    db.flush()
    lege_kontenrahmen_an(db, mandant)

    konto = {
        k.nummer: k
        for k in db.scalars(select(models.Konto).where(models.Konto.mandant_id == mandant.id))
    }

    # Anfangsbestände (Stand: Sonntag vor Fensterbeginn)
    stichtag = start - timedelta(days=1)
    db.add_all(
        [
            models.Bestand(mandant_id=mandant.id, typ=BestandTyp.BANK.value,
                           konto_id=konto["1200"].id, datum=stichtag, wert=Decimal("41250.00")),
            models.Bestand(mandant_id=mandant.id, typ=BestandTyp.KASSE.value,
                           konto_id=konto["1000"].id, datum=stichtag, wert=Decimal("1830.00")),
            models.Bestand(mandant_id=mandant.id, typ=BestandTyp.WAREN.value,
                           datum=stichtag, wert=Decimal("87500.00")),
        ]
    )
    konto["1200"].kreditlinie = Decimal("25000.00")

    # Offene Eingangsrechnungen und Forderungen
    op = [
        (PostenArt.KREDITOR, "Energieversorger Stadtwerke", "4240", 12, "3480.50"),
        (PostenArt.KREDITOR, "Großhandel Nord GmbH", "3400", 5, "18740.00"),
        (PostenArt.KREDITOR, "Großhandel Nord GmbH", "3400", 19, "9310.00"),
        (PostenArt.KREDITOR, "Spedition Weber", "4900", 8, "2140.00"),
        (PostenArt.KREDITOR, "IT-Service Runge", "4920", -3, "890.00"),  # überfällig
        (PostenArt.DEBITOR, "Kunde Albrecht AG", "8400", 7, "22610.00"),
        (PostenArt.DEBITOR, "Kunde Behrens KG", "8400", 14, "15470.00"),
        (PostenArt.DEBITOR, "Kunde Colmar GmbH", "8300", 24, "6420.00"),
    ]
    for art, partner, knr, tage, betrag in op:
        db.add(
            models.OffenerPosten(
                mandant_id=mandant.id,
                art=art.value,
                partner=partner,
                faellig_am=heute + timedelta(days=tage),
                betrag_brutto=Decimal(betrag),
                konto_id=konto[knr].id,
            )
        )

    # Dauerbuchungen
    dauer = [
        ("Miete Halle + Büro", "4210", 1, "8900.00"),
        ("Leasing Fuhrpark", "4530", 15, "2450.00"),
        ("Versicherungspaket", "4360", 5, "1180.00"),
    ]
    for name, knr, stichtag_tag, betrag in dauer:
        db.add(
            models.Dauerbuchung(
                mandant_id=mandant.id,
                name=name,
                art=PostenArt.KREDITOR.value,
                konto_id=konto[knr].id,
                betrag_brutto=Decimal(betrag),
                intervall=Intervall.MONATLICH.value,
                stichtag=stichtag_tag,
                gueltig_von=heute - timedelta(days=365),
            )
        )

    # Budgets (netto, je Monat) für laufenden + 3 Folgemonate
    budgets = [
        ("8400", "95000.00"),
        ("8300", "12000.00"),
        ("3400", "-48000.00"),
        ("4110", "-28000.00"),
        ("4130", "-6500.00"),
        ("4920", "-450.00"),
        ("4930", "-600.00"),
    ]
    j, m = heute.year, heute.month
    for _ in range(4):
        for knr, betrag in budgets:
            db.add(
                models.Budget(
                    mandant_id=mandant.id,
                    konto_id=konto[knr].id,
                    jahr=j,
                    monat=m,
                    betrag_netto=abs(Decimal(betrag)),
                )
            )
        m += 1
        if m > 12:
            j, m = j + 1, 1

    # Terminregeln mit festen Beträgen füllen (Demo hat wenig Historie)
    for regel in db.scalars(
        select(models.TerminRegel).where(models.TerminRegel.mandant_id == mandant.id)
    ):
        regel.betrag_modus = "FIX"
        regel.betrag_fix = {
            "SV": Decimal("11200.00"),
            "UST_VA": Decimal("7400.00"),
            "LST": Decimal("5100.00"),
        }.get(regel.typ, Decimal("0"))

    # Ist-Buchungen der laufenden Woche (DATEV-Logik: Betrag positiv, S/H auf Konto)
    ist = [
        (0, "1200", "8400", "11900.00", "S", "ZE Kunde Albrecht AG Teilzahlung"),
        (0, "3400", "1200", "5950.00", "S", "Zahlung Großhandel Nord"),
        (1, "1200", "8300", "3210.00", "S", "Barverkauf Woche"),
        (1, "4210", "1200", "8900.00", "S", "Miete"),
        (2, "4920", "1200", "890.00", "S", "IT-Service Runge"),
    ]
    for offset, knr, gknr, betrag, sh, text in ist:
        d = start + timedelta(days=offset)
        if d > heute:
            continue
        db.add(
            models.Buchung(
                mandant_id=mandant.id,
                datum=d,
                konto_nr=knr,
                gegenkonto_nr=gknr,
                betrag=Decimal(betrag),
                sh=sh,
                text=text,
            )
        )

    db.commit()
    generiere_termine(db, mandant, start, start + timedelta(days=13 * 7 - 1), heute)
    return mandant
