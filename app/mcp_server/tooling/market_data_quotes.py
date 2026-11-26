"""Market data utilities: quotes, OHLCV, and technical indicators.

This module contains functions for fetching market data (quotes, OHLCV candles)
and computing technical indicators (SMA, EMA, RSI, MACD, Bollinger, ATR, Pivot,
ADX, Stochastic RSI, OBV, Fibonacci).
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pandas as pd

import app.services.brokers.upbit.client as upbit_service
import app.services.brokers.yahoo.client as yahoo_service
import app.services.market_data as market_data_service
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.symbol import to_db_symbol
from app.core.timezone import now_kst
from app.mcp_server.tooling.market_data_indicators import (
    IndicatorType,
    _calculate_rsi_series,
    _compute_crypto_realtime_rsi_from_frame,
    _compute_indicators,
    _fetch_ohlcv_for_indicators,
)
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    DATA_STATE_PREMARKET_UNAVAILABLE,
    DATA_STATE_STALE,
    DATA_STATE_UNKNOWN,
    KRX_OHLCV_CARRIED_PREVIOUS_CLOSE,
    NXT_NO_TRADE_OBSERVED,
    PRICE_AS_OF_KIND_DATE_ONLY,
    PRICE_AS_OF_KIND_NONE,
    PRICE_AS_OF_KIND_TIME,
    PRICE_UNAVAILABLE_DATE_ONLY,
    PRICE_UNAVAILABLE_PREMARKET,
    US_SESSION_AFTERHOURS,
    US_SESSION_CLOSED,
    US_SESSION_PREMARKET,
    is_kr_session_day,
    kr_market_data_state,
    us_market_session,
)
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
)
from app.mcp_server.tooling.shared import (
    error_payload_from_exception as _error_payload_from_exception,
)
from app.mcp_server.tooling.shared import (
    normalize_market as _normalize_market,
)
from app.mcp_server.tooling.shared import (
    normalize_rows as _normalize_rows,
)
from app.mcp_server.tooling.shared import (
    normalize_symbol_input as _normalize_symbol_input,
)
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)
from app.services import kis_ohlcv_cache, kr_quote_price, krx_after_market
from app.services.brokers.kis.client import KISClient
from app.services.brokers.toss.market_calendar import get_kr_nxt_session_from_toss
from app.services.daily_candles.read_service import rows_to_frame
from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey
from app.services.execution_strength.query_service import compute_execution_strength
from app.services.kr_hourly_candles_read_service import (
    read_kr_hourly_candles_1h,
    read_kr_intraday_candles,
)
from app.services.kr_symbol_universe_service import (
    get_kr_krx_after_tradability,
    get_kr_nxt_tradability,
    search_kr_symbols,
)
from app.services.market_data.constants import (
    CRYPTO_MINUTE_OHLCV_PERIODS,
    CRYPTO_MINUTE_PUBLIC_ROW_KEYS,
    CRYPTO_MINUTE_REQUIRED_SOURCE_COLUMNS,
    KR_INTRADAY_OHLCV_PERIODS,
    ORDERBOOK_ASOF_MAX_AGE_S148_N5,
    US_INTRADAY_OHLCV_PERIODS,
    validate_ohlcv_period,
)
from app.services.market_data.toss_ohlcv import (
    fetch_daily_toss_frame,
    fetch_kr_intraday_toss_frame,
)
from app.services.symbol_analysis.freshness import compute_is_stale
from app.services.upbit_symbol_universe_service import search_upbit_symbols
from app.services.us_intraday_candles_read_service import read_us_intraday_candles
from app.services.us_symbol_universe_service import (
    USSymbolInactiveError,
    USSymbolNotRegisteredError,
    USSymbolUniverseEmptyError,
    get_us_exchange_by_symbol,
    search_us_symbols,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastmcp import FastMCP


logger = logging.getLogger(__name__)


def _to_float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


_OHLCV_INDICATOR_ROW_KEYS = (
    "rsi_14",
    "ema_20",
    "bb_upper",
    "bb_mid",
    "bb_lower",
    "vwap",
)


def _kis_end_date(end_date: datetime.datetime | None) -> pd.Timestamp | None:
    if end_date is None:
        return None
    return pd.Timestamp(end_date.date())


# ---------------------------------------------------------------------------
# ``get_ohlcv(end_date=...)`` row cutoff (hk 6030)
#
# ``end_date`` is an inclusive upper bound for every market, period and data
# path (canonical DB, Redis cache hit/miss, provider fetch).  A date-only value
# ("YYYY-MM-DD") keeps that whole market-local calendar day: KST for crypto and
# KR equity, America/New_York for US equity.  A timestamped value cuts at that
# market-local instant.  Day/week/month bars are judged by their labelled date
# and intraday bars by their timestamp, so a bar labelled on the cutoff day is
# returned and any bar labelled after it never is.
# ---------------------------------------------------------------------------

_OHLCV_MARKET_TZ: dict[str, ZoneInfo] = {
    "crypto": ZoneInfo("Asia/Seoul"),
    "equity_kr": ZoneInfo("Asia/Seoul"),
    "equity_us": ZoneInfo("America/New_York"),
}


@dataclass(frozen=True, slots=True)
class _EndDateCutoff:
    date_only: bool
    local_date: datetime.date
    local_datetime: datetime.datetime


def _end_date_cutoff(
    end_date: datetime.datetime | None,
    *,
    date_only: bool,
    market_type: str,
) -> _EndDateCutoff | None:
    if end_date is None:
        return None
    tz = _OHLCV_MARKET_TZ.get(market_type)
    local = (
        end_date.astimezone(tz).replace(tzinfo=None)
        if tz is not None and end_date.tzinfo is not None
        else end_date
    )
    if date_only:
        local_date = local.date()
        return _EndDateCutoff(
            date_only=True,
            local_date=local_date,
            local_datetime=datetime.datetime.combine(
                local_date, datetime.time(23, 59, 59, 999999)
            ),
        )
    return _EndDateCutoff(
        date_only=False,
        local_date=local.date(),
        local_datetime=local,
    )


def _apply_end_date_cutoff(
    df: pd.DataFrame,
    cutoff: _EndDateCutoff | None,
    *,
    market_type: str,
    count: int,
) -> pd.DataFrame:
    """Keep only rows at or before ``cutoff``, filling ``count`` backwards.

    Applied before indicator enrichment so every indicator is computed from the
    same bars the caller can see.
    """
    if cutoff is None or df.empty:
        return df
    tz = _OHLCV_MARKET_TZ.get(market_type)
    frame = df
    if "datetime" in frame.columns:
        values = pd.to_datetime(frame["datetime"], errors="coerce")
        if isinstance(values.dtype, pd.DatetimeTZDtype) and tz is not None:
            values = values.dt.tz_convert(tz).dt.tz_localize(None)
        if cutoff.date_only:
            mask = values.dt.date <= cutoff.local_date
        else:
            mask = values <= pd.Timestamp(cutoff.local_datetime)
        frame = frame.loc[mask]
    elif "date" in frame.columns:
        values = pd.to_datetime(frame["date"], errors="coerce")
        if isinstance(values.dtype, pd.DatetimeTZDtype) and tz is not None:
            values = values.dt.tz_convert(tz).dt.tz_localize(None)
        frame = frame.loc[values.dt.date <= cutoff.local_date]
    return frame.tail(count).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Canonical daily RSI (ROB-1382)
#
# ``analyze_stock_batch(quick)`` projects rsi_14 from the ``daily_candles``
# store (latest QUICK_CANDLE_COUNT closed bars) via the shared Wilder series
# ``market_data_indicators._calculate_rsi_series``.  For ``period="day"`` the
# ``get_ohlcv(include_indicators=True)`` enrichment must read that same window
# and map the series onto returned rows by date, so both surfaces report the
# identical value for the same symbol+session — regardless of which provider
# served the OHLCV rows themselves.
# ---------------------------------------------------------------------------

_CANONICAL_DAILY_SOURCE = "daily_candles_db"
# Pinned to analysis_quick.QUICK_CANDLE_COUNT — a test asserts parity so the
# two windows can never drift apart again.
_CANONICAL_DAILY_COUNT = 250
_CANONICAL_MARKET_KEY = {
    "equity_kr": MarketKey.KR,
    "equity_us": MarketKey.US,
    "crypto": MarketKey.CRYPTO,
}
# Mirrors analysis_quick's partition selection exactly: KRX for KR, the
# upbit_krw venue lookup for crypto, and no partition filter for US.
_CANONICAL_PARTITION = {"equity_kr": "KRX", "crypto": "upbit_krw"}


async def _canonical_daily_frame(symbol: str, market_type: str) -> pd.DataFrame | None:
    """Read the same daily_candles window that ``analyze_stock_batch(quick)``
    projects (``fetch_recent_batch``, same market/partition/count).

    Fail-open: any DB problem returns ``None`` so the caller computes the RSI
    over the provider frame instead — ``rsi_source`` then names that provider.
    """
    market_key = _CANONICAL_MARKET_KEY.get(market_type)
    if market_key is None:
        return None
    try:
        async with AsyncSessionLocal() as session:
            repo = DailyCandlesRepository(session=session)
            frames = await repo.fetch_recent_batch(
                market=market_key,
                symbols=[symbol],
                partition=_CANONICAL_PARTITION.get(market_type),
                count=_CANONICAL_DAILY_COUNT,
            )
        rows = frames.get(str(symbol).strip().upper(), [])
        if not rows:
            return None
        return rows_to_frame(rows)
    except Exception:
        logger.warning(
            "canonical daily_candles read failed symbol=%s market_type=%s; "
            "falling back to provider frame for rsi_14",
            symbol,
            market_type,
            exc_info=True,
        )
        return None


def _canonical_daily_usable(
    canonical_daily: pd.DataFrame | None, df: pd.DataFrame
) -> bool:
    return (
        canonical_daily is not None
        and not canonical_daily.empty
        and {"date", "close"} <= set(canonical_daily.columns)
        and "date" in df.columns
    )


def _canonical_daily_rsi(
    frame: pd.DataFrame, canonical_daily: pd.DataFrame
) -> pd.Series:
    """RSI-14 per row computed on the canonical daily_candles series and
    mapped onto ``frame`` rows by date.

    A returned row whose date is absent from the canonical store (e.g. the
    still-forming session bar that a live provider includes but the store
    never carries) gets ``None`` — the canonical series does not report an
    RSI for a bar it does not carry.
    """
    canonical_close = pd.to_numeric(canonical_daily["close"], errors="coerce")
    rsi = _calculate_rsi_series(canonical_close).round(2)
    canonical_dates = pd.to_datetime(canonical_daily["date"], errors="coerce").dt.date
    rsi_by_date = {
        d: v for d, v in zip(canonical_dates, rsi, strict=False) if pd.notna(d)
    }
    row_dates = pd.to_datetime(frame["date"], errors="coerce").dt.date
    return pd.Series(
        [rsi_by_date.get(d) if pd.notna(d) else None for d in row_dates],
        index=frame.index,
        dtype="object",
    )


def _rsi_provenance(
    source: str, df: pd.DataFrame, canonical_daily: pd.DataFrame | None
) -> tuple[str, int]:
    """``(rsi_source, bars_used)`` describing the series that fed rsi_14."""
    if _canonical_daily_usable(canonical_daily, df):
        return _CANONICAL_DAILY_SOURCE, len(canonical_daily)
    return source, len(df)


async def _canonical_daily_for(
    symbol: str,
    market_type: str,
    period: str,
    end_date: datetime.datetime | None,
    *,
    include_indicators: bool,
) -> pd.DataFrame | None:
    """Resolve the canonical daily frame when this call should use it.

    Canonical applies only to ``period="day"`` without an ``end_date``: the
    latest-N store cannot answer historical windows, and non-day periods are
    not stored in ``daily_candles``.
    """
    if not include_indicators or period != "day" or end_date is not None:
        return None
    return await _canonical_daily_frame(symbol, market_type)


def _normalize_ohlcv_rows(
    df: pd.DataFrame,
    *,
    period: str,
    include_indicators: bool,
    canonical_daily: pd.DataFrame | None = None,
) -> list[dict[str, Any]]:
    frame = df
    if include_indicators and not df.empty:
        frame = _enrich_ohlcv_with_indicators(
            df, period, canonical_daily=canonical_daily
        )
    return _normalize_rows(frame)


def _normalize_kr_intraday_payload_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "datetime" not in df.columns:
        return df

    frame = df.copy()
    frame["datetime"] = frame["datetime"].map(
        lambda value: (
            value
            if pd.isna(value)
            else pd.Timestamp(value).tz_convert("Asia/Seoul").tz_localize(None)
            if pd.Timestamp(value).tzinfo is not None
            else pd.Timestamp(value)
        )
    )
    return frame


def _build_ohlcv_payload(
    *,
    symbol: str,
    instrument_type: str,
    source: str,
    period: str,
    count: int,
    df: pd.DataFrame,
    include_indicators: bool,
    message: str | None = None,
    canonical_daily: pd.DataFrame | None = None,
) -> dict[str, Any]:
    normalized_df = df
    if instrument_type == "equity_kr" and period in KR_INTRADAY_OHLCV_PERIODS:
        normalized_df = _normalize_kr_intraday_payload_frame(df)

    payload: dict[str, Any] = {
        "symbol": symbol,
        "instrument_type": instrument_type,
        "source": source,
        "period": period,
        "count": count,
        "rows": _normalize_ohlcv_rows(
            normalized_df,
            period=period,
            include_indicators=include_indicators,
            canonical_daily=canonical_daily,
        ),
    }
    if include_indicators:
        payload["indicators_included"] = True
        rsi_source, bars_used = _rsi_provenance(source, normalized_df, canonical_daily)
        payload["rsi_source"] = rsi_source
        payload["bars_used"] = bars_used
    if message is not None:
        payload["message"] = message
    return payload


def _classify_orderbook_pressure(ratio: float | None) -> str | None:
    if ratio is None:
        return None
    if ratio > 2.0:
        return "strong_buy"
    if ratio > 1.3:
        return "buy"
    if ratio >= 0.7:
        return "neutral"
    if ratio >= 0.5:
        return "sell"
    return "strong_sell"


def _build_orderbook_pressure_desc(
    *,
    pressure: str | None,
    total_ask_qty: float,
    total_bid_qty: float,
) -> str | None:
    if pressure is None:
        return None
    if pressure == "neutral":
        return "매수/매도 잔량이 균형권 - 중립"

    if pressure in {"strong_buy", "buy"}:
        if total_ask_qty <= 0:
            return None
        multiplier = total_bid_qty / total_ask_qty
        suffix = "강한 매수 압력" if pressure == "strong_buy" else "매수 압력"
        return f"매수잔량이 매도잔량의 {multiplier:.1f}배 - {suffix}"

    if total_bid_qty <= 0:
        return None
    multiplier = total_ask_qty / total_bid_qty
    suffix = "강한 매도 압력" if pressure == "strong_sell" else "매도 압력"
    return f"매도잔량이 매수잔량의 {multiplier:.1f}배 - {suffix}"


def _calculate_orderbook_spread(
    snapshot: market_data_service.OrderbookSnapshot,
) -> tuple[float | None, float | None]:
    if not snapshot.asks or not snapshot.bids:
        return None, None

    best_ask = snapshot.asks[0].price
    best_bid = snapshot.bids[0].price
    if best_bid <= 0:
        return None, None

    spread = best_ask - best_bid

    spread_pct = round((spread / best_bid) * 100, 3)
    return spread, spread_pct


def _validate_crypto_orderbook_symbol_input(symbol: str | int) -> str:
    value = str(symbol).strip().upper()
    if not value:
        raise ValueError("symbol is required")
    if not value.startswith("KRW-"):
        raise ValueError("crypto orderbook only supports KRW-* symbols")
    return value


_KST = ZoneInfo("Asia/Seoul")


def _current_kst_datetime(now: datetime.datetime | None = None) -> datetime.datetime:
    current = now or now_kst()
    if current.tzinfo is None:
        return current.replace(tzinfo=_KST)
    return current.astimezone(_KST)


async def _nxt_quote_session(
    data_state: str,
    *,
    now: datetime.datetime | None = None,
) -> str | None:
    current = _current_kst_datetime(now)
    toss_session = await get_kr_nxt_session_from_toss(current)
    venue_session = kr_quote_price.quote_session_at(current, "nxt")
    # Do not let a provider calendar's broader label move the quote boundary:
    # research minute labels and quote selection share 08:00 / 16:00 / 20:00.
    if (
        toss_session == "nxt_premarket"
        and venue_session == kr_quote_price.PRICE_SESSION_NXT_PRE
    ):
        return toss_session
    if (
        toss_session == "nxt_after"
        and venue_session == kr_quote_price.PRICE_SESSION_NXT_POST
    ):
        return toss_session
    if toss_session == "closed":
        return None

    if data_state == DATA_STATE_PREMARKET_UNAVAILABLE:
        return "nxt_premarket"

    if not is_kr_session_day(current.date()):
        return None

    if venue_session == kr_quote_price.PRICE_SESSION_NXT_POST:
        return "nxt_after"
    return None


def _krx_regular_close_from_frame(
    frame: pd.DataFrame, *, session_date: datetime.date
) -> tuple[float, str] | None:
    """Find the KRX 15:30 closing-auction bar and preserve its bar timestamp."""
    if frame is None or frame.empty or "datetime" not in frame:
        return None
    for _, row in frame.iterrows():
        raw_at = row.get("datetime")
        if raw_at is None or pd.isna(raw_at):
            continue
        at = pd.Timestamp(raw_at).to_pydatetime()
        if at.tzinfo is None:
            at = at.replace(tzinfo=_KST)
        else:
            at = at.astimezone(_KST)
        if (
            at.date() != session_date
            or at.time().replace(tzinfo=None)
            != kr_quote_price.KRX_AUCTION_WINDOWS[1][1]
        ):
            continue
        close = kr_quote_price.positive_price(row.get("close"))
        if close is not None:
            return close, at.isoformat()
    return None


def _quote_session_ohlcv_from_frame(
    frame: pd.DataFrame, *, venue: str
) -> dict[str, dict[str, Any]]:
    """Aggregate supplied KIS minute bars into venue-specific session OHLCV."""
    from app.services.research_candles.session_segment import (
        KRX_AFTER,
        KRX_REGULAR,
        NXT_POST,
        session_segment_of,
    )

    if frame is None or frame.empty or "datetime" not in frame:
        return {}
    segment_for_venue = {
        "krx": {KRX_REGULAR: "regular", KRX_AFTER: "krx_after"},
        "nxt": {NXT_POST: "nxt_post"},
    }.get(venue, {})
    bars: dict[str, list[dict[str, Any]]] = {}
    for _, row in frame.iterrows():
        raw_at = row.get("datetime")
        if raw_at is None or pd.isna(raw_at):
            continue
        at = pd.Timestamp(raw_at).to_pydatetime()
        if at.tzinfo is None:
            at = at.replace(tzinfo=_KST)
        else:
            at = at.astimezone(_KST)
        segment = session_segment_of("kr", "KRX" if venue == "krx" else "NTX", at)
        # KIS labels the closing-auction print with a 15:30 bar start. The shared
        # research classifier intentionally leaves that boundary minute UNKNOWN;
        # for quote OHLCV the bar is the regular session's closing evidence.
        if (
            venue == "krx"
            and segment == "UNKNOWN"
            and at.time().replace(tzinfo=None)
            == kr_quote_price.KRX_AUCTION_WINDOWS[1][1]
        ):
            segment = KRX_REGULAR
        key = segment_for_venue.get(segment)
        if key is None:
            continue
        bars.setdefault(key, []).append({**row.to_dict(), "_at": at})
    result: dict[str, dict[str, Any]] = {}
    for key, rows in bars.items():
        rows.sort(key=lambda item: item["_at"])
        result[key] = {
            "open": rows[0].get("open"),
            "high": max(
                (row.get("high") for row in rows if row.get("high") is not None),
                default=None,
            ),
            "low": min(
                (row.get("low") for row in rows if row.get("low") is not None),
                default=None,
            ),
            "close": rows[-1].get("close"),
            "volume": sum((float(row.get("volume") or 0) for row in rows), 0.0),
            "source": "kis_minute_chart",
            "as_of": rows[-1]["_at"].isoformat(),
        }
    return result


async def _fetch_krx_regular_close(
    symbol: str, *, session_date: datetime.date
) -> tuple[float, str] | None:
    """Read the KRX closing-auction minute independently of the after-market quote."""
    try:
        frame = await KISClient().inquire_minute_chart(
            code=symbol,
            market="J",
            time_unit=1,
            n=30,
            end_date=session_date,
            end_time="153100",
        )
    except Exception as exc:  # noqa: BLE001 - close provenance is optional quote context
        logger.info("KRX regular close unavailable for %s: %s", symbol, exc)
        return None
    return _krx_regular_close_from_frame(frame, session_date=session_date)


async def _fetch_quote_venue_session_bars(
    kis: Any,
    *,
    symbol: str,
    market: str,
    now: datetime.datetime,
    session_open: datetime.time,
) -> pd.DataFrame:
    """Page today's minute bars back to the venue session open."""
    parts: list[pd.DataFrame] = []
    cursor = now.strftime("%H%M%S")
    seen: set[datetime.datetime] = set()
    for _ in range(32):
        frame = await kis.inquire_minute_chart(
            code=symbol,
            market=market,
            time_unit=1,
            n=30,
            end_date=now.date(),
            end_time=cursor,
        )
        if frame is None or frame.empty or "datetime" not in frame:
            break
        fresh_rows = []
        for _, row in frame.iterrows():
            raw_at = row.get("datetime")
            if raw_at is None or pd.isna(raw_at):
                continue
            at = pd.Timestamp(raw_at).to_pydatetime()
            if at.tzinfo is None:
                at = at.replace(tzinfo=_KST)
            else:
                at = at.astimezone(_KST)
            if at not in seen:
                seen.add(at)
                fresh_rows.append(row)
        if not fresh_rows:
            break
        parts.append(pd.DataFrame(fresh_rows))
        oldest = min(
            pd.Timestamp(row["datetime"]).to_pydatetime().replace(tzinfo=_KST)
            if pd.Timestamp(row["datetime"]).tzinfo is None
            else pd.Timestamp(row["datetime"]).to_pydatetime().astimezone(_KST)
            for row in fresh_rows
        )
        if (
            oldest.date() != now.date()
            or oldest.time().replace(tzinfo=None) <= session_open
        ):
            break
        cursor = (oldest - datetime.timedelta(seconds=1)).strftime("%H%M%S")
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


