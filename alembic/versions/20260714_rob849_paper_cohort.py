"""ROB-849 immutable paper cohort and canonical snapshots.

Revision ID: 20260714_rob849_paper_cohort
Revises: 20260714_rob870_approval_batches
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260714_rob849_paper_cohort"
down_revision = "20260714_rob870_approval_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHA256 = "^[0-9a-f]{64}$"
_AUDIT_TABLES = (
    "paper_validation_cohorts",
    "paper_validation_cohort_assignments",
    "canonical_market_snapshots",
    "paper_cohort_decisions",
    "paper_cohort_venue_intents",
    "paper_run_order_links",
    "paper_cohort_target_reservations",
    "paper_cohort_terminal_fences",
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
        CREATE OR REPLACE FUNCTION research.reject_paper_cohort_audit_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'research.% is append-only/immutable; % rejected',
                TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION research.validate_paper_cohort_composition()
        RETURNS trigger AS $$
        DECLARE
            target_cohort_id text;
            champion_count integer;
            challenger_count integer;
            assignment_count integer;
            expected_assignment_count integer;
        BEGIN
            target_cohort_id := NEW.cohort_id;
            SELECT
                count(*) FILTER (WHERE role = 'champion'),
                count(*) FILTER (WHERE role = 'challenger'),
                count(*)
            INTO champion_count, challenger_count, assignment_count
            FROM research.paper_validation_cohort_assignments
            WHERE cohort_id = target_cohort_id;

            SELECT c.assignment_count INTO expected_assignment_count
            FROM research.paper_validation_cohorts AS c
            WHERE c.cohort_id = target_cohort_id;

            IF champion_count <> 1
               OR challenger_count > 2
               OR assignment_count < 1
               OR assignment_count > 3
               OR assignment_count <> expected_assignment_count THEN
                RAISE EXCEPTION
                    'paper cohort % requires exactly one champion and at most two challengers',
                    target_cohort_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def _create_immutable_triggers(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_rob849_{table}_immutable "
        f"BEFORE UPDATE OR DELETE ON research.{table} FOR EACH ROW EXECUTE "
        "FUNCTION research.reject_paper_cohort_audit_mutation()"
    )
    op.execute(
        f"CREATE TRIGGER trg_rob849_{table}_truncate_immutable "
        f"BEFORE TRUNCATE ON research.{table} FOR EACH STATEMENT EXECUTE "
        "FUNCTION research.reject_paper_cohort_audit_mutation()"
    )


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS research")
    op.create_table(
        "paper_validation_cohorts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cohort_id", sa.String(128), nullable=False),
        sa.Column("cohort_hash", sa.String(64), nullable=False),
        sa.Column("venues", postgresql.JSONB(), nullable=False),
        sa.Column("symbols", postgresql.JSONB(), nullable=False),
        sa.Column("market", sa.String(16), nullable=False),
        sa.Column("leverage", sa.Numeric(8, 4), nullable=False),
        sa.Column("interval", sa.String(8), nullable=False),
        sa.Column("required_lookback", sa.Integer(), nullable=False),
        sa.Column("max_capture_skew_ms", sa.Integer(), nullable=False),
        sa.Column("max_ticker_age_ms", sa.Integer(), nullable=False),
        sa.Column("capital_notional_usd", sa.Numeric(24, 12), nullable=False),
        sa.Column("assignment_count", sa.Integer(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stop_at", sa.DateTime(timezone=True), nullable=True),
        _timestamps(),
        sa.UniqueConstraint("cohort_id", name="uq_paper_validation_cohort_id"),
        sa.UniqueConstraint(
            "cohort_id",
            "cohort_hash",
            name="uq_paper_validation_cohort_lineage",
        ),
        sa.CheckConstraint(
            'venues = \'["binance", "alpaca"]\'::jsonb',
            name=op.f("ck_paper_validation_cohort_venues"),
        ),
        sa.CheckConstraint(
            'symbols = \'["BTCUSDT", "ETHUSDT"]\'::jsonb',
            name=op.f("ck_paper_validation_cohort_symbols"),
        ),
        sa.CheckConstraint(
            "market = 'spot'", name=op.f("ck_paper_validation_cohort_market")
        ),
        sa.CheckConstraint(
            "leverage = 1", name=op.f("ck_paper_validation_cohort_leverage")
        ),
        sa.CheckConstraint(
            "interval = '1m'", name=op.f("ck_paper_validation_cohort_interval")
        ),
        sa.CheckConstraint(
            "required_lookback BETWEEN 1 AND 1000 AND max_capture_skew_ms > 0 "
            "AND max_ticker_age_ms > 0",
            name=op.f("ck_paper_validation_cohort_capture_limits"),
        ),
        sa.CheckConstraint(
            "capital_notional_usd > 0",
            name=op.f("ck_paper_validation_cohort_capital"),
        ),
        sa.CheckConstraint(
            "assignment_count BETWEEN 1 AND 3",
            name=op.f("ck_paper_validation_cohort_assignment_count"),
        ),
        sa.CheckConstraint(
            f"cohort_hash ~ '{_SHA256}'",
            name=op.f("ck_paper_validation_cohort_hash"),
        ),
        sa.CheckConstraint(
            "stop_at IS NULL OR stop_at > activated_at",
            name=op.f("ck_paper_validation_cohort_times"),
        ),
        schema="research",
    )
    op.create_table(
        "paper_validation_cohort_assignments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("assignment_id", sa.String(128), nullable=False),
        sa.Column("cohort_id", sa.String(128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("validation_id", sa.String(128), nullable=False),
        sa.Column("validation_version", sa.Integer(), nullable=False),
        sa.Column("experiment_id", sa.String(64), nullable=False),
        sa.Column("source_backtest_run_id", sa.BigInteger(), nullable=False),
        sa.Column("strategy_version_id", sa.String(128), nullable=False),
        sa.Column("target_weights", postgresql.JSONB(), nullable=False),
        sa.Column("experiment_hash", sa.String(64), nullable=False),
        sa.Column("strategy_hash", sa.String(64), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        _timestamps(),
        sa.ForeignKeyConstraint(
            ["cohort_id"],
            ["research.paper_validation_cohorts.cohort_id"],
            ondelete="RESTRICT",
            name="fk_paper_cohort_assignment_cohort",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["research.strategy_experiments.experiment_id"],
            ondelete="RESTRICT",
            name="fk_paper_cohort_assignment_experiment",
        ),
        sa.ForeignKeyConstraint(
            ["source_backtest_run_id"],
            ["research.backtest_runs.id"],
            ondelete="RESTRICT",
            name="fk_paper_cohort_assignment_backtest_run",
        ),
        sa.UniqueConstraint("assignment_id", name="uq_paper_cohort_assignment_id"),
        sa.UniqueConstraint(
            "cohort_id",
            "assignment_id",
            name="uq_paper_cohort_assignment_lineage",
        ),
        sa.UniqueConstraint(
            "cohort_id", "ordinal", name="uq_paper_cohort_assignment_ordinal"
        ),
        sa.UniqueConstraint(
            "cohort_id",
            "experiment_id",
            name="uq_paper_cohort_assignment_experiment",
        ),
        sa.UniqueConstraint(
            "cohort_id",
            "validation_id",
            name="uq_paper_cohort_assignment_validation",
        ),
        sa.CheckConstraint(
            "(role = 'champion' AND ordinal = 0) OR "
            "(role = 'challenger' AND ordinal IN (1, 2))",
            name=op.f("ck_paper_cohort_assignment_role_ordinal"),
        ),
        sa.CheckConstraint(
            "experiment_hash = experiment_id AND "
            + " AND ".join(
                f"{name} ~ '{_SHA256}'"
                for name in (
                    "experiment_hash",
                    "strategy_hash",
                    "config_hash",
                    "policy_hash",
                    "input_hash",
                )
            ),
            name=op.f("ck_paper_cohort_assignment_hashes"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(target_weights) = 'object' "
            "AND target_weights ?& ARRAY['BTCUSDT','ETHUSDT'] "
            "AND (target_weights - ARRAY['BTCUSDT','ETHUSDT']) = '{}'::jsonb "
            "AND (target_weights->>'BTCUSDT')::numeric > 0 "
            "AND (target_weights->>'ETHUSDT')::numeric > 0 "
            "AND ((target_weights->>'BTCUSDT')::numeric + "
            "(target_weights->>'ETHUSDT')::numeric) <= 1",
            name=op.f("ck_paper_cohort_assignment_weights"),
        ),
        schema="research",
    )
    op.create_index(
        "ix_paper_cohort_assignment_cohort",
        "paper_validation_cohort_assignments",
        ["cohort_id", "ordinal"],
        schema="research",
    )
    op.create_table(
        "canonical_market_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("snapshot_id", sa.String(128), nullable=False),
        sa.Column("cohort_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("round_decision_id", sa.String(128), nullable=False),
        sa.Column("schema_id", sa.String(64), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("host", sa.String(128), nullable=False),
        sa.Column("interval", sa.String(8), nullable=False),
        sa.Column("required_lookback", sa.Integer(), nullable=False),
        sa.Column("max_capture_skew_ms", sa.Integer(), nullable=False),
        sa.Column("max_ticker_age_ms", sa.Integer(), nullable=False),
        sa.Column("capture_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capture_completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        _timestamps(),
        sa.ForeignKeyConstraint(
            ["cohort_id"],
            ["research.paper_validation_cohorts.cohort_id"],
            ondelete="RESTRICT",
            name="fk_canonical_snapshot_cohort",
        ),
        sa.UniqueConstraint("snapshot_id", name="uq_canonical_snapshot_id"),
        sa.UniqueConstraint(
            "cohort_id",
            "run_id",
            "round_decision_id",
            name="uq_canonical_snapshot_round",
        ),
        sa.UniqueConstraint(
            "cohort_id",
            "run_id",
            "round_decision_id",
            "snapshot_id",
            "content_hash",
            name="uq_canonical_snapshot_lineage",
        ),
        sa.CheckConstraint(
            "schema_id = 'canonical_market_snapshot.v1'",
            name=op.f("ck_canonical_snapshot_schema"),
        ),
        sa.CheckConstraint(
            "source = 'binance_public_spot'",
            name=op.f("ck_canonical_snapshot_source"),
        ),
        sa.CheckConstraint(
            "host = 'https://api.binance.com'",
            name=op.f("ck_canonical_snapshot_host"),
        ),
        sa.CheckConstraint(
            "interval = '1m'", name=op.f("ck_canonical_snapshot_interval")
        ),
        sa.CheckConstraint(
            f"content_hash ~ '{_SHA256}' AND capture_completed_at >= capture_started_at",
            name=op.f("ck_canonical_snapshot_hash_and_time"),
        ),
        schema="research",
    )
    op.create_table(
        "paper_cohort_decisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("decision_id", sa.String(128), nullable=False),
        sa.Column("cohort_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("round_decision_id", sa.String(128), nullable=False),
        sa.Column("assignment_id", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("snapshot_id", sa.String(128), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("signal_payload", postgresql.JSONB(), nullable=False),
        sa.Column("signal_hash", sa.String(64), nullable=False),
        _timestamps(),
        sa.ForeignKeyConstraint(
            ["cohort_id", "assignment_id"],
            [
                "research.paper_validation_cohort_assignments.cohort_id",
                "research.paper_validation_cohort_assignments.assignment_id",
            ],
            ondelete="RESTRICT",
            name="fk_paper_cohort_decision_assignment_lineage",
        ),
        sa.ForeignKeyConstraint(
            [
                "cohort_id",
                "run_id",
                "round_decision_id",
                "snapshot_id",
                "snapshot_hash",
            ],
            [
                "research.canonical_market_snapshots.cohort_id",
                "research.canonical_market_snapshots.run_id",
                "research.canonical_market_snapshots.round_decision_id",
                "research.canonical_market_snapshots.snapshot_id",
                "research.canonical_market_snapshots.content_hash",
            ],
            ondelete="RESTRICT",
            name="fk_paper_cohort_decision_snapshot_lineage",
        ),
        sa.UniqueConstraint("decision_id", name="uq_paper_cohort_decision_id"),
        sa.UniqueConstraint(
            "cohort_id",
            "run_id",
            "round_decision_id",
            "assignment_id",
            "symbol",
            name="uq_paper_cohort_decision_identity",
        ),
        sa.UniqueConstraint(
            "cohort_id",
            "run_id",
            "round_decision_id",
            "decision_id",
            "assignment_id",
            "symbol",
            "snapshot_id",
            "snapshot_hash",
            name="uq_paper_cohort_decision_lineage",
        ),
        sa.CheckConstraint(
            "mode IN ('shadow','paper_active')",
            name=op.f("ck_paper_cohort_decision_mode"),
        ),
        sa.CheckConstraint(
            "symbol IN ('BTCUSDT','ETHUSDT')",
            name=op.f("ck_paper_cohort_decision_symbol"),
        ),
        sa.CheckConstraint(
            f"snapshot_hash ~ '{_SHA256}' AND signal_hash ~ '{_SHA256}'",
            name=op.f("ck_paper_cohort_decision_hashes"),
        ),
        schema="research",
    )
    op.create_table(
        "paper_cohort_venue_intents",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("intent_id", sa.String(128), nullable=False),
        sa.Column("cohort_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("round_decision_id", sa.String(128), nullable=False),
        sa.Column("decision_id", sa.String(128), nullable=False),
        sa.Column("assignment_id", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("snapshot_id", sa.String(128), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("venue", sa.String(16), nullable=False),
        sa.Column("execution_ordinal", sa.Integer(), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("venue_quote_evidence", postgresql.JSONB(), nullable=False),
        sa.Column("would_order_evidence", postgresql.JSONB(), nullable=False),
        _timestamps(),
        sa.ForeignKeyConstraint(
            [
                "cohort_id",
                "run_id",
                "round_decision_id",
                "decision_id",
                "assignment_id",
                "symbol",
                "snapshot_id",
                "snapshot_hash",
            ],
            [
                "research.paper_cohort_decisions.cohort_id",
                "research.paper_cohort_decisions.run_id",
                "research.paper_cohort_decisions.round_decision_id",
                "research.paper_cohort_decisions.decision_id",
                "research.paper_cohort_decisions.assignment_id",
                "research.paper_cohort_decisions.symbol",
                "research.paper_cohort_decisions.snapshot_id",
                "research.paper_cohort_decisions.snapshot_hash",
            ],
            ondelete="RESTRICT",
            name="fk_paper_cohort_intent_decision_lineage",
        ),
        sa.UniqueConstraint("intent_id", name="uq_paper_cohort_venue_intent_id"),
        sa.UniqueConstraint(
            "decision_id", "venue", name="uq_paper_cohort_venue_intent"
        ),
        sa.UniqueConstraint(
            "cohort_id",
            "run_id",
            "round_decision_id",
            "intent_id",
            "decision_id",
            "assignment_id",
            "symbol",
            "snapshot_id",
            "snapshot_hash",
            "venue",
            name="uq_paper_cohort_intent_lineage",
        ),
        sa.UniqueConstraint(
            "cohort_id",
            "run_id",
            "round_decision_id",
            "intent_id",
            "decision_id",
            "assignment_id",
            "symbol",
            "snapshot_id",
            "snapshot_hash",
            "venue",
            "execution_ordinal",
            name="uq_paper_cohort_intent_reservation_lineage",
        ),
        sa.UniqueConstraint(
            "cohort_id",
            "run_id",
            "round_decision_id",
            "execution_ordinal",
            name="uq_paper_cohort_intent_execution_ordinal",
        ),
        sa.CheckConstraint(
            "venue IN ('binance','alpaca')",
            name=op.f("ck_paper_cohort_venue_intent_venue"),
        ),
        sa.CheckConstraint(
            f"request_hash ~ '{_SHA256}'",
            name=op.f("ck_paper_cohort_venue_intent_hash"),
        ),
        sa.CheckConstraint(
            "symbol IN ('BTCUSDT','ETHUSDT') AND execution_ordinal >= 0",
            name=op.f("ck_paper_cohort_venue_intent_execution_identity"),
        ),
        schema="research",
    )
    op.create_table(
        "paper_cohort_run_claims",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cohort_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("round_decision_id", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("owner_token", sa.String(128), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "claim_status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'in_progress'"),
        ),
        sa.Column("result_payload", postgresql.JSONB(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_reason", sa.String(64), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        _timestamps(),
        sa.ForeignKeyConstraint(
            ["cohort_id"],
            ["research.paper_validation_cohorts.cohort_id"],
            ondelete="RESTRICT",
            name="fk_paper_cohort_run_claim_cohort",
        ),
        sa.UniqueConstraint(
            "cohort_id",
            "run_id",
            "round_decision_id",
            name="uq_paper_cohort_run_claim",
        ),
        sa.CheckConstraint(
            f"request_hash ~ '{_SHA256}'",
            name=op.f("ck_paper_cohort_run_claim_hash"),
        ),
        sa.CheckConstraint(
            "claim_status IN ('in_progress','completed','blocked',"
            "'reconciliation_required')",
            name=op.f("ck_paper_cohort_run_claim_status"),
        ),
        sa.CheckConstraint(
            "(claim_status = 'in_progress' AND result_payload IS NULL "
            "AND completed_at IS NULL AND terminal_reason IS NULL "
            "AND terminal_at IS NULL) OR "
            "(claim_status = 'completed' AND result_payload IS NOT NULL "
            "AND completed_at IS NOT NULL AND terminal_reason IS NULL "
            "AND terminal_at IS NULL) OR "
            "(claim_status IN ('blocked','reconciliation_required') "
            "AND result_payload IS NULL AND completed_at IS NULL "
            "AND terminal_reason IS NOT NULL AND terminal_at IS NOT NULL)",
            name=op.f("ck_paper_cohort_run_claim_state_consistency"),
        ),
        schema="research",
    )
    op.create_table(
        "paper_run_order_links",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cohort_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("round_decision_id", sa.String(128), nullable=False),
        sa.Column("intent_id", sa.String(128), nullable=False),
        sa.Column("decision_id", sa.String(128), nullable=False),
        sa.Column("assignment_id", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("snapshot_id", sa.String(128), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("venue", sa.String(16), nullable=False),
        sa.Column("native_ledger_kind", sa.String(64), nullable=False),
        sa.Column("native_ledger_row_id", sa.BigInteger(), nullable=False),
        sa.Column("client_order_id", sa.String(128), nullable=False),
        sa.Column("broker_order_id", sa.String(128), nullable=False),
        _timestamps(),
        sa.ForeignKeyConstraint(
            [
                "cohort_id",
                "run_id",
                "round_decision_id",
                "intent_id",
                "decision_id",
                "assignment_id",
                "symbol",
                "snapshot_id",
                "snapshot_hash",
                "venue",
            ],
            [
                "research.paper_cohort_venue_intents.cohort_id",
                "research.paper_cohort_venue_intents.run_id",
                "research.paper_cohort_venue_intents.round_decision_id",
                "research.paper_cohort_venue_intents.intent_id",
                "research.paper_cohort_venue_intents.decision_id",
                "research.paper_cohort_venue_intents.assignment_id",
                "research.paper_cohort_venue_intents.symbol",
                "research.paper_cohort_venue_intents.snapshot_id",
                "research.paper_cohort_venue_intents.snapshot_hash",
                "research.paper_cohort_venue_intents.venue",
            ],
            ondelete="RESTRICT",
            name="fk_paper_run_order_link_intent_lineage",
        ),
        sa.UniqueConstraint(
            "intent_id",
            name="uq_paper_run_order_link_intent",
        ),
        sa.UniqueConstraint(
            "native_ledger_kind",
            "native_ledger_row_id",
            name="uq_paper_run_order_link_native_row",
        ),
        sa.UniqueConstraint(
            "venue",
            "client_order_id",
            name="uq_paper_run_order_link_client_order",
        ),
        sa.CheckConstraint(
            "venue IN ('binance','alpaca')",
            name=op.f("ck_paper_run_order_link_venue"),
        ),
        sa.CheckConstraint(
            "native_ledger_kind IN ('binance_demo_order_ledger',"
            "'alpaca_paper_order_ledger')",
            name=op.f("ck_paper_run_order_link_ledger_kind"),
        ),
        sa.CheckConstraint(
            "(venue = 'binance' AND "
            "native_ledger_kind = 'binance_demo_order_ledger') OR "
            "(venue = 'alpaca' AND "
            "native_ledger_kind = 'alpaca_paper_order_ledger')",
            name=op.f("ck_paper_run_order_link_venue_ledger"),
        ),
        sa.CheckConstraint(
            f"snapshot_hash ~ '{_SHA256}'",
            name=op.f("ck_paper_run_order_link_snapshot_hash"),
        ),
        schema="research",
    )
    op.create_table(
        "paper_cohort_target_reservations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cohort_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("round_decision_id", sa.String(128), nullable=False),
        sa.Column("intent_id", sa.String(128), nullable=False),
        sa.Column("decision_id", sa.String(128), nullable=False),
        sa.Column("assignment_id", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("snapshot_id", sa.String(128), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("venue", sa.String(16), nullable=False),
        sa.Column("execution_ordinal", sa.Integer(), nullable=False),
        _timestamps(),
        sa.ForeignKeyConstraint(
            [
                "cohort_id",
                "run_id",
                "round_decision_id",
                "intent_id",
                "decision_id",
                "assignment_id",
                "symbol",
                "snapshot_id",
                "snapshot_hash",
                "venue",
                "execution_ordinal",
            ],
            [
                "research.paper_cohort_venue_intents.cohort_id",
                "research.paper_cohort_venue_intents.run_id",
                "research.paper_cohort_venue_intents.round_decision_id",
                "research.paper_cohort_venue_intents.intent_id",
                "research.paper_cohort_venue_intents.decision_id",
                "research.paper_cohort_venue_intents.assignment_id",
                "research.paper_cohort_venue_intents.symbol",
                "research.paper_cohort_venue_intents.snapshot_id",
                "research.paper_cohort_venue_intents.snapshot_hash",
                "research.paper_cohort_venue_intents.venue",
                "research.paper_cohort_venue_intents.execution_ordinal",
            ],
            ondelete="RESTRICT",
            name="fk_paper_cohort_target_reservation_intent_lineage",
        ),
        sa.UniqueConstraint(
            "intent_id",
            name="uq_paper_cohort_target_reservation_intent",
        ),
        sa.UniqueConstraint(
            "cohort_id",
            "assignment_id",
            "symbol",
            "venue",
            name="uq_paper_cohort_target_reservation_target",
        ),
        sa.CheckConstraint(
            "symbol IN ('BTCUSDT','ETHUSDT') "
            "AND venue IN ('binance','alpaca') AND execution_ordinal >= 0",
            name=op.f("ck_paper_cohort_target_reservation_identity"),
        ),
        sa.CheckConstraint(
            f"snapshot_hash ~ '{_SHA256}'",
            name=op.f("ck_paper_cohort_target_reservation_snapshot_hash"),
        ),
        schema="research",
    )
    op.create_table(
        "paper_cohort_terminal_fences",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("fence_id", sa.String(128), nullable=False),
        sa.Column("cohort_id", sa.String(128), nullable=False),
        sa.Column("cohort_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("actor_role", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("reason_text", sa.Text(), nullable=False),
        sa.Column("validation_evidence", postgresql.JSONB(), nullable=False),
        sa.Column("fenced_at", sa.DateTime(timezone=True), nullable=False),
        _timestamps(),
        sa.ForeignKeyConstraint(
            ["cohort_id", "cohort_hash"],
            [
                "research.paper_validation_cohorts.cohort_id",
                "research.paper_validation_cohorts.cohort_hash",
            ],
            ondelete="RESTRICT",
            name="fk_paper_cohort_terminal_fence_cohort_lineage",
        ),
        sa.UniqueConstraint(
            "fence_id",
            name="uq_paper_cohort_terminal_fence_id",
        ),
        sa.UniqueConstraint(
            "cohort_id",
            name="uq_paper_cohort_terminal_fence_cohort",
        ),
        sa.UniqueConstraint(
            "cohort_id",
            "idempotency_key",
            name="uq_paper_cohort_terminal_fence_idempotency",
        ),
        sa.CheckConstraint(
            "actor_role IN ('operator','system')",
            name=op.f("ck_paper_cohort_terminal_fence_actor_role"),
        ),
        sa.CheckConstraint(
            f"cohort_hash ~ '{_SHA256}' AND request_hash ~ '{_SHA256}'",
            name=op.f("ck_paper_cohort_terminal_fence_hashes"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(validation_evidence) = 'object'",
            name=op.f("ck_paper_cohort_terminal_fence_evidence"),
        ),
        sa.CheckConstraint(
            "fence_id ~ '[^[:space:]]' AND char_length(fence_id) <= 128 "
            "AND cohort_id ~ '[^[:space:]]' "
            "AND char_length(cohort_id) <= 128 "
            "AND idempotency_key ~ '[^[:space:]]' "
            "AND char_length(idempotency_key) <= 128 "
            "AND actor_id ~ '[^[:space:]]' "
            "AND char_length(actor_id) <= 128 "
            "AND reason_code ~ '[^[:space:]]' "
            "AND char_length(reason_code) <= 64 "
            "AND reason_text ~ '[^[:space:]]' "
            "AND char_length(reason_text) <= 1024",
            name=op.f("ck_paper_cohort_terminal_fence_text_bounds"),
        ),
        schema="research",
    )

    _create_functions()
    for table in _AUDIT_TABLES:
        _create_immutable_triggers(table)
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_paper_cohort_composition_from_cohort "
        "AFTER INSERT ON research.paper_validation_cohorts "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
        "research.validate_paper_cohort_composition()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_paper_cohort_composition_from_assignment "
        "AFTER INSERT ON research.paper_validation_cohort_assignments "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
        "research.validate_paper_cohort_composition()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_paper_cohort_composition_from_assignment "
        "ON research.paper_validation_cohort_assignments"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_paper_cohort_composition_from_cohort "
        "ON research.paper_validation_cohorts"
    )
    for table in reversed(_AUDIT_TABLES):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_rob849_{table}_truncate_immutable "
            f"ON research.{table}"
        )
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_rob849_{table}_immutable ON research.{table}"
        )
    op.execute("DROP FUNCTION IF EXISTS research.validate_paper_cohort_composition()")
    op.execute("DROP FUNCTION IF EXISTS research.reject_paper_cohort_audit_mutation()")
    op.drop_table("paper_cohort_terminal_fences", schema="research")
    op.drop_table("paper_cohort_target_reservations", schema="research")
    op.drop_table("paper_run_order_links", schema="research")
    op.drop_table("paper_cohort_run_claims", schema="research")
    op.drop_table("paper_cohort_venue_intents", schema="research")
    op.drop_table("paper_cohort_decisions", schema="research")
    op.drop_table("canonical_market_snapshots", schema="research")
    op.drop_index(
        "ix_paper_cohort_assignment_cohort",
        table_name="paper_validation_cohort_assignments",
        schema="research",
    )
    op.drop_table("paper_validation_cohort_assignments", schema="research")
    op.drop_table("paper_validation_cohorts", schema="research")
