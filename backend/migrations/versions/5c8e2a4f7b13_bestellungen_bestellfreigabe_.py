"""Bestellungen, Bestellfreigabe, Belegablage, Systemeinstellungen

Revision: 5c8e2a4f7b13
Vorgänger: 9b3d5e7f1a20
Erstellt: 2026-10-30
"""
from alembic import op
import sqlalchemy as sa


revision = '5c8e2a4f7b13'
down_revision = '9b3d5e7f1a20'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('system_einstellungen',
    sa.Column('schluessel', sa.String(length=64), nullable=False),
    sa.Column('wert', sa.Text(), nullable=True),
    sa.Column('verschluesselt', sa.Boolean(), nullable=False),
    sa.Column('geaendert_am', sa.DateTime(timezone=True), nullable=False),
    sa.Column('geaendert_von', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['geaendert_von'], ['benutzer.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('schluessel')
    )
    op.create_table('bestellungen',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('mandant_id', sa.Integer(), nullable=False),
    sa.Column('kreditor', sa.String(length=255), nullable=False),
    sa.Column('bestellnummer', sa.String(length=64), nullable=True),
    sa.Column('beschreibung', sa.Text(), nullable=True),
    sa.Column('bestelldatum', sa.Date(), nullable=False),
    sa.Column('lieferdatum', sa.Date(), nullable=True),
    sa.Column('zahldatum', sa.Date(), nullable=False),
    sa.Column('betrag_netto', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('ust_satz', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('konto_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('entschieden_am', sa.DateTime(timezone=True), nullable=True),
    sa.Column('entschieden_von', sa.Integer(), nullable=True),
    sa.Column('abgeloest_am', sa.Date(), nullable=True),
    sa.Column('abgeloest_durch', sa.String(length=10), nullable=True),
    sa.Column('datei_name', sa.String(length=255), nullable=True),
    sa.Column('datei_mime', sa.String(length=100), nullable=True),
    sa.Column('datei_groesse', sa.Integer(), nullable=True),
    sa.Column('datei_ablage', sa.String(length=16), nullable=True),
    sa.Column('datei_pfad', sa.String(length=1024), nullable=True),
    sa.Column('datei_drive_id', sa.String(length=255), nullable=True),
    sa.Column('datei_item_id', sa.String(length=255), nullable=True),
    sa.Column('datei_web_url', sa.String(length=1024), nullable=True),
    sa.Column('ki_daten', sa.JSON(), nullable=True),
    sa.Column('erstellt_am', sa.DateTime(timezone=True), nullable=False),
    sa.Column('erstellt_von', sa.Integer(), nullable=True),
    sa.Column('geaendert_am', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['entschieden_von'], ['benutzer.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['erstellt_von'], ['benutzer.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['konto_id'], ['konten.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandanten.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('bestellungen', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_bestellungen_mandant_id'), ['mandant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_bestellungen_zahldatum'), ['zahldatum'], unique=False)

    op.create_table('bestell_journal',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('mandant_id', sa.Integer(), nullable=False),
    sa.Column('bestellung_id', sa.Integer(), nullable=True),
    sa.Column('zeit', sa.DateTime(timezone=True), nullable=False),
    sa.Column('benutzer_id', sa.Integer(), nullable=True),
    sa.Column('benutzer_name', sa.String(length=255), nullable=True),
    sa.Column('aktion', sa.String(length=24), nullable=False),
    sa.Column('status_vorher', sa.String(length=12), nullable=True),
    sa.Column('status_nachher', sa.String(length=12), nullable=True),
    sa.Column('begruendung', sa.Text(), nullable=True),
    sa.Column('bestellung_text', sa.String(length=255), nullable=True),
    sa.Column('kennzahlen', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['benutzer_id'], ['benutzer.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['bestellung_id'], ['bestellungen.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandanten.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('bestell_journal', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_bestell_journal_bestellung_id'), ['bestellung_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_bestell_journal_mandant_id'), ['mandant_id'], unique=False)

    op.create_table('bestell_abloesungen',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('mandant_id', sa.Integer(), nullable=False),
    sa.Column('bestellung_id', sa.Integer(), nullable=False),
    sa.Column('art', sa.String(length=10), nullable=False),
    sa.Column('buchung_id', sa.Integer(), nullable=True),
    sa.Column('transaktion_id', sa.Integer(), nullable=True),
    sa.Column('posten_id', sa.Integer(), nullable=True),
    sa.Column('betrag', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('datum', sa.Date(), nullable=False),
    sa.Column('abschliessend', sa.Boolean(), nullable=False),
    sa.Column('notiz', sa.String(length=255), nullable=True),
    sa.Column('erstellt_am', sa.DateTime(timezone=True), nullable=False),
    sa.Column('benutzer_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['benutzer_id'], ['benutzer.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['bestellung_id'], ['bestellungen.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['buchung_id'], ['buchungen.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandanten.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['posten_id'], ['offene_posten.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['transaktion_id'], ['bank_transaktionen.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('bestell_abloesungen', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_bestell_abloesungen_bestellung_id'), ['bestellung_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_bestell_abloesungen_buchung_id'), ['buchung_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_bestell_abloesungen_mandant_id'), ['mandant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_bestell_abloesungen_posten_id'), ['posten_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_bestell_abloesungen_transaktion_id'), ['transaktion_id'], unique=False)

    with op.batch_alter_table('benutzer', schema=None) as batch_op:
        batch_op.add_column(sa.Column('darf_freigeben', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))

    with op.batch_alter_table('mandanten', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ablage_typ', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('ablage_pfad', sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column('ablage_site_id', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('ablage_drive_id', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('ablage_ordner_id', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('ablage_web_url', sa.String(length=1024), nullable=True))
        batch_op.add_column(sa.Column('bestellungen_offen_einplanen', sa.Boolean(), nullable=False,
                                      server_default=sa.true()))



def downgrade() -> None:
    with op.batch_alter_table('mandanten', schema=None) as batch_op:
        batch_op.drop_column('bestellungen_offen_einplanen')
        batch_op.drop_column('ablage_web_url')
        batch_op.drop_column('ablage_ordner_id')
        batch_op.drop_column('ablage_drive_id')
        batch_op.drop_column('ablage_site_id')
        batch_op.drop_column('ablage_pfad')
        batch_op.drop_column('ablage_typ')

    with op.batch_alter_table('benutzer', schema=None) as batch_op:
        batch_op.drop_column('darf_freigeben')

    with op.batch_alter_table('bestell_abloesungen', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bestell_abloesungen_transaktion_id'))
        batch_op.drop_index(batch_op.f('ix_bestell_abloesungen_posten_id'))
        batch_op.drop_index(batch_op.f('ix_bestell_abloesungen_mandant_id'))
        batch_op.drop_index(batch_op.f('ix_bestell_abloesungen_buchung_id'))
        batch_op.drop_index(batch_op.f('ix_bestell_abloesungen_bestellung_id'))

    op.drop_table('bestell_abloesungen')
    with op.batch_alter_table('bestell_journal', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bestell_journal_mandant_id'))
        batch_op.drop_index(batch_op.f('ix_bestell_journal_bestellung_id'))

    op.drop_table('bestell_journal')
    with op.batch_alter_table('bestellungen', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bestellungen_zahldatum'))
        batch_op.drop_index(batch_op.f('ix_bestellungen_mandant_id'))

    op.drop_table('bestellungen')
    op.drop_table('system_einstellungen')
