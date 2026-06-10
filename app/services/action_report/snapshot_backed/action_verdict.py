# app/services/action_report/snapshot_backed/action_verdict.py
"""ROB-335 — deterministic ActionPacket sub-verdict vocabulary + rules.

Sub-verdicts are *sub-labels over the locked ROB-301 ``decision_bucket``
5-value enum* (spec §2 decision A). They are stored on each report item's
``evidence_snapshot["action_verdict"]`` (JSON; no migration, decision B) and
projected at read-time by ``build_action_packet``.

Per decision C′, only the *honest* verdicts are assigned deterministically
here: ``data_gap`` / ``keep`` / ``no_add`` / ``sell_review`` (held) and
``buy_review`` / ``no_new_buy_candidates`` (candidate). Directional
``trim_review`` / ``add_review`` / ``limit_wait`` / ``rejected`` exist in the
vocabulary for Hermes push to fill but are never fabricated here.
"""

from __future__ import annotations

from typing import Any

# action_verdict -> locked decision_bucket. Keys are the full vocabulary.
VERDICT_TO_BUCKET: dict[str, str] = {
    "buy_review": "new_buy_candidate",
    "limit_wait": "new_buy_candidate",
    "no_new_buy_candidates": "new_buy_candidate",
    "sell_review": "open_action",
    "trim_review": "open_action",
    "add_review": "open_action",
    "keep": "completed_or_existing",
    "no_add": "completed_or_existing",
    "watch_only": "risk_watch",
    "rejected": "deferred_no_action",
    "data_gap": "deferred_no_action",
}

ACTION_VERDICTS: frozenset[str] = frozenset(VERDICT_TO_BUCKET)


def _quote_is_actionable(quote: Any) -> bool:
    # Mirrors auto_emit._quote_is_actionable so held + candidate gates agree.
    if not isinstance(quote, dict):
        return False
    if quote.get("status") != "ok":
        return False
    best_bid = quote.get("best_bid") or 0
    best_ask = quote.get("best_ask") or 0
    bid_depth = quote.get("bid_depth") or 0
    ask_depth = quote.get("ask_depth") or 0
    return best_bid > 0 and best_ask > 0 and (bid_depth > 0 or ask_depth > 0)


def classify_held_symbol(
    holding: dict[str, Any],
    quote: dict[str, Any] | None,
    *,
    in_candidate_universe: bool,
) -> str:
    """Deterministic verdict for ONE KIS-primary held symbol (decision C′).

    Order (honest range only):
      1. quote missing / not actionable -> ``data_gap`` (no directional call)
      2. sellable_quantity > 0          -> ``sell_review`` (reviewable reduce)
      3. held + in screener universe    -> ``no_add`` (trending, don't add)
      4. otherwise                      -> ``keep`` (default hold)
    """
    if not _quote_is_actionable(quote):
        return "data_gap"
    if (holding.get("sellable_quantity") or 0) > 0:
        return "sell_review"
    if in_candidate_universe:
        return "no_add"
    return "keep"


def classify_candidate_symbol(
    quote: dict[str, Any] | None,
    *,
    universe_useful: bool,
    quote_snapshot_present: bool,
    candidate_fresh: bool = True,
) -> str:
    """Deterministic verdict for ONE non-held screener candidate.

    Honest-verdict only (mirrors ``classify_held_symbol``): never returns the
    directional ``rejected`` / ``limit_wait`` — those are Hermes-only.

    Order:
      1. no symbol/quote snapshot at all                        -> ``data_gap``   (호가 근거 부족)
      2. quote present but not actionable                       -> ``watch_only`` (저유동성)
      3. quote actionable, universe stale OR this candidate stale -> ``watch_only`` (스크리너 stale)
      4. quote actionable, universe useful, candidate fresh     -> ``buy_review``
    """
    if not quote_snapshot_present:
        return "data_gap"
    if not _quote_is_actionable(quote):
        return "watch_only"
    if not universe_useful or not candidate_fresh:
        return "watch_only"
    return "buy_review"


# Deterministic tiebreak when several watch-grade flags co-occur — the first
# matching flag becomes the surfaced reason (penny > illiquid > abnormal_spike >
# screener_stale). Order is policy, not derivable from code.
_QUALITY_WATCH_ORDER: tuple[str, ...] = (
    "penny",
    "illiquid",
    "abnormal_spike",
    "screener_stale",
)


def demote_for_quality(
    verdict: str, quality_flags: frozenset[str]
) -> tuple[str, str | None]:
    """ROB-346 — post-verdict quality demotion. Quality only DEMOTES (never
    upgrades). non_common_stock is always rejected; otherwise only buy_review
    is touched. Returns (new_verdict, reason | None)."""
    if "non_common_stock" in quality_flags:
        return "rejected", "non_common_stock"
    if verdict != "buy_review":
        return verdict, None
    if "common_stock_unknown" in quality_flags:
        return "data_gap", "common_stock_unknown"
    for flag in _QUALITY_WATCH_ORDER:
        if flag in quality_flags:
            return "watch_only", flag
    return "buy_review", None


def demote_for_budget(
    verdict: str, budget_state: dict[str, Any]
) -> tuple[str, list[str]]:
    """ROB-347 — post-verdict budget demotion. Only buy_review is touched;
    budget never upgrades. KRW is reference-only (no KRW→USD fabrication).
    Returns (new_verdict, reasons)."""
    if verdict != "buy_review":
        return verdict, []
    basis = budget_state.get("basis") or "available_usd"
    override = budget_state.get("override_usd")
    krw = budget_state.get("krw") or 0
    # request override (operator/report budget) takes precedence when present.
    usd = override if override is not None else budget_state.get("usd")
    if basis == "krw_orderable_reference" and override is None:
        return "watch_only", ["fx_required"]
    if usd is not None and usd > 0:
        return "buy_review", []
    reasons = ["budget_gap"]
    if krw and krw > 0:
        reasons.append("fx_required")
    if override is None:
        reasons.append("operator_budget_required")
    return "watch_only", reasons
