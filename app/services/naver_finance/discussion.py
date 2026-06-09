"""ROB-449: Naver 종목토론(discussion) AGGREGATE-ONLY signal fetcher.

Reads the discussion *rankings* endpoint (the only Naver discussion surface probed in
this repo, observed 200/signal-only in ROB-197/199) and extracts AGGREGATE counts only —
rank + post/comment/reaction counts per item code. NEVER raw post text / titles / authors
/ nicknames (ToS + ROB-199 aggregate-only contract).

The endpoint is unofficial + market-wide top-N (not per-symbol), so a symbol absent from
the ranking yields no counts (missing != zero). Parsing is defensive (shape may drift)
and fail-open; the live call is gated off by default at the caller. Operator live-smoke
verifies the shape before relying on it.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import httpx

_DISCUSSION_RANKINGS_URL = "https://stock.naver.com/api/community/discussion/rankings"
_TIMEOUT = httpx.Timeout(10.0)
_CACHE_TTL_SECONDS = 600  # 10 min server-side cache (ROB-449: 5-15min)
_CACHE: dict[str, dict[str, Any]] = {}


def _to_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _extract_ranked_items(payload: Any) -> list[dict[str, Any]]:
    """Find the ranked-items list across likely shapes (defensive, aggregate-only)."""
    container: Any = payload
    if isinstance(payload, dict):
        for key in ("rankings", "items", "list", "ranks", "result", "data"):
            seq = payload.get(key)
            if isinstance(seq, list):
                container = seq
                break
    if not isinstance(container, list):
        return []

    items: list[dict[str, Any]] = []
    for idx, row in enumerate(container):
        if not isinstance(row, dict):
            continue
        code = (
            row.get("itemCode")
            or row.get("code")
            or row.get("cd")
            or row.get("reutersCode")
        )
        if not code:
            continue
        rank = _to_int(row.get("rank")) or (idx + 1)
        items.append(
            {
                "code": str(code).strip().upper(),
                "rank": rank,
                # AGGREGATE counts only — never raw text fields.
                "post_count": _to_int(row.get("postCount") or row.get("articleCount")),
                "comment_count": _to_int(row.get("commentCount")),
                "reaction_count": _to_int(
                    row.get("reactionCount") or row.get("sympathyCount")
                ),
            }
        )
    return items


async def fetch_discussion_rankings(
    *,
    size: int = 20,
    fetcher: Any = None,
) -> dict[str, Any]:
    """Return the Naver 종목토론 ranking as aggregate counts per item code.

    Shape: ``{"state": "fresh"|"unavailable", "source": "naver_discussion",
    "fetched_at": iso, "total_count": int|None, "items": [{code, rank, post_count,
    comment_count, reaction_count}]}``. Fail-open: any error → state="unavailable".
    """
    now = dt.datetime.now(dt.UTC)
    cache_key = f"rankings:{size}"
    cached = _CACHE.get(cache_key)
    if cached and cached.get("expires_at", 0) > time.time():
        return cached["value"]

    try:
        if fetcher is not None:
            payload = await fetcher()
        else:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(
                    _DISCUSSION_RANKINGS_URL, params={"size": str(max(1, size))}
                )
                response.raise_for_status()
                payload = response.json()
    except Exception as exc:  # noqa: BLE001 — unofficial endpoint; degrade gracefully
        return {
            "state": "unavailable",
            "source": "naver_discussion",
            "fetched_at": now.isoformat(),
            "total_count": None,
            "items": [],
            "errorReason": str(exc),
        }

    total = _to_int(payload.get("totalCount")) if isinstance(payload, dict) else None
    value = {
        "state": "fresh",
        "source": "naver_discussion",
        "fetched_at": now.isoformat(),
        "total_count": total,
        "items": _extract_ranked_items(payload),
    }
    _CACHE[cache_key] = {"expires_at": time.time() + _CACHE_TTL_SECONDS, "value": value}
    return value
