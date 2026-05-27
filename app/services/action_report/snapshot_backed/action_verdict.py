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
