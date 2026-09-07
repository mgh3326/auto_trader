"""Record ROB-1301 cessation and register (but do not arm) ROB-1351 v2.

Revision ID: 20260907_rob1351_lifecycle
Revises: 20260904_web_approval_marker
Create Date: 2026-09-07

This is additive lifecycle evidence only.  The v1 epoch and every forecast
table are untouched.  The v2 epoch table is intentionally created empty:
registration is not activation.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260907_rob1351_lifecycle"
down_revision: str | Sequence[str] | None = "20260904_web_approval_marker"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TERMINATED_AT = "2026-09-07T09:43:42+09:00"
_ROB_1301_SPEC_SHA256 = (
    "c07fb69001f5e48759718a4d725a327d5b6b1fb5d4aea442f3aeb7b170ffcd5b"
)
_ROB_1301_POLICY_PROJECTION_SHA256 = (
    "c47ce8e132b7c88fa9e2554cdddc0f84663b467e115d45b79a07c618de9d857d"
)
_V2_SPEC_SHA256 = "c156fdb3c3fcd5e122bf71d37e64bc3b8feac087dee7f93f8ac1a012b2cca14f"
_V2_POLICY_PROJECTION_SHA256 = (
    "33488817d1191b7ad54800da1b397682e1097627ce8fb454a36faf5394018507"
)
_V2_POLICY_PROJECTION: dict[str, object] = {
    "schema": "rob-1351-buy-gate-policy-projection.v1",
    "experiment_id": "rob-1351-buy-gate-moderate-live",
    "source": "app.services.buy_gate_ab_shadow.evaluate.evaluate_candidate",
    "markets": ["kr", "us"],
    "variant_a": {
        "label": "A",
        "role": "live",
        "support_strength_min": "moderate",
        "executes": True,
    },
    "variant_b": {
        "label": "B",
        "role": "shadow",
        "support_strength_min": "weak",
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

_APPEND_ONLY_TABLES = (
    "buy_gate_ab_experiment_termination",
    "buy_gate_ab_experiment_registration",
    "buy_gate_ab_collection_epoch_v2",
)


def _create_append_only_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION review.reject_buy_gate_ab_lifecycle_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'review.% is append-only; % rejected', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in _APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON review.{table} "
            "FOR EACH ROW EXECUTE FUNCTION "
            "review.reject_buy_gate_ab_lifecycle_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_truncate_append_only "
            f"BEFORE TRUNCATE ON review.{table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION "
            "review.reject_buy_gate_ab_lifecycle_mutation()"
        )
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON review.{table} FROM PUBLIC")


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS review")
    op.create_table(
        "buy_gate_ab_experiment_termination",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("experiment_id", sa.Text(), nullable=False),
        sa.Column("epoch_id", sa.Text(), nullable=False),
        sa.Column("terminated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=False),
        sa.Column("carryover", sa.Text(), nullable=False),
        sa.Column("terminal_status", sa.Text(), nullable=False),
        sa.Column("terminal_outcome", sa.Text(), nullable=False),
        sa.Column("preregistration_spec_sha256", sa.Text(), nullable=False),
        sa.Column("policy_projection_sha256", sa.Text(), nullable=False),
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
            "reason = 'STOPPED_BY_OPERATOR_DECISION'",
            name="reason",
        ),
        sa.CheckConstraint("decided_by = 'operator'", name="decided_by"),
        sa.CheckConstraint("carryover = 'forbidden'", name="carryover"),
        sa.CheckConstraint(
            "terminal_status = 'INSUFFICIENT_SAMPLE'",
            name="terminal_status",
        ),
        sa.CheckConstraint("terminal_outcome = 'NO_FIRING'", name="terminal_outcome"),
        sa.CheckConstraint(
            "preregistration_spec_sha256 ~ '^[0-9a-f]{64}$'",
            name="preregistration_spec_sha256",
        ),
        sa.CheckConstraint(
            "policy_projection_sha256 ~ '^[0-9a-f]{64}$'",
            name="policy_projection_sha256",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "experiment_id", name="uq_buy_gate_ab_termination_experiment"
        ),
        schema="review",
    )

    termination = sa.table(
        "buy_gate_ab_experiment_termination",
        sa.column("id", sa.SmallInteger()),
        sa.column("experiment_id", sa.Text()),
        sa.column("epoch_id", sa.Text()),
        sa.column("terminated_at", sa.TIMESTAMP(timezone=True)),
        sa.column("reason", sa.Text()),
        sa.column("decided_by", sa.Text()),
        sa.column("carryover", sa.Text()),
        sa.column("terminal_status", sa.Text()),
        sa.column("terminal_outcome", sa.Text()),
        sa.column("preregistration_spec_sha256", sa.Text()),
        sa.column("policy_projection_sha256", sa.Text()),
        schema="review",
    )
    op.bulk_insert(
        termination,
        [
            {
                "id": 1,
                "experiment_id": "rob-1301-buy-gate-ab-shadow",
                "epoch_id": "rob-1301-q6-collection-epoch.v1",
                "terminated_at": datetime.fromisoformat(_TERMINATED_AT),
                "reason": "STOPPED_BY_OPERATOR_DECISION",
                "decided_by": "operator",
                "carryover": "forbidden",
                "terminal_status": "INSUFFICIENT_SAMPLE",
                "terminal_outcome": "NO_FIRING",
                "preregistration_spec_sha256": _ROB_1301_SPEC_SHA256,
                "policy_projection_sha256": _ROB_1301_POLICY_PROJECTION_SHA256,
            }
        ],
    )

    op.create_table(
        "buy_gate_ab_experiment_registration",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("experiment_id", sa.Text(), nullable=False),
        sa.Column("preregistration_version", sa.Text(), nullable=False),
        sa.Column("spec_sha256", sa.Text(), nullable=False),
        sa.Column("policy_projection_sha256", sa.Text(), nullable=False),
        sa.Column(
            "policy_projection",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("registered_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("predecessor_experiment_id", sa.Text(), nullable=False),
        sa.Column("carryover", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="singleton_id"),
        sa.CheckConstraint(
            "experiment_id = 'rob-1351-buy-gate-moderate-live'",
            name="experiment_id",
        ),
        sa.CheckConstraint(
            "preregistration_version = 'rob-1351-buy-gate-moderate-live.v1'",
            name="preregistration_version",
        ),
        sa.CheckConstraint("spec_sha256 ~ '^[0-9a-f]{64}$'", name="spec_sha256"),
        sa.CheckConstraint(
            "policy_projection_sha256 ~ '^[0-9a-f]{64}$'",
            name="policy_projection_sha256",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(policy_projection) = 'object'",
            name="policy_projection_object",
        ),
        sa.CheckConstraint(
            "predecessor_experiment_id = 'rob-1301-buy-gate-ab-shadow'",
            name="predecessor_experiment_id",
        ),
        sa.CheckConstraint("carryover = 'forbidden'", name="carryover"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "experiment_id", name="uq_buy_gate_ab_registration_experiment"
        ),
        schema="review",
    )

    registration = sa.table(
        "buy_gate_ab_experiment_registration",
        sa.column("id", sa.SmallInteger()),
        sa.column("experiment_id", sa.Text()),
        sa.column("preregistration_version", sa.Text()),
        sa.column("spec_sha256", sa.Text()),
        sa.column("policy_projection_sha256", sa.Text()),
        sa.column("policy_projection", postgresql.JSONB()),
        sa.column("registered_at", sa.TIMESTAMP(timezone=True)),
        sa.column("predecessor_experiment_id", sa.Text()),
        sa.column("carryover", sa.Text()),
        schema="review",
    )
    op.bulk_insert(
        registration,
        [
            {
                "id": 1,
                "experiment_id": "rob-1351-buy-gate-moderate-live",
                "preregistration_version": "rob-1351-buy-gate-moderate-live.v1",
                "spec_sha256": _V2_SPEC_SHA256,
                "policy_projection_sha256": _V2_POLICY_PROJECTION_SHA256,
                "policy_projection": _V2_POLICY_PROJECTION,
                "registered_at": datetime.fromisoformat(_TERMINATED_AT),
                "predecessor_experiment_id": "rob-1301-buy-gate-ab-shadow",
                "carryover": "forbidden",
            }
        ],
    )

    op.create_table(
        "buy_gate_ab_collection_epoch_v2",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("experiment_id", sa.Text(), nullable=False),
        sa.Column("epoch_id", sa.Text(), nullable=False),
        sa.Column("addendum_version", sa.Text(), nullable=False),
        sa.Column("collection_armed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("collection_start", sa.Date(), nullable=False),
        sa.Column("collection_end_exclusive", sa.Date(), nullable=False),
        sa.Column("collection_calendar_days", sa.SmallInteger(), nullable=False),
        sa.Column("collection_clock_timezone", sa.Text(), nullable=False),
        sa.Column("policy_projection_sha256", sa.Text(), nullable=False),
        sa.Column("preregistration_spec_sha256", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="singleton_id"),
        sa.CheckConstraint(
            "experiment_id = 'rob-1351-buy-gate-moderate-live'",
            name="experiment_id",
        ),
        sa.CheckConstraint("collection_calendar_days = 28", name="calendar_days"),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", name="uq_buy_gate_ab_epoch_v2_experiment"),
        sa.UniqueConstraint("epoch_id", name="uq_buy_gate_ab_epoch_v2_id"),
        schema="review",
    )

    _create_append_only_guards()


def downgrade() -> None:
    for table in reversed(_APPEND_ONLY_TABLES):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table}_truncate_append_only ON review.{table}"
        )
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON review.{table}")
        op.drop_table(table, schema="review")
    op.execute("DROP FUNCTION IF EXISTS review.reject_buy_gate_ab_lifecycle_mutation()")
