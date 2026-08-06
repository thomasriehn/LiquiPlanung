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


def test_teilbezahlter_posten_plant_nur_restbetrag(db):
    m = _mandant(db)
    material = _konto(db, m, "3400")
    db.add(models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="X",
                                faellig_am=START + timedelta(days=5),
                                betrag_brutto=Decimal("1000"),
                                bezahlt_betrag=Decimal("400"),
                                konto_id=material.id))
    db.commit()
    plan = berechne_plan(db, m, start=START, heute=HEUTE)
    mat = next(g for g in plan["zeilen"] if g["code"] == "A_MAT")
    zeile = next(k for k in mat["kinder"] if k["nummer"] == "3400")
    assert zeile["plan"][(START + timedelta(days=5)).isoformat()] == -600.0


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


def test_insolvenzgeld_unterdrueckt_personal_sv_lst(db):
    m = _mandant(db)
    m.insolvenzgeld_aktiv = True
    m.insolvenzgeld_von = date(2026, 8, 1)
    m.insolvenzgeld_bis = date(2026, 8, 31)
    lohn = _konto(db, m, "4110")
    sv_konto = _konto(db, m, "1742")
    ust_konto = _konto(db, m, "1780")
    # Personal-Budget August und September
    for monat in (8, 9):
        db.add(models.Budget(mandant_id=m.id, konto_id=lohn.id, jahr=2026, monat=monat,
                             betrag_netto=Decimal("10000")))
    # Lohnlauf als Dauerbuchung am 25.
    db.add(models.Dauerbuchung(mandant_id=m.id, name="Lohnlauf", art="KREDITOR",
                               konto_id=lohn.id, betrag_brutto=Decimal("7000"),
                               intervall="MONATLICH", stichtag=25,
                               gueltig_von=date(2026, 1, 1)))
    # SV-Termine August (im Zeitraum) und September (außerhalb); USt bleibt immer
    db.add(models.Zahlungstermin(mandant_id=m.id, typ="SV", periode="2026-08",
                                 datum=date(2026, 8, 27), betrag=Decimal("9000"),
                                 konto_id=sv_konto.id))
    db.add(models.Zahlungstermin(mandant_id=m.id, typ="SV", periode="2026-09",
                                 datum=date(2026, 9, 28), betrag=Decimal("9000"),
                                 konto_id=sv_konto.id))
    db.add(models.Zahlungstermin(mandant_id=m.id, typ="LST", periode="2026-08",
                                 datum=date(2026, 9, 10), betrag=Decimal("4000"),
                                 konto_id=_konto(db, m, "1741").id))
    db.add(models.Zahlungstermin(mandant_id=m.id, typ="UST_VA", periode="2026-08",
                                 datum=date(2026, 9, 10), betrag=Decimal("5000"),
                                 konto_id=ust_konto.id))
    db.commit()

    plan = berechne_plan(db, m, start=START, heute=HEUTE)
    pers = next(g for g in plan["zeilen"] if g["code"] == "A_PERS")
    zeile_lohn = next(k for k in pers["kinder"] if k["nummer"] == "4110")
    august = sum(v for t, v in zeile_lohn["plan"].items() if t.startswith("2026-08"))
    september = sum(v for t, v in zeile_lohn["plan"].items() if t.startswith("2026-09"))
    assert august == 0.0                      # Budget + Lohnlauf im Zeitraum entfallen
    assert september < -14000                 # ab September wieder Lohn + Restbudget
    sv = next(g for g in plan["zeilen"] if g["code"] == "A_SV")
    zeile_sv = next(k for k in sv["kinder"] if k["nummer"] == "1742")
    assert "2026-08-27" not in zeile_sv["plan"]          # August-SV entfällt komplett
    assert zeile_sv["plan"]["2026-09-28"] == -9000.0     # September-SV bleibt
    steuern = next(g for g in plan["zeilen"] if g["code"] == "A_STEUER")
    # LSt für August entfällt komplett -> Zeile 1741 taucht gar nicht erst auf
    zeile_lst = next((k for k in steuern["kinder"] if k["nummer"] == "1741"), None)
    assert zeile_lst is None or "2026-09-10" not in zeile_lst["plan"]
    zeile_ust = next(k for k in steuern["kinder"] if k["nummer"] == "1780")
    assert zeile_ust["plan"]["2026-09-10"] == -5000.0    # USt unberührt
    assert plan["insolvenzgeld"]["aktiv"] is True
    assert plan["insolvenzgeld"]["entlastung_fenster"] > 20000


def test_insolvenzgeld_teilmonat_kuerzt_anteilig(db):
    m = _mandant(db)
    m.insolvenzgeld_aktiv = True
    m.insolvenzgeld_von = date(2026, 8, 1)
    m.insolvenzgeld_bis = date(2026, 9, 15)  # halber September
    sv_konto = _konto(db, m, "1742")
    db.add(models.Zahlungstermin(mandant_id=m.id, typ="SV", periode="2026-09",
                                 datum=date(2026, 9, 28), betrag=Decimal("9000"),
                                 konto_id=sv_konto.id))
    db.commit()
    plan = berechne_plan(db, m, start=START, heute=HEUTE)
    sv = next(g for g in plan["zeilen"] if g["code"] == "A_SV")
    zeile = next(k for k in sv["kinder"] if k["nummer"] == "1742")
    # 15 von 30 Septembertagen im Zeitraum -> Kürzung 50 %
    assert zeile["plan"]["2026-09-28"] == -4500.0


