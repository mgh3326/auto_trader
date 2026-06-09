"""ROB-449: get_retail_sentiment — Naver 종목토론 AGGREGATE retail-activity signal (KR).

Promotes the ROB-199 PoC to a live (operator-gated) tool WITHOUT touching the ROB-199
``StockDetailDiscussionSignal`` aggregate-only hardlock (kept intact as the /invest/stocks
detail safety contract). This is a separate, generic tool returning aggregate counts only.

Safety:
  * Live fetch is OFF by default (``settings.retail_sentiment_live_enabled``) — a
    ToS-sensitive UGC source. Off → status="disabled" (honest scaffold).
  * AGGREGATE ONLY: rank + post/comment/reaction counts. NEVER raw post text / titles /
    authors / nicknames.
  * The rankings source is market-wide top-N; a symbol absent from it → status="not_ranked"
    (missing != zero — itself a coarse "not in the hot-discussion list" signal).
  * bull_bear_lean / top_themes are DEFERRED — they border on UGC interpretation and need
    a careful classifier; v1 ships counts + overheat only.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.core.config import settings
from app.mcp_server.tooling.shared import error_payload as _error_payload
from app.mcp_server.tooling.shared import (
    is_korean_equity_code as _is_korean_equity_code,
)
from app.services.naver_finance.discussion import fetch_discussion_rankings

# Top-N rank at/under which a symbol's discussion activity is flagged "overheated".
_OVERHEAT_RANK = 5


async def handle_get_retail_sentiment(
    symbol: str,
    market: str = "kr",
    window: str = "1d",
) -> dict[str, Any]:
    """Aggregate retail-discussion activity for a KR symbol (Naver 종목토론, gated)."""
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    if (market or "kr").strip().lower() != "kr" or not _is_korean_equity_code(symbol):
        raise ValueError(
            "retail sentiment is only available for Korean stocks "
            "(6-digit codes like '005930')"
        )

    now = dt.datetime.now(dt.UTC)
    base = {
        "symbol": symbol,
        "source": "naver_discussion",
        "market": "kr",
        "window": window,
        "observed_at": now.isoformat(),
    }

    # Operator gate: ToS-sensitive UGC source stays off until explicitly enabled.
    if not settings.retail_sentiment_live_enabled:
        return {
            **base,
            "status": "disabled",
            "note": (
                "live Naver 종목토론 fetch is disabled "
                "(set RETAIL_SENTIMENT_LIVE_ENABLED=true after a ToS/endpoint review)"
            ),
        }

    try:
        ranking = await fetch_discussion_rankings(size=20)
    except Exception as exc:  # noqa: BLE001 — defensive; fetcher is already fail-open
        return _error_payload(
            source="naver_discussion",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_kr",
        )

    if ranking.get("state") != "fresh":
        return {**base, "status": "unavailable", "freshness": "missing"}

    match = next(
        (it for it in ranking.get("items", []) if it.get("code") == symbol.upper()),
        None,
    )
    if match is None:
        # Not in the hot-discussion top-N → no per-symbol counts (missing != zero).
        return {
            **base,
            "status": "not_ranked",
            "freshness": "fresh",
            "fetched_at": ranking.get("fetched_at"),
            "activity_rank": None,
            "overheat_flag": False,
        }

    rank = match.get("rank")
    return {
        **base,
        "status": "ok",
        "freshness": "fresh",
        "fetched_at": ranking.get("fetched_at"),
        "activity_rank": rank,
        "post_count": match.get("post_count"),
        "comment_count": match.get("comment_count"),
        "reaction_count": match.get("reaction_count"),
        # momentum/bull_bear_lean/top_themes intentionally deferred (need history /
        # UGC-safe classifier — ROB-449 follow-up).
        "momentum": "unknown",
        "overheat_flag": rank is not None and rank <= _OVERHEAT_RANK,
    }
