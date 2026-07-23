# app/mcp_server/tooling/forecast_registration.py
"""ROB-650 — MCP registration for resolvable forecast tools."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.forecast_tools import (
    forecast_resolve,
    forecast_save,
    get_forecast_calibration,
    get_forecasts,
)

FORECAST_TOOL_NAMES: set[str] = {
    "forecast_save",
    "forecast_resolve",
    "get_forecasts",
    "get_forecast_calibration",
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
            "'target_price' plus outcome_rule_version="
            "'window-touch-v1-high-gte-low-lte'; for 'terminal_close' use "
            "direction in {up, down}, "
            "'target_price', outcome_rule_version="
            "'terminal-close-v1-up-gte-down-lt', and price_adjustment_policy "
            "'unverified_fail_closed' or 'explicit-factor-v1'). The explicit "
            "policy also requires target_to_close_factor plus authenticated, "
            "hashed, symbol/action/basis-bound review-date "
            "adjustment_provenance; evidence-bearing MCP writes require an "
            "active MCP_AUTH_TOKEN and configured "
            "FORECAST_EVIDENCE_AUTHENTICATED_ACTOR_ID matching the payload. "
            "probability in [0,1], review_date "
            "(YYYY-MM-DD). "
            "Optional probability_range_low/high (probability must fall inside), "
            "horizon, evidence_ids, contrary_evidence, forecast_start_date, "
            "artifact_uuid/journal_id/report_uuid/report_item_uuid/correlation_id "
            "links, and session_label/model_label/policy_version for calibration. "
            "A typed terminal claim is immutable after creation; an exact replay "
            "is idempotent and the only update is a target-version CAS promotion "
            "from unverified_fail_closed to explicit-factor-v1. Versionless "
            "legacy price targets require typed touch attestation or a new "
            "terminal row with durable supersession evidence. Composition is the "
            "caller's judgment; storage/scoring is deterministic."
        ),
    )(forecast_save)
    _ = mcp.tool(
        name="forecast_resolve",
        description=(
            "Resolve due forecasts deterministically and score them (Brier = "
            "(probability - outcome)^2). dry_run-default: with dry_run=true "
            "(default) it computes and previews without writing the forecast. "
            "With backfill_missing=true (also default), a dry run may still "
            "fetch and persist shared daily candles; set backfill_missing=false "
            "for a genuinely read-only review. dry_run=false persists "
            "(status -> closed). Typed deterministic persistence requires the "
            "target_version, immutable_claim_hash, and resolution_fingerprint "
            "returned by the reviewed dry run (or expected_resolutions by ID for "
            "batch mode). With forecast_id it resolves that one; "
            "without it, resolves every open forecast whose review_date has "
            "passed (up to limit); quarantined rows are reported separately and "
            "do not consume the eligible-row limit. Versioned price_target "
            "forecasts resolve against loaded daily OHLCV as window high/low "
            "touches (ROB-639 DB-first, "
            "equity_kr/equity_us/crypto). terminal_close forecasts use only the "
            "review-date regular-session close (never high/low, adj_close, or "
            "extended-hours data): up is close >= adjusted target and down is "
            "close < adjusted target. They fail closed unless exactly one final "
            "review-date candle with typed final/session/source/ingestion/basis "
            "provenance and explicit-factor-v1 corporate-action evidence are "
            "available. "
            "Placeholder forecasts whose target kind is "
            "'no_resolvable_forecast' auto-close without an outcome or Brier "
            "score (dry-run reports 'would_close_no_claim'; persisted status is "
            "'closed_no_claim'). "
            "Non-price kinds (or price forecasts you must override) require an "
            "explicit forecast_id plus manual_outcome (bool) and manual_evidence. "
            "Idempotent: a closed forecast is never re-scored. "
            "Missing daily candles for a not-yet-loaded symbol are lazily "
            "fetched+persisted once, then re-read when backfill_missing=true."
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
