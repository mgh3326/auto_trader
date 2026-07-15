"""ROB-850 immutable paper-evaluation tables.

Revision ID: 20260714_rob850_paper_evaluation
Revises: 20260714_rob878_shadow
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260714_rob850_paper_evaluation"
down_revision = "20260714_rob878_shadow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHA256 = "^[0-9a-f]{64}$"
_AUDIT_TABLES = (
    "evaluation_configs",
    "evaluation_epochs",
    "evaluation_scorecards",
    "evaluation_verdicts",
)


def _timestamps() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def _create_functions() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION research.reject_evaluation_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'research.% is append-only/immutable; % rejected',
                TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )


def _create_immutable_triggers(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_rob850_{table}_immutable "
        f"BEFORE UPDATE OR DELETE ON research.{table} FOR EACH ROW EXECUTE "
        "FUNCTION research.reject_evaluation_mutation()"
    )
    op.execute(
        f"CREATE TRIGGER trg_rob850_{table}_truncate_immutable "
        f"BEFORE TRUNCATE ON research.{table} FOR EACH STATEMENT EXECUTE "
        "FUNCTION research.reject_evaluation_mutation()"
    )


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS research")
    op.create_table(
        "evaluation_configs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("schema_id", sa.String(64), nullable=False),
        sa.Column("formula_version", sa.String(16), nullable=False),
        sa.Column("currency_conversion_policy", sa.String(16), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        _timestamps(),
        sa.UniqueConstraint("config_hash", name="uq_evaluation_config_hash"),
        sa.CheckConstraint(
            f"config_hash ~ '{_SHA256}'",
            name=op.f("ck_evaluation_config_hash"),
        ),
        sa.CheckConstraint(
            "schema_id = 'paper_evaluation_config.v1'",
            name=op.f("ck_evaluation_config_schema_id"),
        ),
        sa.CheckConstraint(
            "formula_version = 'v1'",
            name=op.f("ck_evaluation_config_formula_version"),
        ),
        sa.CheckConstraint(
            "currency_conversion_policy = 'none'",
            name=op.f("ck_evaluation_config_currency_conversion_policy"),
        ),
        schema="research",
    )
    op.create_table(
        "evaluation_epochs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("epoch_id", sa.String(128), nullable=False),
        sa.Column("assignment_id", sa.String(128), nullable=False),
        sa.Column("validation_id", sa.String(128), nullable=False),
        sa.Column("cohort_id", sa.String(128), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("initial_equity", postgresql.JSONB(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reset_reason", sa.String(32), nullable=True),
        sa.Column("prior_epoch_id", sa.String(128), nullable=True),
        sa.Column("experiment_hash", sa.String(64), nullable=False),
        sa.Column("cohort_hash", sa.String(64), nullable=False),
        _timestamps(),
        sa.ForeignKeyConstraint(
            ["cohort_id"],
            ["research.paper_validation_cohorts.cohort_id"],
            ondelete="RESTRICT",
            name="fk_evaluation_epoch_cohort",
        ),
        sa.ForeignKeyConstraint(
            ["cohort_id", "assignment_id"],
            [
                "research.paper_validation_cohort_assignments.cohort_id",
                "research.paper_validation_cohort_assignments.assignment_id",
            ],
            ondelete="RESTRICT",
            name="fk_evaluation_epoch_assignment",
        ),
        sa.ForeignKeyConstraint(
            ["cohort_id", "cohort_hash"],
            [
                "research.paper_validation_cohorts.cohort_id",
                "research.paper_validation_cohorts.cohort_hash",
            ],
            ondelete="RESTRICT",
            name="fk_evaluation_epoch_cohort_lineage",
        ),
        sa.ForeignKeyConstraint(
            ["cohort_id", "assignment_id", "prior_epoch_id"],
            [
                "research.evaluation_epochs.cohort_id",
                "research.evaluation_epochs.assignment_id",
                "research.evaluation_epochs.epoch_id",
            ],
            ondelete="RESTRICT",
            name="fk_evaluation_epoch_prior_lineage",
        ),
        sa.ForeignKeyConstraint(
            ["config_hash"],
            ["research.evaluation_configs.config_hash"],
            ondelete="RESTRICT",
            name="fk_evaluation_epoch_config",
        ),
        sa.UniqueConstraint(
            "cohort_id",
            "assignment_id",
            "epoch_id",
            name="uq_evaluation_epoch_lineage",
        ),
        sa.UniqueConstraint(
            "epoch_id",
            "assignment_id",
            "config_hash",
            "experiment_hash",
            "cohort_hash",
            name="uq_evaluation_epoch_identity",
        ),
        sa.UniqueConstraint(
            "cohort_id",
            "assignment_id",
            "config_hash",
            "started_at",
            name="uq_evaluation_epoch_start",
        ),
        sa.CheckConstraint(
            f"config_hash ~ '{_SHA256}'",
            name=op.f("ck_evaluation_epoch_config_hash"),
        ),
        sa.CheckConstraint(
            f"experiment_hash ~ '{_SHA256}'",
            name=op.f("ck_evaluation_epoch_experiment_hash"),
        ),
        sa.CheckConstraint(
            f"cohort_hash ~ '{_SHA256}'",
            name=op.f("ck_evaluation_epoch_cohort_hash"),
        ),
        sa.CheckConstraint(
            "((prior_epoch_id IS NULL AND reset_reason IS NULL) OR "
            "(prior_epoch_id IS NOT NULL AND reset_reason IN "
            "('account_reset','api_key_recreation','initial_equity_change')))",
            name=op.f("ck_evaluation_epoch_reset_reason"),
        ),
        sa.CheckConstraint(
            "prior_epoch_id IS NULL OR prior_epoch_id <> epoch_id",
            name=op.f("ck_evaluation_epoch_prior_not_self"),
        ),
        schema="research",
    )
    op.execute(
        "CREATE INDEX ix_evaluation_epoch_cohort_started "
        "ON research.evaluation_epochs (cohort_id, started_at DESC)"
    )
    op.create_index(
        "ix_evaluation_epoch_assignment_started",
        "evaluation_epochs",
        ["cohort_id", "assignment_id", "started_at"],
        schema="research",
    )
    op.create_table(
        "evaluation_scorecards",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("evaluation_id", sa.String(64), nullable=False),
        sa.Column("epoch_id", sa.String(128), nullable=False),
        sa.Column("assignment_id", sa.String(128), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("view_name", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("experiment_hash", sa.String(64), nullable=False),
        sa.Column("cohort_hash", sa.String(64), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        _timestamps(),
        sa.ForeignKeyConstraint(
            [
                "epoch_id",
                "assignment_id",
                "config_hash",
                "experiment_hash",
                "cohort_hash",
            ],
            [
                "research.evaluation_epochs.epoch_id",
                "research.evaluation_epochs.assignment_id",
                "research.evaluation_epochs.config_hash",
                "research.evaluation_epochs.experiment_hash",
                "research.evaluation_epochs.cohort_hash",
            ],
            ondelete="RESTRICT",
            name="fk_evaluation_scorecard_epoch_identity",
        ),
        sa.UniqueConstraint(
            "evaluation_id",
            "view_name",
            name="uq_evaluation_scorecard_evaluation_view",
        ),
        sa.CheckConstraint(
            f"evaluation_id ~ '{_SHA256}'",
            name=op.f("ck_evaluation_scorecard_evaluation_id"),
        ),
        sa.CheckConstraint(
            f"config_hash ~ '{_SHA256}'",
            name=op.f("ck_evaluation_scorecard_config_hash"),
        ),
        sa.CheckConstraint(
            f"experiment_hash ~ '{_SHA256}'",
            name=op.f("ck_evaluation_scorecard_experiment_hash"),
        ),
        sa.CheckConstraint(
            f"cohort_hash ~ '{_SHA256}'",
            name=op.f("ck_evaluation_scorecard_cohort_hash"),
        ),
        sa.CheckConstraint(
            "view_name IN ('binance_broker','alpaca_broker','canonical_shadow')",
            name=op.f("ck_evaluation_scorecard_view_name"),
        ),
        sa.CheckConstraint(
            "currency IN ('USDT','USD')",
            name=op.f("ck_evaluation_scorecard_currency"),
        ),
        sa.CheckConstraint(
            "(view_name = 'binance_broker' AND currency = 'USDT') OR "
            "(view_name = 'alpaca_broker' AND currency = 'USD') OR "
            "(view_name = 'canonical_shadow' AND currency = 'USDT')",
            name=op.f("ck_evaluation_scorecard_view_currency_consistency"),
        ),
        schema="research",
    )
    op.create_index(
        "ix_evaluation_scorecard_epoch",
        "evaluation_scorecards",
        ["epoch_id"],
        schema="research",
    )
    op.create_table(
        "evaluation_verdicts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("evaluation_id", sa.String(64), nullable=False),
        sa.Column("epoch_id", sa.String(128), nullable=False),
        sa.Column("assignment_id", sa.String(128), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("verdict_status", sa.String(32), nullable=False),
        sa.Column("verdict_payload", postgresql.JSONB(), nullable=False),
        sa.Column("experiment_hash", sa.String(64), nullable=False),
        sa.Column("cohort_hash", sa.String(64), nullable=False),
        _timestamps(),
        sa.ForeignKeyConstraint(
            [
                "epoch_id",
                "assignment_id",
                "config_hash",
                "experiment_hash",
                "cohort_hash",
            ],
            [
                "research.evaluation_epochs.epoch_id",
                "research.evaluation_epochs.assignment_id",
                "research.evaluation_epochs.config_hash",
                "research.evaluation_epochs.experiment_hash",
                "research.evaluation_epochs.cohort_hash",
            ],
            ondelete="RESTRICT",
            name="fk_evaluation_verdict_epoch_identity",
        ),
        sa.UniqueConstraint("evaluation_id", name="uq_evaluation_verdict_evaluation"),
        sa.UniqueConstraint(
            "epoch_id",
            "idempotency_key",
            name="uq_evaluation_verdict_idempotency",
        ),
        sa.CheckConstraint(
            f"evaluation_id ~ '{_SHA256}'",
            name=op.f("ck_evaluation_verdict_evaluation_id"),
        ),
        sa.CheckConstraint(
            f"config_hash ~ '{_SHA256}'",
            name=op.f("ck_evaluation_verdict_config_hash"),
        ),
        sa.CheckConstraint(
            f"request_hash ~ '{_SHA256}'",
            name=op.f("ck_evaluation_verdict_request_hash"),
        ),
        sa.CheckConstraint(
            f"experiment_hash ~ '{_SHA256}'",
            name=op.f("ck_evaluation_verdict_experiment_hash"),
        ),
        sa.CheckConstraint(
            f"cohort_hash ~ '{_SHA256}'",
            name=op.f("ck_evaluation_verdict_cohort_hash"),
        ),
        sa.CheckConstraint(
            "verdict_status IN ('promotion_eligible','insufficient_evidence',"
            "'gate_blocked','benchmark_not_beaten','mdd_exceeded')",
            name=op.f("ck_evaluation_verdict_status"),
        ),
        schema="research",
    )
    op.create_index(
        "ix_evaluation_verdict_epoch",
        "evaluation_verdicts",
        ["epoch_id"],
        schema="research",
    )

    _create_functions()
    for table in _AUDIT_TABLES:
        _create_immutable_triggers(table)


def downgrade() -> None:
    for table in reversed(_AUDIT_TABLES):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_rob850_{table}_truncate_immutable "
            f"ON research.{table}"
        )
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_rob850_{table}_immutable ON research.{table}"
        )
    op.execute("DROP FUNCTION IF EXISTS research.reject_evaluation_mutation()")
    op.drop_index(
        "ix_evaluation_verdict_epoch",
        table_name="evaluation_verdicts",
        schema="research",
    )
    op.drop_table("evaluation_verdicts", schema="research")
    op.drop_index(
        "ix_evaluation_scorecard_epoch",
        table_name="evaluation_scorecards",
        schema="research",
    )
    op.drop_table("evaluation_scorecards", schema="research")
    op.drop_index(
        "ix_evaluation_epoch_assignment_started",
        table_name="evaluation_epochs",
        schema="research",
    )
    op.execute("DROP INDEX IF EXISTS research.ix_evaluation_epoch_cohort_started")
    op.drop_table("evaluation_epochs", schema="research")
    op.drop_table("evaluation_configs", schema="research")
