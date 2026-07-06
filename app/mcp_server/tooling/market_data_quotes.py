"""Market data utilities: quotes, OHLCV, and technical indicators.

This module contains functions for fetching market data (quotes, OHLCV candles)
and computing technical indicators (SMA, EMA, RSI, MACD, Bollinger, ATR, Pivot,
ADX, Stochastic RSI, OBV, Fibonacci).
"""

from __future__ import annotations

import datetime
import logging
from statistics import median
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pandas as pd

import app.services.brokers.upbit.client as upbit_service
import app.services.brokers.yahoo.client as yahoo_service
import app.services.market_data as market_data_service
from app.core.config import settings
from app.core.symbol import to_db_symbol
from app.core.timezone import now_kst
from app.mcp_server.tooling.market_data_indicators import (
    IndicatorType,
    _compute_crypto_realtime_rsi_from_frame,
    _compute_indicators,
    _fetch_ohlcv_for_indicators,
)
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    DATA_STATE_PREMARKET_UNAVAILABLE,
    DATA_STATE_STALE,
    US_SESSION_CLOSED,
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
from app.services import kis_ohlcv_cache
from app.services.brokers.kis.client import KISClient
from app.services.brokers.toss.market_calendar import get_kr_nxt_session_from_toss
from app.services.execution_strength.query_service import compute_execution_strength
from app.services.kr_hourly_candles_read_service import (
    read_kr_hourly_candles_1h,
    read_kr_intraday_candles,
)
from app.services.kr_symbol_universe_service import (
    get_kr_nxt_tradability,
    search_kr_symbols,
)
from app.services.market_data.constants import (
    CRYPTO_MINUTE_OHLCV_PERIODS,
    CRYPTO_MINUTE_PUBLIC_ROW_KEYS,
    CRYPTO_MINUTE_REQUIRED_SOURCE_COLUMNS,
    KR_INTRADAY_OHLCV_PERIODS,
    US_INTRADAY_OHLCV_PERIODS,
    validate_ohlcv_period,
)
from app.services.market_data.toss_ohlcv import (
    fetch_daily_toss_frame,
    fetch_kr_intraday_toss_frame,
)
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

_NON_INTRADAY_PERIODS = {"day", "week", "month"}


def _numeric_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(index=df.index, dtype="float64")
    return pd.to_numeric(df[column], errors="coerce")


def _kis_end_date(end_date: datetime.datetime | None) -> pd.Timestamp | None:
    if end_date is None:
        return None
    return pd.Timestamp(end_date.date())


def _build_rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def _build_indicator_rows(df: pd.DataFrame, period: str) -> list[dict[str, Any]]:
    close = _numeric_series(df, "close")
    high = _numeric_series(df, "high")
    low = _numeric_series(df, "low")
    volume = _numeric_series(df, "volume")

    ema_20 = close.ewm(span=20, adjust=False).mean()
    rsi_14 = _build_rsi_series(close).round(2)
    bb_mid = close.rolling(window=20).mean()
    bb_std = close.rolling(window=20).std()
    bb_upper = bb_mid + (bb_std * 2.0)
    bb_lower = bb_mid - (bb_std * 2.0)

    indicator_frame = pd.DataFrame(
        {
            "rsi_14": rsi_14,
            "ema_20": ema_20,
            "bb_upper": bb_upper,
            "bb_mid": bb_mid,
            "bb_lower": bb_lower,
        },
        index=df.index,
    )

    if period in _NON_INTRADAY_PERIODS:
        indicator_frame["vwap"] = None
    else:
        typical_price = (high + low + close) / 3.0
        cumulative_volume = volume.cumsum()
        weighted_total = (typical_price * volume).cumsum()
        indicator_frame["vwap"] = weighted_total / cumulative_volume.where(
            cumulative_volume != 0
        )

    return _normalize_rows(indicator_frame.loc[:, list(_OHLCV_INDICATOR_ROW_KEYS)])


