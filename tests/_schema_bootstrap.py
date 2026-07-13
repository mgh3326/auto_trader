"""Single source of truth for the pytest test-schema DDL (ROB-723).

Unifies the DDL that previously lived (duplicated) inside the ``db_session``
fixture (tests/conftest.py) and the ``session`` fixture
(tests/_investment_reports_helpers.py). Run exactly once per test DB by the
``_bootstrap_test_schema`` barrier in conftest, so no schema DDL ever overlaps
a concurrent xdist worker's test body.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from sqlalchemy import text

# Bump when adding an ORM table that has NO mirrored ALTER string below, so the
# content hash changes and a persistent local DB re-bootstraps once. Adding a
# mirrored ALTER already changes the hash automatically.
#
# v5 (ROB-816): review.order_proposals / review.order_proposal_rungs (Task 4)
# have no mirrored ALTER string below — they rely solely on
# Base.metadata.create_all. Persistent local test DBs bootstrapped before
# Task 4 landed never got these tables until this bump forces one re-run.
# v6 (ROB-846): research.strategy_experiments / research.backtest_runs
# append-only immutability triggers are non-ORM DDL and are mirrored below.
# v7 (ROB-846 review): trigger now blocks legacy->trial UPDATE conversion, plus
# trial all-or-none + promotion identity-complete CHECK constraints (NOT VALID).
# v8 (ROB-859): trade_forecasts accepts the unscored closed_no_claim status.
# v9 (ROB-844): binance_demo_order_ledger partial-unique indexes
# (uq_binance_demo_ledger_open_root / _broker_ack) mirrored in _DDL_STATEMENTS.
# v10 (ROB-844 review): broker-ack identity adds instrument_id. Persistent test
# DBs may already have the same named 3-column index, so a shape-aware refresher
# replaces only a mismatched definition before CREATE IF NOT EXISTS.
# v11 (ROB-866): review.toss_manual_activity_alerts (new ORM table) — create_all
# builds it; bump forces a persistent local DB to re-bootstrap once.
SCHEMA_BOOTSTRAP_VERSION = 13

# ---- constraints + enums (moved verbatim from conftest.py) ----
MARKET_VALUATION_SOURCE_CHECK_NAME = "ck_market_valuation_snapshots_source"
MARKET_VALUATION_SOURCE_MODEL_CHECK_NAME = (
    "ck_market_valuation_snapshots_ck_market_valuation_snapshots_source"
)
MARKET_VALUATION_SOURCE_VALUES = ("naver_finance", "yahoo", "toss_openapi")

SNAPSHOT_KIND_CHECK_NAME = "ck_investment_snapshots_snapshot_kind"
SNAPSHOT_KIND_MODEL_CHECK_NAME = (
    "ck_investment_snapshots_ck_investment_snapshots_snapshot_kind"
)
SNAPSHOT_KIND_CHECK_NAMES = (SNAPSHOT_KIND_MODEL_CHECK_NAME, SNAPSHOT_KIND_CHECK_NAME)
SNAPSHOT_KIND_VALUES = (
    "portfolio",
    "market",
    "news",
    "symbol",
    "candidate_universe",
    "browser_probe",
    "invest_page",
    "journal",
    "watch_context",
    "naver_remote_debug",
    "toss_remote_debug",
    "llm_input_frozen",
    "pending_orders",
    "validated_run_card",
    "kr_market_ranking",
    "investor_flow",
)

TRADE_FORECAST_STATUS_CHECK_NAME = "ck_trade_forecasts_status"
TRADE_FORECAST_STATUS_MODEL_CHECK_NAME = "ck_trade_forecasts_ck_trade_forecasts_status"
TRADE_FORECAST_STATUS_CHECK_NAMES = (
    TRADE_FORECAST_STATUS_MODEL_CHECK_NAME,
    TRADE_FORECAST_STATUS_CHECK_NAME,
)
TRADE_FORECAST_STATUS_VALUES = ("open", "closed", "closed_no_claim")


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _check_constraint_sql(column_name: str, values: tuple[str, ...]) -> str:
    values_sql = ",".join(f"'{value}'" for value in values)
    return f"CHECK ({column_name} IN ({values_sql}))"


def _constraint_definitions_need_refresh(
    definitions: Iterable[str | None],
    required_values: tuple[str, ...],
) -> bool:
    definitions = list(definitions)
    if not definitions:
        return True
    return any(
        not all(value in (definition or "") for value in required_values)
        for definition in definitions
    )


ROB844_ACK_INDEX_REFRESH_DDL = (
    "DO $rob844_ack$ DECLARE current_columns text[]; "
    "current_is_unique boolean; current_predicate text; BEGIN "
    "IF to_regclass('uq_binance_demo_ledger_broker_ack') IS NOT NULL THEN "
    "SELECT array_agg(attribute.attname ORDER BY key.ordinality), "
    "index_meta.indisunique, "
    "pg_get_expr(index_meta.indpred, index_meta.indrelid) "
    "INTO current_columns, current_is_unique, current_predicate "
    "FROM pg_index index_meta "
    "JOIN pg_class index_class ON index_class.oid = index_meta.indexrelid "
    "CROSS JOIN LATERAL unnest(index_meta.indkey) WITH ORDINALITY "
    "AS key(attnum, ordinality) "
    "JOIN pg_attribute attribute "
    "ON attribute.attrelid = index_meta.indrelid "
    "AND attribute.attnum = key.attnum "
    "WHERE index_class.oid = "
    "to_regclass('uq_binance_demo_ledger_broker_ack') "
    "GROUP BY index_meta.indisunique, index_meta.indpred, index_meta.indrelid; "
    "IF current_columns IS DISTINCT FROM "
    "ARRAY['product','venue_host','instrument_id','broker_order_id']::text[] "
    "OR current_is_unique IS DISTINCT FROM true "
    "OR regexp_replace(lower(coalesce(current_predicate, '')), "
    "'[^a-z0-9_]', '', 'g') <> 'broker_order_idisnotnull' "
    "THEN DROP INDEX uq_binance_demo_ledger_broker_ack; END IF; "
    "END IF; END $rob844_ack$"
)
ROB844_ACK_INDEX_CREATE_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_binance_demo_ledger_broker_ack "
    "ON binance_demo_order_ledger "
    "(product, venue_host, instrument_id, broker_order_id) "
    "WHERE broker_order_id IS NOT NULL"
)


async def _ensure_market_valuation_source_constraint(conn) -> None:
    constraints = await conn.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) AS definition "
            "FROM pg_constraint "
            "WHERE conrelid = 'market_valuation_snapshots'::regclass "
            "AND pg_get_constraintdef(oid) LIKE '%source%' "
            "AND contype = 'c'"
        )
    )
    rows = list(constraints)
    if not _constraint_definitions_need_refresh(
        [row[1] for row in rows],
        MARKET_VALUATION_SOURCE_VALUES,
    ):
        return

    for name, _definition in rows:
        await conn.execute(
            text(
                "ALTER TABLE market_valuation_snapshots "
                f"DROP CONSTRAINT IF EXISTS {_quote_ident(name)}"
            )
        )
    await conn.execute(
        text(
            "ALTER TABLE market_valuation_snapshots "
            f"ADD CONSTRAINT {MARKET_VALUATION_SOURCE_CHECK_NAME} "
            f"{_check_constraint_sql('source', MARKET_VALUATION_SOURCE_VALUES)}"
        )
    )


async def _ensure_investment_snapshot_kind_constraint(conn) -> None:
    names_sql = ",".join(f"'{name}'" for name in SNAPSHOT_KIND_CHECK_NAMES)
    constraints = await conn.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) AS definition "
            "FROM pg_constraint "
            "WHERE conrelid = 'review.investment_snapshots'::regclass "
            f"AND conname IN ({names_sql}) "
            "AND contype = 'c'"
        )
    )
    rows = list(constraints)
    if not _constraint_definitions_need_refresh(
        [row[1] for row in rows],
        SNAPSHOT_KIND_VALUES,
    ):
        return

    for name in SNAPSHOT_KIND_CHECK_NAMES:
        await conn.execute(
            text(
                "ALTER TABLE review.investment_snapshots "
                f"DROP CONSTRAINT IF EXISTS {_quote_ident(name)}"
            )
        )
    await conn.execute(
        text(
            "ALTER TABLE review.investment_snapshots "
            f"ADD CONSTRAINT {SNAPSHOT_KIND_CHECK_NAME} "
            f"{_check_constraint_sql('snapshot_kind', SNAPSHOT_KIND_VALUES)}"
        )
    )


async def _ensure_trade_forecast_status_constraint(conn) -> None:
    names_sql = ",".join(f"'{name}'" for name in TRADE_FORECAST_STATUS_CHECK_NAMES)
    constraints = await conn.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) AS definition "
            "FROM pg_constraint "
            "WHERE conrelid = 'review.trade_forecasts'::regclass "
            f"AND conname IN ({names_sql}) "
            "AND contype = 'c'"
        )
    )
    rows = list(constraints)
    if not _constraint_definitions_need_refresh(
        [row[1] for row in rows],
        TRADE_FORECAST_STATUS_VALUES,
    ):
        return

    for name in TRADE_FORECAST_STATUS_CHECK_NAMES:
        await conn.execute(
            text(
                "ALTER TABLE review.trade_forecasts "
                f"DROP CONSTRAINT IF EXISTS {_quote_ident(name)}"
            )
        )
    await conn.execute(
        text(
            "ALTER TABLE review.trade_forecasts "
            f"ADD CONSTRAINT {TRADE_FORECAST_STATUS_CHECK_NAME} "
            f"{_check_constraint_sql('status', TRADE_FORECAST_STATUS_VALUES)}"
        )
    )


# --------------------------------------------------------------------------- #
# Idempotent, unconditional DDL — UNION of the two original fixtures.        #
# Every statement here is safe to run on both a fresh and a persistent DB.    #
# Conditional "ALTER only when genuinely missing" probes live as code in      #
# apply_test_schema() (they need a catalog probe so an unconditional form     #
# would force an AccessExclusive lock on hot tables).                         #
# --------------------------------------------------------------------------- #
_DDL_STATEMENTS: tuple[str, ...] = (
    # ---- market_events / us_symbol_universe ----
    "ALTER TABLE market_events ADD COLUMN IF NOT EXISTS currency TEXT",
    "ALTER TABLE us_symbol_universe ADD COLUMN IF NOT EXISTS is_common_stock BOOLEAN",
    # ---- analysis_artifacts ----
    "ALTER TABLE review.analysis_artifacts ADD COLUMN IF NOT EXISTS correlation_id TEXT",
    "ALTER TABLE review.analysis_artifacts ADD COLUMN IF NOT EXISTS account_scope TEXT",
    "ALTER TABLE review.analysis_artifacts ADD COLUMN IF NOT EXISTS content_hash TEXT",
    "ALTER TABLE review.analysis_artifacts "
    "ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE review.analysis_artifacts ADD COLUMN IF NOT EXISTS readiness_label TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_analysis_artifacts_correlation_id "
    "ON review.analysis_artifacts (correlation_id)",
    # readiness_label CHECK (wrapped in DO $$ because Postgres has no
    # ADD CONSTRAINT IF NOT EXISTS; the body is unconditional so it can
    # belong here).
    "DO $$ BEGIN "
    "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname = 'ck_analysis_artifacts_readiness_label') "
    "THEN ALTER TABLE review.analysis_artifacts "
    "ADD CONSTRAINT ck_analysis_artifacts_readiness_label "
    "CHECK (readiness_label IS NULL OR readiness_label IN ("
    "'screen_grade','not_decision_ready',"
    "'ready_for_order_review','blocked')); "
    "END IF; END $$",
    # ---- kis_live_order_ledger + live_order_ledger approval_hash / idempotency_key ----
    "ALTER TABLE review.kis_live_order_ledger ADD COLUMN IF NOT EXISTS approval_hash TEXT",
    "ALTER TABLE review.kis_live_order_ledger ADD COLUMN IF NOT EXISTS idempotency_key TEXT",
    "ALTER TABLE review.live_order_ledger ADD COLUMN IF NOT EXISTS approval_hash TEXT",
    "ALTER TABLE review.live_order_ledger ADD COLUMN IF NOT EXISTS idempotency_key TEXT",
    # ---- ROB-800: send-time exit_intent (loss_cut) on live/kis-live ledgers ----
    "ALTER TABLE review.kis_live_order_ledger ADD COLUMN IF NOT EXISTS exit_intent TEXT",
    "ALTER TABLE review.live_order_ledger ADD COLUMN IF NOT EXISTS exit_intent TEXT",
    # ---- invest_kr_fundamentals_snapshots (ROB-430) ----
    "ALTER TABLE invest_kr_fundamentals_snapshots ADD COLUMN IF NOT EXISTS week_high_52_date DATE",
    # ---- report snapshot metadata + diagnostics (review.investment_reports) ----
    "ALTER TABLE review.investment_reports ADD COLUMN IF NOT EXISTS snapshot_bundle_uuid UUID",
    "ALTER TABLE review.investment_reports ADD COLUMN IF NOT EXISTS snapshot_policy_version TEXT",
    "ALTER TABLE review.investment_reports ADD COLUMN IF NOT EXISTS snapshot_coverage_summary JSONB",
    "ALTER TABLE review.investment_reports ADD COLUMN IF NOT EXISTS snapshot_freshness_summary JSONB",
    "ALTER TABLE review.investment_reports ADD COLUMN IF NOT EXISTS source_conflicts JSONB",
    "ALTER TABLE review.investment_reports ADD COLUMN IF NOT EXISTS unavailable_sources JSONB",
    "ALTER TABLE review.investment_reports ADD COLUMN IF NOT EXISTS snapshot_report_diagnostics JSONB",
    "CREATE INDEX IF NOT EXISTS ix_investment_reports_snapshot_bundle_uuid "
    "ON review.investment_reports (snapshot_bundle_uuid)",
    # ROB-269 Phase 3 — hard-stale published-on guard
    "ALTER TABLE review.investment_reports DROP CONSTRAINT IF EXISTS "
    "ck_investment_reports_no_published_on_hard_stale",
    "ALTER TABLE review.investment_reports ADD CONSTRAINT "
    "ck_investment_reports_no_published_on_hard_stale "
    "CHECK ("
    "status <> 'published' "
    "OR snapshot_freshness_summary IS NULL "
    "OR ("
    "(snapshot_freshness_summary->>'overall') IS NOT NULL "
    "AND (snapshot_freshness_summary->>'overall') IN "
    "('fresh','soft_stale','partial')"
    "))",
    # ---- investment_report_items proposal-state columns + CHECKs (ROB-274) ----
    "ALTER TABLE review.investment_report_items ADD COLUMN IF NOT EXISTS operation TEXT",
    "ALTER TABLE review.investment_report_items ADD COLUMN IF NOT EXISTS target_ref JSONB",
    "ALTER TABLE review.investment_report_items ADD COLUMN IF NOT EXISTS current_state JSONB",
    "ALTER TABLE review.investment_report_items ADD COLUMN IF NOT EXISTS proposed_state JSONB",
    "ALTER TABLE review.investment_report_items ADD COLUMN IF NOT EXISTS diff JSONB",
    "ALTER TABLE review.investment_report_items ADD COLUMN IF NOT EXISTS apply_policy TEXT",
    "ALTER TABLE review.investment_report_items ADD COLUMN IF NOT EXISTS decision_bucket TEXT",
    "ALTER TABLE review.investment_report_items ADD COLUMN IF NOT EXISTS cited_symbol_report_uuid UUID",
    "ALTER TABLE review.investment_report_items "
    "ADD COLUMN IF NOT EXISTS cited_dimension_report_uuids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[]",
    "ALTER TABLE review.investment_report_items "
    "ADD COLUMN IF NOT EXISTS cited_snapshot_uuids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[]",
    "ALTER TABLE review.investment_report_items DROP CONSTRAINT IF EXISTS "
    "ck_investment_report_items_ck_investment_report_items_decision_bucket",
    "ALTER TABLE review.investment_report_items DROP CONSTRAINT IF EXISTS "
    "ck_investment_report_items_decision_bucket",
    "ALTER TABLE review.investment_report_items "
    "ADD CONSTRAINT ck_investment_report_items_decision_bucket "
    "CHECK ("
    "decision_bucket IS NULL OR decision_bucket IN ("
    "'new_buy_candidate','open_action','completed_or_existing','deferred_no_action','risk_watch'"
    "))",
    "CREATE INDEX IF NOT EXISTS ix_investment_report_items_operation_kind "
    "ON review.investment_report_items (operation, item_kind, status)",
    "ALTER TABLE review.investment_report_items DROP CONSTRAINT IF EXISTS "
    "ck_investment_report_items_operation",
    "ALTER TABLE review.investment_report_items "
    "ADD CONSTRAINT ck_investment_report_items_operation "
    "CHECK ("
    "operation IS NULL OR operation IN ("
    "'create','modify','cancel','keep','replace','review'"
    "))",
    "ALTER TABLE review.investment_report_items DROP CONSTRAINT IF EXISTS "
    "ck_investment_report_items_apply_policy",
    "ALTER TABLE review.investment_report_items "
    "ADD CONSTRAINT ck_investment_report_items_apply_policy "
    "CHECK ("
    "apply_policy IS NULL "
    "OR apply_policy = 'requires_user_approval'"
    ")",
    # Watch-invariant CHECKs (ROB-274): drop canonical + hashed names then recreate
    "ALTER TABLE review.investment_report_items DROP CONSTRAINT IF EXISTS "
    '"ck_investment_report_items_ck_investment_report_items_w_421e"',
    "ALTER TABLE review.investment_report_items DROP CONSTRAINT IF EXISTS "
    "ck_investment_report_items_watch_has_condition",
    "ALTER TABLE review.investment_report_items "
    "ADD CONSTRAINT ck_investment_report_items_watch_has_condition "
    "CHECK ("
    "item_kind <> 'watch' "
    "OR operation IN ('cancel','keep','review') "
    "OR watch_condition IS NOT NULL"
    ")",
    "ALTER TABLE review.investment_report_items DROP CONSTRAINT IF EXISTS "
    '"ck_investment_report_items_ck_investment_report_items_w_fdaa"',
    "ALTER TABLE review.investment_report_items DROP CONSTRAINT IF EXISTS "
    "ck_investment_report_items_watch_has_expiry",
    "ALTER TABLE review.investment_report_items "
    "ADD CONSTRAINT ck_investment_report_items_watch_has_expiry "
    "CHECK ("
    "item_kind <> 'watch' "
    "OR operation IN ('cancel','keep','review') "
    "OR valid_until IS NOT NULL"
    ")",
    # ---- ROB-455 decision-verb CHECK on investment_report_item_decisions ----
    # Unique to the helper session fixture before ROB-723; carries cancel/reprice.
    "ALTER TABLE review.investment_report_item_decisions DROP CONSTRAINT IF EXISTS "
    "ck_investment_report_item_decisions_ck_investment_repor_9aa6",
    "ALTER TABLE review.investment_report_item_decisions DROP CONSTRAINT IF EXISTS "
    "ck_investment_report_item_decisions_decision",
    "ALTER TABLE review.investment_report_item_decisions "
    "ADD CONSTRAINT ck_investment_report_item_decisions_decision "
    "CHECK (decision IN ('approve','deny','defer','skip',"
    "'partial_approve','cancel','reprice'))",
    # ---- kis_mock_order_ledger (ROB-321 + ROB-406) ----
    "ALTER TABLE review.kis_mock_order_ledger ADD COLUMN IF NOT EXISTS correlation_id TEXT",
    "ALTER TABLE review.kis_mock_order_ledger ADD COLUMN IF NOT EXISTS scalping_role TEXT",
    "ALTER TABLE review.kis_mock_order_ledger ADD COLUMN IF NOT EXISTS exit_reason TEXT",
    "ALTER TABLE review.kis_mock_order_ledger ADD COLUMN IF NOT EXISTS gross_pnl NUMERIC(20, 4)",
    "ALTER TABLE review.kis_mock_order_ledger ADD COLUMN IF NOT EXISTS net_pnl NUMERIC(20, 4)",
    "ALTER TABLE review.kis_mock_order_ledger DROP CONSTRAINT IF EXISTS "
    "kis_mock_ledger_lifecycle_state_allowed",
    "ALTER TABLE review.kis_mock_order_ledger DROP CONSTRAINT IF EXISTS "
    "ck_kis_mock_order_ledger_kis_mock_ledger_lifecycle_stat_8e10",
    "ALTER TABLE review.kis_mock_order_ledger "
    "ADD CONSTRAINT ck_kis_mock_order_ledger_kis_mock_ledger_lifecycle_stat_8e10 "
    "CHECK (lifecycle_state IN ("
    "'planned','previewed','submitted','accepted','pending',"
    "'fill','reconciled','stale','failed','anomaly','cancelled'"
    "))",
    # ---- investment_watch_alerts (ROB-403) ----
    "ALTER TABLE review.investment_watch_alerts ADD COLUMN IF NOT EXISTS threshold_high NUMERIC(20,8)",
    "ALTER TABLE review.investment_watch_alerts ADD COLUMN IF NOT EXISTS conditions JSONB "
    "NOT NULL DEFAULT '[]'::jsonb",
    "ALTER TABLE review.investment_watch_alerts ADD COLUMN IF NOT EXISTS combine TEXT "
    "NOT NULL DEFAULT 'and'",
    "ALTER TABLE review.investment_watch_alerts DROP CONSTRAINT IF EXISTS "
    "ck_investment_watch_alerts_operator",
    "ALTER TABLE review.investment_watch_alerts DROP CONSTRAINT IF EXISTS "
    "ck_investment_watch_alerts_ck_investment_watch_alerts_operator",
    "ALTER TABLE review.investment_watch_alerts "
    "ADD CONSTRAINT ck_investment_watch_alerts_operator "
    "CHECK (operator IN ('above','below','between'))",
    "ALTER TABLE review.investment_watch_alerts DROP CONSTRAINT IF EXISTS "
    "ck_investment_watch_alerts_combine",
    "ALTER TABLE review.investment_watch_alerts DROP CONSTRAINT IF EXISTS "
    "ck_investment_watch_alerts_ck_investment_watch_alerts_combine",
    "ALTER TABLE review.investment_watch_alerts "
    "ADD CONSTRAINT ck_investment_watch_alerts_combine "
    "CHECK (combine IN ('and'))",
    # ---- investment_watch_events (ROB-403 / ROB-402) ----
    "ALTER TABLE review.investment_watch_events ADD COLUMN IF NOT EXISTS threshold_high NUMERIC(20,8)",
    "ALTER TABLE review.investment_watch_events DROP CONSTRAINT IF EXISTS "
    "ck_investment_watch_events_operator",
    "ALTER TABLE review.investment_watch_events DROP CONSTRAINT IF EXISTS "
    "ck_investment_watch_events_ck_investment_watch_events_operator",
    "ALTER TABLE review.investment_watch_events "
    "ADD CONSTRAINT ck_investment_watch_events_operator "
    "CHECK (operator IN ('above','below','between'))",
    # investment_watch_alerts.action_mode
    "ALTER TABLE review.investment_watch_alerts DROP CONSTRAINT IF EXISTS "
    "ck_investment_watch_alerts_action_mode",
    "ALTER TABLE review.investment_watch_alerts DROP CONSTRAINT IF EXISTS "
    "ck_investment_watch_alerts_ck_investment_watch_alerts_action_mode",
    "ALTER TABLE review.investment_watch_alerts DROP CONSTRAINT IF EXISTS "
    "ck_investment_watch_alerts_ck_investment_watch_alerts_a_646d",
    "ALTER TABLE review.investment_watch_alerts "
    "ADD CONSTRAINT ck_investment_watch_alerts_action_mode "
    "CHECK (action_mode IN ('notify_only','preview_only',"
    "'approval_required','auto_execute_mock'))",
    # investment_watch_events.action_mode (drop both hashed names + canonical)
    "ALTER TABLE review.investment_watch_events DROP CONSTRAINT IF EXISTS "
    "ck_investment_watch_events_action_mode",
    "ALTER TABLE review.investment_watch_events DROP CONSTRAINT IF EXISTS "
    "ck_investment_watch_events_ck_investment_watch_events_action_mode",
    "ALTER TABLE review.investment_watch_events DROP CONSTRAINT IF EXISTS "
    "ck_investment_watch_events_ck_investment_watch_events_a_05f0",
    "ALTER TABLE review.investment_watch_events DROP CONSTRAINT IF EXISTS "
    "ck_investment_watch_events_ck_investment_watch_events_ac_6a20",
    "ALTER TABLE review.investment_watch_events "
    "ADD CONSTRAINT ck_investment_watch_events_action_mode "
    "CHECK (action_mode IN ('notify_only','preview_only',"
    "'approval_required','auto_execute_mock'))",
    # investment_watch_events.outcome
    "ALTER TABLE review.investment_watch_events DROP CONSTRAINT IF EXISTS "
    "ck_investment_watch_events_outcome",
    "ALTER TABLE review.investment_watch_events DROP CONSTRAINT IF EXISTS "
    "ck_investment_watch_events_ck_investment_watch_events_outcome",
    "ALTER TABLE review.investment_watch_events "
    "ADD CONSTRAINT ck_investment_watch_events_outcome "
    "CHECK (outcome IN ('notified','review_required','preview_attached',"
    "'executed','expired','ignored','failed'))",
    # ---- trade_journals (ROB-405 / ROB-568) ----
    "ALTER TABLE review.trade_journals ADD COLUMN IF NOT EXISTS correlation_id TEXT",
    "ALTER TABLE review.trade_journals DROP CONSTRAINT IF EXISTS trade_journals_account_type",
    "ALTER TABLE review.trade_journals DROP CONSTRAINT IF EXISTS "
    "ck_trade_journals_trade_journals_account_type",
    "ALTER TABLE review.trade_journals "
    "ADD CONSTRAINT trade_journals_account_type "
    "CHECK (account_type IN ('live','paper','mock'))",
    # ---- report_item_uuid on live order ledgers (ROB-473) ----
    "ALTER TABLE review.kis_live_order_ledger ADD COLUMN IF NOT EXISTS report_item_uuid UUID",
    "ALTER TABLE review.live_order_ledger ADD COLUMN IF NOT EXISTS report_item_uuid UUID",
    "CREATE INDEX IF NOT EXISTS ix_kis_live_ledger_report_item_uuid "
    "ON review.kis_live_order_ledger (report_item_uuid)",
    "CREATE INDEX IF NOT EXISTS ix_live_ledger_report_item_uuid "
    "ON review.live_order_ledger (report_item_uuid)",
    # ---- ROB-568 US FX PnL fields (trade_journals + ledgers + retrospectives) ----
    "ALTER TABLE review.trade_journals ADD COLUMN IF NOT EXISTS buy_fx_rate NUMERIC(18, 4)",
    "ALTER TABLE review.trade_journals ADD COLUMN IF NOT EXISTS sell_fx_rate NUMERIC(18, 4)",
    "ALTER TABLE review.trade_journals ADD COLUMN IF NOT EXISTS fx_pnl_krw NUMERIC(20, 4)",
    "ALTER TABLE review.trade_journals ADD COLUMN IF NOT EXISTS security_pnl_usd NUMERIC(20, 4)",
    "ALTER TABLE review.trade_journals ADD COLUMN IF NOT EXISTS security_pnl_krw NUMERIC(20, 4)",
    "ALTER TABLE review.trade_journals ADD COLUMN IF NOT EXISTS total_pnl_krw NUMERIC(20, 4)",
    "ALTER TABLE review.trade_journals ADD COLUMN IF NOT EXISTS fx_rate_source TEXT",
    "ALTER TABLE review.trade_journals ADD COLUMN IF NOT EXISTS fx_pnl_accuracy TEXT",
    "ALTER TABLE review.live_order_ledger ADD COLUMN IF NOT EXISTS buy_fx_rate NUMERIC(18, 4)",
    "ALTER TABLE review.live_order_ledger ADD COLUMN IF NOT EXISTS sell_fx_rate NUMERIC(18, 4)",
    "ALTER TABLE review.live_order_ledger ADD COLUMN IF NOT EXISTS fx_pnl_krw NUMERIC(20, 4)",
    "ALTER TABLE review.live_order_ledger ADD COLUMN IF NOT EXISTS security_pnl_usd NUMERIC(20, 4)",
    "ALTER TABLE review.live_order_ledger ADD COLUMN IF NOT EXISTS security_pnl_krw NUMERIC(20, 4)",
    "ALTER TABLE review.live_order_ledger ADD COLUMN IF NOT EXISTS total_pnl_krw NUMERIC(20, 4)",
    "ALTER TABLE review.live_order_ledger ADD COLUMN IF NOT EXISTS fx_rate_source TEXT",
    "ALTER TABLE review.live_order_ledger ADD COLUMN IF NOT EXISTS fx_pnl_accuracy TEXT",
    "ALTER TABLE review.toss_live_order_ledger ADD COLUMN IF NOT EXISTS buy_fx_rate NUMERIC(18, 4)",
    "ALTER TABLE review.toss_live_order_ledger ADD COLUMN IF NOT EXISTS sell_fx_rate NUMERIC(18, 4)",
    "ALTER TABLE review.toss_live_order_ledger ADD COLUMN IF NOT EXISTS fx_pnl_krw NUMERIC(20, 4)",
    "ALTER TABLE review.toss_live_order_ledger ADD COLUMN IF NOT EXISTS security_pnl_usd NUMERIC(20, 4)",
    "ALTER TABLE review.toss_live_order_ledger ADD COLUMN IF NOT EXISTS security_pnl_krw NUMERIC(20, 4)",
    "ALTER TABLE review.toss_live_order_ledger ADD COLUMN IF NOT EXISTS total_pnl_krw NUMERIC(20, 4)",
    "ALTER TABLE review.toss_live_order_ledger ADD COLUMN IF NOT EXISTS fx_rate_source TEXT",
    "ALTER TABLE review.toss_live_order_ledger ADD COLUMN IF NOT EXISTS fx_pnl_accuracy TEXT",
    "ALTER TABLE review.trade_retrospectives ADD COLUMN IF NOT EXISTS buy_fx_rate NUMERIC(18, 4)",
    "ALTER TABLE review.trade_retrospectives ADD COLUMN IF NOT EXISTS sell_fx_rate NUMERIC(18, 4)",
    "ALTER TABLE review.trade_retrospectives ADD COLUMN IF NOT EXISTS fx_pnl_krw NUMERIC(20, 4)",
    "ALTER TABLE review.trade_retrospectives ADD COLUMN IF NOT EXISTS security_pnl_usd NUMERIC(20, 4)",
    "ALTER TABLE review.trade_retrospectives ADD COLUMN IF NOT EXISTS security_pnl_krw NUMERIC(20, 4)",
    "ALTER TABLE review.trade_retrospectives ADD COLUMN IF NOT EXISTS total_pnl_krw NUMERIC(20, 4)",
    "ALTER TABLE review.trade_retrospectives ADD COLUMN IF NOT EXISTS fx_rate_source TEXT",
    "ALTER TABLE review.trade_retrospectives ADD COLUMN IF NOT EXISTS fx_pnl_accuracy TEXT",
    # ---- trade_retrospectives account_mode CHECK ----
    "ALTER TABLE review.trade_retrospectives DROP CONSTRAINT IF EXISTS "
    "ck_trade_retrospectives_account_mode",
    "ALTER TABLE review.trade_retrospectives DROP CONSTRAINT IF EXISTS "
    "ck_trade_retrospectives_ck_trade_retrospectives_account_mode",
    "ALTER TABLE review.trade_retrospectives "
    "ADD CONSTRAINT ck_trade_retrospectives_account_mode "
    "CHECK (account_mode IN ('kis_mock','kiwoom_mock','kis_live','toss_live',"
    "'alpaca_paper','upbit_live','paper'))",
    # ---- ROB-647 postmortem columns + CHECKs ----
    "ALTER TABLE review.trade_retrospectives ADD COLUMN IF NOT EXISTS trigger_type TEXT",
    "ALTER TABLE review.trade_retrospectives ADD COLUMN IF NOT EXISTS root_cause_class TEXT",
    "ALTER TABLE review.trade_retrospectives ADD COLUMN IF NOT EXISTS intended_vs_happened JSONB",
    "ALTER TABLE review.trade_retrospectives ADD COLUMN IF NOT EXISTS next_actions JSONB",
    "ALTER TABLE review.trade_retrospectives ADD COLUMN IF NOT EXISTS guardrail_fired TEXT",
    "ALTER TABLE review.trade_retrospectives ADD COLUMN IF NOT EXISTS policy_version TEXT",
    "ALTER TABLE review.trade_retrospectives DROP CONSTRAINT IF EXISTS "
    "ck_trade_retrospectives_trigger_type",
    "ALTER TABLE review.trade_retrospectives "
    "ADD CONSTRAINT ck_trade_retrospectives_trigger_type "
    "CHECK (trigger_type IS NULL OR trigger_type IN ("
    "'fill','partial_fill','rejected_order','cancelled','expired',"
    "'thesis_change','policy_violation','stale_evidence',"
    "'guardrail_block','stop_loss'))",
    "ALTER TABLE review.trade_retrospectives DROP CONSTRAINT IF EXISTS "
    "ck_trade_retrospectives_root_cause_class",
    "ALTER TABLE review.trade_retrospectives "
    "ADD CONSTRAINT ck_trade_retrospectives_root_cause_class "
    "CHECK (root_cause_class IS NULL OR root_cause_class IN ("
    "'user_input','analysis','policy','execution','harness'))",
    # ---- ROB-705 paper provenance (paper.paper_trades / paper_pending_orders) ----
    "ALTER TABLE paper.paper_trades ADD COLUMN IF NOT EXISTS correlation_id TEXT",
    "ALTER TABLE paper.paper_trades ADD COLUMN IF NOT EXISTS journal_id BIGINT",
    "ALTER TABLE paper.paper_trades ADD COLUMN IF NOT EXISTS artifact_uuid TEXT",
    "ALTER TABLE paper.paper_trades ADD COLUMN IF NOT EXISTS forecast_id TEXT",
    "ALTER TABLE paper.paper_pending_orders ADD COLUMN IF NOT EXISTS correlation_id TEXT",
    "ALTER TABLE paper.paper_pending_orders ADD COLUMN IF NOT EXISTS journal_id BIGINT",
    "ALTER TABLE paper.paper_pending_orders ADD COLUMN IF NOT EXISTS artifact_uuid TEXT",
    "ALTER TABLE paper.paper_pending_orders ADD COLUMN IF NOT EXISTS forecast_id TEXT",
    # ---- benchmark_return_bps on scalping_daily_reviews (B-1) ----
    "ALTER TABLE scalping_daily_reviews ADD COLUMN IF NOT EXISTS benchmark_return_bps NUMERIC(12, 4)",
    # ---- ROB-734 mirror counterfactual metadata ----
    "ALTER TABLE review.kis_mock_order_ledger ADD COLUMN IF NOT EXISTS report_item_uuid UUID",
    "ALTER TABLE review.kis_mock_order_ledger ADD COLUMN IF NOT EXISTS mirror_cohort TEXT",
    "ALTER TABLE review.kis_mock_order_ledger ADD COLUMN IF NOT EXISTS mirror_source_bucket TEXT",
    "CREATE INDEX IF NOT EXISTS ix_kis_mock_ledger_report_item_uuid ON review.kis_mock_order_ledger (report_item_uuid)",
    "CREATE INDEX IF NOT EXISTS ix_kis_mock_ledger_mirror_cohort_created ON review.kis_mock_order_ledger (mirror_cohort, created_at)",
    "ALTER TABLE review.kis_mock_order_ledger DROP CONSTRAINT IF EXISTS ck_kis_mock_ledger_mirror_cohort",
    "ALTER TABLE review.kis_mock_order_ledger ADD CONSTRAINT ck_kis_mock_ledger_mirror_cohort CHECK (mirror_cohort IS NULL OR mirror_cohort IN ('mock_counterfactual'))",
    "ALTER TABLE review.kis_mock_order_ledger DROP CONSTRAINT IF EXISTS ck_kis_mock_ledger_mirror_source_bucket",
    "ALTER TABLE review.kis_mock_order_ledger ADD CONSTRAINT ck_kis_mock_ledger_mirror_source_bucket CHECK (mirror_source_bucket IS NULL OR mirror_source_bucket IN ('place_original','watch_trigger','deferred_min_rung'))",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_kis_mock_mirror_report_item_once ON review.kis_mock_order_ledger (mirror_cohort, report_item_uuid) WHERE mirror_cohort = 'mock_counterfactual' AND report_item_uuid IS NOT NULL",
    "ALTER TABLE review.trade_retrospectives DROP CONSTRAINT IF EXISTS uq_trade_retrospectives_correlation_id",
    "ALTER TABLE review.trade_retrospectives DROP CONSTRAINT IF EXISTS uq_trade_retrospectives_correlation_account",
    "ALTER TABLE review.trade_retrospectives ADD CONSTRAINT uq_trade_retrospectives_correlation_account UNIQUE (correlation_id, account_mode)",
    "ALTER TABLE review.order_proposals ADD COLUMN IF NOT EXISTS exit_intent TEXT",
    "ALTER TABLE review.order_proposals ADD COLUMN IF NOT EXISTS exit_reason TEXT",
    "ALTER TABLE review.order_proposals ADD COLUMN IF NOT EXISTS retrospective_id BIGINT",
    "ALTER TABLE review.order_proposals ADD COLUMN IF NOT EXISTS approval_issue_id TEXT",
    # ---- ROB-832: replace/cancel proposal group columns ----
    "ALTER TABLE review.order_proposals ADD COLUMN IF NOT EXISTS action TEXT",
    "ALTER TABLE review.order_proposals "
    "ADD COLUMN IF NOT EXISTS target_broker_order_id TEXT",
    "DO $$ BEGIN "
    "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname = 'order_proposals_action' "
    "AND conrelid = 'review.order_proposals'::regclass) "
    "THEN ALTER TABLE review.order_proposals "
    "ADD CONSTRAINT order_proposals_action "
    "CHECK (action IS NULL OR action IN ('place','replace','cancel')); "
    "END IF; END $$",
    # ---- ROB-846: append-only trial-child columns on research.backtest_runs +
    # run/config/data hash linkage on research.promotion_candidates. create_all
    # skips existing tables, so a persistent test DB needs these ALTERs (they
    # are no-ops on a fresh DB where create_all already built the ORM shape).
    "ALTER TABLE research.backtest_runs "
    "ADD COLUMN IF NOT EXISTS strategy_experiment_id BIGINT",
    "ALTER TABLE research.backtest_runs ADD COLUMN IF NOT EXISTS trial_index INTEGER",
    "ALTER TABLE research.backtest_runs ADD COLUMN IF NOT EXISTS seed BIGINT",
    "ALTER TABLE research.backtest_runs "
    "ADD COLUMN IF NOT EXISTS information_cutoff TIMESTAMPTZ",
    "ALTER TABLE research.backtest_runs "
    "ADD COLUMN IF NOT EXISTS trial_status VARCHAR(16)",
    "ALTER TABLE research.backtest_runs "
    "ADD COLUMN IF NOT EXISTS gate_artifact_hash VARCHAR(64)",
    "ALTER TABLE research.backtest_runs "
    "ADD COLUMN IF NOT EXISTS trial_idempotency_key VARCHAR(128)",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='fk_research_backtest_runs_experiment_id' "
    "AND conrelid='research.backtest_runs'::regclass) THEN "
    "ALTER TABLE research.backtest_runs "
    "ADD CONSTRAINT fk_research_backtest_runs_experiment_id "
    "FOREIGN KEY (strategy_experiment_id) "
    "REFERENCES research.strategy_experiments(id) ON DELETE RESTRICT; "
    "END IF; END $$",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='uq_research_backtest_runs_experiment_trial_index' "
    "AND conrelid='research.backtest_runs'::regclass) THEN "
    "ALTER TABLE research.backtest_runs "
    "ADD CONSTRAINT uq_research_backtest_runs_experiment_trial_index "
    "UNIQUE (strategy_experiment_id, trial_index); END IF; END $$",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='uq_research_backtest_runs_experiment_idempotency' "
    "AND conrelid='research.backtest_runs'::regclass) THEN "
    "ALTER TABLE research.backtest_runs "
    "ADD CONSTRAINT uq_research_backtest_runs_experiment_idempotency "
    "UNIQUE (strategy_experiment_id, trial_idempotency_key); END IF; END $$",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conrelid='research.backtest_runs'::regclass AND contype='c' "
    "AND pg_get_constraintdef(oid) LIKE '%trial_status%') THEN "
    "ALTER TABLE research.backtest_runs "
    "ADD CONSTRAINT ck_research_backtest_runs_trial_status "
    "CHECK (trial_status IS NULL OR trial_status IN "
    "('completed','rejected','crashed','timeout')); END IF; END $$",
    "CREATE INDEX IF NOT EXISTS ix_research_backtest_runs_experiment "
    "ON research.backtest_runs (strategy_experiment_id, trial_index)",
    # trial all-or-none integrity (NOT VALID: enforce new rows, skip legacy)
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_research_backtest_runs_trial_all_or_none' "
    "AND conrelid='research.backtest_runs'::regclass) THEN "
    "ALTER TABLE research.backtest_runs "
    "ADD CONSTRAINT ck_research_backtest_runs_trial_all_or_none CHECK ("
    "(strategy_experiment_id IS NULL AND trial_index IS NULL "
    "AND trial_status IS NULL AND trial_idempotency_key IS NULL "
    "AND seed IS NULL AND information_cutoff IS NULL "
    "AND gate_artifact_hash IS NULL) "
    "OR (strategy_experiment_id IS NOT NULL AND trial_index IS NOT NULL "
    "AND trial_status IS NOT NULL)) NOT VALID; END IF; END $$",
    "ALTER TABLE research.promotion_candidates "
    "ADD COLUMN IF NOT EXISTS experiment_id VARCHAR(64)",
    "ALTER TABLE research.promotion_candidates "
    "ADD COLUMN IF NOT EXISTS run_config_hash VARCHAR(64)",
    "ALTER TABLE research.promotion_candidates "
    "ADD COLUMN IF NOT EXISTS run_data_hash VARCHAR(64)",
    # AC#5: new promotion candidates must carry full identity (NOT VALID keeps
    # any legacy null-identity rows for compat, blocks new incomplete writes)
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_research_promotion_candidates_identity_complete' "
    "AND conrelid='research.promotion_candidates'::regclass) THEN "
    "ALTER TABLE research.promotion_candidates "
    "ADD CONSTRAINT ck_research_promotion_candidates_identity_complete CHECK ("
    "experiment_id IS NOT NULL AND run_config_hash IS NOT NULL "
    "AND run_data_hash IS NOT NULL) NOT VALID; END IF; END $$",
    # ---- ROB-846: append-only immutability triggers (mirror of the migration)
    # research.strategy_experiments rows are fully immutable; research.backtest_runs
    # rows are immutable only when they are trials (strategy_experiment_id NOT NULL),
    # so legacy summary upserts still work.
    "CREATE OR REPLACE FUNCTION research.reject_strategy_experiment_mutation() "
    "RETURNS trigger AS $$ BEGIN "
    "RAISE EXCEPTION "
    "'research.strategy_experiments is append-only/immutable; % rejected', TG_OP "
    "USING ERRCODE = 'restrict_violation'; "
    "END; $$ LANGUAGE plpgsql",
    "DROP TRIGGER IF EXISTS trg_strategy_experiments_immutable "
    "ON research.strategy_experiments",
    "CREATE TRIGGER trg_strategy_experiments_immutable "
    "BEFORE UPDATE OR DELETE ON research.strategy_experiments "
    "FOR EACH ROW EXECUTE FUNCTION research.reject_strategy_experiment_mutation()",
    "CREATE OR REPLACE FUNCTION research.reject_backtest_trial_mutation() "
    "RETURNS trigger AS $$ BEGIN "
    "IF TG_OP = 'DELETE' THEN "
    "IF OLD.strategy_experiment_id IS NOT NULL THEN "
    "RAISE EXCEPTION "
    "'research.backtest_runs trial rows are append-only; DELETE rejected on id=%', "
    "OLD.id USING ERRCODE = 'restrict_violation'; "
    "END IF; RETURN OLD; "
    "END IF; "
    "IF OLD.strategy_experiment_id IS NOT NULL "
    "OR NEW.strategy_experiment_id IS NOT NULL THEN "
    "RAISE EXCEPTION "
    "'research.backtest_runs trial rows are append-only; UPDATE/convert rejected on id=%', "
    "OLD.id USING ERRCODE = 'restrict_violation'; "
    "END IF; "
    "RETURN NEW; "
    "END; $$ LANGUAGE plpgsql",
    "DROP TRIGGER IF EXISTS trg_backtest_runs_trial_immutable "
    "ON research.backtest_runs",
    "CREATE TRIGGER trg_backtest_runs_trial_immutable "
    "BEFORE UPDATE OR DELETE ON research.backtest_runs "
    "FOR EACH ROW EXECUTE FUNCTION research.reject_backtest_trial_mutation()",
    # ---- ROB-848: immutable paper-validation audit + experiment hash binding
    # Tables/checks/FKs are owned by the ORM metadata above; these PostgreSQL
    # trigger functions are non-ORM DDL and mirror the Alembic revision.
    "CREATE OR REPLACE FUNCTION "
    "research.reject_paper_validation_audit_mutation() "
    "RETURNS trigger AS $$ BEGIN "
    "RAISE EXCEPTION "
    "'research.% is append-only/immutable; % rejected', TG_TABLE_NAME, TG_OP "
    "USING ERRCODE = 'restrict_violation'; "
    "END; $$ LANGUAGE plpgsql",
    "ALTER TABLE research.paper_validation_state_transitions "
    "ADD COLUMN IF NOT EXISTS input_bundle_id VARCHAR(128) "
    "NOT NULL DEFAULT 'bootstrap-legacy'",
    "ALTER TABLE research.paper_validation_state_transitions "
    "ALTER COLUMN input_bundle_id DROP DEFAULT",
    "ALTER TABLE research.paper_validation_state_transitions "
    "ADD COLUMN IF NOT EXISTS policy_version VARCHAR(128) "
    "NOT NULL DEFAULT 'bootstrap-legacy'",
    "ALTER TABLE research.paper_validation_state_transitions "
    "ALTER COLUMN policy_version DROP DEFAULT",
    "CREATE OR REPLACE FUNCTION "
    "research.validate_paper_validation_experiment_identity() "
    "RETURNS trigger AS $$ DECLARE "
    "registered research.strategy_experiments%ROWTYPE; "
    "BEGIN SELECT * INTO registered FROM research.strategy_experiments "
    "WHERE experiment_id = NEW.experiment_id; "
    "IF NOT FOUND THEN RAISE EXCEPTION "
    "'paper validation experiment % is not registered', NEW.experiment_id "
    "USING ERRCODE = 'foreign_key_violation'; END IF; "
    "IF NEW.experiment_hash <> NEW.experiment_id "
    "OR NEW.strategy_version_id <> registered.strategy_version "
    "OR NEW.strategy_hash <> registered.strategy_hash "
    "OR NEW.config_hash <> registered.frozen_config_hash "
    "OR NEW.policy_hash <> registered.policy_hash THEN "
    "RAISE EXCEPTION 'paper validation experiment identity mismatch for %', "
    "NEW.experiment_id USING ERRCODE = 'integrity_constraint_violation'; "
    "END IF; RETURN NEW; END; $$ LANGUAGE plpgsql",
    "DROP TRIGGER IF EXISTS trg_paper_validation_transitions_experiment_identity "
    "ON research.paper_validation_state_transitions",
    "CREATE TRIGGER trg_paper_validation_transitions_experiment_identity "
    "BEFORE INSERT ON research.paper_validation_state_transitions "
    "FOR EACH ROW EXECUTE FUNCTION "
    "research.validate_paper_validation_experiment_identity()",
    "DROP TRIGGER IF EXISTS trg_paper_validation_transitions_immutable "
    "ON research.paper_validation_state_transitions",
    "CREATE TRIGGER trg_paper_validation_transitions_immutable "
    "BEFORE UPDATE OR DELETE ON research.paper_validation_state_transitions "
    "FOR EACH ROW EXECUTE FUNCTION "
    "research.reject_paper_validation_audit_mutation()",
    "DROP TRIGGER IF EXISTS trg_paper_validation_hypotheses_experiment_identity "
    "ON research.strategy_hypothesis_drafts",
    "CREATE TRIGGER trg_paper_validation_hypotheses_experiment_identity "
    "BEFORE INSERT ON research.strategy_hypothesis_drafts "
    "FOR EACH ROW EXECUTE FUNCTION "
    "research.validate_paper_validation_experiment_identity()",
    "DROP TRIGGER IF EXISTS trg_paper_validation_hypotheses_immutable "
    "ON research.strategy_hypothesis_drafts",
    "CREATE TRIGGER trg_paper_validation_hypotheses_immutable "
    "BEFORE UPDATE OR DELETE ON research.strategy_hypothesis_drafts "
    "FOR EACH ROW EXECUTE FUNCTION "
    "research.reject_paper_validation_audit_mutation()",
    "DROP TRIGGER IF EXISTS trg_paper_validation_reviews_experiment_identity "
    "ON research.paper_validation_postmortem_reviews",
    "CREATE TRIGGER trg_paper_validation_reviews_experiment_identity "
    "BEFORE INSERT ON research.paper_validation_postmortem_reviews "
    "FOR EACH ROW EXECUTE FUNCTION "
    "research.validate_paper_validation_experiment_identity()",
    "DROP TRIGGER IF EXISTS trg_paper_validation_reviews_immutable "
    "ON research.paper_validation_postmortem_reviews",
    "CREATE TRIGGER trg_paper_validation_reviews_immutable "
    "BEFORE UPDATE OR DELETE ON research.paper_validation_postmortem_reviews "
    "FOR EACH ROW EXECUTE FUNCTION "
    "research.reject_paper_validation_audit_mutation()",
    # ---- ROB-844: binance_demo_order_ledger root-exposure + broker-ack partial
    # uniqueness (mirrors migration 20260713_rob844_*). create_all skips these on
    # a persistent DB where the table already exists, so mirror them here.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_binance_demo_ledger_open_root "
    "ON binance_demo_order_ledger (product, instrument_id) "
    "WHERE parent_client_order_id IS NULL "
    "AND lifecycle_state IN "
    "('planned','previewed','validated','submitted','filled','anomaly')",
    ROB844_ACK_INDEX_REFRESH_DDL,
    ROB844_ACK_INDEX_CREATE_DDL,
)


# --------------------------------------------------------------------------- #
# ROB-534 — Toss symbol master columns. Conditional ALTER (per-column probe)  #
# so persistent local DBs don't take unconditional AccessExclusive locks on   #
# every hot column.                                                           #
# --------------------------------------------------------------------------- #
_ROB_534_SYMBOL_UNIVERSE_COLUMNS: tuple[
    tuple[str, tuple[tuple[str, str], ...]], ...
] = (
    (
        "kr_symbol_universe",
        (
            ("security_type", "VARCHAR(20)"),
            ("is_common_share", "BOOLEAN"),
            ("listing_status", "VARCHAR(20)"),
            ("list_date", "DATE"),
            ("delist_date", "DATE"),
            ("shares_outstanding", "NUMERIC(30, 0)"),
            ("leverage_factor", "NUMERIC(12, 6)"),
            ("krx_trading_suspended", "BOOLEAN"),
            ("nxt_trading_suspended", "BOOLEAN"),
            ("isin", "VARCHAR(20)"),
            ("toss_master_updated_at", "TIMESTAMP WITH TIME ZONE"),
        ),
    ),
    (
        "us_symbol_universe",
        (
            ("security_type", "VARCHAR(20)"),
            ("is_common_share", "BOOLEAN"),
            ("listing_status", "VARCHAR(20)"),
            ("list_date", "DATE"),
            ("delist_date", "DATE"),
            ("shares_outstanding", "NUMERIC(30, 0)"),
            ("leverage_factor", "NUMERIC(12, 6)"),
            ("isin", "VARCHAR(20)"),
            ("toss_master_updated_at", "TIMESTAMP WITH TIME ZONE"),
        ),
    ),
)


# --------------------------------------------------------------------------- #
# ROB-443 / ROB-284 / ROB-440 hot-column conditional ALTERs. They stay as    #
# code (NOT in _DDL_STATEMENTS) so the content-hash sentinel isn't disturbed #
# by transient catalogue state.                                               #
# --------------------------------------------------------------------------- #
async def _maybe_add_column(conn, table: str, column: str, ddl_type: str) -> None:
    has = (
        await conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        )
    ).first()
    if has:
        return
    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


async def _maybe_add_unique_constraint(
    conn, table: str, constraint_name: str, columns: str
) -> None:
    has = (
        await conn.execute(
            text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conrelid = CAST(:relid AS regclass) "
                "AND conname = :c"
            ),
            {"relid": table, "c": constraint_name},
        )
    ).first()
    if has:
        return
    await conn.execute(
        text(f"ALTER TABLE {table} ADD CONSTRAINT {constraint_name} UNIQUE ({columns})")
    )


async def _ensure_analysis_artifacts_created_by_constraint(conn) -> None:
    constraints = await conn.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) AS definition "
            "FROM pg_constraint "
            "WHERE conrelid = 'review.analysis_artifacts'::regclass "
            "AND conname = 'ck_analysis_artifacts_created_by' "
            "AND contype = 'c'"
        )
    )
    rows = list(constraints)
    if rows and "codex" in (rows[0][1] or ""):
        return

    await conn.execute(
        text(
            "ALTER TABLE review.analysis_artifacts "
            "DROP CONSTRAINT IF EXISTS ck_analysis_artifacts_created_by"
        )
    )
    await conn.execute(
        text(
            "ALTER TABLE review.analysis_artifacts "
            "ADD CONSTRAINT ck_analysis_artifacts_created_by "
            "CHECK (created_by IN ('claude', 'operator', 'system', 'codex'))"
        )
    )


async def _ensure_operator_session_context_created_by_constraint(conn) -> None:
    constraints = await conn.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) AS definition "
            "FROM pg_constraint "
            "WHERE conrelid = 'review.operator_session_context'::regclass "
            "AND conname = 'ck_operator_session_context_created_by' "
            "AND contype = 'c'"
        )
    )
    rows = list(constraints)
    if rows and "codex" in (rows[0][1] or ""):
        return

    await conn.execute(
        text(
            "ALTER TABLE review.operator_session_context "
            "DROP CONSTRAINT IF EXISTS ck_operator_session_context_created_by"
        )
    )
    await conn.execute(
        text(
            "ALTER TABLE review.operator_session_context "
            "ADD CONSTRAINT ck_operator_session_context_created_by "
            "CHECK (created_by IN ('claude', 'operator', 'system', 'codex'))"
        )
    )


def schema_content_hash() -> str:
    """SHA256 hex of the bootstrap version + the DDL tuple.

    Bumping ``SCHEMA_BOOTSTRAP_VERSION`` (or appending to ``_DDL_STATEMENTS``)
    changes the sentinel hash so a persistent local DB re-bootstraps exactly
    once.
    """
    payload = f"{SCHEMA_BOOTSTRAP_VERSION}\n" + "\n".join(_DDL_STATEMENTS)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def apply_test_schema(conn) -> None:
    """Create schemas + all ORM tables + apply the idempotent DDL union.

    ``conn`` is an AsyncConnection already inside a transaction (the caller
    wraps it in ``engine.begin()``). Idempotent: safe to run on both a fresh
    and a persistent DB.
    """
    import app.models  # noqa: F401  (register all ORM tables)
    import app.models.market_events  # noqa: F401
    from app.models.base import Base

    for schema in ("paper", "research", "review"):
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))

    # ROB-284 — drop the legacy crypto_candles_1d shape BEFORE create_all so
    # Base.metadata rebuilds it from the new ORM model within this same barrier
    # run. (The barrier applies the schema exactly once; if the drop ran after
    # create_all the table would be left missing until the next hash change.)
    legacy_has_symbol = (
        await conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'crypto_candles_1d' "
                "AND column_name = 'symbol'"
            )
        )
    ).first()
    if legacy_has_symbol:
        await conn.execute(
            text("DROP TABLE IF EXISTS public.crypto_candles_1d CASCADE")
        )

    await conn.run_sync(Base.metadata.create_all)

    # --- conditional "only when genuinely missing" probes (per-table catalog
    # probes that don't take AccessExclusive unless the column is genuinely
    # absent) -- kept as code, NOT in _DDL_STATEMENTS.

    # ROB-440 — high_52w_date on market_valuation_snapshots
    await _maybe_add_column(conn, "market_valuation_snapshots", "high_52w_date", "DATE")

    # ROB-443 — funding_rate / OI / long-short on crypto screener snapshots
    await _maybe_add_column(
        conn,
        "invest_crypto_screener_snapshots",
        "funding_rate",
        "NUMERIC(12, 8)",
    )

    await _maybe_add_column(
        conn,
        "invest_crypto_screener_snapshots",
        "open_interest_usd",
        "NUMERIC(28, 2)",
    )

    await _maybe_add_column(
        conn,
        "invest_crypto_screener_snapshots",
        "oi_change_24h",
        "NUMERIC(10, 4)",
    )
    await _maybe_add_column(
        conn,
        "invest_crypto_screener_snapshots",
        "long_short_account_ratio",
        "NUMERIC(10, 4)",
    )
    # ROB-534 — symbol master columns
    for table, cols in _ROB_534_SYMBOL_UNIVERSE_COLUMNS:
        for col_name, col_type in cols:
            await _maybe_add_column(conn, table, col_name, col_type)

    # --- conditional CHECK refreshers (drop+recreate depending on catalogue) ---

    # Rebuild market_valuation_snapshots UNIQUE only if it's truly missing —
    # ADD CONSTRAINT has no IF NOT EXISTS so we probe pg_constraint first.
    await _maybe_add_unique_constraint(
        conn,
        "market_valuation_snapshots",
        "uq_market_valuation_snapshots_market_symbol_date_source",
        "market, symbol, snapshot_date, source",
    )

    await _ensure_market_valuation_source_constraint(conn)
    await _ensure_investment_snapshot_kind_constraint(conn)
    await _ensure_trade_forecast_status_constraint(conn)
    await _ensure_analysis_artifacts_created_by_constraint(conn)
    await _ensure_operator_session_context_created_by_constraint(conn)

    for stmt in _DDL_STATEMENTS:
        await conn.execute(text(stmt))
