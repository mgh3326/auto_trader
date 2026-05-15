"""add crypto insight snapshots

Revision ID: 20260513_rob227
Revises: 20260513_rob222
Create Date: 2026-05-13 22:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260513_rob227"
down_revision: str | None = "20260513_rob222"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crypto_insight_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("metric", sa.String(length=48), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("value", sa.Numeric(24, 10), nullable=True),
        sa.Column("unit", sa.String(length=16), nullable=True),
        sa.Column("label", sa.String(length=80), nullable=True),
        sa.Column("snapshot_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("freshness_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_crypto_insight_snapshots_global_identity",
        "crypto_insight_snapshots",
        ["metric", "provider", "snapshot_at"],
        unique=True,
        postgresql_where=sa.text("symbol IS NULL"),
    )
    op.create_index(
        "uq_crypto_insight_snapshots_symbol_identity",
        "crypto_insight_snapshots",
        ["metric", "provider", "symbol", "snapshot_at"],
        unique=True,
        postgresql_where=sa.text("symbol IS NOT NULL"),
    )
    op.create_index(
        "ix_crypto_insight_snapshots_metric_at",
        "crypto_insight_snapshots",
        ["metric", "snapshot_at"],
    )
    op.create_index(
        "ix_crypto_insight_snapshots_provider_at",
        "crypto_insight_snapshots",
        ["provider", "snapshot_at"],
    )
    op.create_index(
        "ix_crypto_insight_snapshots_symbol_metric_at",
        "crypto_insight_snapshots",
        ["symbol", "metric", "snapshot_at"],
        postgresql_where=sa.text("symbol IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_crypto_insight_snapshots_symbol_metric_at",
        table_name="crypto_insight_snapshots",
    )
    op.drop_index(
        "ix_crypto_insight_snapshots_provider_at",
        table_name="crypto_insight_snapshots",
    )
    op.drop_index(
        "ix_crypto_insight_snapshots_metric_at",
        table_name="crypto_insight_snapshots",
    )
    op.drop_index(
        "uq_crypto_insight_snapshots_symbol_identity",
        table_name="crypto_insight_snapshots",
    )
    op.drop_index(
        "uq_crypto_insight_snapshots_global_identity",
        table_name="crypto_insight_snapshots",
    )
    op.drop_table("crypto_insight_snapshots")
