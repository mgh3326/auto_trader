"""Extend review.screener_pick_log with the full A-record columns (task #884).

Revision ID: 20260928_884_fanout_a_record
Revises: 20260926_task711_dispatch
Create Date: 2026-09-28

Additive DDL only: nullable columns and CHECK constraints on the existing
append-only table.  No existing column, constraint, index, or row is touched
and no rows are backfilled — rows written before this change keep NULL
``collection_version``.  The A-record writer stays behind
``SCREENER_PICK_LOG_ENABLED`` (default off) and this migration is applied
separately via ``alembic upgrade head``; nothing auto-applies it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260928_884_fanout_a_record"
down_revision: str | Sequence[str] | None = "20260926_task711_dispatch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_COLUMNS: tuple[str, ...] = (
    "collection_version",
    "admission",
    "admission_reason",
    "selection_seq",
    "source_status",
    "data_asof",
    "fetched_at",
    "raw_row",
    "gate_features",
    "call_context",
)

_NEW_CHECKS: tuple[str, ...] = (
    "admission_vocabulary",
    "admission_reason_nonempty",
    "selection_seq_positive",
    "source_status_vocabulary",
    "collection_version_nonempty",
    "raw_row_object",
    "gate_features_object",
    "call_context_object",
)


def upgrade() -> None:
    op.add_column(
        "screener_pick_log",
        sa.Column("collection_version", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "screener_pick_log",
        sa.Column("admission", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "screener_pick_log",
        sa.Column("admission_reason", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "screener_pick_log",
        sa.Column("selection_seq", sa.Integer(), nullable=True),
        schema="review",
    )
    op.add_column(
        "screener_pick_log",
        sa.Column("source_status", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "screener_pick_log",
        sa.Column("data_asof", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "screener_pick_log",
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema="review",
    )
    op.add_column(
        "screener_pick_log",
        sa.Column("raw_row", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema="review",
    )
    op.add_column(
        "screener_pick_log",
        sa.Column(
            "gate_features", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        schema="review",
    )
    op.add_column(
        "screener_pick_log",
        sa.Column(
            "call_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        schema="review",
    )
    op.create_check_constraint(
        "admission_vocabulary",
        "screener_pick_log",
        "admission IS NULL OR admission IN "
        "('admitted','not_admitted','dropped_preselection')",
        schema="review",
    )
    op.create_check_constraint(
        "admission_reason_nonempty",
        "screener_pick_log",
        "admission_reason IS NULL OR length(btrim(admission_reason)) > 0",
        schema="review",
    )
    op.create_check_constraint(
        "selection_seq_positive",
        "screener_pick_log",
        "selection_seq IS NULL OR selection_seq >= 1",
        schema="review",
    )
    op.create_check_constraint(
        "source_status_vocabulary",
        "screener_pick_log",
        "source_status IS NULL OR source_status IN "
        "('ok','empty','stale_dropped','error')",
        schema="review",
    )
    op.create_check_constraint(
        "collection_version_nonempty",
        "screener_pick_log",
        "collection_version IS NULL OR length(btrim(collection_version)) > 0",
        schema="review",
    )
    op.create_check_constraint(
        "raw_row_object",
        "screener_pick_log",
        "raw_row IS NULL OR jsonb_typeof(raw_row) = 'object'",
        schema="review",
    )
    op.create_check_constraint(
        "gate_features_object",
        "screener_pick_log",
        "gate_features IS NULL OR jsonb_typeof(gate_features) = 'object'",
        schema="review",
    )
    op.create_check_constraint(
        "call_context_object",
        "screener_pick_log",
        "call_context IS NULL OR jsonb_typeof(call_context) = 'object'",
        schema="review",
    )


def downgrade() -> None:
    for name in _NEW_CHECKS:
        op.drop_constraint(name, "screener_pick_log", schema="review", type_="check")
    for name in _NEW_COLUMNS:
        op.drop_column("screener_pick_log", name, schema="review")