def test_szenario_faktoren_und_verzoegerung(db):
    m = _mandant(db)
    erloes = _konto(db, m, "8400")
    material = _konto(db, m, "3400")
    szenario = models.Szenario(mandant_id=m.id, name="Worst", ein_faktor=Decimal("80"),
                               aus_faktor=Decimal("110"), debitoren_verzoegerung_tage=14)
    db.add(szenario)
    db.add(models.Budget(mandant_id=m.id, konto_id=erloes.id, jahr=2026, monat=8,
                         betrag_netto=Decimal("10000")))
    db.add(models.Budget(mandant_id=m.id, konto_id=material.id, jahr=2026, monat=8,
                         betrag_netto=Decimal("1000")))
    # Forderung fällig Do 13.08. -> mit +14 Tagen am Do 27.08.; Kreditor unverändert
    db.add(models.OffenerPosten(mandant_id=m.id, art="DEBITOR", partner="Kunde",
                                faellig_am=date(2026, 8, 13),
                                betrag_brutto=Decimal("1000"), konto_id=erloes.id))
    db.add(models.OffenerPosten(mandant_id=m.id, art="KREDITOR", partner="Lieferant",
                                faellig_am=date(2026, 8, 13),
                                betrag_brutto=Decimal("500"), konto_id=material.id))
    db.commit()

    basis = berechne_plan(db, m, start=START, heute=HEUTE)
    worst = berechne_plan(db, m, start=START, heute=HEUTE, szenario=szenario)

    def august_summe(plan, code, nummer):
        gruppe = next(g for g in plan["zeilen"] if g["code"] == code)
        zeile = next(k for k in gruppe["kinder"] if k["nummer"] == nummer)
        return sum(v for t, v in zeile["plan"].items() if t.startswith("2026-08")), zeile

    basis_ein, _ = august_summe(basis, "E_UMSATZ", "8400")
    worst_ein, worst_zeile = august_summe(worst, "E_UMSATZ", "8400")
    # Einzahlungen (Budget + Forderung) auf 80 % skaliert
    assert abs(worst_ein - basis_ein * 0.8) < 0.1
    # Forderung um 14 Tage verschoben
    assert "2026-08-13" not in worst_zeile["plan"] or worst_zeile["plan"]["2026-08-13"] < 801
    assert worst_zeile["plan"]["2026-08-27"] >= 800.0
    # Kreditor-OP unverändert, Budget-Auszahlung mit Faktor 110
    _, mat_zeile = august_summe(worst, "A_MAT", "3400")
    assert mat_zeile["plan"]["2026-08-13"] == -500.0
    basis_aus, _ = august_summe(basis, "A_MAT", "3400")
    worst_aus, _ = august_summe(worst, "A_MAT", "3400")
    # Budgetanteil: Basis = -500 (OP) + Restbudget; Worst skaliert nur den Budgetanteil
    basis_budget = basis_aus + 500
    worst_budget = worst_aus + 500
    assert abs(worst_budget - basis_budget * 1.1) < 26  # Restbudget-Wechselwirkung toleriert
    assert worst["szenario"]["name"] == "Worst"


def test_verteilungsprofil_monatsende_und_freitags(db):
    m = _mandant(db)
    lohn = _konto(db, m, "4110")
    lohn.verteilung = "MONATSENDE"
    telefon = _konto(db, m, "4920")
    telefon.verteilung = "WTAG_FR"
    telefon.ust_satz = None  # netto = brutto für einfache Zahlen
    db.add(models.Budget(mandant_id=m.id, konto_id=lohn.id, jahr=2026, monat=8,
                         betrag_netto=Decimal("10000")))
    db.add(models.Budget(mandant_id=m.id, konto_id=telefon.id, jahr=2026, monat=8,
                         betrag_netto=Decimal("4000")))
    db.commit()
    plan = berechne_plan(db, m, start=START, heute=HEUTE)
    pers = next(g for g in plan["zeilen"] if g["code"] == "A_PERS")
    zeile_lohn = next(k for k in pers["kinder"] if k["nummer"] == "4110")
    # kompletter Monatsbetrag am letzten Bankarbeitstag (31.08.2026, Montag)
    assert zeile_lohn["plan"]["2026-08-31"] == -10000.0
    assert "2026-08-14" not in zeile_lohn["plan"]
    sonst = next(g for g in plan["zeilen"] if g["code"] == "A_SONST")
    zeile_tel = next(k for k in sonst["kinder"] if k["nummer"] == "4920")
    # August 2026 hat vier Freitage (7., 14., 21., 28.) -> je 1.000
    for tag in ("2026-08-07", "2026-08-14", "2026-08-21", "2026-08-28"):
        assert zeile_tel["plan"][tag] == -1000.0
    assert "2026-08-10" not in zeile_tel["plan"]


