"""ROB-1195 immutable fill observation and projection foundation.

Revision ID: 20260801_rob1195_fillobs
Revises: 20260728_rob1109_watch_intent
Create Date: 2026-08-01

This migration is additive and intentionally performs no backfill or consumer
cutover. Existing broker-native ledgers and ``review.trades`` remain intact.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260801_rob1195_fillobs"
down_revision: str | Sequence[str] | None = "20260728_rob1109_watch_intent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_SHA256 = "^[0-9a-f]{64}$"

_IMMUTABILITY_DDL = (
    """
    CREATE OR REPLACE FUNCTION review.reject_fill_observation_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION
            '%.% is append-only/immutable; % rejected',
            TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER trg_fill_observations_immutable
    BEFORE UPDATE OR DELETE ON review.fill_observations
    FOR EACH ROW EXECUTE FUNCTION review.reject_fill_observation_mutation()
    """,
    """
    CREATE TRIGGER trg_fill_observations_truncate_immutable
    BEFORE TRUNCATE ON review.fill_observations
    FOR EACH STATEMENT EXECUTE FUNCTION
        review.reject_fill_observation_mutation()
    """,
    """
    CREATE TRIGGER trg_fill_settlement_enrichments_immutable
    BEFORE UPDATE OR DELETE ON review.fill_settlement_enrichments
    FOR EACH ROW EXECUTE FUNCTION review.reject_fill_observation_mutation()
    """,
    """
    CREATE TRIGGER trg_fill_settlement_enrichments_truncate_immutable
    BEFORE TRUNCATE ON review.fill_settlement_enrichments
    FOR EACH STATEMENT EXECUTE FUNCTION
        review.reject_fill_observation_mutation()
    """,
)


def upgrade() -> None:
    op.create_table(
        "fill_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("observation_identity", sa.String(64), nullable=False),
        sa.Column("identity_kind", sa.String(32), nullable=False),
        sa.Column("broker", sa.String(32), nullable=False),
        sa.Column("account_ref", sa.String(128), nullable=False),
        sa.Column("account_mode", sa.String(32), nullable=False),
        sa.Column("venue", sa.String(64), nullable=False),
        sa.Column("order_id", sa.String(128), nullable=False),
        sa.Column(
            "instrument_type",
            postgresql.ENUM(name="instrument_type", create_type=False),
            nullable=False,
        ),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("currency", sa.String(16), nullable=False),
        sa.Column("broker_fill_sequence", sa.String(128), nullable=True),
        sa.Column("cumulative_quantity", sa.Numeric(38, 18), nullable=True),
        sa.Column("reported_fill_quantity", sa.Numeric(38, 18), nullable=True),
        sa.Column("fill_delta_quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("average_price", sa.Numeric(38, 18), nullable=True),
        sa.Column("last_fill_price", sa.Numeric(38, 18), nullable=True),
        sa.Column("cumulative_notional", sa.Numeric(38, 18), nullable=True),
        sa.Column("fee_total", sa.Numeric(38, 18), nullable=True),
        sa.Column("evidence_source", sa.String(64), nullable=False),
        sa.Column("evidence_ref", sa.Text(), nullable=False),
        sa.Column("fill_fact_hash", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("filled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "observation_identity",
            name="uq_fill_observation_identity",
        ),
        sa.CheckConstraint(
            f"observation_identity ~ '{_SHA256}' AND fill_fact_hash ~ '{_SHA256}'",
            name=op.f("ck_fill_observation_hashes"),
        ),
        sa.CheckConstraint(
            "identity_kind IN ('broker_fill_sequence','cumulative_quantity')",
            name=op.f("ck_fill_observation_identity_kind"),
        ),
        sa.CheckConstraint(
            "(identity_kind = 'broker_fill_sequence' "
            "AND broker_fill_sequence IS NOT NULL "
            "AND btrim(broker_fill_sequence) <> '') "
            "OR (identity_kind = 'cumulative_quantity' "
            "AND broker_fill_sequence IS NULL "
            "AND cumulative_quantity IS NOT NULL)",
            name=op.f("ck_fill_observation_identity_source"),
        ),
        sa.CheckConstraint(
            "fill_delta_quantity > 0 "
            "AND (cumulative_quantity IS NULL OR cumulative_quantity > 0) "
            "AND (reported_fill_quantity IS NULL "
            "OR reported_fill_quantity >= 0)",
            name=op.f("ck_fill_observation_positive_quantity"),
        ),
        sa.CheckConstraint(
            "(average_price IS NULL OR average_price > 0) "
            "AND (last_fill_price IS NULL OR last_fill_price > 0) "
            "AND (cumulative_notional IS NULL OR cumulative_notional >= 0) "
            "AND (fee_total IS NULL OR fee_total >= 0)",
            name=op.f("ck_fill_observation_nonnegative_economics"),
        ),
        sa.CheckConstraint(
            "side IN ('buy','sell')",
            name=op.f("ck_fill_observation_side"),
        ),
        sa.CheckConstraint(
            "btrim(broker) <> '' AND btrim(account_ref) <> '' "
            "AND btrim(account_mode) <> '' AND btrim(venue) <> '' "
            "AND btrim(order_id) <> '' AND btrim(symbol) <> '' "
            "AND btrim(currency) <> '' AND btrim(evidence_source) <> '' "
            "AND btrim(evidence_ref) <> ''",
            name=op.f("ck_fill_observation_nonblank_scope"),
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_fill_observation_order_stream",
        "fill_observations",
        ["broker", "account_ref", "order_id", "id"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_fill_observation_created_at",
        "fill_observations",
        [sa.text("created_at DESC"), sa.text("id DESC")],
        schema=_SCHEMA,
    )

    op.create_table(
        "fill_settlement_enrichments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("fill_observation_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("settlement_hash", sa.String(64), nullable=False),
        sa.Column("cumulative_quantity", sa.Numeric(38, 18), nullable=True),
        sa.Column("reported_fill_quantity", sa.Numeric(38, 18), nullable=True),
        sa.Column("average_price", sa.Numeric(38, 18), nullable=True),
        sa.Column("last_fill_price", sa.Numeric(38, 18), nullable=True),
        sa.Column("cumulative_notional", sa.Numeric(38, 18), nullable=True),
        sa.Column("fee_total", sa.Numeric(38, 18), nullable=True),
        sa.Column("filled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("evidence_source", sa.String(64), nullable=False),
        sa.Column("evidence_ref", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["fill_observation_id"],
            ["review.fill_observations.id"],
            ondelete="RESTRICT",
            name="fk_fill_settlement_enrichment_observation",
        ),
        sa.UniqueConstraint(
            "fill_observation_id",
            "revision",
            name="uq_fill_settlement_enrichment_revision",
        ),
        sa.CheckConstraint(
            f"settlement_hash ~ '{_SHA256}'",
            name=op.f("ck_fill_settlement_enrichment_hash"),
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name=op.f("ck_fill_settlement_enrichment_revision"),
        ),
        sa.CheckConstraint(
            "(cumulative_quantity IS NULL OR cumulative_quantity > 0) "
            "AND (reported_fill_quantity IS NULL "
            "OR reported_fill_quantity >= 0) "
            "AND (average_price IS NULL OR average_price > 0) "
            "AND (last_fill_price IS NULL OR last_fill_price > 0) "
            "AND (cumulative_notional IS NULL OR cumulative_notional >= 0) "
            "AND (fee_total IS NULL OR fee_total >= 0)",
            name=op.f("ck_fill_settlement_enrichment_economics"),
        ),
        sa.CheckConstraint(
            "btrim(evidence_source) <> '' AND btrim(evidence_ref) <> ''",
            name=op.f("ck_fill_settlement_enrichment_nonblank_evidence"),
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_fill_settlement_enrichment_latest",
        "fill_settlement_enrichments",
        ["fill_observation_id", sa.text("revision DESC")],
        schema=_SCHEMA,
    )

    op.create_table(
        "fill_projection_outbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("delivery_key", sa.String(64), nullable=False),
        sa.Column("projection_name", sa.String(128), nullable=False),
        sa.Column("partition_key", sa.String(64), nullable=False),
        sa.Column("fill_observation_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "state",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "available_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "lease_token",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["fill_observation_id"],
            ["review.fill_observations.id"],
            ondelete="RESTRICT",
            name="fk_fill_projection_outbox_observation",
        ),
        sa.UniqueConstraint(
            "delivery_key",
            name="uq_fill_projection_outbox_delivery_key",
        ),
        sa.UniqueConstraint(
            "projection_name",
            "fill_observation_id",
            name="uq_fill_projection_outbox_observation",
        ),
        sa.CheckConstraint(
            f"delivery_key ~ '{_SHA256}' AND partition_key ~ '{_SHA256}'",
            name=op.f("ck_fill_projection_outbox_hashes"),
        ),
        sa.CheckConstraint(
            "state IN ('pending','processing','retry','succeeded')",
            name=op.f("ck_fill_projection_outbox_state"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_fill_projection_outbox_attempt_count"),
        ),
        sa.CheckConstraint(
            "(state = 'processing' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) "
            "OR (state <> 'processing' AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
            name=op.f("ck_fill_projection_outbox_lease"),
        ),
        sa.CheckConstraint(
            "(state = 'succeeded' AND completed_at IS NOT NULL) "
            "OR (state <> 'succeeded' AND completed_at IS NULL)",
            name=op.f("ck_fill_projection_outbox_completion"),
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_fill_projection_outbox_ready",
        "fill_projection_outbox",
        ["projection_name", "state", "available_at", "fill_observation_id"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_fill_projection_outbox_partition",
        "fill_projection_outbox",
        ["projection_name", "partition_key", "fill_observation_id"],
        schema=_SCHEMA,
    )

    op.create_table(
        "fill_projection_cursors",
        sa.Column("projection_name", sa.String(128), primary_key=True),
        sa.Column("partition_key", sa.String(64), primary_key=True),
        sa.Column("last_fill_observation_id", sa.BigInteger(), nullable=False),
        sa.Column("last_observation_identity", sa.String(64), nullable=False),
        sa.Column(
            "advanced_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
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
        sa.ForeignKeyConstraint(
            ["last_fill_observation_id"],
            ["review.fill_observations.id"],
            ondelete="RESTRICT",
            name="fk_fill_projection_cursor_observation",
        ),
        sa.CheckConstraint(
            f"partition_key ~ '{_SHA256}' AND last_observation_identity ~ '{_SHA256}'",
            name=op.f("ck_fill_projection_cursor_hashes"),
        ),
        sa.CheckConstraint(
            "btrim(projection_name) <> ''",
            name=op.f("ck_fill_projection_cursor_projection_name"),
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_fill_projection_cursor_observation",
        "fill_projection_cursors",
        ["last_fill_observation_id"],
        schema=_SCHEMA,
    )

    for statement in _IMMUTABILITY_DDL:
        op.execute(statement)


def downgrade() -> None:
    # Operational rollback is writer-off, never observation deletion. Refuse a
    # schema downgrade after authority rows exist so audit evidence survives.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM review.fill_observations) THEN
                RAISE EXCEPTION
                    'cannot downgrade: review.fill_observations contains immutable evidence';
            END IF;
        END
        $$;
        """
    )
    op.drop_index(
        "ix_fill_projection_cursor_observation",
        table_name="fill_projection_cursors",
        schema=_SCHEMA,
    )
    op.drop_table("fill_projection_cursors", schema=_SCHEMA)
    op.drop_index(
        "ix_fill_projection_outbox_partition",
        table_name="fill_projection_outbox",
        schema=_SCHEMA,
    )
    op.drop_index(
        "ix_fill_projection_outbox_ready",
        table_name="fill_projection_outbox",
        schema=_SCHEMA,
    )
    op.drop_table("fill_projection_outbox", schema=_SCHEMA)
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "trg_fill_settlement_enrichments_truncate_immutable "
        "ON review.fill_settlement_enrichments"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_fill_settlement_enrichments_immutable "
        "ON review.fill_settlement_enrichments"
    )
    op.drop_index(
        "ix_fill_settlement_enrichment_latest",
        table_name="fill_settlement_enrichments",
        schema=_SCHEMA,
    )
    op.drop_table("fill_settlement_enrichments", schema=_SCHEMA)
    op.execute(
        "DROP TRIGGER IF EXISTS trg_fill_observations_truncate_immutable "
        "ON review.fill_observations"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_fill_observations_immutable "
        "ON review.fill_observations"
    )
    op.execute("DROP FUNCTION IF EXISTS review.reject_fill_observation_mutation()")
    op.drop_index(
        "ix_fill_observation_created_at",
        table_name="fill_observations",
        schema=_SCHEMA,
    )
    op.drop_index(
        "ix_fill_observation_order_stream",
        table_name="fill_observations",
        schema=_SCHEMA,
    )
    op.drop_table("fill_observations", schema=_SCHEMA)
