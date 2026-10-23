"""Sondereffekt-Kennzeichen fuer Kontogruppen

Revision: 1f19e7bf46e5
Vorgänger: dd045215d9b6
Erstellt: 2026-10-23
"""
from alembic import op
import sqlalchemy as sa


revision = '1f19e7bf46e5'
down_revision = 'dd045215d9b6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('konto_gruppen', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sondereffekt', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
    # Vorbelegung für bestehende Mandanten: insolvenzspezifische Gruppen und
    # Anfechtungen gelten als Sondereffekt (jederzeit unter „Konten“ änderbar)
    op.execute(
        "UPDATE konto_gruppen SET sondereffekt = (1 = 1) "
        "WHERE lower(name) LIKE '%insolvenz%' OR lower(name) LIKE '%anfechtung%'"
    )


def downgrade() -> None:
    with op.batch_alter_table('konto_gruppen', schema=None) as batch_op:
        batch_op.drop_column('sondereffekt')
