from datetime import date, timedelta
from decimal import Decimal

from app import models
from app.services.kontenrahmen import lege_kontenrahmen_an
from app.services.liquiditaet import (
    berechne_plan,
    erstelle_snapshot,
    soll_ist_vergleich,
    wochen_start,
)

HEUTE = date(2026, 8, 5)   # Mittwoch
START = date(2026, 8, 3)   # Montag


def _mandant(db) -> models.Mandant:
    m = models.Mandant(name="Test GmbH", kurzname="test", kontenrahmen="SKR03", bundesland="NW")
    db.add(m)
    db.flush()
    lege_kontenrahmen_an(db, m)
    return m


def _konto(db, m, nummer) -> models.Konto:
    from sqlalchemy import select

    return db.scalar(
        select(models.Konto).where(
            models.Konto.mandant_id == m.id, models.Konto.nummer == nummer
        )
    )


def test_wochen_start():
    assert wochen_start(HEUTE) == START
    assert wochen_start(START) == START


def test_ist_fluesse_und_bestand(db):
    m = _mandant(db)
    bank = _konto(db, m, "1200")
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=bank.id,
                          datum=START - timedelta(days=1), wert=Decimal("10000")))
    # Zahlungseingang 1190 (Bank Soll), Zahlung 500 (Bank Haben)
    db.add(models.Buchung(mandant_id=m.id, datum=START, konto_nr="1200",
                          gegenkonto_nr="8400", betrag=Decimal("1190"), sh="S"))
    db.add(models.Buchung(mandant_id=m.id, datum=START + timedelta(days=1), konto_nr="3400",
                          gegenkonto_nr="1200", betrag=Decimal("500"), sh="S"))
    db.commit()

    plan = berechne_plan(db, m, heute=HEUTE)
    mo, di = START.isoformat(), (START + timedelta(days=1)).isoformat()

    umsatz = next(g for g in plan["zeilen"] if g["code"] == "E_UMSATZ")
    zeile_8400 = next(k for k in umsatz["kinder"] if k["nummer"] == "8400")
    assert zeile_8400["ist"][mo] == 1190.0

    material = next(g for g in plan["zeilen"] if g["code"] == "A_MAT")
    zeile_3400 = next(k for k in material["kinder"] if k["nummer"] == "3400")
    assert zeile_3400["ist"][di] == -500.0

    bank_zeile = next(f for f in plan["bestaende"]["finanzkonten"] if f["nummer"] == "1200")
    assert bank_zeile["bestand"][mo] == 11190.0
    assert bank_zeile["bestand"][di] == 10690.0
    assert plan["summen"]["netto"]["ist"][mo] == 1190.0


def test_bank_an_bank_ist_keine_zahlung(db):
    m = _mandant(db)
    kasse = _konto(db, m, "1000")
    bank = _konto(db, m, "1200")
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=bank.id,
                          datum=START - timedelta(days=1), wert=Decimal("1000")))
    db.add(models.Bestand(mandant_id=m.id, typ="KASSE", konto_id=kasse.id,
                          datum=START - timedelta(days=1), wert=Decimal("500")))
    # Bareinzahlung auf Bank: 1200 S an 1000 H
    db.add(models.Buchung(mandant_id=m.id, datum=START, konto_nr="1200",
                          gegenkonto_nr="1000", betrag=Decimal("300"), sh="S"))
    db.commit()

    plan = berechne_plan(db, m, heute=HEUTE)
    mo = START.isoformat()
    assert plan["summen"]["netto"].get("ist", {}).get(mo) is None  # keine Zahlungswirkung
    bank_zeile = next(f for f in plan["bestaende"]["finanzkonten"] if f["nummer"] == "1200")
    kassen_zeile = next(f for f in plan["bestaende"]["finanzkonten"] if f["nummer"] == "1000")
    assert bank_zeile["bestand"][mo] == 1300.0
    assert kassen_zeile["bestand"][mo] == 200.0
    assert plan["bestaende"]["liquiditaet"][mo] == 1500.0


def test_offene_posten_und_ueberfaellig(db):
    m = _mandant(db)
    material = _konto(db, m, "3400")
    erloes = _konto(db, m, "8400")
    # überfälliger Kreditor -> Erwartung: nächster Bankarbeitstag ab heute
    db.add(models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="Alt",
                                faellig_am=START - timedelta(days=30),
                                betrag_brutto=Decimal("1000"), konto_id=material.id))
    db.add(models.OffenerPosten(mandant_id=m.id, art="DEBITOR", partner="Kunde",
                                faellig_am=START + timedelta(days=10),
                                betrag_brutto=Decimal("2380"), konto_id=erloes.id))
    db.commit()

    plan = berechne_plan(db, m, heute=HEUTE)
    faellig = (START + timedelta(days=10)).isoformat()
    mat_gruppe = next(g for g in plan["zeilen"] if g["code"] == "A_MAT")
    zeile_3400 = next(k for k in mat_gruppe["kinder"] if k["nummer"] == "3400")
    assert zeile_3400["plan"][HEUTE.isoformat()] == -1000.0
    umsatz = next(g for g in plan["zeilen"] if g["code"] == "E_UMSATZ")
    zeile_8400 = next(k for k in umsatz["kinder"] if k["nummer"] == "8400")
    assert zeile_8400["plan"][faellig] == 2380.0