def test_szenario_detailregeln_engine(db):
    m = _mandant(db)
    e8400 = _konto(db, m, "8400")   # 19 % USt, Gruppe E_UMSATZ
    e8300 = _konto(db, m, "8300")   # 7 % USt, Gruppe E_UMSATZ
    for konto in (e8400, e8300):
        db.add(models.Budget(mandant_id=m.id, konto_id=konto.id, jahr=2026, monat=8,
                             betrag_netto=Decimal("10000")))
    szenario = models.Szenario(mandant_id=m.id, name="Detail", ein_faktor=Decimal("80"),
                               aus_faktor=Decimal("100"))
    db.add(szenario)
    db.flush()
    gruppe = e8400.gruppe
    db.add(models.SzenarioRegel(szenario_id=szenario.id, gruppe_id=gruppe.id,
                                faktor=Decimal("60")))
    db.add(models.SzenarioRegel(szenario_id=szenario.id, konto_id=e8400.id,
                                faktor=Decimal("50")))
    db.commit()
    db.refresh(szenario)

    plan = berechne_plan(db, m, start=START, heute=HEUTE, szenario=szenario)
    umsatz = next(g for g in plan["zeilen"] if g["code"] == "E_UMSATZ")

    def august(nummer):
        zeile = next(k for k in umsatz["kinder"] if k["nummer"] == nummer)
        return sum(v for t, v in zeile["plan"].items() if t.startswith("2026-08"))

    # Konto-Regel (50 %) schlägt Gruppen-Regel; Gruppen-Regel (60 %) schlägt global (80 %)
    assert abs(august("8400") - 11900 * 0.5) < 0.1
    assert abs(august("8300") - 10700 * 0.6) < 0.1
    assert plan["szenario"]["regeln_anzahl"] == 2


def test_mehrere_bestandsanker_stueckweise(db):
    # Tägliche Kontoauszugssalden: Anker sind maßgeblich, dazwischen zählen Buchungen
    m = _mandant(db)
    bank = _konto(db, m, "1200")
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=bank.id,
                          datum=START, wert=Decimal("1000")))          # Mo
    db.add(models.Bestand(mandant_id=m.id, typ="BANK", konto_id=bank.id,
                          datum=START + timedelta(days=2), wert=Decimal("5000")))  # Mi
    db.add(models.Buchung(mandant_id=m.id, datum=START + timedelta(days=1),
                          konto_nr="1200", gegenkonto_nr="8400",
                          betrag=Decimal("100"), sh="S"))              # Di +100
    db.commit()
    plan = berechne_plan(db, m, start=START, heute=START + timedelta(days=3))
    bank_zeile = next(f for f in plan["bestaende"]["finanzkonten"] if f["nummer"] == "1200")
    assert bank_zeile["bestand"][START.isoformat()] == 1000.0
    assert bank_zeile["bestand"][(START + timedelta(days=1)).isoformat()] == 1100.0
    # Mittwoch: Auszugssaldo überschreibt die Fortschreibung
    assert bank_zeile["bestand"][(START + timedelta(days=2)).isoformat()] == 5000.0
    assert bank_zeile["bestand"][(START + timedelta(days=3)).isoformat()] == 5000.0


def test_szenarien_vergleich(db):
    from app.services.liquiditaet import szenarien_vergleich

    m = _mandant(db)
    erloes = _konto(db, m, "8400")
    db.add(models.Budget(mandant_id=m.id, konto_id=erloes.id, jahr=2026, monat=9,
                         betrag_netto=Decimal("10000")))
    db.add(models.Szenario(mandant_id=m.id, name="Worst", ein_faktor=Decimal("50"),
                           aus_faktor=Decimal("100"), debitoren_verzoegerung_tage=0))
    db.commit()
    v = szenarien_vergleich(db, m, start=START, heute=HEUTE)
    namen = [s["name"] for s in v["szenarien"]]
    assert namen[0] == "Basisplan" and "Worst" in namen
    basis = v["szenarien"][0]
    worst = next(s for s in v["szenarien"] if s["name"] == "Worst")
    # September-Wochen: Worst-Einzahlungen = 50 % der Basis
    idx = next(i for i, w in enumerate(v["wochen"]) if w["von"].startswith("2026-09"))
    assert basis["einzahlungen"][idx] > 0
    assert abs(worst["einzahlungen"][idx] - basis["einzahlungen"][idx] * 0.5) < 0.1
    assert worst["endbestand"] < basis["endbestand"]
    assert "datum" in worst["min_liquiditaet"]


def test_unbekanntes_konto_wird_gemeldet(db):
    m = _mandant(db)
    db.add(models.Buchung(mandant_id=m.id, datum=START, konto_nr="1200",
                          gegenkonto_nr="9999", betrag=Decimal("100"), sh="S"))
    db.commit()
    plan = berechne_plan(db, m, heute=HEUTE)
    assert plan["unbekannte_konten"] == ["9999"]
    ohne = next(g for g in plan["zeilen"] if g["code"] == "OHNE")
    assert any(k["nummer"] == "9999" for k in ohne["kinder"])
