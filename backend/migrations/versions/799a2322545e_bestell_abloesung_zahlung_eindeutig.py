"""Bestell-Ablösung: eine Zahlung löst höchstens eine Bestellung ab

Partielle eindeutige Indizes auf bestell_abloesungen.buchung_id bzw.
.transaktion_id (WHERE … IS NOT NULL) ersetzen die einfachen Indizes – so
scheitert auch bei gleichzeitigen Anfragen die zweite Zuordnung derselben Zahlung.

Revision: 799a2322545e
Vorgänger: 5c8e2a4f7b13
Erstellt: 2026-10-31
"""
from alembic import op
import sqlalchemy as sa


revision = '799a2322545e'
down_revision = '5c8e2a4f7b13'
branch_labels = None
depends_on = None

_SPALTEN = ('buchung_id', 'transaktion_id')


def _doppelte_pruefen() -> None:
    """Klare Meldung statt IntegrityError, falls schon Doppelzuordnungen bestehen."""
    if op.get_context().as_sql:   # Offline-SQL (--sql): keine Prüfung möglich
        return
    verbindung = op.get_bind()
    for spalte in _SPALTEN:
        doppelt = verbindung.execute(sa.text(
            f"SELECT {spalte}, COUNT(*) FROM bestell_abloesungen WHERE {spalte} IS NOT NULL "
            f"GROUP BY {spalte} HAVING COUNT(*) > 1"
        )).fetchall()
        if doppelt:
            liste = ", ".join(str(z[0]) for z in doppelt[:20])
            raise RuntimeError(
                f"bestell_abloesungen: {spalte} mehrfach zugeordnet ({liste}) – bitte die "
                "doppelten Ablösungen in der Anwendung aufheben und die Migration wiederholen")


def upgrade() -> None:
    _doppelte_pruefen()
    with op.batch_alter_table('bestell_abloesungen', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bestell_abloesungen_buchung_id'))
        batch_op.drop_index(batch_op.f('ix_bestell_abloesungen_transaktion_id'))
        for spalte in _SPALTEN:
            bedingung = sa.text(f"{spalte} IS NOT NULL")
            batch_op.create_index(f'uq_bestell_abloesungen_{spalte}', [spalte], unique=True,
                                  sqlite_where=bedingung, postgresql_where=bedingung)


def downgrade() -> None:
    with op.batch_alter_table('bestell_abloesungen', schema=None) as batch_op:
        for spalte in reversed(_SPALTEN):
            batch_op.drop_index(f'uq_bestell_abloesungen_{spalte}')
        batch_op.create_index(batch_op.f('ix_bestell_abloesungen_transaktion_id'),
                              ['transaktion_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_bestell_abloesungen_buchung_id'),
                              ['buchung_id'], unique=False)
