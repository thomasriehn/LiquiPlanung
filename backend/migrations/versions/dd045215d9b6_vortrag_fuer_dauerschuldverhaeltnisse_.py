"""Vortrag fuer Dauerschuldverhaeltnisse und Soll-Referenz

Revision: dd045215d9b6
Vorgänger: 15c2bf98e494
Erstellt: 2026-10-23 11:18:19.178572
"""
from alembic import op
import sqlalchemy as sa


revision = 'dd045215d9b6'
down_revision = '15c2bf98e494'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Bestandsdaten: Vortrag standardmäßig an, noch keine Soll-Referenz gewählt
    with op.batch_alter_table('dauerbuchungen', schema=None) as batch_op:
        batch_op.add_column(sa.Column('vortragen', sa.Boolean(), nullable=False,
                                      server_default=sa.true()))

    with op.batch_alter_table('plan_snapshots', schema=None) as batch_op:
        batch_op.add_column(sa.Column('referenz', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('plan_snapshots', schema=None) as batch_op:
        batch_op.drop_column('referenz')

    with op.batch_alter_table('dauerbuchungen', schema=None) as batch_op:
        batch_op.drop_column('vortragen')
