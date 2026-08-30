"""Seal the immutable ROB-1331 Q6 collection activation epoch.

Revision ID: 20260830_rob1331_q6_epoch
Revises: 20260824_s257_rung_reason
Create Date: 2026-08-30

The migration creates one additive review table and inserts exactly one
pre-registered marker.  The timestamp and 28-day window are literals selected
before the first durable B\\A event; database application time cannot move
them.  ``first_valid_record_at`` is deliberately not a marker column.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260830_rob1331_q6_epoch"
down_revision: str | Sequence[str] | None = "20260824_s257_rung_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY_PROJECTION_SHA256 = (
    "c47ce8e132b7c88fa9e2554cdddc0f84663b467e115d45b79a07c618de9d857d"
)
_PREREGISTRATION_SPEC_SHA256 = (
    "c07fb69001f5e48759718a4d725a327d5b6b1fb5d4aea442f3aeb7b170ffcd5b"
)
_POLICY_PROJECTION: dict[str, object] = {
    "schema": "rob-1301-buy-gate-policy-projection.v1",
    "experiment_id": "rob-1301-buy-gate-ab-shadow",
    "source": "app.services.buy_gate_ab_shadow.evaluate.evaluate_candidate",
    "markets": ["kr", "us"],
    "variant_a": {
        "label": "A",
        "role": "live",
        "support_strength_min": "strong",
        "executes": True,
    },
    "variant_b": {
        "label": "B",
        "role": "shadow",
        "support_strength_min": "moderate",
        "executes": False,
        "register_as": "shadow_buy",
    },
    "support_strength_order": ["weak", "moderate", "strong"],
    "shared_gates": {
        "rsi": {
            "operator": "lt",
            "threshold": "45",
            "missing": "reject",
        },
        "support_distance_pct": {
            "operator": "closed_interval",
            "minimum": "0",
            "maximum": "8",
            "missing": "reject",
        },
        "honest_upside_pct": {
            "operator": "gte",
            "threshold": "40",
            "missing": "reject",
        },
        "other_gate_bits": {
            "keys": [
                "liquid_midcap",
                "concentration",
                "overhang",
            ],
            "required_value": True,
            "missing_value": False,
            "non_boolean": "reject",
        },
    },
    "only_difference": "support_strength_min",
}


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS review")
    op.create_table(
        "buy_gate_ab_collection_epoch",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("experiment_id", sa.Text(), nullable=False),
        sa.Column("epoch_id", sa.Text(), nullable=False),
        sa.Column("addendum_version", sa.Text(), nullable=False),
        sa.Column(
            "collection_armed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column("collection_start", sa.Date(), nullable=False),
        sa.Column("collection_end_exclusive", sa.Date(), nullable=False),
        sa.Column("collection_calendar_days", sa.SmallInteger(), nullable=False),
        sa.Column("collection_clock_timezone", sa.Text(), nullable=False),
        sa.Column("policy_projection_sha256", sa.Text(), nullable=False),
        sa.Column("preregistration_spec_sha256", sa.Text(), nullable=False),
        sa.Column(
            "policy_projection",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="singleton_id"),
        sa.CheckConstraint(
            "experiment_id = 'rob-1301-buy-gate-ab-shadow'",
            name="experiment_id",
        ),
        sa.CheckConstraint(
            "epoch_id = 'rob-1301-q6-collection-epoch.v1'",
            name="epoch_id",
        ),
        sa.CheckConstraint(
            "addendum_version = 'rob-1331-q6-activation-epoch.v1'",
            name="addendum_version",
        ),
        sa.CheckConstraint(
            "collection_calendar_days = 28",
            name="calendar_days",
        ),
        sa.CheckConstraint(
            "collection_end_exclusive = "
            "collection_start + collection_calendar_days::integer",
            name="fixed_window",
        ),
        sa.CheckConstraint(
            "collection_clock_timezone = 'Asia/Seoul'",
            name="clock_timezone",
        ),
        sa.CheckConstraint(
            "policy_projection_sha256 ~ '^[0-9a-f]{64}$'",
            name="policy_projection_sha256",
        ),
        sa.CheckConstraint(
            "preregistration_spec_sha256 ~ '^[0-9a-f]{64}$'",
            name="preregistration_spec_sha256",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(policy_projection) = 'object'",
            name="policy_projection_object",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", name="uq_buy_gate_ab_epoch_experiment"),
        sa.UniqueConstraint("epoch_id", name="uq_buy_gate_ab_epoch_id"),
        schema="review",
    )

    marker = sa.table(
        "buy_gate_ab_collection_epoch",
        sa.column("id", sa.SmallInteger()),
        sa.column("experiment_id", sa.Text()),
        sa.column("epoch_id", sa.Text()),
        sa.column("addendum_version", sa.Text()),
        sa.column("collection_armed_at", sa.TIMESTAMP(timezone=True)),
        sa.column("collection_start", sa.Date()),
        sa.column("collection_end_exclusive", sa.Date()),
        sa.column("collection_calendar_days", sa.SmallInteger()),
        sa.column("collection_clock_timezone", sa.Text()),
        sa.column("policy_projection_sha256", sa.Text()),
        sa.column("preregistration_spec_sha256", sa.Text()),
        sa.column("policy_projection", postgresql.JSONB()),
        schema="review",
    )
    op.bulk_insert(
        marker,
        [
            {
                "id": 1,
                "experiment_id": "rob-1301-buy-gate-ab-shadow",
                "epoch_id": "rob-1301-q6-collection-epoch.v1",
                "addendum_version": "rob-1331-q6-activation-epoch.v1",
                "collection_armed_at": datetime.fromisoformat(
                    "2026-08-30T09:17:36+09:00"
                ),
                "collection_start": date.fromisoformat("2026-08-31"),
                "collection_end_exclusive": date.fromisoformat("2026-09-28"),
                "collection_calendar_days": 28,
                "collection_clock_timezone": "Asia/Seoul",
                "policy_projection_sha256": _POLICY_PROJECTION_SHA256,
                "preregistration_spec_sha256": _PREREGISTRATION_SPEC_SHA256,
                "policy_projection": _POLICY_PROJECTION,
            }
        ],
    )

    op.execute(
        """
        CREATE FUNCTION review.reject_buy_gate_ab_epoch_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'review.buy_gate_ab_collection_epoch is immutable; % rejected',
                TG_OP USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_buy_gate_ab_collection_epoch_immutable
        BEFORE UPDATE OR DELETE ON review.buy_gate_ab_collection_epoch
        FOR EACH ROW EXECUTE FUNCTION review.reject_buy_gate_ab_epoch_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_buy_gate_ab_collection_epoch_truncate_immutable
        BEFORE TRUNCATE ON review.buy_gate_ab_collection_epoch
        FOR EACH STATEMENT EXECUTE FUNCTION review.reject_buy_gate_ab_epoch_mutation()
        """
    )
    op.execute(
        "REVOKE UPDATE, DELETE, TRUNCATE "
        "ON review.buy_gate_ab_collection_epoch FROM PUBLIC"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_buy_gate_ab_collection_epoch_truncate_immutable "
        "ON review.buy_gate_ab_collection_epoch"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_buy_gate_ab_collection_epoch_immutable "
        "ON review.buy_gate_ab_collection_epoch"
    )
    op.execute("DROP FUNCTION IF EXISTS review.reject_buy_gate_ab_epoch_mutation()")
    op.drop_table("buy_gate_ab_collection_epoch", schema="review")
