"""Passwortwechsel beim nächsten Login erzwingen

Revision: 4c7e1b2d9a51
Vorgänger: 702e60bbd3bf
Erstellt: 2026-10-30
"""
from alembic import op
import sqlalchemy as sa


revision = '4c7e1b2d9a51'
down_revision = '702e60bbd3bf'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('benutzer', schema=None) as batch_op:
        batch_op.add_column(sa.Column('passwort_aendern', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('benutzer', schema=None) as batch_op:
        batch_op.drop_column('passwort_aendern')
