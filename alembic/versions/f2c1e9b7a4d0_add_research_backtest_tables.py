from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f2c1e9b7a4d0"
down_revision: str | Sequence[str] | None = (
    "0d59098a1b34",
    "add_dca_plans_and_steps",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS research")

    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("strategy_name", sa.String(length=128), nullable=False),
        sa.Column("strategy_version", sa.String(length=128), nullable=True),
        sa.Column(
            "exchange", sa.String(length=32), nullable=False, server_default="binance"
        ),
        sa.Column(
            "market", sa.String(length=32), nullable=False, server_default="spot"
        ),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("timerange", sa.String(length=64), nullable=True),
        sa.Column("runner", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("total_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "profit_factor",
            sa.Numeric(18, 8),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "max_drawdown",
            sa.Numeric(18, 8),
            nullable=False,
            server_default="0",
        ),
        sa.Column("win_rate", sa.Numeric(18, 8), nullable=True),
        sa.Column("expectancy", sa.Numeric(18, 8), nullable=True),
        sa.Column("total_return", sa.Numeric(18, 8), nullable=True),
        sa.Column("artifact_path", sa.Text(), nullable=True),
        sa.Column("artifact_hash", sa.String(length=128), nullable=True),
        sa.Column(
            "raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("run_id", name="uq_research_backtest_runs_run_id"),
        schema="research",
    )
    op.create_index(
        "ix_research_backtest_runs_runner",
        "backtest_runs",
        ["runner"],
        unique=False,
        schema="research",
    )
    op.create_index(
        "ix_research_backtest_runs_strategy",
        "backtest_runs",
        ["strategy_name"],
        unique=False,
        schema="research",
    )

    op.create_table(
        "backtest_pairs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("backtest_run_id", sa.BigInteger(), nullable=False),
        sa.Column("pair", sa.String(length=32), nullable=False),
        sa.Column("total_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("profit_factor", sa.Numeric(18, 8), nullable=True),
        sa.Column("max_drawdown", sa.Numeric(18, 8), nullable=True),
        sa.Column("total_return", sa.Numeric(18, 8), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["backtest_run_id"],
            ["research.backtest_runs.id"],
            ondelete="CASCADE",
            name="fk_research_backtest_pairs_run_id",
        ),
        sa.UniqueConstraint(
            "backtest_run_id", "pair", name="uq_research_backtest_pairs_run_pair"
        ),
        schema="research",
    )
    op.create_index(
        "ix_research_backtest_pairs_run_id",
        "backtest_pairs",
        ["backtest_run_id"],
        unique=False,
        schema="research",
    )

    op.create_table(
        "promotion_candidates",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("backtest_run_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("thresholds", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "evaluated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["backtest_run_id"],
            ["research.backtest_runs.id"],
            ondelete="CASCADE",
            name="fk_research_promotion_candidates_run_id",
        ),
        sa.UniqueConstraint(
            "backtest_run_id", name="uq_research_promotion_candidates_run_id"
        ),
        schema="research",
    )

    op.create_table(
        "sync_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("backtest_run_id", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("source_file", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "error_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["backtest_run_id"],
            ["research.backtest_runs.id"],
            ondelete="SET NULL",
            name="fk_research_sync_jobs_run_id",
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_research_sync_jobs_idempotency"
        ),
        schema="research",
    )
    op.create_index(
        "ix_research_sync_jobs_status",
        "sync_jobs",
        ["status"],
        unique=False,
        schema="research",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_sync_jobs_status", table_name="sync_jobs", schema="research"
    )
    op.drop_table("sync_jobs", schema="research")

    op.drop_table("promotion_candidates", schema="research")

    op.drop_index(
        "ix_research_backtest_pairs_run_id",
        table_name="backtest_pairs",
        schema="research",
    )
    op.drop_table("backtest_pairs", schema="research")

    op.drop_index(
        "ix_research_backtest_runs_strategy",
        table_name="backtest_runs",
        schema="research",
    )
    op.drop_index(
        "ix_research_backtest_runs_runner",
        table_name="backtest_runs",
        schema="research",
    )
    op.drop_table("backtest_runs", schema="research")

    op.execute("DROP SCHEMA IF EXISTS research")
