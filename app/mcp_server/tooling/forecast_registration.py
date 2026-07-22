# app/mcp_server/tooling/forecast_registration.py
"""ROB-650 — MCP registration for resolvable forecast tools."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.forecast_tools import (
    forecast_resolve,
    forecast_save,
    get_forecast_calibration,
    get_forecasts,
    missed_opportunity_save,
)

FORECAST_TOOL_NAMES: set[str] = {
    "forecast_save",
    "forecast_resolve",
    "get_forecasts",
    "get_forecast_calibration",
    "missed_opportunity_save",
}


def register_forecast_tools(mcp: Any) -> None:
    _ = mcp.tool(
        name="forecast_save",
        description=(
            "Record a resolvable probabilistic forecast (a buy thesis or a "
            "profit-taking WATCH->PLACE verdict made resolvable). Required: "
            "created_by, symbol, instrument_type in {equity_kr, equity_us, "
            "crypto, forex, index}, forecast_target (object with 'kind'; for "
            "'price_target' also 'direction' in {at_or_above, at_or_below} and "
            "'target_price'), probability in [0,1], review_date (YYYY-MM-DD). "
            "Optional probability_range_low/high (probability must fall inside), "
            "horizon, evidence_ids, contrary_evidence, forecast_start_date, "
            "artifact_uuid/journal_id/report_uuid/report_item_uuid/correlation_id "
            "links, and session_label/model_label/policy_version for calibration. "
            "Idempotent per forecast_id (omit to create; supply to update while "
            "open — a closed/resolved forecast is immutable). Composition is the "
            "caller's judgment; storage/scoring is deterministic."
        ),
    )(forecast_save)
    _ = mcp.tool(
        name="missed_opportunity_save",
        description=(
            "ROB-1017 session-close storage hook. If and only if the absolute "
            "same-day index move is greater than 2% and new_buy_count is zero, "
            "atomically publish exactly top_n ranked unbought candidates as "
            "linked D+5 return_at_horizon forecasts and trade retrospectives "
            "with trigger_type=missed_opportunity. Each candidate requires "
            "symbol, matching instrument_type, D0 reference_price, "
            "target_return_pct, probability/confidence, and rejection_reason. "
            "KR/US D+5 means five confirmed trading sessions; crypto uses five "
            "calendar days. Exact retries are idempotent; changing membership "
            "under the same session_label is rejected. DB learning writes only: "
            "no broker/order/live mutation."
        ),
    )(missed_opportunity_save)
    _ = mcp.tool(
        name="forecast_resolve",
        description=(
            "Resolve due forecasts deterministically and score them (Brier = "
            "(probability - outcome)^2). dry_run-default: with dry_run=true "
            "(default) it computes and previews without writing; dry_run=false "
            "persists (status -> closed). With forecast_id it resolves that one; "
            "without it, resolves every open forecast whose review_date has "
            "passed (up to limit). price_target forecasts resolve against loaded "
            "daily OHLCV (ROB-639 DB-first, equity_kr/equity_us/crypto). "
            "return_at_horizon forecasts resolve against the exact review-date "
            "daily close and store the observed point return. "
            "Placeholder forecasts whose target kind is "
            "'no_resolvable_forecast' auto-close without an outcome or Brier "
            "score (dry-run reports 'would_close_no_claim'; persisted status is "
            "'closed_no_claim'). "
            "Other non-price kinds (or forecasts you must override) require an "
            "explicit forecast_id plus manual_outcome (bool) and manual_evidence. "
            "Idempotent: a closed forecast is never re-scored. "
            "Missing daily candles for a not-yet-loaded symbol are lazily "
            "fetched+persisted once, then re-read (backfill_missing=true "
            "default; set false for a fast peek that skips the fetch)."
        ),
    )(forecast_resolve)
    _ = mcp.tool(
        name="get_forecasts",
        description=(
            "List forecasts with filters (status open/closed/closed_no_claim, symbol, "
            "created_by, correlation_id). Read-only."
        ),
    )(get_forecasts)
    _ = mcp.tool(
        name="get_forecast_calibration",
        description=(
            "Calibration aggregate over closed, scored forecasts: average Brier "
            "score, hit-rate, average probability and calibration_gap "
            "(avg_probability - hit_rate; positive = over-confident) per cohort. "
            "group_by in {created_by, session_label, model_label, day} — the "
            "objective metric for comparing whether different sessions/models "
            "reach equally well-calibrated calls. Filters: created_by, symbol, "
            "instrument_type, days. Read-only."
        ),
    )(get_forecast_calibration)


__all__ = ["FORECAST_TOOL_NAMES", "register_forecast_tools"]
