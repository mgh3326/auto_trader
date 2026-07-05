"""ROB-722 — deterministic upcoming-earnings context for a symbol.

Read-only. No LLM (ROB-501), no schema change. Reuses the validated
handle_get_earnings_calendar dispatch (US live Finnhub / KR market_events DB)
and shapes a compact "next earnings D-n / timing / consensus, or explicit
no-earnings" signal. Attached to analyze_stock_batch compact responses so each
fresh analysis session sees the symbol's earnings proximity.

No-earnings is itself a signal (HCA: '30일 내 무실적' as an entry justification),
so a zero-earnings window yields has_upcoming=False + note — NOT omission.
Only crypto / non-equity symbols are omitted (no earnings concept).
"""

from __future__ import annotations

import datetime
from typing import Any

_WINDOW_DAYS = 30
_TIMING_MAP = {"bmo": "BMO", "amc": "AMC", "dmh": "DMH"}


def _map_timing(hour: str | None) -> str:
    if not hour:
        return "unknown"
    return _TIMING_MAP.get(hour.strip().lower(), "unknown")


def _compact_earnings(
    tool_result: dict[str, Any],
    *,
    today: datetime.date,
    freshness: str,
    data_as_of: str | None,
) -> dict[str, Any]:
    source = tool_result.get("source")
    market_label = "kr" if (
        tool_result.get("market") == "kr" or source == "market_events"
    ) else "us"

    ctx: dict[str, Any] = {
        "symbol": tool_result.get("symbol"),
        "market": market_label,
        "as_of": today.isoformat(),
        "window_days": _WINDOW_DAYS,
        "freshness": freshness,
        "source": source,
    }
    if data_as_of is not None:
        ctx["data_as_of"] = data_as_of

    if tool_result.get("error"):
        ctx["has_upcoming"] = False
        ctx["next_earnings"] = None
        ctx["note"] = f"earnings lookup degraded: {tool_result.get('error')}"
        return ctx

    upcoming: list[tuple[datetime.date, dict[str, Any]]] = []
    for item in tool_result.get("earnings") or []:
        raw = item.get("date")
        if not raw:
            continue
        try:
            edate = datetime.date.fromisoformat(raw)
        except (ValueError, TypeError):
            continue
        if edate >= today:
            upcoming.append((edate, item))

    if not upcoming:
        ctx["has_upcoming"] = False
        ctx["next_earnings"] = None
        ctx["note"] = f"no scheduled earnings within {_WINDOW_DAYS} days"
        return ctx

    upcoming.sort(key=lambda pair: pair[0])
    edate, item = upcoming[0]
    ctx["has_upcoming"] = True
    ctx["next_earnings"] = {
        "date": edate.isoformat(),
        "d_minus": (edate - today).days,
        "timing": _map_timing(item.get("hour") or item.get("time_hint")),
        "eps_estimate": item.get("eps_estimate"),
        "revenue_estimate": item.get("revenue_estimate"),
        "quarter": item.get("quarter"),
        "year": item.get("year"),
        "status": item.get("status"),
    }
    return ctx