def test_ueberfaellige_op_in_projektion(db):
    # Überfällige offene Verbindlichkeit muss die Liquiditätsprojektion mindern
    m = _mandant(db)
    material = _konto(db, m, "3400")
    bank = _konto(db, m, "1200")
    heute = date(2026, 8, 7)  # Freitag
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=bank.id,
                          datum=date(2026, 8, 2), wert=Decimal("10000")))
    db.add(models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="Alt",
                                faellig_am=START, betrag_brutto=Decimal("5000"),
                                konto_id=material.id))
    db.commit()
    plan = berechne_plan(db, m, start=START, heute=heute)
    # Projektion: Rollen strikt hinter heute -> Montag 10.08.
    assert plan["bestaende"]["liquiditaet"]["2026-08-07"] == 10000.0
    assert plan["bestaende"]["liquiditaet"]["2026-08-10"] == 5000.0
    assert plan["bestaende"]["liquiditaet"][plan["ende"]] == 5000.0


def test_bezahlte_zukunfts_op_nicht_doppelt_in_projektion(db):
    # Vorzeitig bezahlter, ursprünglich in der Zukunft geplanter Posten darf die
    # Projektion nicht zusätzlich mindern (Ist-Bestand enthält die Zahlung bereits)
    m = _mandant(db)
    material = _konto(db, m, "3400")
    bank = _konto(db, m, "1200")
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=bank.id,
                          datum=START - timedelta(days=1), wert=Decimal("10000")))
    db.add(models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="X",
                                faellig_am=date(2026, 8, 20), betrag_brutto=Decimal("700"),
                                konto_id=material.id, status="BEZAHLT",
                                bezahlt_am=date(2026, 8, 4)))
    db.add(models.Buchung(mandant_id=m.id, datum=date(2026, 8, 4), konto_nr="3400",
                          gegenkonto_nr="1200", betrag=Decimal("700"), sh="S"))
    db.commit()
    plan = berechne_plan(db, m, start=START, heute=HEUTE)
    # Soll-Sicht zeigt den geplanten Abfluss am 20.08. ...
    mat = next(g for g in plan["zeilen"] if g["code"] == "A_MAT")
    zeile = next(k for k in mat["kinder"] if k["nummer"] == "3400")
    assert zeile["plan"]["2026-08-20"] == -700.0
    # ... aber die Projektion bleibt nach der Ist-Zahlung konstant
    assert plan["bestaende"]["liquiditaet"][HEUTE.isoformat()] == 9300.0
    assert plan["bestaende"]["liquiditaet"][plan["ende"]] == 9300.0


def test_dauerbuchung_monatlich_mit_verschiebung(db):
    m = _mandant(db)
    miete = _konto(db, m, "4210")
    # Stichtag 1.; 01.11.2026 ist ein Sonntag -> 02.11.2026
    db.add(models.Dauerbuchung(mandant_id=m.id, name="Miete", art="KREDITOR",
                               konto_id=miete.id, betrag_brutto=Decimal("2000"),
                               intervall="MONATLICH", stichtag=1,
                               gueltig_von=date(2026, 1, 1)))
    db.commit()
    plan = berechne_plan(db, m, start=date(2026, 10, 26), wochen=2, heute=date(2026, 10, 26))
    raum = next(g for g in plan["zeilen"] if g["code"] == "A_RAUM")
    zeile = next(k for k in raum["kinder"] if k["nummer"] == "4210")
    assert zeile["plan"]["2026-11-02"] == -2000.0


def test_dauerbuchung_fensterrand_geht_nicht_verloren(db):
    # Rastertermin 01.11. (Sonntag) vor Fensterbeginn 02.11. (Montag):
    # der verschobene Zahltag liegt im Fenster und muss erscheinen
    m = _mandant(db)
    miete = _konto(db, m, "4210")
    db.add(models.Dauerbuchung(mandant_id=m.id, name="Miete", art="KREDITOR",
                               konto_id=miete.id, betrag_brutto=Decimal("2000"),
                               intervall="MONATLICH", stichtag=1,
                               gueltig_von=date(2026, 1, 1)))
    db.commit()
    plan = berechne_plan(db, m, start=date(2026, 11, 2), wochen=13, heute=date(2026, 11, 2))
    raum = next(g for g in plan["zeilen"] if g["code"] == "A_RAUM")
    zeile = next(k for k in raum["kinder"] if k["nummer"] == "4210")
    assert zeile["plan"]["2026-11-02"] == -2000.0  # Novemberrate
    assert zeile["plan"]["2026-12-01"] == -2000.0  # Dezemberrate


