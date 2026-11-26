"""Read-only KR trading-flow and event evidence for MCP."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

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
    stamps: list[tuple[float, dt.date | dt.datetime]] = []
    for row in records:
        value = row.get("updated_at") or row.get("date")
        if isinstance(value, dt.datetime):
            comparable = value
            if comparable.tzinfo is None:
                comparable = comparable.replace(tzinfo=dt.UTC)
            order = comparable.timestamp()
        elif isinstance(value, dt.date):
            order = dt.datetime.combine(value, dt.time.min, tzinfo=dt.UTC).timestamp()
        else:
            continue
        stamps.append((order, value))
    return _wire(max(stamps, key=lambda item: item[0])[1]) if stamps else None


def _vi_as_of(
    rows: list[dict[str, Any]], session_date: dt.date
) -> dt.date | dt.datetime:
    stamps: list[dt.datetime] = []
    timezone = ZoneInfo("Asia/Seoul")
    for row in rows:
        source_date = row.get("bsop_date")
        source_time = row.get("cntg_vi_hour")
        if not isinstance(source_date, dt.date) or not isinstance(source_time, str):
            continue
        try:
            parsed_time = dt.datetime.strptime(source_time, "%H%M%S").time()
        except ValueError:
            continue
        stamps.append(dt.datetime.combine(source_date, parsed_time, tzinfo=timezone))
    return max(stamps) if stamps else session_date


def _ranking_snapshot(rankings: dict[str, Any]) -> dict[str, Any]:
    sections: dict[str, Any] = {}
    stamps = []
    for investor, section in rankings.items():
        stamp = section.ranked_at
        if stamp is not None:
            stamps.append(stamp)
        sections[investor] = {
            "as_of": _wire(stamp),
            "buy": section.buy,
            "sell": section.sell,
        }
    return {"as_of": _wire(max(stamps)) if stamps else None, "data": sections}


async def _trading_flags_snapshot(symbol: str) -> dict[str, Any]:
    from app.core.db import AsyncSessionLocal
    from app.services.kr_trading_flags import vocabulary as v
    from app.services.kr_trading_flags.store import load_flag_records

    async with AsyncSessionLocal() as db:
        records = await load_flag_records(db, [symbol])
    stored = records.get(symbol, {})
    data: dict[str, Any] = {}
    stamps = []
    for category in v.CATEGORIES:
        record = stored.get(category)
        if record is None:
            data[category] = {
                "state": "unknown",
                "raw_code": None,
                "reason_code": None,
                "effective_date": None,
                "source": None,
                "observed_at": None,
            }
            continue
        stamps.append(record.observed_at)
        data[category] = {
            "state": record.state,
            "raw_code": record.raw_code,
            "reason_code": record.reason_code,
            "effective_date": record.effective_date,
            "source": record.source,
            "observed_at": record.observed_at,
        }
    return {
        "as_of": max(stamps) if stamps else None,
        "data": data,
        "status": "ok" if stamps else "empty",
    }


def _has_data(value: Any) -> bool:
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, dict):
        return any(_has_data(item) for item in value.values())
    return value is not None


def _source_result(
    awaitable: Awaitable[Any], normalize: Callable[[Any], Any]
) -> Awaitable[dict[str, Any]]:
    async def run() -> dict[str, Any]:
        try:
            normalized = normalize(await awaitable)
            records = normalized if isinstance(normalized, list) else []
            data = (
                normalized.get("data") if isinstance(normalized, dict) else normalized
            )
            as_of = (
                normalized.get("as_of", normalized.get("observed_at"))
                if isinstance(normalized, dict)
                else _as_of(records)
            )
            status = normalized.get("status") if isinstance(normalized, dict) else None
            return {
                "status": status or ("ok" if _has_data(data) else "empty"),
                "as_of": _wire(as_of),
                "fetched_at": dt.datetime.now(dt.UTC).isoformat(),
                "data": _wire(data),
            }
        except Exception as exc:  # source failures are isolated by contract
            return {
                "status": "error",
                "as_of": None,
                "fetched_at": dt.datetime.now(dt.UTC).isoformat(),
                "error_class": type(exc).__name__,
            }

    return run()


def _error_source(error: Exception) -> Awaitable[dict[str, Any]]:
    async def run() -> dict[str, Any]:
        return {
            "status": "error",
            "as_of": None,
            "fetched_at": dt.datetime.now(dt.UTC).isoformat(),
            "error_class": type(error).__name__,
        }

    return run()


def _guarded_source(
    factory: Callable[[], Awaitable[Any]],
    normalize: Callable[[Any], Any],
    construction_error: Exception | None,
) -> Awaitable[dict[str, Any]]:
    if construction_error is not None:
        return _error_source(construction_error)
    return _source_result(factory(), normalize)


async def handle_get_kr_flow_signals(symbol: str, days: int = 20) -> dict[str, Any]:
    from app.services.brokers.kis.client import KISClient
    from app.services.brokers.toss.client import TossReadClient

    symbol = (symbol or "").strip()
    if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ValueError("symbol must be a six-digit Korean stock code")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 100:
        raise ValueError("days must be an integer from 1 to 100")

    toss = None
    toss_error = None
    try:
        toss = TossReadClient.from_settings()
    except Exception as exc:
        toss_error = exc
    kis = None
    kis_error = None
    try:
        kis = KISClient()
    except Exception as exc:
        kis_error = exc
    session_date = today_kst()
    risk_sources = {
        "kis_vi_status": _guarded_source(
            lambda: kis.inquire_vi_status(symbol, session_date),
            lambda rows: {
                "as_of": _vi_as_of(rows, session_date),
                "data": rows,
            },
            kis_error,
        ),
        "trading_flags": _source_result(
            _trading_flags_snapshot(symbol), lambda value: value
        ),
    }
    reference_sources = {
        "toss_short_selling": _guarded_source(
            lambda: toss.stock_short_selling(symbol, count=days),
            lambda page: {"as_of": _as_of(page.records), "data": page.records},
            toss_error,
        ),
        "toss_securities_lending": _guarded_source(
            lambda: toss.stock_securities_lending(symbol, count=days),
            lambda page: {"as_of": _as_of(page.records), "data": page.records},
            toss_error,
        ),
        "toss_credit_trades": _guarded_source(
            lambda: toss.stock_credit_trades(symbol, count=days),
            lambda page: {"as_of": _as_of(page.records), "data": page.records},
            toss_error,
        ),
        "toss_program_trades": _guarded_source(
            lambda: toss.stock_program_trades(symbol, count=days),
            lambda page: {"as_of": _as_of(page.records), "data": page.records},
            toss_error,
        ),
        "kis_broker_window": _guarded_source(
            lambda: kis.inquire_member(symbol),
            lambda raw: {"as_of": None, "data": raw},
            kis_error,
        ),
        "toss_investor_rankings": _guarded_source(
            lambda: toss.investor_trading_rankings(count=days),
            _ranking_snapshot,
            toss_error,
        ),
    }
    try:
        risk_results, reference_results = await asyncio.gather(
            asyncio.gather(*risk_sources.values()),
            asyncio.gather(*reference_sources.values()),
        )
    finally:
        if toss is not None:
            await asyncio.gather(toss.aclose(), return_exceptions=True)
    return {
        "symbol": symbol,
        "market": "kr",
        "requested_days": days,
        "risk": dict(zip(risk_sources, risk_results, strict=True)),
        "reference": dict(zip(reference_sources, reference_results, strict=True)),
    }


async def handle_get_kr_investor_rankings(
    investor: str = "foreign",
    side: str = "buy",
    limit: int = 20,
) -> dict[str, Any]:
    from app.services.brokers.toss.client import TossReadClient

    investor_key = {
        "foreign": "foreigner",
        "institution": "institution",
        "individual": "individual",
    }.get(investor)
    if investor_key is None:
        raise ValueError("investor must be foreign, institution, or individual")
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")

    try:
        client = TossReadClient.from_settings()
    except Exception as exc:
        source = await _error_source(exc)
        return {
            "market": "kr",
            "investor": investor,
            "side": side,
            "reference": {"toss_investor_rankings": source},
        }
    try:
        try:
            rankings = await client.investor_trading_rankings(count=limit)
            section = rankings[investor_key]
            rows = section.buy if side == "buy" else section.sell
            source = {
                "status": "ok" if rows else "empty",
                "as_of": _wire(section.ranked_at),
                "fetched_at": dt.datetime.now(dt.UTC).isoformat(),
                "data": _wire(rows[:limit]),
            }
        except Exception as exc:
            source = await _error_source(exc)
        return {
            "market": "kr",
            "investor": investor,
            "side": side,
            "reference": {"toss_investor_rankings": source},
        }
    finally:
        await asyncio.gather(client.aclose(), return_exceptions=True)