async def _fetch_quote_session_ohlcv(
    symbol: str, *, now: datetime.datetime
) -> dict[str, dict[str, Any]]:
    """Read KRX regular/after and NXT post minute bars for today's sessions."""
    try:
        kis = KISClient()
        krx_result, nxt_result = await asyncio.gather(
            _fetch_quote_venue_session_bars(
                kis,
                symbol=symbol,
                market="J",
                now=now,
                session_open=kr_quote_price.KRX_REGULAR_START,
            ),
            _fetch_quote_venue_session_bars(
                kis,
                symbol=symbol,
                market="NX",
                now=now,
                session_open=kr_quote_price.NXT_POST_START,
            ),
            return_exceptions=True,
        )
    except Exception as exc:  # noqa: BLE001 - supplemental session OHLCV is advisory
        logger.info("KR session OHLCV unavailable for %s: %s", symbol, exc)
        return {}
    result: dict[str, dict[str, Any]] = {}
    if isinstance(krx_result, Exception):
        logger.info("KRX session OHLCV unavailable for %s: %s", symbol, krx_result)
    else:
        result.update(_quote_session_ohlcv_from_frame(krx_result, venue="krx"))
    if isinstance(nxt_result, Exception):
        logger.info("NXT session OHLCV unavailable for %s: %s", symbol, nxt_result)
    else:
        result.update(_quote_session_ohlcv_from_frame(nxt_result, venue="nxt"))
    return result