def test_deaktiviertes_konto_mit_plandaten_kein_fehler(db):
    m = _mandant(db)
    material = _konto(db, m, "3400")
    db.add(models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="X",
                                faellig_am=START + timedelta(days=3),
                                betrag_brutto=Decimal("500"), konto_id=material.id))
    material.aktiv = False
    db.commit()
    plan = berechne_plan(db, m, start=START, heute=HEUTE)  # darf keinen KeyError werfen
    mat = next(g for g in plan["zeilen"] if g["code"] == "A_MAT")
    zeile = next(k for k in mat["kinder"] if k["nummer"] == "3400")
    assert "(inaktiv)" in zeile["name"]
    assert zeile["plan"][(START + timedelta(days=3)).isoformat()] == -500.0


def test_zukunftsfenster_projektion_startet_beim_bestand(db):
    m = _mandant(db)
    bank = _konto(db, m, "1200")
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=bank.id,
                          datum=date(2026, 8, 4), wert=Decimal("50000")))
    db.commit()
    # Fenster beginnt erst nächste Woche; Projektion muss beim bekannten Stand starten
    plan = berechne_plan(db, m, start=date(2026, 8, 10), wochen=4, heute=HEUTE)
    assert plan["bestaende"]["liquiditaet"]["2026-08-10"] == 50000.0


def test_budget_restlogik_und_ust(db):
    m = _mandant(db)
    erloes = _konto(db, m, "8400")  # 19 % USt
    telefon = _konto(db, m, "4920")  # 19 % USt
    # Budget August: Erlöse 10.000 netto -> 11.900 brutto verteilt
    db.add(models.Budget(mandant_id=m.id, konto_id=erloes.id, jahr=2026, monat=8,
                         betrag_netto=Decimal("10000")))
    db.add(models.Budget(mandant_id=m.id, konto_id=telefon.id, jahr=2026, monat=8,
                         betrag_netto=Decimal("100")))
    # expliziter Posten auf Telefon in Woche 1 über dem Wochenbudget -> Restbudget 0
    db.add(models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="Telko",
                                faellig_am=START + timedelta(days=2),
                                betrag_brutto=Decimal("500"), konto_id=telefon.id))
    db.commit()

    plan = berechne_plan(db, m, start=START, heute=HEUTE)
    umsatz = next(g for g in plan["zeilen"] if g["code"] == "E_UMSATZ")
    zeile_8400 = next(k for k in umsatz["kinder"] if k["nummer"] == "8400")
    august_summe = sum(v for t, v in zeile_8400["plan"].items() if t.startswith("2026-08"))
    assert abs(august_summe - 11900.0) < 0.05  # brutto inkl. 19 %

    sonst = next(g for g in plan["zeilen"] if g["code"] == "A_SONST")
    zeile_4920 = next(k for k in sonst["kinder"] if k["nummer"] == "4920")
    woche1 = [(START + timedelta(days=i)).isoformat() for i in range(7)]
    w1_werte = [zeile_4920["plan"].get(t, 0) for t in woche1]
    # nur der explizite Posten (-500), kein zusätzliches Budget in Woche 1
    assert abs(sum(w1_werte) + 500.0) < 0.01


def test_zahlungstermine_im_plan(db):
    m = _mandant(db)
    sv_konto = _konto(db, m, "1742")
    db.add(models.Zahlungstermin(mandant_id=m.id, typ="SV", datum=date(2026, 8, 27),
                                 betrag=Decimal("9000"), konto_id=sv_konto.id))
    db.commit()
    plan = berechne_plan(db, m, start=START, heute=HEUTE)
    sv = next(g for g in plan["zeilen"] if g["code"] == "A_SV")
    zeile = next(k for k in sv["kinder"] if k["nummer"] == "1742")
    assert zeile["plan"]["2026-08-27"] == -9000.0


