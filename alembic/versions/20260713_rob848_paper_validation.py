"""ROB-848 append-only paper-validation state and role audit.

Revision ID: 20260713_rob848_paper_validation
Revises: 20260713_rob866_manual_alerts
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260713_rob848_paper_validation"
down_revision = "20260713_rob866_manual_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_HASH_COLUMNS = (
    "experiment_hash",
    "cohort_hash",
    "strategy_hash",
    "config_hash",
    "policy_hash",
    "input_hash",
)
_HASH_CHECK = " AND ".join(
    f"{name} ~ '^[0-9a-f]{{64}}$'" for name in _HASH_COLUMNS
)


def _identity_columns() -> list[sa.Column]:
    return [
        sa.Column("validation_id", sa.String(128), nullable=False),
        sa.Column("validation_version", sa.Integer(), nullable=False),
        sa.Column("experiment_id", sa.String(64), nullable=False),
        sa.Column("strategy_version_id", sa.String(128), nullable=False),
        sa.Column("cohort_id", sa.String(128), nullable=False),
    ]


def _hash_columns() -> list[sa.Column]:
    return [sa.Column(name, sa.String(64), nullable=False) for name in _HASH_COLUMNS]


_TRIGGER_DDL = (
    """
    CREATE OR REPLACE FUNCTION research.reject_paper_validation_audit_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION
            'research.% is append-only/immutable; % rejected',
            TG_TABLE_NAME, TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE FUNCTION research.validate_paper_validation_experiment_identity()
    RETURNS trigger AS $$
    DECLARE
        registered research.strategy_experiments%ROWTYPE;
    BEGIN
        SELECT * INTO registered
          FROM research.strategy_experiments
         WHERE experiment_id = NEW.experiment_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION
                'paper validation experiment % is not registered', NEW.experiment_id
                USING ERRCODE = 'foreign_key_violation';
        END IF;
        IF NEW.experiment_hash <> NEW.experiment_id
           OR NEW.strategy_version_id <> registered.strategy_version
           OR NEW.strategy_hash <> registered.strategy_hash
           OR NEW.config_hash <> registered.frozen_config_hash
           OR NEW.policy_hash <> registered.policy_hash THEN
            RAISE EXCEPTION
                'paper validation experiment identity mismatch for %',
                NEW.experiment_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
)


