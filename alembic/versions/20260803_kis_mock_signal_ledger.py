"""kis_mock pre-submit signal ledger (additive: one new table only).

Revision ID: 20260803_kis_mock_signal
Revises: 20260802_rob1036_sample_elig
Create Date: 2026-08-03

Purely additive. Creates ``review.kis_mock_signal_ledger`` and nothing else:
no existing table, column, constraint, index, or row is altered or dropped.

Why a new table rather than NOT NULL on ``review.kis_mock_order_ledger``:
``correlation_id`` / ``strategy`` there were added additively as nullable
(ROB-321 / ROB-730) and historical rows predating the place-time mint carry
NULLs, so a NOT NULL rewrite would fail on production data. Attribution is
instead enforced NOT NULL on this fresh table, which is written *before* the
broker send — that is where pre-submit enforcement belongs, and it has no
legacy rows to break. See docs/runbooks/kis-mock-attribution-chain.md.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260803_kis_mock_signal"
down_revision = "20260802_rob1036_sample_elig"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "kis_mock_signal_ledger"
_SCHEMA = "review"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("strategy", sa.Text(), nullable=False),
        sa.Column("signal_source", sa.Text(), nullable=False),
        sa.Column(
            "account_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'kis_mock'"),
        ),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("instrument_type", sa.Text(), nullable=True),
        sa.Column("side", sa.Text(), nullable=True),
        sa.Column("intended_quantity", sa.Numeric(20, 8), nullable=True),
        sa.Column("intended_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column(
            "outcome_state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'recorded'"),
        ),
        sa.Column("suppressed_reason", sa.Text(), nullable=True),
        sa.Column("report_item_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("kst_date", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "correlation_id", name="uq_kis_mock_signal_ledger_correlation_id"
        ),
        sa.CheckConstraint(
            "account_mode = 'kis_mock'", name="ck_kis_mock_signal_account_mode"
        ),
        sa.CheckConstraint(
            "decision IN ('order','no_order')", name="ck_kis_mock_signal_decision"
        ),
        sa.CheckConstraint(
            "outcome_state IN ('recorded','submitted','suppressed','failed')",
            name="ck_kis_mock_signal_outcome_state",
        ),
        sa.CheckConstraint(
            "side IS NULL OR side IN ('buy','sell')", name="ck_kis_mock_signal_side"
        ),
        sa.CheckConstraint(
            "length(btrim(strategy)) > 0",
            name="ck_kis_mock_signal_strategy_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(signal_source)) > 0",
            name="ck_kis_mock_signal_source_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(correlation_id)) > 0",
            name="ck_kis_mock_signal_correlation_nonblank",
        ),
        schema=_SCHEMA,
    )
    op.create_index("ix_kis_mock_signal_kst_date", _TABLE, ["kst_date"], schema=_SCHEMA)
    op.create_index("ix_kis_mock_signal_strategy", _TABLE, ["strategy"], schema=_SCHEMA)
    op.create_index("ix_kis_mock_signal_symbol", _TABLE, ["symbol"], schema=_SCHEMA)
    op.create_index(
        "ix_kis_mock_signal_outcome_state", _TABLE, ["outcome_state"], schema=_SCHEMA
    )


def downgrade() -> None:
    op.drop_index("ix_kis_mock_signal_outcome_state", _TABLE, schema=_SCHEMA)
    op.drop_index("ix_kis_mock_signal_symbol", _TABLE, schema=_SCHEMA)
    op.drop_index("ix_kis_mock_signal_strategy", _TABLE, schema=_SCHEMA)
    op.drop_index("ix_kis_mock_signal_kst_date", _TABLE, schema=_SCHEMA)
    op.drop_table(_TABLE, schema=_SCHEMA)
