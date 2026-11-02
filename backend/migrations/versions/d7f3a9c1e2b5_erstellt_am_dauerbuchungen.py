"""Dauerbuchungen: Anlagezeitpunkt (erstellt_am)

Wie bei Posten und Zahlungsterminen (c5e8f1a2b4d6): Eine erst nach einer Planverschiebung
angelegte Dauerbuchung darf mit ihrer Rate am Ursprungstag die Verschiebung nicht
übernehmen – ihre Raten standen beim Anlegen nicht in der Zelle. Neue Zeilen erhalten den
Anlagezeitpunkt über den Modell-Standard; bestehende bleiben NULL („unbekannt, alt“) und
verhalten sich wie bisher.

Revision: d7f3a9c1e2b5
Vorgänger: c5e8f1a2b4d6
Erstellt: 2026-11-02
"""
from alembic import op
import sqlalchemy as sa


revision = 'd7f3a9c1e2b5'
down_revision = 'c5e8f1a2b4d6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('dauerbuchungen', schema=None) as batch_op:
        batch_op.add_column(sa.Column('erstellt_am', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('dauerbuchungen', schema=None) as batch_op:
        batch_op.drop_column('erstellt_am')
