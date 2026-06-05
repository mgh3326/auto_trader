"""ROB-441: allow US non-DART sources in financial_fundamentals_snapshots.

Additive: widens the ``source`` CHECK constraint from ('dart') to
('dart', 'yfinance', 'finnhub') so US fundamentals periods (parsed from yfinance
income statements, finnhub as fallback) can be stored alongside KR DART rows.
The market CHECK already allows ('kr', 'us'); no other column changes. The derive
layer is market-agnostic and reused as-is.

Revision ID: 20260605_rob441
Revises: 20260604_rob430
Create Date: 2026-06-05
"""

from __future__ import annotations

from alembic import op

revision = "20260605_rob441"
down_revision = "20260604_rob430"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_financial_fundamentals_snapshots_source"
_TABLE = "financial_fundamentals_snapshots"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        "source IN ('dart', 'yfinance', 'finnhub')",
    )


def downgrade() -> None:
    # Reverting requires no non-DART rows remain (else the tighter CHECK fails).
    op.execute(f"DELETE FROM {_TABLE} WHERE source <> 'dart'")
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, "source IN ('dart')")