def test_snapshot_und_sollist(db):
    m = _mandant(db)
    material = _konto(db, m, "3400")
    db.add(models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="X",
                                faellig_am=START + timedelta(days=1),
                                betrag_brutto=Decimal("700"), konto_id=material.id))
    db.commit()
    snap = erstelle_snapshot(db, m, start=START, heute=START)
    assert snap.stichtag == START

    # Ist weicht ab: nur 400 gezahlt
    db.add(models.Buchung(mandant_id=m.id, datum=START + timedelta(days=1),
                          konto_nr="3400", gegenkonto_nr="1200",
                          betrag=Decimal("400"), sh="S"))
    db.commit()

    vergleich = soll_ist_vergleich(db, m, heute=HEUTE)
    zeile = next(z for z in vergleich["zeilen"] if str(material.id) == z["key"])
    kw = START.isocalendar()
    schluessel = f"{kw.year}-{kw.week:02d}"
    assert zeile["plan"][schluessel] == -700.0
    assert zeile["ist"][schluessel] == -400.0

    # Plan-Ansicht nutzt Snapshot als Vergleichsbasis
    plan = berechne_plan(db, m, start=START, heute=HEUTE)
    assert plan["vergleichsbasis"] == "SNAPSHOT"


def test_insolvenzforderung_zahlungssperre(db):
    # Insolvenzforderungen (§ 38) dürfen weder im Plan noch in der Projektion auftauchen
    m = _mandant(db)
    material = _konto(db, m, "3400")
    bank = _konto(db, m, "1200")
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=bank.id,
                          datum=START - timedelta(days=1), wert=Decimal("10000")))
    db.add(models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="Altlieferant",
                                faellig_am=START + timedelta(days=5),
                                betrag_brutto=Decimal("8000"), konto_id=material.id,
                                forderungsklasse="INSOLVENZFORDERUNG"))
    db.add(models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="Neulieferant",
                                faellig_am=START + timedelta(days=5),
                                betrag_brutto=Decimal("1200"), konto_id=material.id,
                                forderungsklasse="MASSE"))
    db.commit()
    plan = berechne_plan(db, m, start=START, heute=HEUTE)
    mat = next(g for g in plan["zeilen"] if g["code"] == "A_MAT")
    zeile = next(k for k in mat["kinder"] if k["nummer"] == "3400")
    faellig = (START + timedelta(days=5)).isoformat()
    assert zeile["plan"][faellig] == -1200.0  # nur die Masseverbindlichkeit
    assert plan["gesperrte_insolvenzforderungen"] == 8000.0
    assert plan["bestaende"]["liquiditaet"][plan["ende"]] == 8800.0  # nur -1200


def test_snapshot_ohne_fensterueberlappung_wird_ignoriert(db):
    m = _mandant(db)
    # Snapshot 04.05.2026, 13 Wochen -> Fenster endet 02.08.2026
    erstelle_snapshot(db, m, start=date(2026, 5, 4), heute=date(2026, 5, 4))
    plan = berechne_plan(db, m, start=START, heute=HEUTE)  # Fenster ab 03.08.
    assert plan["vergleichsbasis"] == "LIVE"
    assert plan["snapshot"] is None


def test_termine_regeneration_ohne_duplikate(db):
    from sqlalchemy import select

    from app.services.zahlungskalender import generiere_termine

    m = _mandant(db)
    regel = db.scalar(select(models.TerminRegel).where(
        models.TerminRegel.mandant_id == m.id, models.TerminRegel.typ == "SV"))
    regel.betrag_modus = "FIX"
    regel.betrag_fix = Decimal("9000")
    db.commit()
    ende = START + timedelta(days=13 * 7 - 1)
    generiere_termine(db, m, START, ende, heute=HEUTE)
    sv = list(db.scalars(select(models.Zahlungstermin).where(
        models.Zahlungstermin.mandant_id == m.id, models.Zahlungstermin.typ == "SV")))
    anzahl = len(sv)
    assert anzahl >= 3
    # Termin manuell verschieben (z. B. Stundung) und erneut generieren
    sv[0].datum = sv[0].datum + timedelta(days=3)
    sv[0].status = "ANGEPASST"
    db.commit()
    erg = generiere_termine(db, m, START, ende, heute=HEUTE)
    sv_neu = list(db.scalars(select(models.Zahlungstermin).where(
        models.Zahlungstermin.mandant_id == m.id, models.Zahlungstermin.typ == "SV")))
    assert len(sv_neu) == anzahl  # keine Duplikate trotz Verschiebung
    assert erg["angelegt"] == 0


def test_unbekanntes_konto_wird_gemeldet(db):
    m = _mandant(db)
    db.add(models.Buchung(mandant_id=m.id, datum=START, konto_nr="1200",
                          gegenkonto_nr="9999", betrag=Decimal("100"), sh="S"))
    db.commit()
    plan = berechne_plan(db, m, heute=HEUTE)
    assert plan["unbekannte_konten"] == ["9999"]
    ohne = next(g for g in plan["zeilen"] if g["code"] == "OHNE")
    assert any(k["nummer"] == "9999" for k in ohne["kinder"])
