import enum
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    Column,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Rolle(str, enum.Enum):
    ADMIN = "ADMIN"
    BEARBEITER = "BEARBEITER"
    LESER = "LESER"


class KontoTyp(str, enum.Enum):
    BANK = "BANK"
    KASSE = "KASSE"
    ERLOES = "ERLOES"
    AUFWAND = "AUFWAND"
    PERSONAL = "PERSONAL"
    SV = "SV"
    STEUER = "STEUER"
    INVESTITION = "INVESTITION"
    FINANZIERUNG = "FINANZIERUNG"
    INFO = "INFO"


class Richtung(str, enum.Enum):
    EIN = "EIN"    # Einzahlungen
    AUS = "AUS"    # Auszahlungen
    INFO = "INFO"  # nachrichtlich (Finanzkonten, Warenbestand)


class PostenArt(str, enum.Enum):
    KREDITOR = "KREDITOR"  # Eingangsrechnung -> Auszahlung
    DEBITOR = "DEBITOR"    # Ausgangsrechnung/Forderung -> Einzahlung


class PostenStatus(str, enum.Enum):
    OFFEN = "OFFEN"
    BEZAHLT = "BEZAHLT"
    STORNIERT = "STORNIERT"


class Intervall(str, enum.Enum):
    WOECHENTLICH = "WOECHENTLICH"
    MONATLICH = "MONATLICH"
    QUARTAL = "QUARTAL"
    JAEHRLICH = "JAEHRLICH"


class TerminTyp(str, enum.Enum):
    SV = "SV"
    UST_VA = "UST_VA"
    LST = "LST"
    GEWST = "GEWST"
    KST = "KST"
    SONSTIG = "SONSTIG"


class TerminStatus(str, enum.Enum):
    GEPLANT = "GEPLANT"
    ANGEPASST = "ANGEPASST"
    ERLEDIGT = "ERLEDIGT"


class BestandTyp(str, enum.Enum):
    BANK = "BANK"
    KASSE = "KASSE"
    WAREN = "WAREN"


class UStZeitraum(str, enum.Enum):
    MONAT = "MONAT"
    QUARTAL = "QUARTAL"


benutzer_mandanten = Table(
    "benutzer_mandanten",
    Base.metadata,
    Column("benutzer_id", ForeignKey("benutzer.id", ondelete="CASCADE"), primary_key=True),
    Column("mandant_id", ForeignKey("mandanten.id", ondelete="CASCADE"), primary_key=True),
)


class Benutzer(Base):
    __tablename__ = "benutzer"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    passwort_hash: Mapped[str] = mapped_column(String(512))
    rolle: Mapped[str] = mapped_column(String(20), default=Rolle.BEARBEITER.value)
    aktiv: Mapped[bool] = mapped_column(Boolean, default=True)
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    mandanten: Mapped[list["Mandant"]] = relationship(
        secondary=benutzer_mandanten, back_populates="benutzer"
    )


class Mandant(Base):
    __tablename__ = "mandanten"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    kurzname: Mapped[str] = mapped_column(String(64), unique=True)
    kontenrahmen: Mapped[str] = mapped_column(String(16), default="SKR03")  # SKR03/SKR04/EIGEN
    bundesland: Mapped[str] = mapped_column(String(2), default="NW")
    ust_zeitraum: Mapped[str] = mapped_column(String(10), default=UStZeitraum.MONAT.value)
    dauerfrist: Mapped[bool] = mapped_column(Boolean, default=False)
    # Insolvenzkontext
    verfahrensstatus: Mapped[str] = mapped_column(String(32), default="REGELMANDAT")
    # REGELMANDAT / VORLAEUFIG / EROEFFNET / EIGENVERWALTUNG
    aktenzeichen: Mapped[str | None] = mapped_column(String(64), nullable=True)
    insolvenz_stichtag: Mapped[date | None] = mapped_column(Date, nullable=True)
    aktiv: Mapped[bool] = mapped_column(Boolean, default=True)
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    benutzer: Mapped[list[Benutzer]] = relationship(
        secondary=benutzer_mandanten, back_populates="mandanten"
    )
    konten: Mapped[list["Konto"]] = relationship(back_populates="mandant")


