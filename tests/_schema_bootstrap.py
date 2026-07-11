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
SCHEMA_BOOTSTRAP_VERSION = 5

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
    await _ensure_analysis_artifacts_created_by_constraint(conn)
    await _ensure_operator_session_context_created_by_constraint(conn)

    for stmt in _DDL_STATEMENTS:
        await conn.execute(text(stmt))