def _create_audit_triggers(table: str, suffix: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_paper_validation_{suffix}_experiment_identity "
        f"BEFORE INSERT ON research.{table} FOR EACH ROW EXECUTE FUNCTION "
        "research.validate_paper_validation_experiment_identity()"
    )
    op.execute(
        f"CREATE TRIGGER trg_paper_validation_{suffix}_immutable "
        f"BEFORE UPDATE OR DELETE ON research.{table} FOR EACH ROW EXECUTE "
        "FUNCTION research.reject_paper_validation_audit_mutation()"
    )


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS research")
    op.create_table(
        "paper_validation_state_transitions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        *_identity_columns(),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("prior_state", sa.String(32), nullable=True),
        sa.Column("new_state", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("actor_role", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("reason_text", sa.Text(), nullable=False),
        *_hash_columns(),
        sa.Column(
            "evidence_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
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
            name="fk_paper_validation_transition_experiment",
        ),
        sa.UniqueConstraint(
            "validation_id",
            "sequence",
            name="uq_paper_validation_transition_sequence",
        ),
        sa.UniqueConstraint(
            "validation_id",
            "idempotency_key",
            name="uq_paper_validation_transition_idempotency",
        ),
        sa.CheckConstraint(
            "actor_role IN ('researcher','reviewer','operator','system')",
            name="ck_paper_validation_transition_actor_role",
        ),
        sa.CheckConstraint(
            f"experiment_hash = experiment_id AND {_HASH_CHECK}",
            name="ck_paper_validation_transition_hashes",
        ),
        sa.CheckConstraint(
            "(sequence = 1 AND prior_state IS NULL AND new_state = 'draft') OR "
            "(sequence > 1 AND ("
            "(prior_state = 'draft' AND new_state = 'offline_eligible') OR "
            "(prior_state = 'offline_eligible' AND new_state = 'shadow_soak') OR "
            "(prior_state = 'shadow_soak' AND new_state = 'paper_active') OR "
            "(prior_state = 'paper_active' AND new_state = 'promotion_eligible') OR "
            "(prior_state = 'promotion_eligible' AND new_state IN "
            "('promoted','rejected','aborted'))))",
            name="ck_paper_validation_transition_graph",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_ids) = 'array'",
            name="ck_paper_validation_transition_evidence_array",
        ),
        schema="research",
    )
    op.create_index(
        "ix_paper_validation_transition_history",
        "paper_validation_state_transitions",
        ["validation_id", "sequence"],
        schema="research",
    )

    op.create_table(
        "strategy_hypothesis_drafts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        *_identity_columns(),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("author_id", sa.String(128), nullable=False),
        sa.Column("author_role", sa.String(16), nullable=False),
        sa.Column("mechanism", sa.Text(), nullable=False),
        sa.Column("universe", postgresql.JSONB(), nullable=False),
        sa.Column("horizon", sa.String(128), nullable=False),
        sa.Column("entry_criteria", postgresql.JSONB(), nullable=False),
        sa.Column("exit_criteria", postgresql.JSONB(), nullable=False),
        sa.Column("invalidation_criteria", postgresql.JSONB(), nullable=False),
        sa.Column("data_requirements", postgresql.JSONB(), nullable=False),
        sa.Column("expected_cost_hurdle", sa.Numeric(24, 12), nullable=False),
        sa.Column("turnover_bound", sa.Numeric(24, 12), nullable=False),
        sa.Column("risk_bound", sa.Numeric(24, 12), nullable=False),
        sa.Column("cited_evidence", postgresql.JSONB(), nullable=False),
        *_hash_columns(),
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
            name="fk_strategy_hypothesis_draft_experiment",
        ),
        sa.UniqueConstraint(
            "validation_id",
            "idempotency_key",
            name="uq_strategy_hypothesis_draft_idempotency",
        ),
        sa.CheckConstraint(
            "author_role = 'researcher'",
            name="ck_strategy_hypothesis_draft_author_role",
        ),
        sa.CheckConstraint(
            _HASH_CHECK, name="ck_strategy_hypothesis_draft_hashes"
        ),
        schema="research",
    )

    op.create_table(
        "paper_validation_postmortem_reviews",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        *_identity_columns(),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("evaluator_id", sa.String(128), nullable=False),
        sa.Column("evaluator_role", sa.String(16), nullable=False),
        sa.Column("review_text", sa.Text(), nullable=False),
        sa.Column("cited_evidence", postgresql.JSONB(), nullable=False),
        *_hash_columns(),
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
            name="fk_paper_validation_review_experiment",
        ),
        sa.UniqueConstraint(
            "validation_id",
            "idempotency_key",
            name="uq_paper_validation_review_idempotency",
        ),
        sa.CheckConstraint(
            "evaluator_role = 'reviewer'",
            name="ck_paper_validation_review_evaluator_role",
        ),
        sa.CheckConstraint(_HASH_CHECK, name="ck_paper_validation_review_hashes"),
        schema="research",
    )

    for statement in _TRIGGER_DDL:
        op.execute(statement)
    _create_audit_triggers(
        "paper_validation_state_transitions", "transitions"
    )
    _create_audit_triggers("strategy_hypothesis_drafts", "hypotheses")
    _create_audit_triggers("paper_validation_postmortem_reviews", "reviews")


def downgrade() -> None:
    for table, suffix in (
        ("paper_validation_postmortem_reviews", "reviews"),
        ("strategy_hypothesis_drafts", "hypotheses"),
        ("paper_validation_state_transitions", "transitions"),
    ):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_paper_validation_{suffix}_immutable "
            f"ON research.{table}"
        )
        op.execute(
            f"DROP TRIGGER IF EXISTS "
            f"trg_paper_validation_{suffix}_experiment_identity "
            f"ON research.{table}"
        )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "research.validate_paper_validation_experiment_identity()"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS research.reject_paper_validation_audit_mutation()"
    )
    op.drop_table("paper_validation_postmortem_reviews", schema="research")
    op.drop_table("strategy_hypothesis_drafts", schema="research")
    op.drop_index(
        "ix_paper_validation_transition_history",
        table_name="paper_validation_state_transitions",
        schema="research",
    )
    op.drop_table("paper_validation_state_transitions", schema="research")
