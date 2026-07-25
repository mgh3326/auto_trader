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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.fundamentals._financials import (
    handle_get_earnings_calendar,
)
from app.models.market_events import MarketEventIngestionPartition
from app.services.market_events.freshness_service import _ensure_aware, _is_stale

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
    market_label = (
        "kr"
        if (tool_result.get("market") == "kr" or source == "market_events")
        else "us"
    )

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


async def _kr_ingestion_freshness(db: AsyncSession) -> tuple[str, str | None]:
    """Newest succeeded KR earnings ingestion → (freshness, data_as_of ISO|None).

    Global per (market=kr, category=earnings) — compute ONCE per batch, not per
    symbol. Reuses the market_events STALE_AFTER_HOURS threshold. Fail-open at
    the caller: a DB error leaves KR rows on ('unknown', None).
    """
    stmt = select(func.max(MarketEventIngestionPartition.finished_at)).where(
        MarketEventIngestionPartition.market == "kr",
        MarketEventIngestionPartition.category == "earnings",
        MarketEventIngestionPartition.status == "succeeded",
    )
    finished_at = (await db.execute(stmt)).scalar_one_or_none()
    if finished_at is None:
        return ("unknown", None)
    aware = _ensure_aware(finished_at)
    freshness = (
        "stale"
        if _is_stale(aware, now=datetime.datetime.now(datetime.UTC))
        else "fresh"
    )
    return (freshness, aware.date().isoformat())


# Accept both the analyze_stock_batch row values (resolve_market_type →
# equity_kr / equity_us / crypto) and the tool-level market params (kr / us).
# The original {"kr", "us"}-only gate made injection a production no-op because
# real compact rows always carry the equity_* form.
_MARKET_ALIASES = {
    "kr": "kr",
    "equity_kr": "kr",
    "us": "us",
    "equity_us": "us",
}


def normalize_earnings_market(market: str | None) -> str | None:
    """Map a market/market_type value to "kr"/"us", or None for non-equity."""
    return _MARKET_ALIASES.get((market or "").strip().lower())


async def build_earnings_context(
    symbol: str,
    market: str,
    *,
    today: datetime.date | None = None,
    kr_freshness: tuple[str, str | None] | None = None,
) -> dict[str, Any] | None:
    """Compact upcoming-earnings context for one symbol, or None to omit.

    Omits (None) only for crypto / non-equity markets — earnings has no meaning
    there. For US/KR equities it ALWAYS returns a dict (no-earnings is an
    explicit has_upcoming=False signal). US freshness is "live"; KR freshness is
    taken from ``kr_freshness`` (computed once per batch by the caller)."""
    market_norm = normalize_earnings_market(market)
    if market_norm is None:
        return None

    today = today or datetime.date.today()
    to_date = today + datetime.timedelta(days=_WINDOW_DAYS)

    tool_result = await handle_get_earnings_calendar(
        symbol, today.isoformat(), to_date.isoformat(), market_norm
    )

    if market_norm == "kr":
        freshness, data_as_of = kr_freshness or ("unknown", None)
    else:
        freshness, data_as_of = "live", None

    return _compact_earnings(
        tool_result, today=today, freshness=freshness, data_as_of=data_as_of
    )
