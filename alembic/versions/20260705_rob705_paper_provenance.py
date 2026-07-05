"""ROB-705 paper provenance: correlation cols + paper account_mode + stop_loss trigger.

Revision ID: 20260705_rob705
Revises: 20260704_rob703
Create Date: 2026-07-05 00:00:00.000000

Additive on the ROB-703 paper sim:

* 4 nullable provenance columns (correlation_id, journal_id, artifact_uuid,
  forecast_id) on ``paper.paper_trades`` and ``paper.paper_pending_orders`` so a
  placed order and its eventual fill share one deterministic spine id linking to
  the draft TradeJournal and optional price_target Forecast.
* ``'paper'`` admitted into the ``trade_retrospectives.account_mode`` CHECK so a
  filled paper trade can surface as a retrospective candidate.
* ``'stop_loss'`` admitted into ``ck_trade_retrospectives_trigger_type`` so a
  loss-making paper sell can be suggested as a stop-loss retrospective.

No FKs (loose coupling — the journal/forecast live in the ``review`` schema and
are linked by id only). Downgrade restores the prior CHECK IN-lists and drops
the four columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260705_rob705"
down_revision: str | Sequence[str] | None = "20260704_rob703"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PAPER_COLS = (
    ("correlation_id", sa.Text()),
    ("journal_id", sa.BigInteger()),
    ("artifact_uuid", sa.Text()),
    ("forecast_id", sa.Text()),
)
_ACCOUNT_MODES_NEW = (
    "account_mode IN "
    "('kis_mock','kiwoom_mock','kis_live','toss_live','alpaca_paper','upbit_live','paper')"
)
_ACCOUNT_MODES_OLD = (
    "account_mode IN "
    "('kis_mock','kiwoom_mock','kis_live','toss_live','alpaca_paper','upbit_live')"
)
_TRIGGER_NEW = (
    "trigger_type IS NULL OR trigger_type IN ("
    "'fill','partial_fill','rejected_order','cancelled','expired',"
    "'thesis_change','policy_violation','stale_evidence','guardrail_block','stop_loss'"
    ")"
)
_TRIGGER_OLD = (
    "trigger_type IS NULL OR trigger_type IN ("
    "'fill','partial_fill','rejected_order','cancelled','expired',"
    "'thesis_change','policy_violation','stale_evidence','guardrail_block'"
    ")"
)
_REVIEW = "review"
_TABLE = "trade_retrospectives"
# Real DB constraint names are DOUBLE-prefixed (SQLAlchemy naming_convention
# ck_%(table_name)s_%(constraint_name)s applied to an already-prefixed name).
# Verified against review.trade_retrospectives via pg_constraint. Drop/add by
# the exact names with raw SQL so alembic's convention resolution can't diverge.
_CK_ACCOUNT = "ck_trade_retrospectives_ck_trade_retrospectives_account_mode"
_CK_TRIGGER = "ck_trade_retrospectives_ck_trade_retrospectives_trigger_type"


def _swap_check(name: str, expr: str) -> None:
    op.execute(f'ALTER TABLE {_REVIEW}.{_TABLE} DROP CONSTRAINT IF EXISTS "{name}"')
    op.execute(f'ALTER TABLE {_REVIEW}.{_TABLE} ADD CONSTRAINT "{name}" CHECK ({expr})')


def upgrade() -> None:
    for tbl in ("paper_trades", "paper_pending_orders"):
        for name, col_type in _PAPER_COLS:
            op.add_column(
                tbl,
                sa.Column(name, col_type, nullable=True),
                schema="paper",
            )
    _swap_check(_CK_ACCOUNT, _ACCOUNT_MODES_NEW)
    _swap_check(_CK_TRIGGER, _TRIGGER_NEW)


def downgrade() -> None:
    _swap_check(_CK_TRIGGER, _TRIGGER_OLD)
    _swap_check(_CK_ACCOUNT, _ACCOUNT_MODES_OLD)
    for tbl in ("paper_trades", "paper_pending_orders"):
        for name, _ in _PAPER_COLS:
            op.drop_column(tbl, name, schema="paper")
