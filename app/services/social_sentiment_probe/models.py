from __future__ import annotations

import datetime as dt
import html
import re
from typing import Any

_TAG_RE = re.compile(r"<[^>]+>")


def strip_markup(value: str | None) -> str | None:
    if value is None:
        return None
    text = html.unescape(_TAG_RE.sub("", value))
    return " ".join(text.split())


def truncate_preview(value: str | None, limit: int = 280) -> str | None:
    if value is None:
        return None
    clean = " ".join(value.split())
    return clean[: max(0, limit)]


def source_result(
    *,
    source: str,
    market: str,
    query: str,
    status: str,
    items: list[dict[str, Any]],
    observed_at: dt.datetime,
    error_reason: str | None = None,
    quota: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": source,
        "market": market,
        "query": query,
        "status": status,
        "observed_at": observed_at.isoformat(),
        "item_count": len(items),
        "items": items,
    }
    if error_reason:
        payload["error_reason"] = error_reason
    if quota:
        payload["quota"] = quota
    return payload


def build_social_sentiment_evidence(
    *,
    market: str,
    symbol: str,
    query: str,
    source_results: list[dict[str, Any]],
    observed_at: dt.datetime,
) -> dict[str, Any]:
    ok_count = sum(1 for result in source_results if result.get("status") == "ok")
    total_items = sum(int(result.get("item_count") or 0) for result in source_results)
    return {
        "source": "free_social_sources_v0",
        "market": market,
        "symbol": symbol,
        "query": query,
        "observed_at": observed_at.isoformat(),
        "advisory_only": True,
        "cost_usd": 0,
        "summary": {
            "source_count": len(source_results),
            "ok_source_count": ok_count,
            "total_item_count": total_items,
        },
        "sources": source_results,
    }