def _normalize_ohlcv_rows(
    df: pd.DataFrame,
    *,
    period: str,
    include_indicators: bool,
) -> list[dict[str, Any]]:
    frame = df
    if include_indicators and not df.empty:
        frame = _enrich_ohlcv_with_indicators(df, period)
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
        ),
    }
    if include_indicators:
        payload["indicators_included"] = True
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


_NXT_AFTER_OPEN = datetime.time(15, 30)
_NXT_AFTER_CLOSE = datetime.time(20, 0)
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
    if toss_session in {"nxt_premarket", "nxt_after"}:
        return toss_session
    if toss_session == "closed":
        return None

    if data_state == DATA_STATE_PREMARKET_UNAVAILABLE:
        return "nxt_premarket"

    if not is_kr_session_day(current.date()):
        return None

    current_time = current.timetz().replace(tzinfo=None)
    if _NXT_AFTER_OPEN <= current_time < _NXT_AFTER_CLOSE:
        return "nxt_after"
    return None


def _positive_price(value: float | int | None) -> float | None:
    try:
        if value is None:
            return None
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _nxt_price_from_orderbook(
    snapshot: market_data_service.OrderbookSnapshot,
) -> tuple[float | None, str | None]:
    if snapshot.is_empty_book:
        return None, None

    expected_price = _positive_price(snapshot.expected_price)
    if expected_price is not None:
        return expected_price, "nxt_expected_price"

    best_ask = _positive_price(snapshot.asks[0].price if snapshot.asks else None)
    best_bid = _positive_price(snapshot.bids[0].price if snapshot.bids else None)
    if best_ask is not None and best_bid is not None:
        return (best_ask + best_bid) / 2.0, "nxt_mid"
    if best_ask is not None:
        return best_ask, "nxt_best_ask"
    if best_bid is not None:
        return best_bid, "nxt_best_bid"
    return None, None


