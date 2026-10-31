"""Planstand: Anteil noch nicht entschiedener Bestellungen kennzeichnen

Planstände frieren den angezeigten Plan vollständig ein, auch die laut
Mandantenschalter eingeplanten offenen Bestellungen. Deren Anteil steht in eigenen
Zeilen mit bestellung_offen = true; die Bestellfreigabe rechnet ihn aus dem Soll
heraus. Bestehende Planstände erhalten false und bleiben unverändert gültig.

Revision: a4d7c2e9b310
Vorgänger: 799a2322545e
Erstellt: 2026-10-31
"""
from alembic import op
import sqlalchemy as sa


revision = 'a4d7c2e9b310'
down_revision = '799a2322545e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('plan_snapshot_werte', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bestellung_offen', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))


def downgrade() -> None:
    # gekennzeichnete Zeilen gehören nur zum vollständigen Plan; ohne Kennzeichnung
    # behandelte die Freigabe sie als Soll – daher vor dem Entfernen löschen
    werte = sa.table('plan_snapshot_werte', sa.column('bestellung_offen', sa.Boolean()))
    op.execute(werte.delete().where(werte.c.bestellung_offen == sa.true()))
    with op.batch_alter_table('plan_snapshot_werte', schema=None) as batch_op:
        batch_op.drop_column('bestellung_offen')
