"""rob-321 PR4a: round-trip scalping columns on kis_mock_order_ledger (additive)

Revision ID: 20260526_rob321_p4a
Revises: 20260526_rob318_p3
Create Date: 2026-05-26

Adds 5 nullable columns + 1 index to ``review.kis_mock_order_ledger`` for the
KIS mock scalping round-trip executor/reconciler (ROB-321 PR4a):

* ``correlation_id`` (Text, indexed) — a buy/sell round trip shares one id.
* ``scalping_role`` (Text) — 'entry' | 'exit'.
* ``exit_reason`` (Text) — 'stop_loss' | 'take_profit' | 'time_stop'.
* ``gross_pnl`` / ``net_pnl`` (Numeric(20,4)) — recorded on the exit leg once
  the round trip is paired from execution evidence (net = gross - fees).

Purely additive and nullable: existing/legacy rows stay readable (NULL). No
CHECK change — the terminal closed state reuses the existing ``reconciled``
lifecycle_state. Operator applies via ``alembic upgrade head`` separately.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260526_rob321_p4a"
down_revision: str | None = "20260526_rob318_p3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "kis_mock_order_ledger"
_SCHEMA = "review"
_INDEX = "ix_kis_mock_ledger_correlation_id"


def upgrade() -> None:
    op.add_column(
        _TABLE, sa.Column("correlation_id", sa.Text(), nullable=True), schema=_SCHEMA
    )
    op.add_column(
        _TABLE, sa.Column("scalping_role", sa.Text(), nullable=True), schema=_SCHEMA
    )
    op.add_column(
        _TABLE, sa.Column("exit_reason", sa.Text(), nullable=True), schema=_SCHEMA
    )
    op.add_column(
        _TABLE,
        sa.Column("gross_pnl", sa.Numeric(20, 4), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE, sa.Column("net_pnl", sa.Numeric(20, 4), nullable=True), schema=_SCHEMA
    )
    op.create_index(_INDEX, _TABLE, ["correlation_id"], schema=_SCHEMA)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE, schema=_SCHEMA)
    op.drop_column(_TABLE, "net_pnl", schema=_SCHEMA)
    op.drop_column(_TABLE, "gross_pnl", schema=_SCHEMA)
    op.drop_column(_TABLE, "exit_reason", schema=_SCHEMA)
    op.drop_column(_TABLE, "scalping_role", schema=_SCHEMA)
    op.drop_column(_TABLE, "correlation_id", schema=_SCHEMA)