async def _fetch_krx_after_venue(
    symbol: str, *, now: datetime.datetime
) -> dict[str, Any]:
    """Return KRX book/trade fields during after-market, failing open on read errors."""
    try:
        snapshot = await market_data_service.get_orderbook(symbol, "kr", venue="krx")
    except Exception as exc:  # noqa: BLE001 - KRX venue is supplemental quote context
        logger.info("KRX after-market book unavailable for %s: %s", symbol, exc)
        return {}
    book = kr_quote_price.VenueBook(
        venue="krx",
        best_bid=snapshot.bids[0].price if snapshot.bids else None,
        best_ask=snapshot.asks[0].price if snapshot.asks else None,
        observed_at=snapshot.as_of,
        expected_price=snapshot.expected_price,
    )
    trade = await _fetch_venue_last_trade(symbol, venue="krx", kis_market_code="J")
    selection = kr_quote_price.select_kr_price(
        session=kr_quote_price.PRICE_SESSION_KRX_AFTER,
        session_start=datetime.datetime.combine(
            now.date(), kr_quote_price.NXT_POST_START, tzinfo=_KST
        ),
        now=now,
        venue_books={"krx": book},
        last_trades={"krx": trade} if trade is not None else {},
        primary_venue="krx",
    )
    return selection.venues


def _positive_price(value: float | int | None) -> float | None:
    try:
        if value is None:
            return None
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


# ROB-888: map the internal NXT session label to a consumer-facing session_state.
_NXT_SESSION_STATE = {
    "nxt_premarket": "premarket",
    "nxt_after": "nxt_after",
}


def _annotate_nxt_session_change(
    quote: dict[str, Any], *, session: str, krx_prev_close: float | None
) -> None:
    """ROB-888: stamp self-describing fields onto an NXT-overlaid quote.

    Consumers (e.g. the operator premarket cross-check) can then judge the real
    gap from the MCP quote alone instead of scraping CDP naver, whose premarket
    dump conflates the KRX prior close with the NXT realtime block.

    - ``session_state``: ``premarket`` | ``nxt_after`` (normalized from the NXT
      session label).
    - ``krx_prev_close``: the most recent completed KRX regular close. During
      ``nxt_after`` the caller supplies the 15:30 KRX closing-auction bar, never
      the after-market quote's pre-overlay price. ``None`` when unavailable.
    - ``change_pct``: ``(nxt_price - krx_prev_close) / krx_prev_close * 100``,
      rounded to 2dp. ``None`` (never a fake 0, never raise) when either side is
      missing/non-positive — mirrors the ROB-448 previous_close convention.
    """
    quote["session_state"] = _NXT_SESSION_STATE.get(session, session)
    quote["krx_prev_close"] = krx_prev_close
    nxt_price = _positive_price(quote.get("price"))
    if krx_prev_close is not None and nxt_price is not None:
        quote["change_pct"] = round(
            (nxt_price - krx_prev_close) / krx_prev_close * 100.0, 2
        )
    else:
        quote["change_pct"] = None


_NXT_PRICE_SOURCE_BY_KIND = {
    kr_quote_price.PRICE_KIND_LAST_TRADE: "nxt_last_trade",
    kr_quote_price.PRICE_KIND_MID: "nxt_mid",
    # Historical name kept: the value is the NXT expected (예상체결가).
    kr_quote_price.PRICE_KIND_EXPECTED: "nxt_expected_price",
}


async def _fetch_venue_last_trade(
    symbol: str, *, venue: str, kis_market_code: str
) -> kr_quote_price.LastTrade | None:
    """hk 6498: the venue's latest trade from the KIS minute chart, else None.

    The minute chart is used because inquire-price (FHKST01010100) carries no
    trade time (repo live verification, ROB-485). The latest bar with volume > 0
    gives the trade price (bar close) and the bar start time (minute resolution).
    Fail-open: any error or missing bar returns None, so the selector falls back
    to the firm mid.
    """
    try:
        kis = KISClient()
        frame = await kis.inquire_minute_chart(
            code=symbol, market=kis_market_code, time_unit=1, n=30
        )
    except Exception as exc:  # noqa: BLE001 - last trade is optional evidence
        logger.info("KR last trade unavailable for %s (%s): %s", symbol, venue, exc)
        return None
    if frame is None or frame.empty:
        return None
    for _, row in frame.iloc[::-1].iterrows():
        volume = kr_quote_price.positive_price(row.get("volume"))
        price = kr_quote_price.positive_price(row.get("close"))
        bar_start = row.get("datetime")
        if volume is None or price is None or pd.isna(bar_start):
            continue
        traded_at = pd.Timestamp(bar_start).to_pydatetime().replace(tzinfo=_KST)
        return kr_quote_price.LastTrade(venue=venue, price=price, traded_at=traded_at)
    return None


async def _fetch_nxt_quote_overlay(
    symbol: str,
    *,
    session: str,
) -> dict[str, Any] | None:
    """Read the NXT book and select the representative price (hk 6498).

    Returns None when the NXT book is empty (no overlay). Otherwise returns the
    overlay even when the selection is unavailable (price None), so a one-sided
    or expected-only book never falls back to the KRX prior close as a price.
    """
    try:
        snapshot = await market_data_service.get_orderbook(symbol, "kr", venue="nxt")
    except Exception as exc:
        logger.warning("NXT quote overlay failed for %s: %s", symbol, exc)
        return None
    if snapshot.is_empty_book:
        return None

    price_session, session_start_time = kr_quote_price.nxt_price_session(session)
    trade: kr_quote_price.LastTrade | None = None
    if price_session is not None:
        trade = await _fetch_venue_last_trade(
            symbol,
            venue="nxt",
            kis_market_code=snapshot.kis_market_code or "NX",
        )
    # Read the clock after the trade request so a trade printed during the
    # request is not judged as later than "now".
    now = _current_kst_datetime()
    session_start: datetime.datetime | None = None
    if price_session is not None and session_start_time is not None:
        session_start = datetime.datetime.combine(
            now.date(), session_start_time, tzinfo=_KST
        )

    book = kr_quote_price.VenueBook(
        venue="nxt",
        best_bid=snapshot.bids[0].price if snapshot.bids else None,
        best_ask=snapshot.asks[0].price if snapshot.asks else None,
        observed_at=snapshot.as_of,
        expected_price=snapshot.expected_price,
    )
    selection = kr_quote_price.select_kr_price(
        session=price_session,
        session_start=session_start,
        now=now,
        venue_books={"nxt": book},
        last_trades={"nxt": trade} if trade is not None else {},
        primary_venue="nxt",
    )
    trade_used = selection.price_kind == kr_quote_price.PRICE_KIND_LAST_TRADE
    overlay: dict[str, Any] = {
        "price": selection.price,
        "price_kind": selection.price_kind,
        "price_session": selection.price_session,
        "expected_price": selection.expected_price,
        "expected_price_venue": selection.expected_price_venue,
        "venues": selection.venues,
        "session": session,
        "venue": snapshot.venue or "nxt",
        "price_source": _NXT_PRICE_SOURCE_BY_KIND.get(selection.price_kind),
        "price_as_of": selection.price_as_of.isoformat()
        if selection.price_as_of is not None
        else None,
        "price_as_of_source": "broker" if trade_used else snapshot.price_as_of_source,
    }
    if selection.reason is not None:
        overlay["price_kind_reason"] = selection.reason
    if snapshot.venue_label is not None:
        overlay["venue_label"] = snapshot.venue_label
    if snapshot.kis_market_code is not None:
        overlay["kis_market_code"] = snapshot.kis_market_code
    if snapshot.source_endpoint is not None:
        overlay["source_endpoint"] = snapshot.source_endpoint
    if snapshot.source_tr_id is not None:
        overlay["source_tr_id"] = snapshot.source_tr_id
    return overlay


async def _apply_nxt_quote_overlay(
    symbol: str, quote: dict[str, Any], *, data_state: str
) -> bool:
    """Overlay an NXT-derived price onto ``quote`` during NXT sessions (ROB-725).

    In-place mutation: ``price`` becomes the NXT expected/mid/best price and
    ``price_source``/``session``/``venue``/``data_state`` are tagged. Returns
    ``True`` when applied. Returns ``False`` (no mutation) when not in an NXT
    session or the NXT orderbook is empty. Never raises — fail-open to the base
    quote.
    """
    session = await _nxt_quote_session(data_state)
    if session is None:
        return False
    now = _current_kst_datetime()
    # ROB-888: the daily quote can already include after-market activity. During
    # NXT post, read the KRX 15:30 closing-auction bar as the only close basis.
    krx_close = None
    if session == "nxt_after":
        krx_close = await _fetch_krx_regular_close(symbol, session_date=now.date())
        session_ohlcv = await _fetch_quote_session_ohlcv(symbol, now=now)
    else:
        base_close = _positive_price(quote.get("price"))
        krx_close = (
            (base_close, quote.get("price_as_of")) if base_close is not None else None
        )
        session_ohlcv = {}
    overlay = await _fetch_nxt_quote_overlay(symbol, session=session)
    krx_venue: dict[str, Any] = {}
    if session == "nxt_after":
        quote["session_state"] = "nxt_after"
        if krx_close is not None:
            quote["krx_prev_close"] = krx_close[0]
            quote["krx_prev_close_source"] = "kis_krx_1530_closing_bar"
            quote["krx_prev_close_as_of"] = krx_close[1]
        else:
            quote["krx_prev_close"] = None
            quote["krx_prev_close_source"] = None
            quote["krx_prev_close_as_of"] = None
        if session_ohlcv:
            quote["session_ohlcv"] = session_ohlcv
        krx_venue = await _fetch_krx_after_venue(symbol, now=now)
    if overlay is None:
        if krx_venue:
            quote.setdefault("venues", {}).update(krx_venue)
        return False
    quote.update(overlay)
    if krx_venue:
        quote.setdefault("venues", {}).update(krx_venue)
    if krx_close is not None:
        quote["krx_prev_close"] = krx_close[0]
        quote["krx_prev_close_source"] = (
            "kis_krx_1530_closing_bar"
            if session == "nxt_after"
            else "kis_regular_quote"
        )
        quote["krx_prev_close_as_of"] = krx_close[1]
    elif session == "nxt_after":
        quote["krx_prev_close"] = None
        quote["krx_prev_close_source"] = None
        quote["krx_prev_close_as_of"] = None
    if session_ohlcv:
        quote["session_ohlcv"] = session_ohlcv
    quote["regular_session_data_state"] = data_state
    quote["data_state"] = DATA_STATE_FRESH
    _annotate_nxt_session_change(
        quote,
        session=session,
        krx_prev_close=krx_close[0] if krx_close is not None else None,
    )
    _annotate_orderbook_price_freshness(
        quote,
        quote.get("price_as_of"),
        require_trading_date=True,
    )
    if quote.get("price_kind") == kr_quote_price.PRICE_KIND_EXPECTED:
        _mark_expected_not_order_basis(quote)
    return True


def _mark_expected_not_order_basis(quote: dict[str, Any]) -> None:
    """hk 6498: an expected price is a representative price only in an auction.

    It is never an order basis, so it is marked unusable whatever its time.
    """
    quote.update(
        {
            "is_stale_price": True,
            "price_freshness": "unavailable",
            "price_usable": False,
            "price_unavailable_reason": kr_quote_price.REASON_EXPECTED_NOT_ORDER_BASIS,
        }
    )


