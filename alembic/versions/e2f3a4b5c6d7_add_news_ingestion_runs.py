"""add news ingestion runs

Revision ID: e2f3a4b5c6d7
Revises: e1f2a3b4c5d6
Create Date: 2026-04-30 11:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e2f3a4b5c6d7"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "news_ingestion_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_uuid", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=20), nullable=False),
        sa.Column("feed_set", sa.String(length=100), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "source_counts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "inserted_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "skipped_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('success', 'partial', 'failed', 'dry_run_ok')",
            name="ck_news_ingestion_runs_status",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_ingestion_runs")),
        sa.UniqueConstraint("run_uuid", name="uq_news_ingestion_runs_run_uuid"),
    )
    op.create_index(
        "ix_news_ingestion_runs_market",
        "news_ingestion_runs",
        ["market"],
        unique=False,
    )
    op.create_index(
        "ix_news_ingestion_runs_finished_at",
        "news_ingestion_runs",
        ["finished_at"],
        unique=False,
    )
    op.create_index(
        "ix_news_ingestion_runs_market_finished",
        "news_ingestion_runs",
        ["market", "finished_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_news_ingestion_runs_market_finished", table_name="news_ingestion_runs"
    )
    op.drop_index(
        "ix_news_ingestion_runs_finished_at", table_name="news_ingestion_runs"
    )
    op.drop_index("ix_news_ingestion_runs_market", table_name="news_ingestion_runs")
    op.drop_table("news_ingestion_runs")
