from __future__ import annotations

import datetime
import logging
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.manual_holdings import BrokerAccount, ManualHolding, MarketType
from app.models.symbol_trade_settings import SymbolTradeSettings
from app.models.trading import InstrumentType, User
from app.services import kr_ohlcv_timeseries_store
from app.services.kis import KISClient
from app.services.kr_ohlcv_metrics import record_fetch_success
from app.services.kr_trading_calendar import exchange_for_route, get_session_bounds

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
_KR_INTRADAY_MAX_PAGE_CALLS_PER_DAY = 10
_BOOTSTRAP_DAYS = 7
_NIGHTLY_DAYS = 30
_INCREMENTAL_DAYS = 1


def _normalize_symbol(value: str) -> str | None:
    normalized = str(value or "").strip().upper()
    if len(normalized) == 6 and normalized.isalnum():
        return normalized
    return None


def _filter_kr_intraday_session(
    frame: pd.DataFrame,
    route_market: str,
    target_day: datetime.date,
) -> pd.DataFrame:
    if frame.empty or "datetime" not in frame.columns:
        return frame

    session_bounds = get_session_bounds(route_market, target_day)
    if session_bounds is None:
        return frame.iloc[0:0].reset_index(drop=True)

    out = frame.copy()
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.dropna(subset=["datetime"])
    if out.empty:
        return out

    session_start, session_end = session_bounds
    start_ts = pd.Timestamp(session_start)
    end_ts = pd.Timestamp(session_end)
    if out["datetime"].dt.tz is None:
        out["_datetime_kst"] = out["datetime"].dt.tz_localize(_KST)
    else:
        out["_datetime_kst"] = out["datetime"].dt.tz_convert(_KST)

    return (
        out.loc[(out["_datetime_kst"] >= start_ts) & (out["_datetime_kst"] <= end_ts)]
        .drop(columns=["_datetime_kst"])
        .reset_index(drop=True)
    )


async def _page_kr_intraday_day(
    *,
    kis: KISClient,
    symbol: str,
    route_market: str,
    target_day: datetime.date,
) -> pd.DataFrame:
    session_bounds = get_session_bounds(route_market, target_day)
    if session_bounds is None:
        return pd.DataFrame()
    session_start = session_bounds[0].strftime("%H%M%S")
    end_time = session_bounds[1].strftime("%H%M%S")
    merged = pd.DataFrame()

    for _ in range(_KR_INTRADAY_MAX_PAGE_CALLS_PER_DAY):
        intraday = await kis.inquire_time_dailychartprice(
            code=symbol,
            market=route_market,
            n=200,
            end_date=target_day,
            end_time=end_time,
        )
        intraday = _filter_kr_intraday_session(
            intraday,
            route_market,
            target_day,
        )
        if intraday.empty:
            break

        merged = pd.concat([merged, intraday], ignore_index=True)
        merged["datetime"] = pd.to_datetime(merged["datetime"], errors="coerce")
        merged = (
            merged.dropna(subset=["datetime"])
            .drop_duplicates(subset=["datetime"], keep="last")
            .sort_values("datetime")
            .reset_index(drop=True)
        )

        intraday_datetimes = pd.to_datetime(intraday["datetime"], errors="coerce")
        intraday_datetimes = intraday_datetimes.dropna()
        if intraday_datetimes.empty:
            break

        oldest = intraday_datetimes.min()
        next_end_time = (oldest - datetime.timedelta(minutes=1)).strftime("%H%M%S")
        if next_end_time < session_start:
            break
        if next_end_time == end_time:
            break
        end_time = next_end_time

    return merged


def _is_dual_route_canary_symbol(symbol: str) -> bool:
    if settings.KR_OHLCV_DUAL_ROUTE_CANARY_ALL:
        return True
    canary_symbols = {
        str(item).strip().upper()
        for item in settings.KR_OHLCV_DUAL_ROUTE_CANARY_SYMBOLS
        if str(item).strip()
    }
    return str(symbol or "").strip().upper() in canary_symbols


async def _resolve_route(symbol: str) -> list[str] | None:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(KRSymbolUniverse).where(KRSymbolUniverse.symbol == symbol)
            )
        ).scalar_one_or_none()
    if row is None or not row.is_active:
        return None
    normalized_symbol = str(symbol or "").strip().upper()
    if not row.nxt_eligible:
        return ["J"]
    if settings.KR_OHLCV_DUAL_ROUTE_ENABLED and _is_dual_route_canary_symbol(
        normalized_symbol
    ):
        return ["J", "NX"]
    return ["J"]


async def _collect_kr_symbols() -> set[str]:
    async with AsyncSessionLocal() as session:
        active_users = list(
            (
                await session.execute(
                    select(User.id).where(User.is_active.is_(True)).order_by(User.id)
                )
            )
            .scalars()
            .all()
        )

        setting_symbols = list(
            (
                await session.execute(
                    select(SymbolTradeSettings.symbol)
                    .join(User, User.id == SymbolTradeSettings.user_id)
                    .where(User.is_active.is_(True))
                    .where(SymbolTradeSettings.is_active.is_(True))
                    .where(
                        SymbolTradeSettings.instrument_type == InstrumentType.equity_kr
                    )
                )
            )
            .scalars()
            .all()
        )

        manual_symbols = list(
            (
                await session.execute(
                    select(ManualHolding.ticker)
                    .join(
                        BrokerAccount,
                        BrokerAccount.id == ManualHolding.broker_account_id,
                    )
                    .join(User, User.id == BrokerAccount.user_id)
                    .where(User.is_active.is_(True))
                    .where(BrokerAccount.is_active.is_(True))
                    .where(ManualHolding.market_type == MarketType.KR)
                )
            )
            .scalars()
            .all()
        )

    symbols: set[str] = set()
    for symbol in [*setting_symbols, *manual_symbols]:
        normalized = _normalize_symbol(str(symbol or ""))
        if normalized is not None:
            symbols.add(normalized)

    if active_users:
        try:
            holdings = await KISClient().fetch_my_stocks()
            for stock in holdings:
                normalized = _normalize_symbol(str(stock.get("pdno") or ""))
                if normalized is not None:
                    symbols.add(normalized)
        except Exception as exc:
            logger.warning("Failed to collect KIS account symbols: %s", exc)

    return symbols


