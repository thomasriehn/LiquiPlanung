"""Offene Posten und Zahlungstermine: Anlagezeitpunkt (erstellt_am)

Eine Planverschiebung wird den Bestandteilen zugeordnet, die beim Anlegen am Ursprungstag
lagen. Ein erst danach erfasster Posten (z. B. eine Eingangsrechnung am Ursprungstag einer
Budgetverschiebung) oder Termin darf die Verschiebung nicht übernehmen – dafür braucht die
Zuordnung den Anlagezeitpunkt, wie bei Bestellungen. Neue Zeilen erhalten ihn über den
Modell-Standard; bestehende bleiben NULL („unbekannt, alt“) und verhalten sich wie bisher.

Revision: c5e8f1a2b4d6
Vorgänger: a4d7c2e9b310
Erstellt: 2026-11-01
"""
from alembic import op
import sqlalchemy as sa


revision = 'c5e8f1a2b4d6'
down_revision = 'a4d7c2e9b310'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('offene_posten', schema=None) as batch_op:
        batch_op.add_column(sa.Column('erstellt_am', sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table('zahlungstermine', schema=None) as batch_op:
        batch_op.add_column(sa.Column('erstellt_am', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('zahlungstermine', schema=None) as batch_op:
        batch_op.drop_column('erstellt_am')
    with op.batch_alter_table('offene_posten', schema=None) as batch_op:
        batch_op.drop_column('erstellt_am')
