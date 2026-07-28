"""ROB-1115 append-only strategy learning events.

Revision ID: 20260728_rob1115_learning
Revises: 20260728_rob1103_watch_links
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260728_rob1115_learning"
down_revision: str | Sequence[str] | None = "20260728_rob1103_watch_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STAGES = "'discovery','offline','sealed_oos','shadow','paper','live','ops'"
_VERDICTS = "'promote','iterate','retire','inconclusive','retry_same_identity'"
_FAILURE_CLASSES = (
    "'data_quality','insufficient_evidence','no_signal','gross_edge',"
    "'cost_turnover','robustness','risk','execution_gap','operational'"
)
_SHA256 = "^[0-9a-f]{64}$"

_IMMUTABILITY_DDL = (
    """
    CREATE OR REPLACE FUNCTION research.reject_strategy_learning_event_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION
            'research.strategy_learning_events is append-only/immutable; % rejected',
            TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER trg_strategy_learning_events_immutable
    BEFORE UPDATE OR DELETE ON research.strategy_learning_events
    FOR EACH ROW EXECUTE FUNCTION
        research.reject_strategy_learning_event_mutation()
    """,
    """
    CREATE TRIGGER trg_strategy_learning_events_truncate_immutable
    BEFORE TRUNCATE ON research.strategy_learning_events
    FOR EACH STATEMENT EXECUTE FUNCTION
        research.reject_strategy_learning_event_mutation()
    """,
)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS research")
    op.create_table(
        "strategy_learning_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("memory_event_id", sa.String(64), nullable=False),
        # Nullable is intentional. Production had zero registered experiments
        # when ROB-1115 was specified. Non-null values remain FK-enforced.
        sa.Column("experiment_id", sa.String(64), nullable=True),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("failure_class", sa.String(32), nullable=False),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "failure_fingerprint",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "learning_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("actor_role", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["research.strategy_experiments.experiment_id"],
            ondelete="RESTRICT",
            name="fk_strategy_learning_event_experiment",
        ),
        sa.UniqueConstraint(
            "memory_event_id",
            name="uq_strategy_learning_event_memory_event_id",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_strategy_learning_event_idempotency_key",
        ),
        sa.CheckConstraint(
            f"memory_event_id ~ '{_SHA256}' AND request_hash ~ '{_SHA256}'",
            name=op.f("ck_strategy_learning_event_hashes"),
        ),
        sa.CheckConstraint(
            f"experiment_id IS NULL OR experiment_id ~ '{_SHA256}'",
            name=op.f("ck_strategy_learning_event_experiment_id"),
        ),
        sa.CheckConstraint(
            f"stage IN ({_STAGES})",
            name=op.f("ck_strategy_learning_event_stage"),
        ),
        sa.CheckConstraint(
            f"verdict IN ({_VERDICTS})",
            name=op.f("ck_strategy_learning_event_verdict"),
        ),
        sa.CheckConstraint(
            f"failure_class IN ({_FAILURE_CLASSES})",
            name=op.f("ck_strategy_learning_event_failure_class"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array' "
            "AND jsonb_array_length(reason_codes) > 0",
            name=op.f("ck_strategy_learning_event_reason_codes"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array'",
            name=op.f("ck_strategy_learning_event_evidence_refs"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(failure_fingerprint) = 'array' "
            "AND jsonb_array_length(failure_fingerprint) = 2",
            name=op.f("ck_strategy_learning_event_failure_fingerprint"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(learning_payload) = 'array' "
            "AND jsonb_array_length(learning_payload) = 2",
            name=op.f("ck_strategy_learning_event_learning_payload"),
        ),
        sa.CheckConstraint(
            "btrim(idempotency_key) <> '' AND btrim(actor_id) <> '' "
            "AND btrim(actor_role) <> ''",
            name=op.f("ck_strategy_learning_event_nonblank_audit"),
        ),
        schema="research",
    )
    op.create_index(
        "ix_strategy_learning_event_experiment_created",
        "strategy_learning_events",
        ["experiment_id", "created_at", "id"],
        schema="research",
    )
    op.create_index(
        "ix_strategy_learning_event_failure_created",
        "strategy_learning_events",
        ["failure_class", sa.text("created_at DESC"), sa.text("id DESC")],
        schema="research",
    )
    for statement in _IMMUTABILITY_DDL:
        op.execute(statement)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_strategy_learning_events_truncate_immutable "
        "ON research.strategy_learning_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_strategy_learning_events_immutable "
        "ON research.strategy_learning_events"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS research.reject_strategy_learning_event_mutation()"
    )
    op.drop_index(
        "ix_strategy_learning_event_failure_created",
        table_name="strategy_learning_events",
        schema="research",
    )
    op.drop_index(
        "ix_strategy_learning_event_experiment_created",
        table_name="strategy_learning_events",
        schema="research",
    )
    op.drop_table("strategy_learning_events", schema="research")