async def _sync_symbol_minutes(symbol: str, days: int) -> dict[str, int | str]:
    normalized_days = max(int(days), 1)
    route_markets = await _resolve_route(symbol)
    if route_markets is None:
        return {
            "status": "skipped",
            "symbol": symbol,
            "reason": "route_not_found",
            "rows": 0,
        }

    kis = KISClient()
    today_kst = datetime.datetime.now(_KST).date()
    start_day = today_kst - datetime.timedelta(days=normalized_days - 1)

    total_rows = 0
    refresh_min_ts: datetime.datetime | None = None
    refresh_max_ts: datetime.datetime | None = None
    for route_market in route_markets:
        merged = pd.DataFrame()
        current_day = today_kst
        while current_day >= start_day:
            daily = await _page_kr_intraday_day(
                kis=kis,
                symbol=symbol,
                route_market=route_market,
                target_day=current_day,
            )
            if not daily.empty:
                merged = pd.concat([merged, daily], ignore_index=True)
            current_day = current_day - datetime.timedelta(days=1)

        record_fetch_success(route_market)
        if merged.empty:
            continue

        merged["datetime"] = pd.to_datetime(merged["datetime"], errors="coerce")
        merged = (
            merged.dropna(subset=["datetime"])
            .drop_duplicates(subset=["datetime"], keep="last")
            .sort_values("datetime")
            .reset_index(drop=True)
        )

        upsert_result = await kr_ohlcv_timeseries_store.upsert_market_candles_1m(
            symbol=symbol,
            exchange=exchange_for_route(route_market),
            route=route_market,
            frame=merged,
            source="kis",
        )
        total_rows += int(upsert_result.get("rows", 0))
        min_ts = upsert_result.get("min_ts")
        max_ts = upsert_result.get("max_ts")
        if isinstance(min_ts, datetime.datetime):
            if refresh_min_ts is None or min_ts < refresh_min_ts:
                refresh_min_ts = min_ts
        if isinstance(max_ts, datetime.datetime):
            if refresh_max_ts is None or max_ts > refresh_max_ts:
                refresh_max_ts = max_ts

    if refresh_min_ts is not None and refresh_max_ts is not None:
        await kr_ohlcv_timeseries_store.refresh_market_candles_1h_kr(
            start_ts=refresh_min_ts,
            end_ts=refresh_max_ts + datetime.timedelta(hours=1),
        )

    return {
        "status": "completed",
        "symbol": symbol,
        "rows": total_rows,
        "route": ",".join(route_markets),
    }


async def run_kr_ohlcv_incremental_precompute() -> dict[str, int | str]:
    try:
        await kr_ohlcv_timeseries_store.ensure_timescale_ready()
        symbols = await _collect_kr_symbols()

        processed = 0
        inserted_rows = 0
        bootstrapped = 0
        for symbol in sorted(symbols):
            latest_bucket = await kr_ohlcv_timeseries_store.fetch_latest_hourly_bucket(
                symbol=symbol
            )
            sync_days = _BOOTSTRAP_DAYS if latest_bucket is None else _INCREMENTAL_DAYS
            if sync_days == _BOOTSTRAP_DAYS:
                bootstrapped += 1

            result = await _sync_symbol_minutes(symbol, sync_days)
            if result.get("status") == "completed":
                processed += 1
                inserted_rows += int(result.get("rows", 0))

        return {
            "status": "completed",
            "mode": "incremental",
            "symbols": len(symbols),
            "processed": processed,
            "bootstrapped": bootstrapped,
            "inserted_rows": inserted_rows,
        }
    except Exception as exc:
        logger.error("KR OHLCV incremental precompute failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "mode": "incremental",
            "error": str(exc),
        }


async def run_kr_ohlcv_nightly_precompute() -> dict[str, int | str]:
    try:
        await kr_ohlcv_timeseries_store.ensure_timescale_ready()
        symbols = await _collect_kr_symbols()

        processed = 0
        inserted_rows = 0
        bootstrapped = 0
        expanded = 0
        for symbol in sorted(symbols):
            latest_bucket = await kr_ohlcv_timeseries_store.fetch_latest_hourly_bucket(
                symbol=symbol
            )
            sync_days = _BOOTSTRAP_DAYS if latest_bucket is None else _NIGHTLY_DAYS
            if sync_days == _BOOTSTRAP_DAYS:
                bootstrapped += 1
            else:
                expanded += 1

            result = await _sync_symbol_minutes(symbol, sync_days)
            if result.get("status") == "completed":
                processed += 1
                inserted_rows += int(result.get("rows", 0))

        return {
            "status": "completed",
            "mode": "nightly",
            "symbols": len(symbols),
            "processed": processed,
            "bootstrapped": bootstrapped,
            "expanded_to_30d": expanded,
            "inserted_rows": inserted_rows,
        }
    except Exception as exc:
        logger.error("KR OHLCV nightly precompute failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "mode": "nightly",
            "error": str(exc),
        }


__all__ = [
    "run_kr_ohlcv_incremental_precompute",
    "run_kr_ohlcv_nightly_precompute",
]
