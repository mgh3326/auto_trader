"""hk 6154: status rows for intraday momentum ranking runs.

Revision ID: 20261124_hk6154_momentum_runs
Revises: 20261124_hk6145_session_minutes
Create Date: 2026-11-24

Additive. ``review``-free: a new ``public.invest_momentum_event_runs`` table with
one row per (run, surface) saying ok / empty / error (+ a closed reason), so a
failed fetch and an empty ranking are distinguishable after the fact.
``invest_momentum_event_snapshots`` is untouched.

Conditional ``at_app`` grant (hard rule 18): SELECT, INSERT only (no UPDATE or
DELETE: status rows are insert-only), only if the role exists.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261124_hk6154_momentum_runs"
down_revision: str | Sequence[str] | None = "20261124_hk6145_session_minutes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ERROR_REASONS = (
    "http_4xx",
    "http_5xx",
    "timeout",
    "transport",
    "bad_response",
    "unparseable",
    "unexpected",
)


def upgrade() -> None:
    reasons = ", ".join(f"'{r}'" for r in ERROR_REASONS)
    op.create_table(
        "invest_momentum_event_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("surface", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "run_id", "surface", name="uq_invest_momentum_event_runs_run_surface"
        ),
        sa.CheckConstraint(
            "(status = 'ok' AND reason IS NULL AND row_count > 0)"
            " OR (status = 'empty' AND reason = 'no_rows' AND row_count = 0)"
            f" OR (status = 'error' AND row_count = 0 AND reason IN ({reasons}))",
            name="ck_invest_momentum_event_runs_status_reason",
        ),
    )
    op.create_index(
        "ix_invest_momentum_event_runs_date_snapshot",
        "invest_momentum_event_runs",
        ["trading_date", "snapshot_at"],
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
                GRANT SELECT, INSERT ON public.invest_momentum_event_runs TO at_app;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.invest_momentum_event_runs) THEN
                RAISE EXCEPTION
                    'invest_momentum_event_runs has rows; downgrade would discard status history';
            END IF;
        END
        $$
        """
    )
    op.drop_index(
        "ix_invest_momentum_event_runs_date_snapshot",
        table_name="invest_momentum_event_runs",
    )
    op.drop_table("invest_momentum_event_runs")