async def _apply_krx_auction_quote(
    symbol: str,
    quote: dict[str, Any],
    *,
    data_state: str,
    now: datetime.datetime,
) -> bool:
    """hk 6498: KRX single-price auction windows use the KRX book's expected price.

    Active only inside the KRX pre-open / closing auction windows on a KRX
    session day. Reads the KRX venue book; a read failure returns False and
    leaves the base quote unchanged (fail-open). In an auction the expected
    price is the representative price but is marked not usable as an order basis.
    """
    if not kr_quote_price.in_krx_auction(now) or not is_kr_session_day(now.date()):
        return False
    try:
        snapshot = await market_data_service.get_orderbook(symbol, "kr", venue="krx")
    except Exception as exc:
        logger.warning("KRX auction quote failed for %s: %s", symbol, exc)
        return False
    book = kr_quote_price.VenueBook(
        venue="krx",
        best_bid=snapshot.bids[0].price if snapshot.bids else None,
        best_ask=snapshot.asks[0].price if snapshot.asks else None,
        observed_at=snapshot.as_of,
        expected_price=snapshot.expected_price,
    )
    selection = kr_quote_price.select_kr_price(
        session=kr_quote_price.PRICE_SESSION_AUCTION,
        session_start=None,
        now=now,
        venue_books={"krx": book},
        last_trades={},
        primary_venue="krx",
    )
    quote.update(
        {
            "data_state": data_state,
            "price": selection.price,
            "price_kind": selection.price_kind,
            "price_session": selection.price_session,
            "expected_price": selection.expected_price,
            "expected_price_venue": selection.expected_price_venue,
            "venues": selection.venues,
            "price_source": "krx_expected_price"
            if selection.price_kind == kr_quote_price.PRICE_KIND_EXPECTED
            else None,
            "price_as_of": selection.price_as_of.isoformat()
            if selection.price_as_of is not None
            else None,
        }
    )
    if selection.reason is not None:
        quote["price_kind_reason"] = selection.reason
    _annotate_orderbook_price_freshness(
        quote,
        quote.get("price_as_of"),
        require_trading_date=True,
        now=now,
    )
    if selection.price_kind == kr_quote_price.PRICE_KIND_EXPECTED:
        _mark_expected_not_order_basis(quote)
    elif selection.price_kind == kr_quote_price.PRICE_KIND_UNAVAILABLE:
        quote["price_usable"] = False
    return True


def _with_base_price_kind(quote: dict[str, Any], data_state: str) -> dict[str, Any]:
    """hk 6498: label the KRX base price without changing any existing key.

    A fresh regular-session quote carries the KIS daily-chart close, which is the
    last trade of the session. Other states carry no live price kind.
    """
    if "price_kind" in quote:
        return quote
    if data_state == DATA_STATE_FRESH:
        quote["price_kind"] = kr_quote_price.PRICE_KIND_LAST_TRADE
        quote["price_session"] = kr_quote_price.PRICE_SESSION_REGULAR
        return quote
    quote["price_kind"] = kr_quote_price.PRICE_KIND_UNAVAILABLE
    quote["price_session"] = None
    quote["price_kind_reason"] = (
        "premarket_unavailable"
        if data_state == DATA_STATE_PREMARKET_UNAVAILABLE
        else "krx_market_closed"
    )
    return quote


def _price_as_of_kind(as_of: Any) -> str:
    """hk 6300: classify a price_as_of value without guessing a trade time.

    A KST midnight value with no time component is the daily-candle date, not an
    exchange trade time, so it is reported as ``date_only``.
    """
    parsed = _parse_price_as_of(as_of)
    if parsed is None:
        return PRICE_AS_OF_KIND_NONE
    if (
        parsed.hour == 0
        and parsed.minute == 0
        and parsed.second == 0
        and parsed.microsecond == 0
    ):
        return PRICE_AS_OF_KIND_DATE_ONLY
    return PRICE_AS_OF_KIND_TIME


def _mark_krx_ohlcv_carried(quote: dict[str, Any]) -> None:
    """hk 6300: before the KRX open the daily row is the prior session's carry.

    open/high/low keep their values but are marked carried; volume and value
    become null so a carried zero is never read as an observed trade count.
    """
    quote["krx_ohlcv_basis"] = KRX_OHLCV_CARRIED_PREVIOUS_CLOSE
    quote["volume"] = None
    quote["value"] = None


def _mark_premarket_price_unusable(quote: dict[str, Any]) -> None:
    """hk 6300: a KR premarket_unavailable price is never usable.

    A date-only price_as_of is never fresh here; a time-bearing one is unusable
    because the KRX session has not opened. Missing as_of keeps the existing
    ``missing_price_asof`` reason from the freshness annotator.
    """
    as_of_kind = _price_as_of_kind(quote.get("price_as_of"))
    quote["price_as_of_kind"] = as_of_kind
    if as_of_kind == PRICE_AS_OF_KIND_NONE:
        return
    quote.update(
        {
            "is_stale_price": True,
            "price_freshness": "stale",
            "price_usable": False,
            "price_unavailable_reason": (
                PRICE_UNAVAILABLE_DATE_ONLY
                if as_of_kind == PRICE_AS_OF_KIND_DATE_ONLY
                else PRICE_UNAVAILABLE_PREMARKET
            ),
        }
    )


async def _kr_premarket_quote(
    symbol: str, quote: dict[str, Any], *, nxt_tradable: bool
) -> dict[str, Any]:
    """hk 6300: KR get_quote before the KRX open (data_state premarket_unavailable).

    The NXT orderbook is consulted only for symbols NXT reports as tradable now.
    Anything else keeps the carried KRX prior close, marked carried and unusable.
    """
    _mark_krx_ohlcv_carried(quote)
    if nxt_tradable and await _apply_nxt_quote_overlay(
        symbol, quote, data_state=DATA_STATE_PREMARKET_UNAVAILABLE
    ):
        if quote.get("price_kind") != kr_quote_price.PRICE_KIND_LAST_TRADE:
            quote["nxt_trade_state"] = NXT_NO_TRADE_OBSERVED
        quote["price_as_of_kind"] = _price_as_of_kind(quote.get("price_as_of"))
        return quote
    quote["data_state"] = DATA_STATE_PREMARKET_UNAVAILABLE
    _mark_premarket_price_unusable(quote)
    return quote


