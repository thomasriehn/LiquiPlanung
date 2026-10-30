"""Budget: Aufteilung (KW/Tag) und Fortschreibung in Folgemonate

Revision: 9b3d5e7f1a20
Vorgänger: 67f54b415cbd
Erstellt: 2026-10-30
"""
from alembic import op
import sqlalchemy as sa


revision = '9b3d5e7f1a20'
down_revision = '67f54b415cbd'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('budgets', schema=None) as batch_op:
        batch_op.add_column(sa.Column('aufteilung', sa.String(length=5), nullable=True))
        batch_op.add_column(sa.Column('fortschreiben', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
    with op.batch_alter_table('budget_wochen', schema=None) as batch_op:
        batch_op.add_column(sa.Column('aufteilung', sa.String(length=5), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('budget_wochen', schema=None) as batch_op:
        batch_op.drop_column('aufteilung')
    with op.batch_alter_table('budgets', schema=None) as batch_op:
        batch_op.drop_column('fortschreiben')
        batch_op.drop_column('aufteilung')
