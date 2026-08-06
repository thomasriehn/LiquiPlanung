from datetime import date
from decimal import Decimal

from app.services.bank import (
    ist_bankformat,
    normalisiere_iban,
    parse_bank_automatisch,
    parse_camt053,
    parse_mt940,
)

MT940 = """:20:STMT-1
:25:DE89 3704 0044 0532 0130 00
:28C:152/1
:60F:C260803EUR41250,00
:61:2608040804CR1190,00NTRFNONREF//B123
:86:166?00GUTSCHRIFT?20SVWZ+RE-100 Zahlung?21Projekt A?32Kunde Albrecht AG
:61:260804DR500,00NDDT0815
:86:105?00LASTSCHRIFT?20SVWZ+Miete August?32Vermieter GmbH
:61:260805RC50,00NRTI
:86:109?20Rueckbelastung
:62F:C260805EUR41890,00
-
""".encode("cp1252")

CAMT = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
 <BkToCstmrStmt><Stmt>
  <ElctrncSeqNb>152</ElctrncSeqNb>
  <Acct><Id><IBAN>DE89370400440532013000</IBAN></Id></Acct>
  <Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp>
   <Amt Ccy="EUR">41250.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-08-03</Dt></Dt></Bal>
  <Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>
   <Amt Ccy="EUR">41940.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-08-04</Dt></Dt></Bal>
  <Ntry><Amt Ccy="EUR">1190.00</Amt><CdtDbtInd>CRDT</CdtDbtInd>
   <BookgDt><Dt>2026-08-04</Dt></BookgDt><ValDt><Dt>2026-08-04</Dt></ValDt>
   <NtryDtls><TxDtls>
    <RltdPties><Dbtr><Nm>Kunde Albrecht AG</Nm></Dbtr></RltdPties>
    <RmtInf><Ustrd>RE-100 Teilzahlung</Ustrd></RmtInf>
   </TxDtls></NtryDtls></Ntry>
  <Ntry><Amt Ccy="EUR">500.00</Amt><CdtDbtInd>DBIT</CdtDbtInd>
   <BookgDt><Dt>2026-08-04</Dt></BookgDt></Ntry>
 </Stmt></BkToCstmrStmt></Document>
""".encode("utf-8")


def test_mt940_parser():
    erg = parse_mt940(MT940)
    assert not erg.warnungen
    assert len(erg.auszuege) == 1
    a = erg.auszuege[0]
    assert normalisiere_iban(a.konto_kennung) == "DE89370400440532013000"
    assert a.anfangssaldo == Decimal("41250.00")
    assert a.endsaldo == Decimal("41890.00")
    assert a.endsaldo_datum == date(2026, 8, 5)
    assert len(a.umsaetze) == 3
    u1, u2, u3 = a.umsaetze
    assert u1.betrag == Decimal("1190.00")
    assert u1.buchungstag == date(2026, 8, 4)
    assert u1.partner == "Kunde Albrecht AG"
    assert u1.verwendungszweck == "RE-100 Zahlung Projekt A"
    assert u2.betrag == Decimal("-500.00")
    assert u2.partner == "Vermieter GmbH"
    assert u3.betrag == Decimal("-50.00")  # RC = Storno einer Gutschrift
    # Plausibilität: Anfangssaldo + Umsätze = Endsaldo
    assert a.anfangssaldo + sum(u.betrag for u in a.umsaetze) == a.endsaldo


def test_camt053_parser():
    erg = parse_camt053(CAMT)
    assert not erg.warnungen
    assert len(erg.auszuege) == 1
    a = erg.auszuege[0]
    assert a.konto_kennung == "DE89370400440532013000"
    assert a.anfangssaldo == Decimal("41250.00")
    assert a.endsaldo == Decimal("41940.00")
    assert a.endsaldo_datum == date(2026, 8, 4)
    assert len(a.umsaetze) == 2
    assert a.umsaetze[0].betrag == Decimal("1190.00")
    assert a.umsaetze[0].partner == "Kunde Albrecht AG"
    assert a.umsaetze[0].verwendungszweck == "RE-100 Teilzahlung"
    assert a.umsaetze[1].betrag == Decimal("-500.00")


def test_automatische_formaterkennung():
    assert parse_bank_automatisch(MT940).format == "MT940"
    assert parse_bank_automatisch(CAMT).format == "CAMT"
    assert ist_bankformat(MT940)
    assert ist_bankformat(CAMT)
    assert not ist_bankformat(b"Datum;Konto;Betrag\n01.08.2026;4920;10,00\n")