class KontoGruppe(Base):
    __tablename__ = "konto_gruppen"
    __table_args__ = (UniqueConstraint("mandant_id", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mandant_id: Mapped[int] = mapped_column(ForeignKey("mandanten.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(255))
    richtung: Mapped[str] = mapped_column(String(10), default=Richtung.AUS.value)
    sortierung: Mapped[int] = mapped_column(Integer, default=0)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("konto_gruppen.id", ondelete="SET NULL"), nullable=True
    )

    konten: Mapped[list["Konto"]] = relationship(back_populates="gruppe")


class Konto(Base):
    __tablename__ = "konten"
    __table_args__ = (UniqueConstraint("mandant_id", "nummer"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mandant_id: Mapped[int] = mapped_column(ForeignKey("mandanten.id", ondelete="CASCADE"), index=True)
    nummer: Mapped[str] = mapped_column(String(16), index=True)
    bezeichnung: Mapped[str] = mapped_column(String(255))
    typ: Mapped[str] = mapped_column(String(20), default=KontoTyp.AUFWAND.value)
    # None = nicht umsatzsteuerpflichtig; sonst Prozentsatz (z. B. 19.00, 7.00, 0.00)
    ust_satz: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    gruppe_id: Mapped[int | None] = mapped_column(
        ForeignKey("konto_gruppen.id", ondelete="SET NULL"), nullable=True
    )
    # Kreditlinie (nur Bankkonten): verfügbare Liquidität = Bestand + freie Linie
    kreditlinie: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    liquiditaetswirksam: Mapped[bool] = mapped_column(Boolean, default=True)
    aktiv: Mapped[bool] = mapped_column(Boolean, default=True)

    mandant: Mapped[Mandant] = relationship(back_populates="konten")
    gruppe: Mapped[KontoGruppe | None] = relationship(back_populates="konten")


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    mandant_id: Mapped[int] = mapped_column(ForeignKey("mandanten.id", ondelete="CASCADE"), index=True)
    dateiname: Mapped[str] = mapped_column(String(255))
    format: Mapped[str] = mapped_column(String(32))  # DATEV / CSV / BWA
    anzahl: Mapped[int] = mapped_column(Integer, default=0)
    warnungen: Mapped[str | None] = mapped_column(Text, nullable=True)
    benutzer_id: Mapped[int | None] = mapped_column(ForeignKey("benutzer.id"), nullable=True)
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Buchung(Base):
    __tablename__ = "buchungen"

    id: Mapped[int] = mapped_column(primary_key=True)
    mandant_id: Mapped[int] = mapped_column(ForeignKey("mandanten.id", ondelete="CASCADE"), index=True)
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=True, index=True
    )
    datum: Mapped[date] = mapped_column(Date, index=True)
    konto_nr: Mapped[str] = mapped_column(String(16), index=True)
    gegenkonto_nr: Mapped[str] = mapped_column(String(16), index=True)
    betrag: Mapped[Decimal] = mapped_column(Numeric(14, 2))  # immer positiv
    sh: Mapped[str] = mapped_column(String(1), default="S")  # S/H bezogen auf konto_nr
    belegfeld: Mapped[str | None] = mapped_column(String(64), nullable=True)
    text: Mapped[str | None] = mapped_column(String(255), nullable=True)


class BWAWert(Base):
    """Historische BWA-/Monatssalden je Konto (Budget-Vorschlagsbasis)."""

    __tablename__ = "bwa_werte"
    __table_args__ = (UniqueConstraint("mandant_id", "konto_nr", "jahr", "monat"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mandant_id: Mapped[int] = mapped_column(ForeignKey("mandanten.id", ondelete="CASCADE"), index=True)
    konto_nr: Mapped[str] = mapped_column(String(16))
    jahr: Mapped[int] = mapped_column(Integer)
    monat: Mapped[int] = mapped_column(Integer)
    betrag: Mapped[Decimal] = mapped_column(Numeric(14, 2))


class OffenerPosten(Base):
    __tablename__ = "offene_posten"

    id: Mapped[int] = mapped_column(primary_key=True)
    mandant_id: Mapped[int] = mapped_column(ForeignKey("mandanten.id", ondelete="CASCADE"), index=True)
    art: Mapped[str] = mapped_column(String(10), default=PostenArt.KREDITOR.value)
    partner: Mapped[str] = mapped_column(String(255))
    belegnr: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rechnungsdatum: Mapped[date | None] = mapped_column(Date, nullable=True)
    faellig_am: Mapped[date] = mapped_column(Date, index=True)
    zahlung_geplant_am: Mapped[date | None] = mapped_column(Date, nullable=True)
    betrag_brutto: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    konto_id: Mapped[int | None] = mapped_column(ForeignKey("konten.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default=PostenStatus.OFFEN.value)
    bezahlt_am: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Insolvenzrechtliche Einordnung (Ausbaustufe: Zahlungssperre)
    forderungsklasse: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notiz: Mapped[str | None] = mapped_column(Text, nullable=True)

    konto: Mapped[Konto | None] = relationship()


class Dauerbuchung(Base):
    __tablename__ = "dauerbuchungen"

    id: Mapped[int] = mapped_column(primary_key=True)
    mandant_id: Mapped[int] = mapped_column(ForeignKey("mandanten.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    art: Mapped[str] = mapped_column(String(10), default=PostenArt.KREDITOR.value)
    konto_id: Mapped[int | None] = mapped_column(ForeignKey("konten.id", ondelete="SET NULL"), nullable=True)
    betrag_brutto: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    intervall: Mapped[str] = mapped_column(String(16), default=Intervall.MONATLICH.value)
    # MONATLICH/QUARTAL/JAEHRLICH: Kalendertag 1..31 | WOECHENTLICH: Wochentag 0=Mo..6=So
    stichtag: Mapped[int] = mapped_column(Integer, default=1)
    gueltig_von: Mapped[date] = mapped_column(Date)
    gueltig_bis: Mapped[date | None] = mapped_column(Date, nullable=True)
    aktiv: Mapped[bool] = mapped_column(Boolean, default=True)

    konto: Mapped[Konto | None] = relationship()


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("mandant_id", "konto_id", "jahr", "monat"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mandant_id: Mapped[int] = mapped_column(ForeignKey("mandanten.id", ondelete="CASCADE"), index=True)
    konto_id: Mapped[int] = mapped_column(ForeignKey("konten.id", ondelete="CASCADE"), index=True)
    jahr: Mapped[int] = mapped_column(Integer)
    monat: Mapped[int] = mapped_column(Integer)
    betrag_netto: Mapped[Decimal] = mapped_column(Numeric(14, 2))

    konto: Mapped[Konto] = relationship()


class BudgetWoche(Base):
    """Wochen-Override: ersetzt für diese ISO-Woche das anteilige Monatsbudget."""

    __tablename__ = "budget_wochen"
    __table_args__ = (UniqueConstraint("mandant_id", "konto_id", "jahr", "kw"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mandant_id: Mapped[int] = mapped_column(ForeignKey("mandanten.id", ondelete="CASCADE"), index=True)
    konto_id: Mapped[int] = mapped_column(ForeignKey("konten.id", ondelete="CASCADE"), index=True)
    jahr: Mapped[int] = mapped_column(Integer)  # ISO-Jahr
    kw: Mapped[int] = mapped_column(Integer)    # ISO-Kalenderwoche
    betrag_netto: Mapped[Decimal] = mapped_column(Numeric(14, 2))


class TerminRegel(Base):
    __tablename__ = "termin_regeln"
    __table_args__ = (UniqueConstraint("mandant_id", "typ"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mandant_id: Mapped[int] = mapped_column(ForeignKey("mandanten.id", ondelete="CASCADE"), index=True)
    typ: Mapped[str] = mapped_column(String(16))
    aktiv: Mapped[bool] = mapped_column(Boolean, default=True)
    betrag_modus: Mapped[str] = mapped_column(String(10), default="FIX")  # FIX / HISTORIE
    betrag_fix: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    konto_id: Mapped[int | None] = mapped_column(ForeignKey("konten.id", ondelete="SET NULL"), nullable=True)

    konto: Mapped[Konto | None] = relationship()


class Zahlungstermin(Base):
    __tablename__ = "zahlungstermine"

    id: Mapped[int] = mapped_column(primary_key=True)
    mandant_id: Mapped[int] = mapped_column(ForeignKey("mandanten.id", ondelete="CASCADE"), index=True)
    typ: Mapped[str] = mapped_column(String(16))
    # fachliche Periode generierter Termine (z. B. "2026-05", "2026-Q3");
    # verhindert Duplikate bei Neugenerierung nach manueller Terminverschiebung
    periode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    datum: Mapped[date] = mapped_column(Date, index=True)
    betrag: Mapped[Decimal] = mapped_column(Numeric(14, 2))  # Auszahlung positiv erfasst
    status: Mapped[str] = mapped_column(String(12), default=TerminStatus.GEPLANT.value)
    konto_id: Mapped[int | None] = mapped_column(ForeignKey("konten.id", ondelete="SET NULL"), nullable=True)
    kommentar: Mapped[str | None] = mapped_column(String(255), nullable=True)
    generiert: Mapped[bool] = mapped_column(Boolean, default=True)

    konto: Mapped[Konto | None] = relationship()


class Bestand(Base):
    """Stichtagsbestand (Anker) je Bank-/Kassenkonto bzw. Warenbestand."""

    __tablename__ = "bestaende"

    id: Mapped[int] = mapped_column(primary_key=True)
    mandant_id: Mapped[int] = mapped_column(ForeignKey("mandanten.id", ondelete="CASCADE"), index=True)
    typ: Mapped[str] = mapped_column(String(10))
    konto_id: Mapped[int | None] = mapped_column(ForeignKey("konten.id", ondelete="CASCADE"), nullable=True)
    datum: Mapped[date] = mapped_column(Date, index=True)
    wert: Mapped[Decimal] = mapped_column(Numeric(14, 2))

    konto: Mapped[Konto | None] = relationship()


class PlanSnapshot(Base):
    __tablename__ = "plan_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    mandant_id: Mapped[int] = mapped_column(ForeignKey("mandanten.id", ondelete="CASCADE"), index=True)
    stichtag: Mapped[date] = mapped_column(Date)   # Fensterbeginn (Montag)
    wochen: Mapped[int] = mapped_column(Integer, default=13)
    kommentar: Mapped[str | None] = mapped_column(String(255), nullable=True)
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    werte: Mapped[list["PlanSnapshotWert"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class PlanSnapshotWert(Base):
    __tablename__ = "plan_snapshot_werte"

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("plan_snapshots.id", ondelete="CASCADE"), index=True
    )
    konto_id: Mapped[int | None] = mapped_column(ForeignKey("konten.id", ondelete="CASCADE"), nullable=True)
    termin_typ: Mapped[str | None] = mapped_column(String(16), nullable=True)  # für Termine ohne Konto
    datum: Mapped[date] = mapped_column(Date, index=True)
    betrag: Mapped[Decimal] = mapped_column(Numeric(14, 2))

    snapshot: Mapped[PlanSnapshot] = relationship(back_populates="werte")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    benutzer_id: Mapped[int | None] = mapped_column(ForeignKey("benutzer.id"), nullable=True)
    mandant_id: Mapped[int | None] = mapped_column(ForeignKey("mandanten.id"), nullable=True)
    aktion: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    zeit: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