def _build_orderbook_walls_for_side(
    levels: list[market_data_service.OrderbookLevel],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    values: list[int] = []

    for level in levels:
        value_krw = int(round(level.price * level.quantity))
        if value_krw <= 0:
            continue
        values.append(value_krw)
        candidates.append(
            {
                "price": level.price,
                "size": level.quantity,
                "value_krw": value_krw,
            }
        )

    if not values:
        return []

    baseline = median(values)
    if baseline <= 0:
        return []

    walls = [entry for entry in candidates if entry["value_krw"] >= baseline * 2]
    walls.sort(key=lambda entry: entry["value_krw"], reverse=True)
    return walls[:3]


def _build_orderbook_walls(
    snapshot: market_data_service.OrderbookSnapshot,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if snapshot.instrument_type != "crypto":
        return [], []
    return (
        _build_orderbook_walls_for_side(snapshot.bids),
        _build_orderbook_walls_for_side(snapshot.asks),
    )


def _build_orderbook_payload(
    snapshot: market_data_service.OrderbookSnapshot,
) -> dict[str, Any]:
    """Build an orderbook response with deliberately asymmetric freshness fields.

    Crypto orderbooks get the N=5-minute freshness gate here because Upbit has
    no separate KR session/date predicate.  KR orderbooks expose ``as_of`` and
    its provenance only; NXT quote consumers apply the stricter date + age gate
    when they have the session context.  This keeps the generic KR orderbook
    payload observational and preserves that intended division of responsibility.
    """
    pressure = _classify_orderbook_pressure(snapshot.bid_ask_ratio)
    spread, spread_pct = _calculate_orderbook_spread(snapshot)
    bid_walls, ask_walls = _build_orderbook_walls(snapshot)
    payload: dict[str, Any] = {
        "symbol": snapshot.symbol,
        "instrument_type": snapshot.instrument_type,
        "source": snapshot.source,
        "asks": [
            {"price": level.price, "quantity": level.quantity}
            for level in snapshot.asks
        ],
        "bids": [
            {"price": level.price, "quantity": level.quantity}
            for level in snapshot.bids
        ],
        "total_ask_qty": snapshot.total_ask_qty,
        "total_bid_qty": snapshot.total_bid_qty,
        "bid_ask_ratio": snapshot.bid_ask_ratio,
        "pressure": pressure,
        "pressure_desc": _build_orderbook_pressure_desc(
            pressure=pressure,
            total_ask_qty=snapshot.total_ask_qty,
            total_bid_qty=snapshot.total_bid_qty,
        ),
        "spread": spread,
        "spread_pct": spread_pct,
        "expected_price": snapshot.expected_price,
        "expected_qty": snapshot.expected_qty,
        "price_as_of_source": snapshot.price_as_of_source,
        "bid_walls": bid_walls,
        "ask_walls": ask_walls,
    }
    if snapshot.as_of is not None:
        payload["as_of"] = snapshot.as_of.isoformat()
    if snapshot.instrument_type == "crypto":
        _annotate_orderbook_price_freshness(
            payload,
            snapshot.as_of,
            require_trading_date=False,
        )
    if snapshot.venue is not None:
        payload["venue"] = snapshot.venue
    if snapshot.venue_label is not None:
        payload["venue_label"] = snapshot.venue_label
    if snapshot.kis_market_code is not None:
        payload["kis_market_code"] = snapshot.kis_market_code
    if snapshot.source_endpoint is not None:
        payload["source_endpoint"] = snapshot.source_endpoint
    if snapshot.source_tr_id is not None:
        payload["source_tr_id"] = snapshot.source_tr_id
    if snapshot.is_empty_book is not None:
        payload["is_empty_book"] = snapshot.is_empty_book
    if snapshot.requires_final_recheck is not None:
        payload["requires_final_recheck"] = snapshot.requires_final_recheck
    if snapshot.empty_reason is not None:
        payload["empty_reason"] = snapshot.empty_reason
    return payload


# ---------------------------------------------------------------------------
# Symbol Search
# ---------------------------------------------------------------------------


async def _search_master_data(
    query: str, limit: int, instrument_type: str | None = None
) -> list[dict[str, Any]]:
    """Search symbols across KRX, US, and Upbit master datasets."""
    results: list[dict[str, Any]] = []

    if instrument_type is None or instrument_type == "equity_kr":
        kr_results = await search_kr_symbols(query, limit)
        results.extend(kr_results)
        if len(results) >= limit:
            return results

    if instrument_type is None or instrument_type == "equity_us":
        remaining = limit - len(results)
        if remaining > 0:
            us_results = await search_us_symbols(query, remaining)
            results.extend(us_results)
            if len(results) >= limit:
                return results

    if instrument_type is None or instrument_type == "crypto":
        remaining = limit - len(results)
        if remaining > 0:
            crypto_results = await search_upbit_symbols(query, remaining)
            results.extend(crypto_results)
            if len(results) >= limit:
                return results

    return results


# ---------------------------------------------------------------------------
# Quote Fetching
# ---------------------------------------------------------------------------


def _parse_price_as_of(value: Any) -> datetime.datetime | None:
    """Parse a provider timestamp without turning integer indexes into epoch data.

    ROB-1121: Reject timezone-naive datetime objects at the freshness boundary.
    Provider timestamps must be timezone-aware (or constructed as KST-aware).
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime) and value.tzinfo is None:
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(timestamp) or timestamp.value <= 0:
        return None
    dt = timestamp.to_pydatetime()
    if dt.tzinfo is None:
        return None
    return dt.astimezone(_KST)


def _kr_price_as_of_from_frame(df: pd.DataFrame) -> datetime.datetime | None:
    """Read a real candle date; RangeIndex values are not timestamps."""
    if df.empty:
        return None
    val = None
    row_value = df.iloc[-1].get("date")
    if row_value is not None and not pd.isna(row_value):
        val = row_value
    elif isinstance(df.index, pd.DatetimeIndex):
        val = df.index[-1]
    if val is None:
        return None
    try:
        timestamp = pd.Timestamp(val)
        if pd.isna(timestamp) or timestamp.value <= 0:
            return None
        dt = timestamp.to_pydatetime()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_KST)
        return dt
    except (TypeError, ValueError, OverflowError):
        return None


def _annotate_kr_price_freshness(
    quote: dict[str, Any],
    as_of: Any,
    *,
    trading_date: datetime.date | None = None,
) -> None:
    """Expose whether a KR quote is safe to consume as a current price."""
    parsed = _parse_price_as_of(as_of)
    quote["price_as_of"] = parsed.isoformat() if parsed is not None else None
    if parsed is None:
        quote.update(
            {
                "is_stale_price": True,
                "price_freshness": "unavailable",
                "price_usable": False,
                "price_unavailable_reason": "missing_price_asof",
            }
        )
        return

    stale = compute_is_stale(
        "price",
        parsed,
        trading_date=trading_date or now_kst().date(),
    )
    quote["is_stale_price"] = stale
    quote["price_freshness"] = "stale" if stale else "fresh"
    quote["price_usable"] = not stale
    if stale:
        quote["price_unavailable_reason"] = "stale_price_asof"
    else:
        quote.pop("price_unavailable_reason", None)


def _annotate_orderbook_price_freshness(
    quote: dict[str, Any],
    as_of: Any,
    *,
    trading_date: datetime.date | None = None,
    require_trading_date: bool,
    now: datetime.datetime | None = None,
) -> None:
    """Apply the orderbook-only N=5분 freshness gate.

    The existing date predicate remains authoritative for NXT and is combined
    with the bounded wall-clock predicate.  Upbit has no KST trading-date
    gate, but uses the same age/future bound.  This helper is intentionally
    separate so other ``compute_is_stale`` consumers keep their contract.
    """
    parsed = _parse_price_as_of(as_of)
    quote["price_as_of"] = parsed.isoformat() if parsed is not None else None
    if parsed is None:
        quote.update(
            {
                "is_stale_price": True,
                "price_freshness": "unavailable",
                "price_usable": False,
                "price_unavailable_reason": "missing_price_asof",
            }
        )
        return

    current = _current_kst_datetime(now)
    date_stale = require_trading_date and compute_is_stale(
        "price", parsed, trading_date=trading_date or current.date()
    )
    time_stale = parsed > current or current - parsed > ORDERBOOK_ASOF_MAX_AGE_S148_N5
    stale = date_stale or time_stale
    quote["is_stale_price"] = stale
    quote["price_freshness"] = "stale" if stale else "fresh"
    quote["price_usable"] = not stale
    if stale:
        quote["price_unavailable_reason"] = "stale_price_asof"
    else:
        quote.pop("price_unavailable_reason", None)


async def _fetch_quote_crypto(symbol: str) -> dict[str, Any]:
    """Fetch crypto quote from Upbit."""
    prices = await upbit_service.fetch_multiple_current_prices([symbol])
    price = prices.get(symbol)
    if price is None:
        raise ValueError(f"Symbol '{symbol}' not found")
    return {
        "symbol": symbol,
        "instrument_type": "crypto",
        "price": price,
        "source": "upbit",
    }


async def _fetch_quote_equity_kr(symbol: str) -> dict[str, Any]:
    """Fetch Korean equity quote from KIS."""
    kis = KISClient()
    df = await kis.inquire_daily_itemchartprice(
        code=symbol,
        market="J",
        n=2,  # ROB-448: 2 candles so we can surface the prior trading day's close
    )
    if df.empty:
        raise ValueError(f"Symbol '{symbol}' not found")
    last = df.iloc[-1].to_dict()
    # ROB-448: previous trading day's close (mirror the US path). None — never 0, never
    # raise — when <2 candles (newly listed / sparse history). Consumers derive
    # %change = (price - previous_close) / previous_close * 100.
    previous_close: float | None = None
    if len(df) >= 2:
        prev_close_raw = df.iloc[-2].to_dict().get("close")
        if prev_close_raw is not None and not pd.isna(prev_close_raw):
            previous_close = float(prev_close_raw)
    quote = {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "price": last.get("close"),
        "previous_close": previous_close,
        "open": last.get("open"),
        "high": last.get("high"),
        "low": last.get("low"),
        "volume": last.get("volume"),
        "value": last.get("value"),
        "source": "kis",
    }
    _annotate_kr_price_freshness(quote, _kr_price_as_of_from_frame(df))
    return quote


async def _fetch_kr_live_quote(symbol: str) -> dict[str, Any] | None:
    """analyze 전용: KR 라이브 현재가(KIS inquire_price, stck_prpr) + as_of.

    공유 _fetch_quote_equity_kr(orders/portfolio 사용)는 건드리지 않는다.
    실패/빈응답이면 None (호출자가 일봉으로 fallback).

    ROB-1121:
    - provider timestamp(체결일자+시각)가 부재하면 price_as_of를 None으로 두고
      freshness는 downstream _annotate_kr_price_freshness가 unavailable/fail-closed
      로 표현한다. 날짜만 있을 때 자정을 합성하지 않는다.
    - provider timestamp와 무관한 호출/수신 시각은 fetched_at으로만 보존.
    """
    kis = KISClient()
    try:
        df = await kis.inquire_price(code=symbol, market="J")
    except Exception:
        return None
    if df.empty:
        return None

    row = df.iloc[0].to_dict()  # index=종목코드
    as_of: datetime.datetime | None = None
    date_val = row.get("date")
    time_val = row.get("time")
    # ROB-1121: date/time 중 하나라도 provider가 주지 않으면 합성하지 않는다.
    # provider timestamp는 KST-aware로 생성한다.
    if (
        date_val is not None
        and not pd.isna(date_val)
        and isinstance(time_val, datetime.time)
        and not pd.isna(time_val)
    ):
        try:
            d = pd.Timestamp(date_val).to_pydatetime()
        except (TypeError, ValueError, OverflowError):
            d = None
        if d is not None:
            as_of = datetime.datetime.combine(d.date(), time_val, tzinfo=_KST)

    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "price": row.get("close"),  # stck_prpr → close
        "open": row.get("open"),
        "high": row.get("high"),
        "low": row.get("low"),
        "volume": row.get("volume"),
        "value": row.get("value"),
        "source": "kis",
        "price_as_of": as_of.isoformat() if as_of is not None else None,
        "fetched_at": now_kst().isoformat(),
    }


async def _fetch_us_quote_from_kis(normalized_symbol: str) -> dict[str, Any] | None:
    """KIS 해외 현재가(HHDFS00000300) primary arm.

    dict → 성공. None → Yahoo fallback 신호(KIS가 응답했으나 무가격, 또는
    거래소 미해석). KIS HTTP/transport 에러는 호출자가 infra로 처리하도록 전파.
    """
    try:
        exchange_code = await get_us_exchange_by_symbol(to_db_symbol(normalized_symbol))
    except (
        USSymbolNotRegisteredError,
        USSymbolInactiveError,
        USSymbolUniverseEmptyError,
    ):
        return None

    df = await KISClient().inquire_overseas_price(normalized_symbol, exchange_code)
    if df.empty:
        return None
    row = df.iloc[0].to_dict()
    price = _to_float_or_none(row.get("close"))
    if price is None or price <= 0:
        return None
    quote = {
        "symbol": normalized_symbol,
        "instrument_type": "equity_us",
        "price": price,
        "previous_close": _to_float_or_none(row.get("previous_close")),
        "open": None,
        "high": None,
        "low": None,
        "volume": _to_int_or_none(row.get("volume")),
        "source": "kis_overseas",
        "delayed": True,
        "venue": exchange_code,
        "price_source": "kis_overseas_last",
    }
    quote_asof = _optional_text(row.get("quote_asof"))
    if quote_asof is not None:
        quote["quote_asof"] = quote_asof
    return quote


def _is_yahoo_symbol_not_found_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "quote not found",
            "symbol not found",
            "not found for symbol",
        )
    )


_US_MARKET_CLOSED_REASON = "us_market_closed"


def _optional_text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _tag_us_quote_session(
    quote: dict[str, Any], *, now: datetime.datetime | None = None
) -> dict[str, Any]:
    session = us_market_session(now)
    quote["session"] = session
    if session == US_SESSION_CLOSED:
        quote["data_state"] = DATA_STATE_STALE
        quote["data_state_reason"] = _US_MARKET_CLOSED_REASON
    else:
        quote["data_state"] = DATA_STATE_FRESH
        quote.pop("data_state_reason", None)
    if "price_source" not in quote:
        quote["price_source"] = (
            "kis_overseas_last"
            if quote.get("source") == "kis_overseas"
            else "yahoo_fast_info_close"
        )
    return quote


# hk 6521: freshness of a Yahoo extended-hours overlay, decided from the age of
# the provider bar time (``quote_asof``) against the request time. Thresholds
# are per session; an overlay older than its threshold is ``stale`` and never
# ``fresh``. A bar stamped more than this far in the future is unverifiable and
# reported as ``unknown`` (clock skew between provider and AT is tolerated up
# to this bound).
EXTENDED_QUOTE_MAX_AGE_BY_SESSION: dict[str, datetime.timedelta] = {
    US_SESSION_PREMARKET: datetime.timedelta(minutes=5),
    US_SESSION_AFTERHOURS: datetime.timedelta(minutes=5),
}
EXTENDED_QUOTE_MAX_FUTURE_SKEW = datetime.timedelta(seconds=60)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _classify_extended_quote_freshness(
    quote_asof: str | None,
    *,
    session: str,
    request_at: datetime.datetime,
) -> tuple[str, str | None, int | None]:
    """Return ``(data_state, data_state_reason, age_seconds)`` for an overlay.

    Pure: no I/O. ``age_seconds`` is ``request_at - quote_asof`` truncated to
    whole seconds, or ``None`` when the bar time cannot be read.
    """
    if quote_asof is None:
        return DATA_STATE_UNKNOWN, "quote_asof_missing", None
    try:
        asof = datetime.datetime.fromisoformat(quote_asof)
    except ValueError:
        return DATA_STATE_UNKNOWN, "quote_asof_unparsable", None
    if asof.tzinfo is None:
        # A naive bar time has no provable zone; it cannot be aged honestly.
        return DATA_STATE_UNKNOWN, "quote_asof_unparsable", None
    age = request_at - asof
    age_seconds = int(age.total_seconds())
    if age < -EXTENDED_QUOTE_MAX_FUTURE_SKEW:
        return DATA_STATE_UNKNOWN, "quote_asof_in_future", age_seconds
    threshold = EXTENDED_QUOTE_MAX_AGE_BY_SESSION.get(session)
    if threshold is None:
        return DATA_STATE_UNKNOWN, "session_threshold_unavailable", age_seconds
    if age > threshold:
        return (
            DATA_STATE_STALE,
            f"extended_quote_age_{age_seconds}s_exceeds_"
            f"{int(threshold.total_seconds())}s",
            age_seconds,
        )
    return DATA_STATE_FRESH, None, age_seconds


async def _apply_extended_hours_overlay(
    quote: dict[str, Any],
    symbol: str,
    include_extended_hours: bool,
    *,
    clock: Callable[[], datetime.datetime] = _utc_now,
) -> dict[str, Any]:
    """ROB-922: opt-in Yahoo prepost overlay for US premarket/afterhours quotes.

    Only attempted when the caller opts in AND the tagged session is an
    extended session (premarket/afterhours) — regular-session and closed
    quotes are untouched (no-op), and KR/crypto callers never reach this
    helper at all. On any failure or empty prepost data the existing quote
    and its labels are kept as-is — never lie about price_source.

    hk 6521: the overlay's ``data_state`` is decided from the age of the
    prepost bar time against the request time (see
    ``_classify_extended_quote_freshness``): ``fresh`` within the session
    threshold, ``stale`` beyond it (reason names the age), ``unknown`` when
    the bar time is missing or unreadable. The overlay reports ``quote_asof``
    (provider bar time), ``received_at`` (when AT fetched it) and
    ``quote_age_seconds``. Only the price and its time come from the prepost
    bar; the 1-minute bar volume is never copied into ``volume``. ``delayed``
    is left as the base quote set it.
    """
    if not include_extended_hours:
        return quote
    if quote.get("session") not in (US_SESSION_PREMARKET, US_SESSION_AFTERHOURS):
        return quote
    request_at = clock()
    try:
        prepost = await yahoo_service.fetch_prepost_quote(symbol)
    except Exception as exc:  # noqa: BLE001 — best-effort overlay, never breaks the base quote
        logger.warning(
            "Yahoo prepost overlay failed for '%s'; keeping existing quote: %s",
            symbol,
            exc,
        )
        return quote
    if prepost is None:
        return quote
    price = _to_float_or_none(prepost.get("price"))
    if price is None:
        return quote
    received_at = clock()
    quote["price"] = price
    quote["price_source"] = "yahoo_prepost_last"
    # The prepost bar time, not the base quote's time: when Yahoo omits it,
    # quote_asof is null rather than the base quote's (unrelated) timestamp.
    quote_asof = _optional_text(prepost.get("quote_asof"))
    data_state, reason, age_seconds = _classify_extended_quote_freshness(
        quote_asof, session=quote["session"], request_at=request_at
    )
    quote["quote_asof"] = quote_asof
    quote["received_at"] = received_at.isoformat()
    quote["quote_age_seconds"] = age_seconds
    quote["data_state"] = data_state
    quote["is_stale_price"] = data_state != DATA_STATE_FRESH
    if reason is None:
        quote.pop("data_state_reason", None)
    else:
        quote["data_state_reason"] = reason
    return quote


async def _fetch_quote_equity_us(
    symbol: str, *, include_extended_hours: bool = False
) -> dict[str, Any]:
    """Fetch US equity quote.

    ROB-471: KIS 해외 현재가 primary(settings.us_quote_kis_primary), Yahoo
    fast_info fallback. 정직 에러 분리:
      - provider 명시적 not-found → symbol_not_found (ValueError)
      - fast_info 정상응답·무가격 → quote_unavailable (RuntimeError)

    ROB-922: include_extended_hours=True opportunistically overlays the
    latest Yahoo prepost (extended-hours) price during premarket/afterhours
    sessions via ``_apply_extended_hours_overlay``. Default False leaves the
    result byte-identical to the pre-ROB-922 payload.
    """
    normalized_symbol = str(symbol or "").strip().upper()
    not_found_message = f"Symbol '{normalized_symbol}' not found"
    unavailable_message = f"US quote temporarily unavailable for '{normalized_symbol}'"

    kis_infra_error = False
    if settings.us_quote_kis_primary:
        try:
            kis_quote = await _fetch_us_quote_from_kis(normalized_symbol)
        except Exception as exc:  # noqa: BLE001 — KIS infra 실패 시 Yahoo로 degrade
            kis_infra_error = True
            logger.warning(
                "KIS overseas quote failed for '%s'; falling back to Yahoo: %s",
                normalized_symbol,
                exc,
            )
        else:
            if kis_quote is not None:
                return await _apply_extended_hours_overlay(
                    _tag_us_quote_session(kis_quote),
                    normalized_symbol,
                    include_extended_hours,
                )

    # FALLBACK: Yahoo fast_info
    try:
        fast_info = await yahoo_service.fetch_fast_info(normalized_symbol)
    except Exception as exc:
        if _is_yahoo_symbol_not_found_error(exc):
            raise ValueError(not_found_message) from exc
        raise RuntimeError(
            f"{unavailable_message} (yahoo fallback failed): {exc}"
        ) from exc

    price = _to_float_or_none(fast_info.get("close"))
    if price is None or price <= 0:
        if kis_infra_error:
            raise RuntimeError(
                f"{unavailable_message} (kis errored, yahoo returned no price)"
            )
        raise RuntimeError(f"{unavailable_message} (yahoo returned no price)")

    return await _apply_extended_hours_overlay(
        _tag_us_quote_session(
            {
                "symbol": normalized_symbol,
                "instrument_type": "equity_us",
                "price": price,
                "previous_close": _to_float_or_none(fast_info.get("previous_close")),
                "open": _to_float_or_none(fast_info.get("open")),
                "high": _to_float_or_none(fast_info.get("high")),
                "low": _to_float_or_none(fast_info.get("low")),
                "volume": _to_int_or_none(fast_info.get("volume")),
                "source": "yahoo",
                "delayed": True,
                "price_source": "yahoo_fast_info_close",
            }
        ),
        normalized_symbol,
        include_extended_hours,
    )


async def fetch_us_live_last_price(symbol: str) -> float | None:
    """Live US last-price scalar (15-min delayed). Never raises.

    Returns the live intraday last-price for a US equity (from Yahoo fast_info
    last_price/regularMarketPrice), or ``None`` when no live source is reachable
    (delisted/after-hours/Yahoo failure). Callers overlay this onto the OHLCV
    close so the reported ``current_price`` is live during regular trading,
    falling back to the OHLCV close when ``None`` (ROB-365 bug 1).
    """
    try:
        quote = await _fetch_quote_equity_us(symbol)
    except Exception:
        # ValueError (missing/zero price) or RuntimeError (Yahoo fetch failed,
        # incl. the 'NoneType' object is not subscriptable yfinance flake).
        return None

    price = quote.get("price")
    if price is None:
        return None
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# ---------------------------------------------------------------------------
# OHLCV Fetching
# ---------------------------------------------------------------------------


_INTRADAY_OHLCV_PERIODS = frozenset({"1m", "5m", "15m", "30m", "1h", "4h"})


def _format_crypto_minute_timestamp(date_value: Any, time_value: Any) -> str | None:
    if pd.isna(date_value) or pd.isna(time_value):
        return None
    return f"{date_value}T{time_value}"


def _validate_crypto_minute_source_columns(df: pd.DataFrame) -> None:
    missing = [
        column
        for column in CRYPTO_MINUTE_REQUIRED_SOURCE_COLUMNS
        if column not in df.columns
    ]
    if missing:
        missing_text = ", ".join(missing)
        raise ValueError(
            f"Crypto minute OHLCV response missing columns: {missing_text}"
        )


def _calculate_ema_20(close: pd.Series) -> pd.Series:
    ema = close.ewm(span=20, adjust=False).mean()
    return ema.where(close.expanding().count() >= 20)


def _calculate_bollinger_bands(
    close: pd.Series,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    middle = close.rolling(window=20, min_periods=20).mean()
    std = close.rolling(window=20, min_periods=20).std()
    upper = middle + (std * 2)
    lower = middle - (std * 2)
    return upper, middle, lower


def _calculate_vwap(df: pd.DataFrame, period: str) -> pd.Series:
    if period not in _INTRADAY_OHLCV_PERIODS:
        return pd.Series([None] * len(df), index=df.index, dtype=object)

    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cumulative_volume = df["volume"].cumsum()
    vwap = (typical_price * df["volume"]).cumsum() / cumulative_volume.replace(0, pd.NA)
    return vwap


def _enrich_ohlcv_with_indicators(
    df: pd.DataFrame,
    period: str,
    *,
    canonical_daily: pd.DataFrame | None = None,
) -> pd.DataFrame:
    frame = df.copy()
    close = frame["close"].astype(float)
    # ROB-1382: one RSI implementation (shared Wilder series). For daily rows
    # the values come from the canonical daily_candles store — the same bars
    # analyze_stock_batch(quick) reads — mapped onto this frame by date.
    frame["rsi_14"] = (
        _canonical_daily_rsi(frame, canonical_daily)
        if _canonical_daily_usable(canonical_daily, frame)
        else _calculate_rsi_series(close).round(2)
    )
    frame["ema_20"] = _calculate_ema_20(close)
    bb_upper, bb_mid, bb_lower = _calculate_bollinger_bands(close)
    frame["bb_upper"] = bb_upper
    frame["bb_mid"] = bb_mid
    frame["bb_lower"] = bb_lower

    if period in _INTRADAY_OHLCV_PERIODS:
        frame["high"] = frame["high"].astype(float)
        frame["low"] = frame["low"].astype(float)
        frame["volume"] = frame["volume"].astype(float)
        frame["vwap"] = _calculate_vwap(frame, period)
    else:
        frame["vwap"] = None

    return frame


def _normalize_crypto_minute_ohlcv_rows(
    df: pd.DataFrame, *, include_indicators: bool
) -> list[dict[str, Any]]:
    frame = df.copy()
    frame["timestamp"] = [
        _format_crypto_minute_timestamp(date_value, time_value)
        for date_value, time_value in zip(frame["date"], frame["time"], strict=False)
    ]
    frame["trade_amount"] = frame["value"]
    row_keys: list[str] = list(CRYPTO_MINUTE_PUBLIC_ROW_KEYS)
    if include_indicators:
        row_keys.extend(_OHLCV_INDICATOR_ROW_KEYS)
    return _normalize_rows(frame.loc[:, row_keys])


async def _fetch_ohlcv_crypto(
    symbol: str,
    count: int,
    period: str,
    end_date: datetime.datetime | None,
    *,
    include_indicators: bool,
    cutoff: _EndDateCutoff | None = None,
    end_date_is_date_only: bool = False,
) -> dict[str, Any]:
    """Fetch crypto OHLCV from Upbit."""
    capped_count = min(count, 200)
    provider_end_date = end_date
    if end_date is not None and end_date_is_date_only:
        # A date-only request covers the whole KST day.  Upbit's ``to`` cursor
        # is a candle timestamp, so midnight would exclude the cutoff day's
        # later buckets; end-of-day makes the day itself fetchable and the row
        # cutoff below keeps the inclusive boundary.
        provider_end_date = datetime.datetime.combine(
            end_date.date(), datetime.time(23, 59, 59)
        )
    df = await upbit_service.fetch_ohlcv(
        market=symbol, days=capped_count, period=period, end_date=provider_end_date
    )
    df = _apply_end_date_cutoff(df, cutoff, market_type="crypto", count=capped_count)

    if df.empty:
        payload = {
            "symbol": symbol,
            "instrument_type": "crypto",
            "source": "upbit",
            "period": period,
            "count": 0,
            "rows": [],
            "indicators_included": include_indicators,
            "message": f"No candle data available for {symbol}",
        }
        if include_indicators:
            payload["rsi_source"] = "upbit"
            payload["bars_used"] = 0
        return payload

    if period in CRYPTO_MINUTE_OHLCV_PERIODS:
        _validate_crypto_minute_source_columns(df)

    canonical_daily = await _canonical_daily_for(
        symbol, "crypto", period, end_date, include_indicators=include_indicators
    )
    if include_indicators:
        df = _enrich_ohlcv_with_indicators(df, period, canonical_daily=canonical_daily)

    payload: dict[str, Any] = {
        "symbol": symbol,
        "instrument_type": "crypto",
        "source": "upbit",
        "period": period,
        "count": capped_count,
        "indicators_included": include_indicators,
        "rows": (
            _normalize_crypto_minute_ohlcv_rows(
                df, include_indicators=include_indicators
            )
            if period in CRYPTO_MINUTE_OHLCV_PERIODS
            else _normalize_rows(df)
        ),
    }
    if include_indicators:
        rsi_source, bars_used = _rsi_provenance("upbit", df, canonical_daily)
        payload["rsi_source"] = rsi_source
        payload["bars_used"] = bars_used
    return payload


_KST = ZoneInfo("Asia/Seoul")


async def _fetch_ohlcv_equity_kr(
    symbol: str,
    count: int,
    period: str,
    end_date: datetime.datetime | None,
    *,
    include_indicators: bool = False,
    cutoff: _EndDateCutoff | None = None,
) -> dict[str, Any]:
    """Fetch Korean equity OHLCV from KIS."""
    capped_count = min(count, 200)
    kis = KISClient()

    if period == "day":

        async def _raw_fetch_day(requested_count: int):
            return await kis.inquire_daily_itemchartprice(
                code=symbol,
                market="J",
                n=requested_count,
                period="D",
                end_date=_kis_end_date(end_date),
            )

        use_cache = end_date is None and settings.kis_ohlcv_cache_enabled
        if use_cache:
            df = await kis_ohlcv_cache.get_candles(
                symbol=symbol,
                count=capped_count,
                period="day",
                raw_fetcher=_raw_fetch_day,
            )
        else:
            df = await _raw_fetch_day(capped_count)
    elif period == "1h":
        df = await read_kr_hourly_candles_1h(
            symbol=symbol,
            count=capped_count,
            end_date=end_date,
        )
    elif period in KR_INTRADAY_OHLCV_PERIODS:
        try:
            df = await fetch_kr_intraday_toss_frame(
                symbol=symbol,
                period=period,
                count=capped_count,
                end_date=end_date,
            )
            df = _apply_end_date_cutoff(
                df, cutoff, market_type="equity_kr", count=capped_count
            )
            return _build_ohlcv_payload(
                symbol=symbol,
                instrument_type="equity_kr",
                source="toss",
                period=period,
                count=capped_count,
                df=df,
                include_indicators=include_indicators,
            )
        except Exception as toss_exc:
            logger.info(
                "Toss KR intraday OHLCV fallback to KIS (MCP) symbol=%s period=%s error=%s",
                symbol,
                period,
                toss_exc,
            )
            df = await read_kr_intraday_candles(
                symbol=symbol,
                period=period,
                count=capped_count,
                end_date=end_date,
            )
    else:
        kis_period_map = {"week": "W", "month": "M"}
        df = await kis.inquire_daily_itemchartprice(
            code=symbol,
            market="J",
            n=capped_count,
            period=kis_period_map.get(period, "D"),
            end_date=_kis_end_date(end_date),
        )

    df = _apply_end_date_cutoff(df, cutoff, market_type="equity_kr", count=capped_count)
    canonical_daily = await _canonical_daily_for(
        symbol, "equity_kr", period, end_date, include_indicators=include_indicators
    )
    return _build_ohlcv_payload(
        symbol=symbol,
        instrument_type="equity_kr",
        source="kis",
        period=period,
        count=capped_count,
        df=df,
        include_indicators=include_indicators,
        canonical_daily=canonical_daily,
    )


async def _fetch_ohlcv_equity_us(
    symbol: str,
    count: int,
    period: str,
    end_date: datetime.datetime | None,
    *,
    include_indicators: bool = False,
    end_date_is_date_only: bool = False,
    cutoff: _EndDateCutoff | None = None,
) -> dict[str, Any]:
    """Fetch US equity OHLCV - intraday from KIS, daily from Yahoo Finance."""
    # Intraday periods use KIS via DB-first reader
    if period in US_INTRADAY_OHLCV_PERIODS:
        capped_count = min(count, 100)
        df = await read_us_intraday_candles(
            symbol=symbol,
            period=period,
            count=capped_count,
            end_date=end_date,
            end_date_is_date_only=end_date_is_date_only,
        )
        df = _apply_end_date_cutoff(
            df, cutoff, market_type="equity_us", count=capped_count
        )
        return _build_ohlcv_payload(
            symbol=symbol,
            instrument_type="equity_us",
            source="kis",
            period=period,
            count=capped_count,
            df=df,
            include_indicators=include_indicators,
        )

    # day/week/month use Yahoo Finance with Toss day-only fallback
    capped_count = min(count, 100)
    try:
        df = await yahoo_service.fetch_ohlcv(
            ticker=symbol, days=capped_count, period=period, end_date=end_date
        )
        source = "yahoo"
    except Exception:
        if period != "day":
            raise
        df = await fetch_daily_toss_frame(
            symbol=symbol,
            count=capped_count,
            end_date=end_date,
        )
        source = "toss"
    df = _apply_end_date_cutoff(df, cutoff, market_type="equity_us", count=capped_count)
    canonical_daily = await _canonical_daily_for(
        symbol, "equity_us", period, end_date, include_indicators=include_indicators
    )
    return _build_ohlcv_payload(
        symbol=symbol,
        instrument_type="equity_us",
        source=source,
        period=period,
        count=capped_count,
        df=df,
        include_indicators=include_indicators,
        canonical_daily=canonical_daily,
    )


# Tool Registration
# ---------------------------------------------------------------------------

MARKET_DATA_TOOL_NAMES: set[str] = {
    "search_symbol",
    "get_quote",
    "get_orderbook",
    "get_ohlcv",
    "get_indicators",
    "get_execution_strength",
}


async def _get_indicators_impl(
    symbol: str, indicators: list[str], market: str | None = None
) -> dict[str, Any]:
    """Calculate requested indicators for a symbol.

    Supported indicators:
    - adx: returns adx, plus_di, minus_di
    - stoch_rsi: returns k, d
    - obv: returns obv, signal, divergence
    """
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    normalized_symbol = _normalize_symbol_input(symbol, market)
    market_missing = market is None or not str(market).strip()
    if market_missing and normalized_symbol.isalpha():
        raise ValueError(
            "market is required for plain alphabetic symbols. Use market='us' "
            "for US equities, or provide KRW-/USDT- prefixed symbol for crypto."
        )

    if not indicators:
        raise ValueError("indicators list is required and cannot be empty")

    valid_indicators = {
        "sma",
        "ema",
        "rsi",
        "macd",
        "bollinger",
        "atr",
        "pivot",
        "adx",
        "stoch_rsi",
        "obv",
    }
    normalized_indicators: list[IndicatorType] = []
    for ind in indicators:
        ind_lower = ind.lower().strip()
        if ind_lower not in valid_indicators:
            raise ValueError(
                f"Invalid indicator '{ind}'. Valid options: {', '.join(sorted(valid_indicators))}"
            )
        normalized_indicators.append(cast(IndicatorType, ind_lower))

    market_type, symbol = _resolve_market_type(normalized_symbol, market)

    source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
    source = source_map[market_type]

    try:
        df = await _fetch_ohlcv_for_indicators(symbol, market_type, count=250)

        if df.empty:
            raise ValueError(f"No data available for symbol '{symbol}'")

        close_fallback_price = (
            float(df["close"].iloc[-1]) if "close" in df.columns else None
        )
        current_price = close_fallback_price
        current_price_source = "ohlcv_close"
        if market_type == "crypto":
            try:
                prices = await upbit_service.fetch_multiple_current_prices([symbol])
                ticker_price = prices.get(symbol)
                if ticker_price is not None:
                    current_price = float(ticker_price)
            except Exception:
                current_price = close_fallback_price
        elif market_type == "equity_us":
            live = await fetch_us_live_last_price(symbol)
            if live is not None:
                current_price = live
                current_price_source = "yahoo_live"

        indicator_results = _compute_indicators(df, normalized_indicators, market_type)

        if market_type == "crypto" and "rsi" in normalized_indicators:
            realtime_rsi = _compute_crypto_realtime_rsi_from_frame(df, current_price)
            if realtime_rsi is not None:
                indicator_results.setdefault("rsi", {})["14"] = realtime_rsi

        result = {
            "symbol": symbol,
            "price": current_price,
            "instrument_type": market_type,
            "source": source,
            "indicators": indicator_results,
        }
        if market_type == "equity_us":
            result["current_price_source"] = current_price_source
            result["current_price_stale"] = current_price_source != "yahoo_live"
        return result

    except Exception as exc:
        return _error_payload_from_exception(
            source=source,
            exc=exc,
            symbol=symbol,
            instrument_type=market_type,
        )


async def _search_symbol_impl(
    query: str,
    limit: int = 20,
    market: str | None = None,
) -> list[dict[str, Any]]:
    """Implementation for search_symbol tool."""
    query = (query or "").strip()
    if not query:
        return []

    instrument_type = _normalize_market(market)

    try:
        capped_limit = min(max(limit, 1), 100)
        return await _search_master_data(query, capped_limit, instrument_type)
    except Exception as exc:
        return [_error_payload(source="master", message=str(exc), query=query)]


async def _krx_after_quote_fields(symbol: str) -> dict[str, Any]:
    """#925 — KRX after-market capability for a KR quote; unknown reads False.

    Fail-open for the quote (a missing table or DB hiccup must not break
    get_quote) but fail-closed for the field: the value is False with a reason,
    never None and never True without a fresh listed-STOCK row.
    """
    try:
        capability = (await get_kr_krx_after_tradability([symbol])).get(symbol)
    except Exception as exc:  # noqa: BLE001 - capability read is advisory for quotes
        logger.warning(
            "KRX after-market capability unavailable for %s: %s", symbol, exc
        )
        return krx_after_market.krx_after_unknown_fields(
            krx_after_market.REASON_LOOKUP_FAILED
        )
    if capability is None:
        return krx_after_market.krx_after_unknown_fields(
            krx_after_market.REASON_NOT_IN_UNIVERSE
        )
    return capability.public_fields()


async def _get_quote_impl(
    symbol: str | int,
    market: str | None = None,
    include_extended_hours: bool = False,
) -> dict[str, Any]:
    """Implementation for get_quote tool.

    ROB-922: include_extended_hours is US-equity-only opt-in (see
    _fetch_quote_equity_us / _apply_extended_hours_overlay); KR and crypto
    ignore the parameter entirely (no-op).
    """
    symbol = _normalize_symbol_input(symbol, market)
    if not symbol:
        raise ValueError("symbol is required")

    market_type, symbol = _resolve_market_type(symbol, market)

    if market_type == "equity_us":
        return await _fetch_quote_equity_us(
            symbol, include_extended_hours=include_extended_hours
        )

    source_map = {"crypto": "upbit", "equity_kr": "kis"}
    source = source_map[market_type]

    try:
        if market_type == "crypto":
            return await _fetch_quote_crypto(symbol)
        # ROB-464: tag stale KRX regular-session data honestly. ROB-511 overlays
        # an NXT-derived price during NXT sessions while preserving the KRX
        # previous_close used for gap calculations.
        data_state = kr_market_data_state()
        quote = await _fetch_quote_equity_kr(symbol)
        tradability_map = await get_kr_nxt_tradability([symbol])
        tradability = tradability_map.get(symbol)
        if tradability is not None:
            quote.update(tradability.public_fields())
        quote.update(await _krx_after_quote_fields(symbol))
        if await _apply_krx_auction_quote(
            symbol,
            quote,
            data_state=data_state,
            now=_current_kst_datetime(),
        ):
            return quote
        if data_state == DATA_STATE_PREMARKET_UNAVAILABLE:
            # hk 6300: before the KRX open only NXT-tradable symbols may take the
            # NXT orderbook price; everything else is carried and unusable.
            premarket_quote = await _kr_premarket_quote(
                symbol, quote, nxt_tradable=quote.get("nxt_tradable") is True
            )
            return _with_base_price_kind(premarket_quote, data_state)
        if await _apply_nxt_quote_overlay(symbol, quote, data_state=data_state):
            return quote

        quote["data_state"] = data_state
        return _with_base_price_kind(quote, data_state)
    except Exception as exc:
        return _error_payload_from_exception(
            source=source,
            exc=exc,
            symbol=symbol,
            instrument_type=market_type,
        )


async def _get_orderbook_impl(
    symbol: str | int,
    market: str = "kr",
    venue: str | None = None,
) -> dict[str, Any]:
    """Implementation for get_orderbook tool."""
    requested_market = str(market or "kr").strip() or "kr"
    market_type = _normalize_market(requested_market)
    if market_type is None:
        raise ValueError(f"Unsupported market: {market}")

    source = "kis"
    instrument_type = "equity_kr"

    if market_type == "equity_kr":
        symbol = _normalize_symbol_input(symbol, "kr")
        if not symbol:
            raise ValueError("symbol is required")
        _, symbol = _resolve_market_type(symbol, "kr")
    elif market_type == "crypto":
        if venue is not None and str(venue).strip():
            raise ValueError("venue is only supported for KR equity orderbook")
        symbol = _validate_crypto_orderbook_symbol_input(symbol)
        source = "upbit"
        instrument_type = "crypto"
    else:
        raise ValueError("get_orderbook only supports KR equity and KRW crypto markets")

    try:
        snapshot = await market_data_service.get_orderbook(
            symbol,
            "crypto" if market_type == "crypto" else "kr",
            venue=venue if market_type == "equity_kr" else None,
        )
        return _build_orderbook_payload(snapshot)
    except Exception as exc:
        return _error_payload_from_exception(
            source=source,
            exc=exc,
            symbol=symbol,
            instrument_type=instrument_type,
        )


async def _get_ohlcv_impl(
    symbol: str,
    count: int = 100,
    period: str = "day",
    end_date: str | None = None,
    market: str | None = None,
    include_indicators: bool = False,
) -> dict[str, Any]:
    """Implementation for get_ohlcv tool."""
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    count = int(count)
    if count <= 0:
        raise ValueError("count must be > 0")

    period = (period or "day").strip().lower()

    market_type, symbol = _resolve_market_type(symbol, market)
    period = validate_ohlcv_period(period, market_type)

    parsed_end_date: datetime.datetime | None = None
    end_date_is_date_only = False
    if end_date:
        try:
            end_date_is_date_only = len(end_date) == 10  # "YYYY-MM-DD"
            if (
                market_type == "equity_us"
                and period in US_INTRADAY_OHLCV_PERIODS
                and end_date_is_date_only
            ):
                parsed_end_date = datetime.datetime.combine(
                    datetime.date.fromisoformat(end_date),
                    datetime.time(20, 0),  # 20:00 ET = post-market close
                )
            else:
                parsed_end_date = datetime.datetime.fromisoformat(end_date)
        except ValueError as exc:
            raise ValueError(
                "end_date must be ISO format (e.g., '2024-01-15')"
            ) from exc

    cutoff = _end_date_cutoff(
        parsed_end_date,
        date_only=end_date_is_date_only,
        market_type=market_type,
    )

    # Period-aware source mapping
    if market_type == "equity_us" and period in US_INTRADAY_OHLCV_PERIODS:
        source = "kis"
    else:
        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]
    try:
        if market_type == "crypto":
            return await _fetch_ohlcv_crypto(
                symbol,
                count,
                period,
                parsed_end_date,
                include_indicators=include_indicators,
                cutoff=cutoff,
                end_date_is_date_only=end_date_is_date_only,
            )
        if market_type == "equity_kr":
            return await _fetch_ohlcv_equity_kr(
                symbol,
                count,
                period,
                parsed_end_date,
                include_indicators=include_indicators,
                cutoff=cutoff,
            )
        return await _fetch_ohlcv_equity_us(
            symbol,
            count,
            period,
            parsed_end_date,
            include_indicators=include_indicators,
            end_date_is_date_only=end_date_is_date_only,
            cutoff=cutoff,
        )
    except Exception as exc:
        if str(exc).startswith("Crypto minute OHLCV response missing columns:"):
            raise
        return _error_payload_from_exception(
            source=source,
            exc=exc,
            symbol=symbol,
            instrument_type=market_type,
        )


# ROB-485: 장중(fresh)인데 KIS 가 체결강도 필드를 안 준 경우의 정직 신호.
# market_session.kr_market_data_state (ROB-464 공유 분류기)는 수정하지 않고
# 이 도구 페이로드에서만 로컬로 대체한다.
DATA_STATE_FIELD_UNAVAILABLE = "field_unavailable"


async def _get_execution_strength_impl(
    symbol: str | int, market: str = "kr"
) -> dict[str, Any]:
    """ROB-462/ROB-485: KR 주식 체결강도 (execution strength) snapshot.

    체결강도 = 매수체결량 / 매도체결량 × 100 (당일 누적). 소스는 KIS
    FHKST01010300 (주식현재가 체결, inquire-ccnl) tick row 의 ``tday_rltv``
    — FHKST01010100 에는 체결강도 필드가 없다 (2026-06-10 라이브 검증).
    buy_volume/sell_volume 은 KIS REST 미제공 (WebSocket H0STCNT0 전용,
    WS 소스 follow-up) — 항상 null. ``as_of`` 는 조회 시각(now_kst),
    ``tick_time`` 은 broker 최신 체결 시각 (HHMMSS KST). KR equity only —
    crypto 는 get_crypto_order_flow.
    """
    requested = str(market or "kr").strip().lower() or "kr"
    if requested not in ("kr", "kospi", "kosdaq"):
        return _error_payload(
            source="validation",
            message=(
                "get_execution_strength supports KR equity only "
                "(crypto: use get_crypto_order_flow)."
            ),
            symbol=str(symbol),
        )

    normalized = _normalize_symbol_input(symbol, "kr")
    if not normalized:
        raise ValueError("symbol is required")
    _, normalized = _resolve_market_type(normalized, "kr")

    try:
        raw = await KISClient().inquire_execution_strength(normalized)
    except Exception as exc:
        return _error_payload_from_exception(
            source="kis",
            exc=exc,
            symbol=normalized,
            instrument_type="equity_kr",
        )

    data = compute_execution_strength(
        raw, symbol=normalized, as_of=now_kst().isoformat()
    )
    data_state = kr_market_data_state()
    if data.execution_strength_pct is None and data_state == DATA_STATE_FRESH:
        # 장중인데 필드가 비어 있으면 "fresh + 전부 null" 로 위장하지 않는다.
        data_state = DATA_STATE_FIELD_UNAVAILABLE
    return {
        "symbol": data.symbol,
        "as_of": data.as_of,
        "tick_time": data.tick_time,
        "execution_strength_pct": data.execution_strength_pct,
        "buy_volume": data.buy_volume,
        "sell_volume": data.sell_volume,
        "trend": data.trend,
        "data_state": data_state,
        "source": "kis",
        "instrument_type": "equity_kr",
    }


def _register_market_data_tools_impl(mcp: FastMCP) -> None:
    @mcp.tool(
        name="search_symbol",
        description=(
            "Search symbols by query (symbol or name). Use market to filter: "
            "kr/kospi/kosdaq (Korean stocks), us/nasdaq/nyse (US stocks), "
            "crypto/upbit (cryptocurrencies)."
        ),
    )
    async def search_symbol(
        query: str, limit: int = 20, market: str | None = None
    ) -> list[dict[str, Any]]:
        return await _search_symbol_impl(query, limit, market)

    @mcp.tool(
        name="get_quote",
        description=(
            "Get latest quote/last price for a symbol (KR equity / US equity / crypto). "
            "Use this for a fast standalone price/last-trade check. "
            "analyze_stock_batch's default quick=True path is a DB-only stale "
            "projection (last CLOSED daily candle) and never fetches a live price — "
            "always call get_quote for that. Only analyze_stock_batch(quick=False) "
            "fetches a fresh price internally, so for KR/crypto and US regular-session "
            "quick=False analysis this call is normally redundant with that path; "
            "still use get_quote for US premarket/afterhours with "
            "include_extended_hours=True or when previous_close is needed, and use "
            "get_ohlcv when OHLC candles are needed. "
            "For KR equities during NXT pre-market/after-hours sessions, price falls "
            "back to the NXT orderbook expected price or best bid/ask mid while "
            "preserving KRX previous_close for gap calculations. "
            "include_extended_hours=True (US equity only, default False) opportunistically "
            "overlays the latest Yahoo pre/post-market 1-minute price during premarket/"
            "afterhours sessions (price_source='yahoo_prepost_last'); no-op for KR/crypto "
            "and for regular/closed US sessions."
        ),
    )
    async def get_quote(
        symbol: str | int,
        market: str | None = None,
        include_extended_hours: bool = False,
    ) -> dict[str, Any]:
        return await _get_quote_impl(symbol, market, include_extended_hours)

    @mcp.tool(
        name="get_orderbook",
        description=(
            "Get 10-level orderbook data with total residual quantities and expected match metadata. "
            "Supports KR equity and KRW crypto markets. "
            "For KR equity, use venue to select the trading venue: "
            "'krx' (default, KRX regular session), 'nxt' (NXT after-hours), "
            "'unified' or '통합' (통합시장). venue applies only to KR equity."
        ),
    )
    async def get_orderbook(
        symbol: str | int, market: str = "kr", venue: str | None = None
    ) -> dict[str, Any]:
        return await _get_orderbook_impl(symbol, market, venue)

    @mcp.tool(
        name="get_execution_strength",
        description=(
            "Get KR equity 체결강도 (execution strength = 매수체결량/매도체결량 "
            "× 100, 당일 누적) from KIS FHKST01010300 (주식현재가 체결) tick "
            "rows. >100 buy-dominant, <100 sell-dominant. Returns "
            "execution_strength_pct, tick_time (latest tick HHMMSS KST), trend, "
            "and data_state (premarket/closed sessions tagged stale; "
            "'field_unavailable' when KIS omits the field during a live "
            "session). buy_volume/sell_volume are always null — per-side "
            "contracted volume is not provided by KIS REST (WebSocket H0STCNT0 "
            "only; WS-sourced follow-up). KR equity only — for crypto taker "
            "order flow use get_crypto_order_flow."
        ),
    )
    async def get_execution_strength(
        symbol: str | int, market: str = "kr"
    ) -> dict[str, Any]:
        return await _get_execution_strength_impl(symbol, market)

    @mcp.tool(
        name="get_ohlcv",
        description=(
            "Get OHLCV candles for a symbol. Supports daily/weekly/monthly periods "
            "plus 1m/5m/15m/30m for KR/US equity and crypto, 4h for crypto, 1h for KR/US equity/crypto, "
            "and date-based pagination. end_date is an inclusive cutoff: a date (YYYY-MM-DD) covers that "
            "whole market-local day (KST for crypto/KR, America/New_York for US), a timestamp cuts at that "
            "instant; count fills backwards from the cutoff and no row after it is returned."
        ),
    )
    async def get_ohlcv(
        symbol: str,
        count: int = 100,
        period: str = "day",
        end_date: str | None = None,
        market: str | None = None,
        include_indicators: bool = False,
    ) -> dict[str, Any]:
        return await _get_ohlcv_impl(
            symbol=symbol,
            count=count,
            period=period,
            end_date=end_date,
            market=market,
            include_indicators=include_indicators,
        )

    @mcp.tool(
        name="get_indicators",
        description=(
            "Calculate technical indicators for a symbol. Available indicators: "
            "sma (Simple Moving Average), ema (Exponential Moving Average), "
            "rsi (Relative Strength Index), macd (MACD), bollinger (Bollinger Bands), "
            "atr (Average True Range), pivot (Pivot Points), "
            "adx (Average Directional Index - returns adx, plus_di, minus_di), "
            "stoch_rsi (Stochastic RSI - returns k, d), "
            "obv (On-Balance Volume - returns obv, signal, divergence)."
        ),
    )
    async def get_indicators(
        symbol: str, indicators: list[str], market: str | None = None
    ) -> dict[str, Any]:
        return await _get_indicators_impl(symbol, indicators, market)


# ---------------------------------------------------------------------------
# Public/Shared Exports
# ---------------------------------------------------------------------------

__all__ = [
    "_fetch_quote_crypto",
    "_fetch_quote_equity_kr",
    "_fetch_kr_live_quote",
    "_fetch_quote_equity_us",
    "_fetch_ohlcv_crypto",
    "_fetch_ohlcv_equity_kr",
    "_fetch_ohlcv_equity_us",
    "_build_orderbook_payload",
    "_get_indicators_impl",
    "_search_symbol_impl",
    "_get_quote_impl",
    "_get_orderbook_impl",
    "_get_ohlcv_impl",
    "MARKET_DATA_TOOL_NAMES",
    "_register_market_data_tools_impl",
]
