"""ROB-637 analysis artifact persistence store

Revision ID: 20260702_rob637
Revises: 20260620_scalp_benchmark
Create Date: 2026-07-02 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260702_rob637"
down_revision: str | None = "20260620_scalp_benchmark"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_artifacts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "artifact_uuid",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "symbols",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "as_of",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "valid_until",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column("session_label", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'claude'"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "artifact_uuid",
            name="uq_analysis_artifacts_artifact_uuid",
        ),
        sa.CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="ck_analysis_artifacts_market",
        ),
        sa.CheckConstraint(
            "kind IN ("
            "'screening_ranking','profit_taking_verdicts',"
            "'support_resistance_map','flow_assessment',"
            "'candidate_pool','session_summary'"
            ")",
            name="ck_analysis_artifacts_kind",
        ),
        sa.CheckConstraint(
            "created_by IN ('claude','operator','system')",
            name="ck_analysis_artifacts_created_by",
        ),
        schema="review",
    )
    op.create_index(
        "ix_analysis_artifacts_kind_market_as_of",
        "analysis_artifacts",
        ["kind", "market", sa.text("as_of DESC")],
        schema="review",
    )
    op.create_index(
        "ix_analysis_artifacts_symbols_gin",
        "analysis_artifacts",
        ["symbols"],
        unique=False,
        schema="review",
        postgresql_using="gin",
    )
    op.create_index(
        "ix_analysis_artifacts_payload_gin",
        "analysis_artifacts",
        ["payload"],
        unique=False,
        schema="review",
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analysis_artifacts_payload_gin",
        table_name="analysis_artifacts",
        schema="review",
    )
    op.drop_index(
        "ix_analysis_artifacts_symbols_gin",
        table_name="analysis_artifacts",
        schema="review",
    )
    op.drop_index(
        "ix_analysis_artifacts_kind_market_as_of",
        table_name="analysis_artifacts",
        schema="review",
    )
    op.drop_table("analysis_artifacts", schema="review")
