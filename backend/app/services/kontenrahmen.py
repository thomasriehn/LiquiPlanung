"""Kontenrahmen-Vorlagen (SKR03/SKR04) mit BWA-Gruppen und USt-Vorbelegung.

Die Vorbelegungen (USt-Satz, Gruppenzuordnung, Kontotyp) sind Vorschlagswerte und
je Mandant frei änderbar. `None` als USt-Satz = nicht umsatzsteuerpflichtig.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..models import KontoTyp, Richtung, TerminTyp

# (code, name, richtung, sortierung)
GRUPPEN = [
    ("E_UMSATZ", "Einzahlungen aus Umsatz", Richtung.EIN.value, 10),
    ("E_SONST", "Sonstige Einzahlungen", Richtung.EIN.value, 20),
    ("A_MAT", "Material / Wareneinkauf", Richtung.AUS.value, 30),
    ("A_PERS", "Personal", Richtung.AUS.value, 40),
    ("A_SV", "Sozialversicherung", Richtung.AUS.value, 50),
    ("A_STEUER", "Steuern", Richtung.AUS.value, 60),
    ("A_RAUM", "Raumkosten", Richtung.AUS.value, 70),
    ("A_VERS", "Versicherungen / Beiträge", Richtung.AUS.value, 80),
    ("A_KFZ", "Fahrzeugkosten", Richtung.AUS.value, 90),
    ("A_WERB", "Werbe- / Reisekosten", Richtung.AUS.value, 100),
    ("A_INST", "Reparatur / Instandhaltung", Richtung.AUS.value, 110),
    ("A_SONST", "Sonstige Auszahlungen", Richtung.AUS.value, 120),
    ("A_ZINS", "Zinsen / Finanzierung", Richtung.AUS.value, 130),
    ("A_INVEST", "Investitionen", Richtung.AUS.value, 140),
    ("A_TILG", "Tilgung / Darlehen", Richtung.AUS.value, 150),
    ("FIN", "Finanzkonten", Richtung.INFO.value, 900),
]

U19 = Decimal("19.00")
U7 = Decimal("7.00")
U0 = Decimal("0.00")

# (nummer, bezeichnung, typ, ust_satz, gruppe)
SKR03 = [
    ("1000", "Kasse", KontoTyp.KASSE.value, None, "FIN"),
    ("1200", "Bank", KontoTyp.BANK.value, None, "FIN"),
    ("8400", "Erlöse 19 % USt", KontoTyp.ERLOES.value, U19, "E_UMSATZ"),
    ("8300", "Erlöse 7 % USt", KontoTyp.ERLOES.value, U7, "E_UMSATZ"),
    ("8120", "Steuerfreie Umsätze § 4 Nr. 1a UStG", KontoTyp.ERLOES.value, U0, "E_UMSATZ"),
    ("8200", "Erlöse (ohne USt)", KontoTyp.ERLOES.value, None, "E_UMSATZ"),
    ("2700", "Sonstige Erträge", KontoTyp.ERLOES.value, None, "E_SONST"),
    ("1400", "Forderungen aus Lieferungen und Leistungen", KontoTyp.ERLOES.value, None, "E_SONST"),
    ("3400", "Wareneingang 19 % Vorsteuer", KontoTyp.AUFWAND.value, U19, "A_MAT"),
    ("3300", "Wareneingang 7 % Vorsteuer", KontoTyp.AUFWAND.value, U7, "A_MAT"),
    ("3100", "Fremdleistungen", KontoTyp.AUFWAND.value, U19, "A_MAT"),
    ("1600", "Verbindlichkeiten aus Lieferungen und Leistungen", KontoTyp.AUFWAND.value, None, "A_MAT"),
    ("4110", "Löhne", KontoTyp.PERSONAL.value, None, "A_PERS"),
    ("4120", "Gehälter", KontoTyp.PERSONAL.value, None, "A_PERS"),
    ("1741", "Verbindlichkeiten Lohn- und Kirchensteuer", KontoTyp.STEUER.value, None, "A_STEUER"),
    ("4130", "Gesetzliche soziale Aufwendungen", KontoTyp.SV.value, None, "A_SV"),
    ("1742", "Verbindlichkeiten soziale Sicherheit", KontoTyp.SV.value, None, "A_SV"),
    ("4138", "Beiträge zur Berufsgenossenschaft", KontoTyp.SV.value, None, "A_SV"),
    ("1780", "Umsatzsteuer-Vorauszahlung", KontoTyp.STEUER.value, None, "A_STEUER"),
    ("4320", "Gewerbesteuer", KontoTyp.STEUER.value, None, "A_STEUER"),
    ("2200", "Körperschaftsteuer", KontoTyp.STEUER.value, None, "A_STEUER"),
    ("4210", "Miete", KontoTyp.AUFWAND.value, None, "A_RAUM"),
    ("4230", "Heizung", KontoTyp.AUFWAND.value, U19, "A_RAUM"),
    ("4240", "Gas, Strom, Wasser", KontoTyp.AUFWAND.value, U19, "A_RAUM"),
    ("4250", "Reinigung", KontoTyp.AUFWAND.value, U19, "A_RAUM"),
    ("4360", "Versicherungen", KontoTyp.AUFWAND.value, None, "A_VERS"),
    ("4380", "Beiträge", KontoTyp.AUFWAND.value, None, "A_VERS"),
    ("4530", "Laufende Kfz-Betriebskosten", KontoTyp.AUFWAND.value, U19, "A_KFZ"),
    ("4540", "Kfz-Versicherungen", KontoTyp.AUFWAND.value, None, "A_KFZ"),
    ("4600", "Werbekosten", KontoTyp.AUFWAND.value, U19, "A_WERB"),
    ("4650", "Bewirtungskosten", KontoTyp.AUFWAND.value, U19, "A_WERB"),
    ("4670", "Reisekosten Unternehmer", KontoTyp.AUFWAND.value, U19, "A_WERB"),
    ("4800", "Reparatur/Instandhaltung Anlagen", KontoTyp.AUFWAND.value, U19, "A_INST"),
    ("4805", "Reparatur/Instandhaltung BGA", KontoTyp.AUFWAND.value, U19, "A_INST"),
    ("4920", "Telefon", KontoTyp.AUFWAND.value, U19, "A_SONST"),
    ("4930", "Bürobedarf", KontoTyp.AUFWAND.value, U19, "A_SONST"),
    ("4950", "Rechts- und Beratungskosten", KontoTyp.AUFWAND.value, U19, "A_SONST"),
    ("4955", "Buchführungskosten", KontoTyp.AUFWAND.value, U19, "A_SONST"),
    ("4970", "Nebenkosten des Geldverkehrs", KontoTyp.AUFWAND.value, None, "A_SONST"),
    ("4900", "Sonstige betriebliche Aufwendungen", KontoTyp.AUFWAND.value, U19, "A_SONST"),
    ("2100", "Zinsen und ähnliche Aufwendungen", KontoTyp.FINANZIERUNG.value, None, "A_ZINS"),
    ("0420", "Büroeinrichtung", KontoTyp.INVESTITION.value, U19, "A_INVEST"),
    ("0320", "Pkw", KontoTyp.INVESTITION.value, U19, "A_INVEST"),
    ("0210", "Maschinen", KontoTyp.INVESTITION.value, U19, "A_INVEST"),
    ("0630", "Verbindlichkeiten gegenüber Kreditinstituten", KontoTyp.FINANZIERUNG.value, None, "A_TILG"),
    ("3970", "Warenbestand (nachrichtlich)", KontoTyp.INFO.value, None, None),
]

SKR04 = [
    ("1600", "Kasse", KontoTyp.KASSE.value, None, "FIN"),
    ("1800", "Bank", KontoTyp.BANK.value, None, "FIN"),
    ("4400", "Erlöse 19 % USt", KontoTyp.ERLOES.value, U19, "E_UMSATZ"),
    ("4300", "Erlöse 7 % USt", KontoTyp.ERLOES.value, U7, "E_UMSATZ"),
    ("4120", "Steuerfreie Umsätze § 4 Nr. 1a UStG", KontoTyp.ERLOES.value, U0, "E_UMSATZ"),
    ("4200", "Erlöse (ohne USt)", KontoTyp.ERLOES.value, None, "E_UMSATZ"),
    ("4830", "Sonstige betriebliche Erträge", KontoTyp.ERLOES.value, None, "E_SONST"),
    ("1200", "Forderungen aus Lieferungen und Leistungen", KontoTyp.ERLOES.value, None, "E_SONST"),
    ("5400", "Wareneingang 19 % Vorsteuer", KontoTyp.AUFWAND.value, U19, "A_MAT"),
    ("5300", "Wareneingang 7 % Vorsteuer", KontoTyp.AUFWAND.value, U7, "A_MAT"),
    ("5900", "Fremdleistungen", KontoTyp.AUFWAND.value, U19, "A_MAT"),
    ("3300", "Verbindlichkeiten aus Lieferungen und Leistungen", KontoTyp.AUFWAND.value, None, "A_MAT"),
    ("6010", "Löhne", KontoTyp.PERSONAL.value, None, "A_PERS"),
    ("6020", "Gehälter", KontoTyp.PERSONAL.value, None, "A_PERS"),
    ("3730", "Verbindlichkeiten Lohn- und Kirchensteuer", KontoTyp.STEUER.value, None, "A_STEUER"),
    ("6110", "Gesetzliche soziale Aufwendungen", KontoTyp.SV.value, None, "A_SV"),
    ("3740", "Verbindlichkeiten soziale Sicherheit", KontoTyp.SV.value, None, "A_SV"),
    ("6120", "Beiträge zur Berufsgenossenschaft", KontoTyp.SV.value, None, "A_SV"),
    ("3820", "Umsatzsteuer-Vorauszahlung", KontoTyp.STEUER.value, None, "A_STEUER"),
    ("7610", "Gewerbesteuer", KontoTyp.STEUER.value, None, "A_STEUER"),
    ("7600", "Körperschaftsteuer", KontoTyp.STEUER.value, None, "A_STEUER"),
    ("6310", "Miete", KontoTyp.AUFWAND.value, None, "A_RAUM"),
    ("6320", "Heizung", KontoTyp.AUFWAND.value, U19, "A_RAUM"),
    ("6325", "Gas, Strom, Wasser", KontoTyp.AUFWAND.value, U19, "A_RAUM"),
    ("6330", "Reinigung", KontoTyp.AUFWAND.value, U19, "A_RAUM"),
    ("6400", "Versicherungen", KontoTyp.AUFWAND.value, None, "A_VERS"),
    ("6420", "Beiträge", KontoTyp.AUFWAND.value, None, "A_VERS"),
    ("6530", "Laufende Kfz-Betriebskosten", KontoTyp.AUFWAND.value, U19, "A_KFZ"),
    ("6520", "Kfz-Versicherungen", KontoTyp.AUFWAND.value, None, "A_KFZ"),
    ("6600", "Werbekosten", KontoTyp.AUFWAND.value, U19, "A_WERB"),
    ("6640", "Bewirtungskosten", KontoTyp.AUFWAND.value, U19, "A_WERB"),
    ("6650", "Reisekosten Arbeitnehmer", KontoTyp.AUFWAND.value, U19, "A_WERB"),
    ("6450", "Reparatur/Instandhaltung Anlagen", KontoTyp.AUFWAND.value, U19, "A_INST"),
    ("6805", "Telefon", KontoTyp.AUFWAND.value, U19, "A_SONST"),
    ("6815", "Bürobedarf", KontoTyp.AUFWAND.value, U19, "A_SONST"),
    ("6825", "Rechts- und Beratungskosten", KontoTyp.AUFWAND.value, U19, "A_SONST"),
    ("6830", "Buchführungskosten", KontoTyp.AUFWAND.value, U19, "A_SONST"),
    ("6855", "Nebenkosten des Geldverkehrs", KontoTyp.AUFWAND.value, None, "A_SONST"),
    ("6300", "Sonstige betriebliche Aufwendungen", KontoTyp.AUFWAND.value, U19, "A_SONST"),
    ("7300", "Zinsen und ähnliche Aufwendungen", KontoTyp.FINANZIERUNG.value, None, "A_ZINS"),
    ("0650", "Büroeinrichtung", KontoTyp.INVESTITION.value, U19, "A_INVEST"),
    ("0520", "Pkw", KontoTyp.INVESTITION.value, U19, "A_INVEST"),
    ("0440", "Maschinen", KontoTyp.INVESTITION.value, U19, "A_INVEST"),
    ("3150", "Verbindlichkeiten gegenüber Kreditinstituten", KontoTyp.FINANZIERUNG.value, None, "A_TILG"),
    ("1140", "Warenbestand (nachrichtlich)", KontoTyp.INFO.value, None, None),
]

# Konto für Terminregel-Verknüpfung je Kontenrahmen
REGEL_KONTEN = {
    "SKR03": {
        TerminTyp.SV.value: "1742",
        TerminTyp.UST_VA.value: "1780",
        TerminTyp.LST.value: "1741",
        TerminTyp.GEWST.value: "4320",
        TerminTyp.KST.value: "2200",
    },
    "SKR04": {
        TerminTyp.SV.value: "3740",
        TerminTyp.UST_VA.value: "3820",
        TerminTyp.LST.value: "3730",
        TerminTyp.GEWST.value: "7610",
        TerminTyp.KST.value: "7600",
    },
}


def lege_standard_szenarien_an(db: Session, mandant: models.Mandant) -> None:
    """Best-/Worst-Case-Vorlagen (editier- und löschbar); Basisplan = kein Szenario."""
    vorhanden = db.scalar(
        select(models.Szenario.id).where(models.Szenario.mandant_id == mandant.id).limit(1)
    )
    if vorhanden:
        return
    db.add(
        models.Szenario(
            mandant_id=mandant.id, name="Best Case",
            kommentar="Einzahlungen +10 %",
            ein_faktor=Decimal("110"), aus_faktor=Decimal("100"),
            debitoren_verzoegerung_tage=0,
        )
    )
    db.add(
        models.Szenario(
            mandant_id=mandant.id, name="Worst Case",
            kommentar="Einzahlungen −20 %, Debitoren +14 Tage, variable Kosten +5 %",
            ein_faktor=Decimal("80"), aus_faktor=Decimal("105"),
            debitoren_verzoegerung_tage=14,
        )
    )


def lege_kontenrahmen_an(db: Session, mandant: models.Mandant) -> None:
    """Erzeugt Gruppen, Konten und Terminregeln für einen neuen Mandanten."""
    vorhanden = db.scalar(
        select(models.Konto.id).where(models.Konto.mandant_id == mandant.id).limit(1)
    )
    if vorhanden:
        return

    gruppen_by_code: dict[str, models.KontoGruppe] = {}
    for code, name, richtung, sort in GRUPPEN:
        g = models.KontoGruppe(
            mandant_id=mandant.id, code=code, name=name, richtung=richtung, sortierung=sort
        )
        db.add(g)
        gruppen_by_code[code] = g
    db.flush()

    vorlage = {"SKR03": SKR03, "SKR04": SKR04}.get(mandant.kontenrahmen, [])
    konten_by_nr: dict[str, models.Konto] = {}
    for nummer, bezeichnung, typ, ust, gruppe_code in vorlage:
        k = models.Konto(
            mandant_id=mandant.id,
            nummer=nummer,
            bezeichnung=bezeichnung,
            typ=typ,
            ust_satz=ust,
            gruppe_id=gruppen_by_code[gruppe_code].id if gruppe_code else None,
            liquiditaetswirksam=typ != KontoTyp.INFO.value,
        )
        db.add(k)
        konten_by_nr[nummer] = k
    db.flush()

    regel_konten = REGEL_KONTEN.get(mandant.kontenrahmen, {})
    for typ in (TerminTyp.SV, TerminTyp.UST_VA, TerminTyp.LST, TerminTyp.GEWST, TerminTyp.KST):
        konto = konten_by_nr.get(regel_konten.get(typ.value, ""))
        db.add(
            models.TerminRegel(
                mandant_id=mandant.id,
                typ=typ.value,
                aktiv=typ in (TerminTyp.SV, TerminTyp.UST_VA, TerminTyp.LST),
                betrag_modus="HISTORIE",
                betrag_fix=Decimal("0"),
                konto_id=konto.id if konto else None,
            )
        )
    db.commit()
