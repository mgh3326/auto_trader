"""Read-only KR trading-flow and event evidence for MCP."""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from typing import Any, Awaitable, Callable

from app.core.timezone import today_kst


def _wire(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_wire(item) for item in value]
    return value


def _as_of(records: list[dict[str, Any]]) -> str | None:
    stamps = [
        value
        for row in records
        for value in (row.get("updated_at"), row.get("date"))
        if value is not None
    ]
    return _wire(max(stamps)) if stamps else None


def _has_data(value: Any) -> bool:
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, dict):
        return any(_has_data(item) for item in value.values())
    return value is not None


def _source_result(awaitable: Awaitable[Any], normalize: Callable[[Any], Any]) -> Awaitable[dict[str, Any]]:
    async def run() -> dict[str, Any]:
        try:
            normalized = normalize(await awaitable)
            records = normalized if isinstance(normalized, list) else []
            data = normalized.get("data") if isinstance(normalized, dict) else normalized
            as_of = (
                normalized.get("as_of", normalized.get("observed_at"))
                if isinstance(normalized, dict)
                else _as_of(records)
            )
            return {
                "status": "ok" if _has_data(data) else "empty",
                "as_of": _wire(as_of),
                "data": _wire(data),
            }
        except Exception as exc:  # source failures are isolated by contract
            return {"status": "error", "as_of": None, "error_class": type(exc).__name__}

    return run()


async def handle_get_kr_flow_signals(symbol: str, days: int = 20) -> dict[str, Any]:
    from app.services.brokers.kis.client import KISClient
    from app.services.brokers.kis.flow_signals import (
        parse_member_summary,
        parse_vi_status_rows,
    )
    from app.services.brokers.toss.client import TossReadClient

    symbol = (symbol or "").strip()
    if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ValueError("symbol must be a six-digit Korean stock code")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 100:
        raise ValueError("days must be an integer from 1 to 100")

    toss = TossReadClient.from_settings()
    kis = KISClient()
    session_date = today_kst()
    sources = {
        "toss_short_selling": _source_result(
            toss.stock_short_selling(symbol, count=days), lambda page: page.records
        ),
        "toss_securities_lending": _source_result(
            toss.stock_securities_lending(symbol, count=days), lambda page: page.records
        ),
        "toss_credit_trades": _source_result(
            toss.stock_credit_trades(symbol, count=days), lambda page: page.records
        ),
        "toss_program_trades": _source_result(
            toss.stock_program_trades(symbol, count=days), lambda page: page.records
        ),
        "kis_vi_status": _source_result(
            kis.inquire_vi_status(symbol, session_date),
            lambda rows: {
                "as_of": session_date,
                "data": parse_vi_status_rows(rows, symbol=symbol),
            },
        ),
        "kis_broker_window": _source_result(
            kis.inquire_member(symbol),
            lambda raw: {
                "observed_at": dt.datetime.now(dt.UTC),
                "data": parse_member_summary(raw),
            },
        ),
    }
    try:
        results = await asyncio.gather(*sources.values())
    finally:
        await asyncio.gather(toss.aclose(), kis.aclose(), return_exceptions=True)
    return {
        "symbol": symbol,
        "market": "kr",
        "requested_days": days,
        "sources": dict(zip(sources, results, strict=True)),
    }


async def handle_get_kr_investor_rankings(
    investor: str = "foreign",
    side: str = "buy",
    limit: int = 20,
) -> dict[str, Any]:
    from app.services.brokers.toss.client import TossReadClient

    investor_key = {"foreign": "foreigner", "institution": "institution", "individual": "individual"}.get(investor)
    if investor_key is None:
        raise ValueError("investor must be foreign, institution, or individual")
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")

    client = TossReadClient.from_settings()
    try:
        try:
            rankings = await client.investor_trading_rankings(count=limit)
            section = rankings[investor_key]
            rows = section.buy if side == "buy" else section.sell
            return {
                "market": "kr",
                "investor": investor,
                "side": side,
                "status": "ok" if rows else "empty",
                "as_of": _wire(section.ranked_at),
                "data": _wire(rows[:limit]),
            }
        except Exception as exc:
            return {
                "market": "kr",
                "investor": investor,
                "side": side,
                "status": "error",
                "as_of": None,
                "error_class": type(exc).__name__,
            }
    finally:
        await client.aclose()