async def _fetch_nxt_quote_overlay(
    symbol: str,
    *,
    session: str,
) -> dict[str, Any] | None:
    try:
        snapshot = await market_data_service.get_orderbook(symbol, "kr", venue="nxt")
    except Exception as exc:
        logger.warning("NXT quote overlay failed for %s: %s", symbol, exc)
        return None

    price, price_source = _nxt_price_from_orderbook(snapshot)
    if price is None or price_source is None:
        return None

    overlay: dict[str, Any] = {
        "price": price,
        "session": session,
        "venue": snapshot.venue or "nxt",
        "price_source": price_source,
    }
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
    overlay = await _fetch_nxt_quote_overlay(symbol, session=session)
    if overlay is None:
        return False
    quote.update(overlay)
    quote["regular_session_data_state"] = data_state
    quote["data_state"] = DATA_STATE_FRESH
    return True


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
        "bid_walls": bid_walls,
        "ask_walls": ask_walls,
    }
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
    return {
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


async def _fetch_kr_live_quote(symbol: str) -> dict[str, Any] | None:
    """analyze 전용: KR 라이브 현재가(KIS inquire_price, stck_prpr) + as_of.

    공유 _fetch_quote_equity_kr(orders/portfolio 사용)는 건드리지 않는다.
    실패/빈응답이면 None (호출자가 일봉으로 fallback).
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
    if date_val is not None:
        d = pd.Timestamp(date_val).to_pydatetime()
        if time_val is not None:
            as_of = datetime.datetime.combine(d.date(), time_val)
        else:
            as_of = d

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


async def _fetch_quote_equity_us(symbol: str) -> dict[str, Any]:
    """Fetch US equity quote.

    ROB-471: KIS 해외 현재가 primary(settings.us_quote_kis_primary), Yahoo
    fast_info fallback. 정직 에러 분리:
      - provider 명시적 not-found → symbol_not_found (ValueError)
      - fast_info 정상응답·무가격 → quote_unavailable (RuntimeError)
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
                return _tag_us_quote_session(kis_quote)

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

    return _tag_us_quote_session(
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


def _calculate_rsi_14(close: pd.Series) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=14, min_periods=14).mean()
    avg_loss = loss.rolling(window=14, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask(avg_loss == 0, 100.0)
    rsi = rsi.mask((avg_gain == 0) & (avg_loss == 0), 50.0)
    return rsi.round(2)


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


def _enrich_ohlcv_with_indicators(df: pd.DataFrame, period: str) -> pd.DataFrame:
    frame = df.copy()
    close = frame["close"].astype(float)
    frame["rsi_14"] = _calculate_rsi_14(close)
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
) -> dict[str, Any]:
    """Fetch crypto OHLCV from Upbit."""
    capped_count = min(count, 200)
    df = await upbit_service.fetch_ohlcv(
        market=symbol, days=capped_count, period=period, end_date=end_date
    )

    if df.empty:
        return {
            "symbol": symbol,
            "instrument_type": "crypto",
            "source": "upbit",
            "period": period,
            "count": 0,
            "rows": [],
            "indicators_included": include_indicators,
            "message": f"No candle data available for {symbol}",
        }

    if period in CRYPTO_MINUTE_OHLCV_PERIODS:
        _validate_crypto_minute_source_columns(df)

    if include_indicators:
        df = _enrich_ohlcv_with_indicators(df, period)

    return {
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


_KST = ZoneInfo("Asia/Seoul")


async def _fetch_ohlcv_equity_kr(
    symbol: str,
    count: int,
    period: str,
    end_date: datetime.datetime | None,
    *,
    include_indicators: bool = False,
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

    return _build_ohlcv_payload(
        symbol=symbol,
        instrument_type="equity_kr",
        source="kis",
        period=period,
        count=capped_count,
        df=df,
        include_indicators=include_indicators,
    )


async def _fetch_ohlcv_equity_us(
    symbol: str,
    count: int,
    period: str,
    end_date: datetime.datetime | None,
    *,
    include_indicators: bool = False,
    end_date_is_date_only: bool = False,
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
    return _build_ohlcv_payload(
        symbol=symbol,
        instrument_type="equity_us",
        source=source,
        period=period,
        count=capped_count,
        df=df,
        include_indicators=include_indicators,
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

        indicator_results = _compute_indicators(df, normalized_indicators)

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


async def _get_quote_impl(
    symbol: str | int,
    market: str | None = None,
) -> dict[str, Any]:
    """Implementation for get_quote tool."""
    symbol = _normalize_symbol_input(symbol, market)
    if not symbol:
        raise ValueError("symbol is required")

    market_type, symbol = _resolve_market_type(symbol, market)

    if market_type == "equity_us":
        return await _fetch_quote_equity_us(symbol)

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
        tradability = (await get_kr_nxt_tradability([symbol])).get(symbol)
        if tradability is not None:
            quote.update(tradability.public_fields())
        if await _apply_nxt_quote_overlay(symbol, quote, data_state=data_state):
            return quote

        quote["data_state"] = data_state
        return quote
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
            is_date_only = len(end_date) == 10  # "YYYY-MM-DD"
            if (
                market_type == "equity_us"
                and period in US_INTRADAY_OHLCV_PERIODS
                and is_date_only
            ):
                end_date_is_date_only = True
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
            )
        if market_type == "equity_kr":
            return await _fetch_ohlcv_equity_kr(
                symbol,
                count,
                period,
                parsed_end_date,
                include_indicators=include_indicators,
            )
        return await _fetch_ohlcv_equity_us(
            symbol,
            count,
            period,
            parsed_end_date,
            include_indicators=include_indicators,
            end_date_is_date_only=end_date_is_date_only,
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
            "For KR equities during NXT pre-market/after-hours sessions, price falls "
            "back to the NXT orderbook expected price or best bid/ask mid while "
            "preserving KRX previous_close for gap calculations."
        ),
    )
    async def get_quote(symbol: str | int, market: str | None = None) -> dict[str, Any]:
        return await _get_quote_impl(symbol, market)

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
            "plus 1m/5m/15m/30m for KR/US equity and crypto, 4h for crypto, 1h for KR/US equity/crypto, and date-based pagination."
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
