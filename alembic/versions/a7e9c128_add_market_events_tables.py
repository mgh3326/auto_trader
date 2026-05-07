"""add market_events tables (ROB-128)

Revision ID: a7e9c128
Revises: 2026_05_06_rob119
Create Date: 2026-05-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a7e9c128"
down_revision: str | Sequence[str] | None = "2026_05_06_rob119"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column(
            "event_uuid",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=True),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("company_name", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("release_time_utc", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("release_time_local", sa.TIMESTAMP(timezone=False), nullable=True),
        sa.Column("source_timezone", sa.Text(), nullable=True),
        sa.Column("time_hint", sa.Text(), nullable=True),
        sa.Column("importance", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="scheduled"),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_event_id", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("fiscal_quarter", sa.Integer(), nullable=True),
        sa.Column(
            "raw_payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
    )

    op.create_index(
        "uq_market_events_source_event_id",
        "market_events",
        ["source", "category", "market", "source_event_id"],
        unique=True,
        postgresql_where=sa.text("source_event_id IS NOT NULL"),
    )
    op.create_index(
        "uq_market_events_natural_key",
        "market_events",
        [
            "source",
            "category",
            "market",
            sa.text("coalesce(symbol, '')"),
            "event_date",
            sa.text("coalesce(fiscal_year, 0)"),
            sa.text("coalesce(fiscal_quarter, 0)"),
        ],
        unique=True,
        postgresql_where=sa.text("source_event_id IS NULL"),
    )
    op.create_index("ix_market_events_event_date", "market_events", ["event_date"])
    op.create_index("ix_market_events_symbol", "market_events", ["symbol"])
    op.create_index(
        "ix_market_events_category_market_date",
        "market_events",
        ["category", "market", "event_date"],
    )

    op.create_table(
        "market_event_values",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("market_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("metric_name", sa.Text(), nullable=False),
        sa.Column("period", sa.Text(), nullable=True),
        sa.Column("actual", sa.Numeric(28, 8), nullable=True),
        sa.Column("forecast", sa.Numeric(28, 8), nullable=True),
        sa.Column("previous", sa.Numeric(28, 8), nullable=True),
        sa.Column("revised_previous", sa.Numeric(28, 8), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("surprise", sa.Numeric(28, 8), nullable=True),
        sa.Column("surprise_pct", sa.Numeric(12, 4), nullable=True),
        sa.Column("released_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
    )
    op.create_index(
        "uq_market_event_values_event_metric_period",
        "market_event_values",
        ["event_id", "metric_name", sa.text("coalesce(period, '')")],
        unique=True,
    )
    op.create_index(
        "ix_market_event_values_event_id", "market_event_values", ["event_id"]
    )

    op.create_table(
        "market_event_ingestion_partitions",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("partition_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_request_hash", sa.Text(), nullable=True),
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
        sa.UniqueConstraint(
            "source",
            "category",
            "market",
            "partition_date",
            name="uq_market_event_ingestion_partitions_source",
        ),
    )
    op.create_index(
        "ix_market_event_ingestion_partitions_status_date",
        "market_event_ingestion_partitions",
        ["status", "partition_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_market_event_ingestion_partitions_status_date",
        table_name="market_event_ingestion_partitions",
    )
    op.drop_table("market_event_ingestion_partitions")

    op.drop_index("ix_market_event_values_event_id", table_name="market_event_values")
    op.drop_index(
        "uq_market_event_values_event_metric_period",
        table_name="market_event_values",
    )
    op.drop_table("market_event_values")

    op.drop_index("ix_market_events_category_market_date", table_name="market_events")
    op.drop_index("ix_market_events_symbol", table_name="market_events")
    op.drop_index("ix_market_events_event_date", table_name="market_events")
    op.drop_index("uq_market_events_natural_key", table_name="market_events")
    op.drop_index("uq_market_events_source_event_id", table_name="market_events")
    op.drop_table("market_events")
