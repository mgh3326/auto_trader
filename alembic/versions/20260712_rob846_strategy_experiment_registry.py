"""ROB-846 immutable strategy experiment registry + complete trial accounting.

Additive, research-schema only. Adds an immutable ``strategy_experiments``
parent, extends ``backtest_runs`` with append-only trial-child fields, and adds
run/config/data hash linkage to ``promotion_candidates``. Row-level
immutability (append-only) is enforced by BEFORE UPDATE/DELETE triggers:

* ``strategy_experiments`` — every UPDATE/DELETE is rejected.
* ``backtest_runs`` — UPDATE/DELETE is rejected only for trial rows
  (``strategy_experiment_id IS NOT NULL``); legacy summary rows ingested via
  ``upsert_backtest_run`` keep ``strategy_experiment_id`` NULL and stay mutable.

Revision ID: 20260712_rob846_experiments
Revises: 20260711_rob832_actions
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260712_rob846_experiments"
down_revision: str | Sequence[str] | None = "20260711_rob832_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_HASH_COLUMNS: tuple[str, ...] = (
    "strategy_hash",
    "code_hash",
    "params_hash",
    "dataset_manifest_hash",
    "universe_hash",
    "pit_hash",
    "frozen_config_hash",
    "policy_hash",
    "benchmark_hash",
    "cost_hash",
    "mdd_hash",
)


# --- append-only immutability triggers (mirrored in tests/_schema_bootstrap) ---
_IMMUTABILITY_DDL: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION research.reject_strategy_experiment_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION
            'research.strategy_experiments is append-only/immutable; % rejected',
            TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_strategy_experiments_immutable "
    "ON research.strategy_experiments",
    """
    CREATE TRIGGER trg_strategy_experiments_immutable
        BEFORE UPDATE OR DELETE ON research.strategy_experiments
        FOR EACH ROW
        EXECUTE FUNCTION research.reject_strategy_experiment_mutation()
    """,
    """
    CREATE OR REPLACE FUNCTION research.reject_backtest_trial_mutation()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            IF OLD.strategy_experiment_id IS NOT NULL THEN
                RAISE EXCEPTION
                    'research.backtest_runs trial rows are append-only; DELETE rejected on id=%',
                    OLD.id
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN OLD;
        END IF;
        -- UPDATE: reject when the row is (OLD) or would become (NEW) a trial.
        -- This blocks both mutating an existing trial and the legacy->trial
        -- conversion bypass. Only legacy->legacy (null->null) edits are allowed.
        IF OLD.strategy_experiment_id IS NOT NULL
           OR NEW.strategy_experiment_id IS NOT NULL THEN
            RAISE EXCEPTION
                'research.backtest_runs trial rows are append-only; '
                'UPDATE/convert rejected on id=%',
                OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_backtest_runs_trial_immutable "
    "ON research.backtest_runs",
    """
    CREATE TRIGGER trg_backtest_runs_trial_immutable
        BEFORE UPDATE OR DELETE ON research.backtest_runs
        FOR EACH ROW
        EXECUTE FUNCTION research.reject_backtest_trial_mutation()
    """,
)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS research")

    op.create_table(
        "strategy_experiments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_key", sa.String(length=128), nullable=False),
        sa.Column("strategy_version", sa.String(length=128), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=True),
        *[
            sa.Column(name, sa.String(length=64), nullable=False)
            for name in _HASH_COLUMNS
        ],
        sa.Column(
            "benchmark_definition",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "cost_definition", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "mdd_definition", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("supersedes_experiment_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "experiment_id", name="uq_research_strategy_experiments_experiment_id"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_experiment_id"],
            ["research.strategy_experiments.experiment_id"],
            ondelete="RESTRICT",
            name="fk_research_strategy_experiments_supersedes",
        ),
        schema="research",
    )
    op.create_index(
        "ix_research_strategy_experiments_strategy_key",
        "strategy_experiments",
        ["strategy_key", "strategy_version"],
        schema="research",
    )
    op.create_index(
        "ix_research_strategy_experiments_supersedes",
        "strategy_experiments",
        ["supersedes_experiment_id"],
        schema="research",
    )

    # ---- backtest_runs: append-only trial-child fields ----
    op.add_column(
        "backtest_runs",
        sa.Column("strategy_experiment_id", sa.BigInteger(), nullable=True),
        schema="research",
    )
    op.add_column(
        "backtest_runs",
        sa.Column("trial_index", sa.Integer(), nullable=True),
        schema="research",
    )
    op.add_column(
        "backtest_runs",
        sa.Column("seed", sa.BigInteger(), nullable=True),
        schema="research",
    )
    op.add_column(
        "backtest_runs",
        sa.Column("information_cutoff", sa.TIMESTAMP(timezone=True), nullable=True),
        schema="research",
    )
    op.add_column(
        "backtest_runs",
        sa.Column("trial_status", sa.String(length=16), nullable=True),
        schema="research",
    )
    op.add_column(
        "backtest_runs",
        sa.Column("gate_artifact_hash", sa.String(length=64), nullable=True),
        schema="research",
    )
    op.add_column(
        "backtest_runs",
        sa.Column("trial_idempotency_key", sa.String(length=128), nullable=True),
        schema="research",
    )
    op.create_foreign_key(
        "fk_research_backtest_runs_experiment_id",
        "backtest_runs",
        "strategy_experiments",
        ["strategy_experiment_id"],
        ["id"],
        source_schema="research",
        referent_schema="research",
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_research_backtest_runs_experiment_trial_index",
        "backtest_runs",
        ["strategy_experiment_id", "trial_index"],
        schema="research",
    )
    op.create_unique_constraint(
        "uq_research_backtest_runs_experiment_idempotency",
        "backtest_runs",
        ["strategy_experiment_id", "trial_idempotency_key"],
        schema="research",
    )
    op.create_check_constraint(
        "ck_research_backtest_runs_trial_status",
        "backtest_runs",
        "trial_status IS NULL OR trial_status IN "
        "('completed','rejected','crashed','timeout')",
        schema="research",
    )
    op.create_index(
        "ix_research_backtest_runs_experiment",
        "backtest_runs",
        ["strategy_experiment_id", "trial_index"],
        schema="research",
    )
    # Trial all-or-none integrity: a row is either a legacy summary (no trial
    # fields at all) or a fully-formed trial (experiment + index + status). Added
    # NOT VALID so pre-existing legacy summary rows are never retro-invalidated
    # while every new INSERT/UPDATE is enforced.
    op.execute(
        "ALTER TABLE research.backtest_runs "
        "ADD CONSTRAINT ck_research_backtest_runs_trial_all_or_none CHECK ("
        "(strategy_experiment_id IS NULL AND trial_index IS NULL "
        "AND trial_status IS NULL AND trial_idempotency_key IS NULL "
        "AND seed IS NULL AND information_cutoff IS NULL "
        "AND gate_artifact_hash IS NULL) "
        "OR (strategy_experiment_id IS NOT NULL AND trial_index IS NOT NULL "
        "AND trial_status IS NOT NULL)) NOT VALID"
    )

    # ---- promotion_candidates: exact run/config/data linkage ----
    op.add_column(
        "promotion_candidates",
        sa.Column("experiment_id", sa.String(length=64), nullable=True),
        schema="research",
    )
    op.add_column(
        "promotion_candidates",
        sa.Column("run_config_hash", sa.String(length=64), nullable=True),
        schema="research",
    )
    op.add_column(
        "promotion_candidates",
        sa.Column("run_data_hash", sa.String(length=64), nullable=True),
        schema="research",
    )
    # AC#5 boundary: a NEW promotion candidate must carry a full exact identity
    # (experiment/config/data hashes). NOT VALID keeps legacy null-identity rows
    # for migration compatibility while blocking any new incomplete write at the
    # DB boundary; the service layer no longer writes identity-less candidates.
    op.execute(
        "ALTER TABLE research.promotion_candidates "
        "ADD CONSTRAINT ck_research_promotion_candidates_identity_complete CHECK ("
        "experiment_id IS NOT NULL AND run_config_hash IS NOT NULL "
        "AND run_data_hash IS NOT NULL) NOT VALID"
    )

    for stmt in _IMMUTABILITY_DDL:
        op.execute(stmt)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_backtest_runs_trial_immutable "
        "ON research.backtest_runs"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_strategy_experiments_immutable "
        "ON research.strategy_experiments"
    )
    op.execute("DROP FUNCTION IF EXISTS research.reject_backtest_trial_mutation()")
    op.execute("DROP FUNCTION IF EXISTS research.reject_strategy_experiment_mutation()")

    op.execute(
        "ALTER TABLE research.promotion_candidates "
        "DROP CONSTRAINT IF EXISTS ck_research_promotion_candidates_identity_complete"
    )
    op.execute(
        "ALTER TABLE research.backtest_runs "
        "DROP CONSTRAINT IF EXISTS ck_research_backtest_runs_trial_all_or_none"
    )

    op.drop_column("promotion_candidates", "run_data_hash", schema="research")
    op.drop_column("promotion_candidates", "run_config_hash", schema="research")
    op.drop_column("promotion_candidates", "experiment_id", schema="research")

    op.drop_index(
        "ix_research_backtest_runs_experiment",
        table_name="backtest_runs",
        schema="research",
    )
    op.drop_constraint(
        "ck_research_backtest_runs_trial_status",
        "backtest_runs",
        type_="check",
        schema="research",
    )
    op.drop_constraint(
        "uq_research_backtest_runs_experiment_idempotency",
        "backtest_runs",
        type_="unique",
        schema="research",
    )
    op.drop_constraint(
        "uq_research_backtest_runs_experiment_trial_index",
        "backtest_runs",
        type_="unique",
        schema="research",
    )
    op.drop_constraint(
        "fk_research_backtest_runs_experiment_id",
        "backtest_runs",
        type_="foreignkey",
        schema="research",
    )
    op.drop_column("backtest_runs", "trial_idempotency_key", schema="research")
    op.drop_column("backtest_runs", "gate_artifact_hash", schema="research")
    op.drop_column("backtest_runs", "trial_status", schema="research")
    op.drop_column("backtest_runs", "information_cutoff", schema="research")
    op.drop_column("backtest_runs", "seed", schema="research")
    op.drop_column("backtest_runs", "trial_index", schema="research")
    op.drop_column("backtest_runs", "strategy_experiment_id", schema="research")

    op.drop_index(
        "ix_research_strategy_experiments_supersedes",
        table_name="strategy_experiments",
        schema="research",
    )
    op.drop_index(
        "ix_research_strategy_experiments_strategy_key",
        table_name="strategy_experiments",
        schema="research",
    )
    op.drop_table("strategy_experiments", schema="research")
