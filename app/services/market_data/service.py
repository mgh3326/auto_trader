from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import httpx
import pandas as pd

from app.core.async_rate_limiter import RateLimitExceededError
from app.core.timezone import KST, now_kst
from app.services import naver_finance
from app.services.brokers.kis.client import KISClient
from app.services.brokers.upbit.client import fetch_multiple_current_prices
from app.services.brokers.upbit.client import fetch_ohlcv as fetch_upbit_ohlcv
from app.services.brokers.yahoo.client import fetch_fast_info
from app.services.brokers.yahoo.client import fetch_ohlcv as fetch_yahoo_ohlcv
from app.services.domain_errors import (
    RateLimitError,
    SymbolNotFoundError,
    UpstreamUnavailableError,
    ValidationError,
)
from app.services.kr_hourly_candles_read_service import read_kr_intraday_candles
from app.services.market_data.constants import (
    KR_INTRADAY_OHLCV_PERIODS,
    US_INTRADAY_OHLCV_PERIODS,
    validate_ohlcv_period,
)
from app.services.market_data.contracts import (
    Candle,
    OrderbookLevel,
    OrderbookSnapshot,
    Quote,
)
from app.services.upbit_orderbook import fetch_orderbook
from app.services.upbit_symbol_universe_service import UpbitSymbolUniverseLookupError
from app.services.us_intraday_candles_read_service import read_us_intraday_candles
from app.services.us_symbol_universe_service import USSymbolUniverseLookupError

logger = logging.getLogger(__name__)


def _normalize_market(market: str) -> str:
    normalized = str(market or "").strip().lower()
    aliases = {
        "kr": "equity_kr",
        "kospi": "equity_kr",
        "kosdaq": "equity_kr",
        "us": "equity_us",
        "nasdaq": "equity_us",
        "nyse": "equity_us",
        "crypto": "crypto",
        "upbit": "crypto",
    }
    resolved = aliases.get(normalized, normalized)
    if resolved not in {"equity_kr", "equity_us", "crypto"}:
        raise ValidationError(f"Unsupported market: {market}")
    return resolved


def _normalize_symbol(symbol: str, market: str) -> str:
    value = str(symbol or "").strip()
    if not value:
        raise ValidationError("symbol is required")
    if market == "crypto":
        upper = value.upper()
        if upper.startswith(("KRW-", "USDT-")):
            return upper
        return f"KRW-{upper}"
    if market == "equity_kr" and value.isdigit() and len(value) <= 6:
        return value.zfill(6)
    return value.upper()


def _normalize_period(period: str, market: str) -> str:
    return validate_ohlcv_period(period, market, error_type=ValidationError)


def _to_candle_rows(
    frame: pd.DataFrame,
    *,
    symbol: str,
    market: str,
    source: str,
    period: str,
) -> list[Candle]:
    if frame.empty:
        return []

    rows: list[Candle] = []
    for _, row in frame.iterrows():
        timestamp_raw = row.get("datetime")
        if timestamp_raw is None:
            date_raw = row.get("date")
            if date_raw is None:
                raise ValidationError("candle row must include datetime or date")
            timestamp_raw = pd.Timestamp(date_raw)
        timestamp = pd.Timestamp(timestamp_raw).to_pydatetime()
        value_raw = row.get("value")
        rows.append(
            Candle(
                symbol=symbol,
                market=market,
                source=source,
                period=period,
                timestamp=timestamp,
                open=float(row.get("open") or 0.0),
                high=float(row.get("high") or 0.0),
                low=float(row.get("low") or 0.0),
                close=float(row.get("close") or 0.0),
                volume=float(row.get("volume") or 0.0),
                value=(float(value_raw) if value_raw is not None else None),
            )
        )
    return rows


def _map_error(exc: Exception) -> Exception:
    if isinstance(
        exc,
        (
            ValidationError,
            SymbolNotFoundError,
            RateLimitError,
            UpstreamUnavailableError,
            UpbitSymbolUniverseLookupError,
            USSymbolUniverseLookupError,
        ),
    ):
        return exc
    if isinstance(exc, RateLimitExceededError):
        return RateLimitError(str(exc))
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {
        418,
        429,
    }:
        return RateLimitError(str(exc))
    if isinstance(exc, (httpx.HTTPStatusError, httpx.RequestError)):
        return UpstreamUnavailableError(str(exc))
    text = str(exc)
    if "not found" in text.lower() or "no data" in text.lower():
        return SymbolNotFoundError(text)
    return UpstreamUnavailableError(text)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_optional_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _validate_crypto_orderbook_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if not value:
        raise ValidationError("symbol is required")
    if not value.startswith("KRW-"):
        raise ValueError("crypto orderbook only supports KRW-* symbols")
    return value


def _current_kst_datetime() -> dt.datetime:
    return now_kst()


def _get_orderbook_session_hint(now_kst: dt.datetime | None = None) -> str:
    current = now_kst or _current_kst_datetime()
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    else:
        current = current.astimezone(KST)
    current_time = current.timetz().replace(tzinfo=None)
    if dt.time(9, 0) <= current_time < dt.time(15, 30):
        return "regular"
    if dt.time(16, 0) <= current_time < dt.time(20, 0):
        return "nxt"
    return "other"


def _extract_expected_match_metadata(
    symbol: str,
    output2: dict[str, Any] | None,
) -> tuple[int | None, int | None]:
    if output2 is None:
        logger.info(
            "Orderbook expected_qty unavailable: symbol=%s session_hint=%s antc_cnpr=%r antc_cnqn=%r output2_keys=%s",
            symbol,
            _get_orderbook_session_hint(),
            None,
            None,
            [],
        )
        return None, None

    raw_expected_price = output2.get("antc_cnpr")
    raw_expected_qty = output2.get("antc_cnqn")
    expected_price = _to_optional_int(raw_expected_price)
    expected_qty = _to_optional_int(raw_expected_qty)

    if raw_expected_qty in (None, ""):
        logger.info(
            "Orderbook expected_qty unavailable: symbol=%s session_hint=%s antc_cnpr=%r antc_cnqn=%r output2_keys=%s",
            symbol,
            _get_orderbook_session_hint(),
            raw_expected_price,
            raw_expected_qty,
            sorted(output2),
        )

    return expected_price, expected_qty


def _to_optional_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_kis_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        formatted = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
        try:
            return dt.date.fromisoformat(formatted).isoformat()
        except ValueError:
            return None
    return None


def _parse_orderbook_levels(
    output: dict[str, Any], prefix: str
) -> list[OrderbookLevel]:
    levels: list[OrderbookLevel] = []
    for idx in range(1, 11):
        price = _to_int(output.get(f"{prefix}p{idx}"))
        if price <= 0:
            continue
        quantity = output.get(f"{prefix}p_rsqn{idx}")
        if quantity is None:
            quantity = output.get(f"{prefix}p{idx}_rsqn")
        levels.append(OrderbookLevel(price=price, quantity=_to_int(quantity)))
    return levels


def _parse_upbit_orderbook_levels(
    orderbook_units: list[dict[str, Any]],
    *,
    side: str,
) -> list[OrderbookLevel]:
    price_key = f"{side}_price"
    size_key = f"{side}_size"
    levels: list[OrderbookLevel] = []
    for unit in orderbook_units:
        if not isinstance(unit, dict):
            continue
        price = _to_float(unit.get(price_key))
        if price <= 0:
            continue
        levels.append(
            OrderbookLevel(price=price, quantity=_to_float(unit.get(size_key)))
        )
    return levels


async def get_kr_volume_rank() -> list[dict[str, Any]]:
    try:
        kis = KISClient()
        rows = await kis.volume_rank()
        return list(rows)
    except Exception as exc:
        raise _map_error(exc) from exc


async def get_quote(symbol: str, market: str) -> Quote:
    resolved_market = _normalize_market(market)
    resolved_symbol = _normalize_symbol(symbol, resolved_market)

    try:
        if resolved_market == "crypto":
            prices = await fetch_multiple_current_prices([resolved_symbol])
            price = prices.get(resolved_symbol)
            if price is None:
                raise SymbolNotFoundError(f"Symbol '{resolved_symbol}' not found")
            return Quote(
                symbol=resolved_symbol,
                market=resolved_market,
                price=float(price),
                source="upbit",
            )

        if resolved_market == "equity_us":
            fast_info = await fetch_fast_info(resolved_symbol)
            close = fast_info.get("close")
            if close is None:
                raise SymbolNotFoundError(f"Symbol '{resolved_symbol}' not found")
            return Quote(
                symbol=resolved_symbol,
                market=resolved_market,
                price=float(close),
                source="yahoo",
                previous_close=(
                    float(fast_info["previous_close"])
                    if fast_info.get("previous_close") is not None
                    else None
                ),
                open=(
                    float(fast_info["open"])
                    if fast_info.get("open") is not None
                    else None
                ),
                high=(
                    float(fast_info["high"])
                    if fast_info.get("high") is not None
                    else None
                ),
                low=(
                    float(fast_info["low"])
                    if fast_info.get("low") is not None
                    else None
                ),
                volume=(
                    int(float(fast_info["volume"]))
                    if fast_info.get("volume") is not None
                    else None
                ),
            )

        kis = KISClient()
        frame = await kis.inquire_daily_itemchartprice(
            code=resolved_symbol,
            market="UN",
            n=1,
            period="D",
        )
        if frame.empty:
            raise SymbolNotFoundError(f"Symbol '{resolved_symbol}' not found")
        last = frame.iloc[-1]
        return Quote(
            symbol=resolved_symbol,
            market=resolved_market,
            price=float(last.get("close") or 0.0),
            source="kis",
            open=(float(last["open"]) if last.get("open") is not None else None),
            high=(float(last["high"]) if last.get("high") is not None else None),
            low=(float(last["low"]) if last.get("low") is not None else None),
            volume=(
                int(float(last["volume"])) if last.get("volume") is not None else None
            ),
            value=(float(last["value"]) if last.get("value") is not None else None),
        )
    except Exception as exc:
        raise _map_error(exc) from exc


async def get_orderbook(symbol: str, market: str = "kr") -> OrderbookSnapshot:
    resolved_market = _normalize_market(market)
    if resolved_market == "crypto":
        resolved_symbol = _validate_crypto_orderbook_symbol(symbol)
        try:
            raw = await fetch_orderbook(resolved_symbol)
            if not raw:
                raise SymbolNotFoundError(f"Symbol '{resolved_symbol}' not found")

            total_ask_qty = _to_float(raw.get("total_ask_size"))
            total_bid_qty = _to_float(raw.get("total_bid_size"))
            return OrderbookSnapshot(
                symbol=resolved_symbol,
                instrument_type="crypto",
                source="upbit",
                asks=_parse_upbit_orderbook_levels(
                    raw.get("orderbook_units", []),
                    side="ask",
                ),
                bids=_parse_upbit_orderbook_levels(
                    raw.get("orderbook_units", []),
                    side="bid",
                ),
                total_ask_qty=total_ask_qty,
                total_bid_qty=total_bid_qty,
                bid_ask_ratio=(
                    round(total_bid_qty / total_ask_qty, 2)
                    if total_ask_qty > 0
                    else None
                ),
                expected_price=None,
                expected_qty=None,
            )
        except Exception as exc:
            raise _map_error(exc) from exc

    if resolved_market != "equity_kr":
        raise ValueError("get_orderbook only supports KR equity and KRW crypto markets")
    resolved_symbol = _normalize_symbol(symbol, resolved_market)

    try:
        kis = KISClient()
        output1, output2 = await kis.inquire_orderbook_snapshot(
            code=resolved_symbol,
            market="UN",
        )
        expected_price, expected_qty = _extract_expected_match_metadata(
            resolved_symbol,
            output2,
        )
        total_ask_qty = _to_int(output1.get("total_askp_rsqn"))
        total_bid_qty = _to_int(output1.get("total_bidp_rsqn"))
        return OrderbookSnapshot(
            symbol=resolved_symbol,
            instrument_type="equity_kr",
            source="kis",
            asks=_parse_orderbook_levels(output1, "ask"),
            bids=_parse_orderbook_levels(output1, "bid"),
            total_ask_qty=total_ask_qty,
            total_bid_qty=total_bid_qty,
            bid_ask_ratio=(
                round(total_bid_qty / total_ask_qty, 2) if total_ask_qty > 0 else None
            ),
            expected_price=expected_price,
            expected_qty=expected_qty,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


async def get_short_interest(symbol: str, days: int = 20) -> dict[str, object]:
    resolved_symbol = _normalize_symbol(symbol, "equity_kr")
    capped_days = min(max(days, 1), 60)
    end_date = dt.date.today()
    start_date = end_date - dt.timedelta(days=capped_days * 2)

    try:
        kis = KISClient()
        output1, output2 = await kis.inquire_short_selling(
            code=resolved_symbol,
            start_date=start_date,
            end_date=end_date,
            market="J",
        )
        name = (
            output1.get("hts_kor_isnm")
            or output1.get("prdt_name")
            or output1.get("name")
            or None
        )
        if not name:
            try:
                info = await naver_finance.fetch_company_profile(resolved_symbol)
                name = info.get("name") or None
            except Exception:
                name = None

        short_data: list[dict[str, object]] = []
        for row in output2:
            date = _format_kis_date(row.get("stck_bsop_date"))
            if date is None:
                continue
            short_data.append(
                {
                    "date": date,
                    "short_volume": _to_optional_int(row.get("ssts_cntg_qty")),
                    "short_amount": _to_optional_int(row.get("ssts_tr_pbmn")),
                    "short_ratio": _to_optional_float(row.get("ssts_vol_rlim")),
                    "total_volume": _to_optional_int(row.get("acml_vol")),
                    "total_amount": _to_optional_int(row.get("acml_tr_pbmn")),
                }
            )

        short_data = sorted(short_data, key=lambda row: str(row["date"]), reverse=True)[
            :capped_days
        ]

        valid_ratios: list[float] = []
        for row in short_data:
            ratio = row["short_ratio"]
            if isinstance(ratio, int | float):
                valid_ratios.append(float(ratio))
        avg_short_ratio = (
            round(sum(valid_ratios) / len(valid_ratios), 2) if valid_ratios else None
        )

        return {
            "symbol": resolved_symbol,
            "name": name,
            "short_data": short_data,
            "avg_short_ratio": avg_short_ratio,
        }
    except Exception as exc:
        raise _map_error(exc) from exc


async def get_ohlcv(
    symbol: str,
    market: str,
    period: str,
    count: int,
    end: dt.datetime | None = None,
) -> list[Candle]:
    resolved_market = _normalize_market(market)
    resolved_symbol = _normalize_symbol(symbol, resolved_market)
    resolved_period = _normalize_period(period, resolved_market)

    if count <= 0:
        raise ValidationError("count must be > 0")

    try:
        if resolved_market == "crypto":
            frame = await fetch_upbit_ohlcv(
                market=resolved_symbol,
                days=min(count, 200),
                period=resolved_period,
                end_date=end,
            )
            return _to_candle_rows(
                frame,
                symbol=resolved_symbol,
                market=resolved_market,
                source="upbit",
                period=resolved_period,
            )

        if resolved_market == "equity_us":
            if resolved_period in US_INTRADAY_OHLCV_PERIODS:
                frame = await read_us_intraday_candles(
                    symbol=resolved_symbol,
                    period=resolved_period,
                    count=min(count, 200),
                    end_date=end,
                )
                return _to_candle_rows(
                    frame,
                    symbol=resolved_symbol,
                    market=resolved_market,
                    source="kis",
                    period=resolved_period,
                )
            # day/week/month use Yahoo Finance
            frame = await fetch_yahoo_ohlcv(
                ticker=resolved_symbol,
                days=min(count, 200),
                period=resolved_period,
                end_date=end,
            )
            return _to_candle_rows(
                frame,
                symbol=resolved_symbol,
                market=resolved_market,
                source="yahoo",
                period=resolved_period,
            )

        kis = KISClient()
        if resolved_period in {"day", "week", "month"}:
            period_map = {"day": "D", "week": "W", "month": "M"}
            frame = await kis.inquire_daily_itemchartprice(
                code=resolved_symbol,
                market="UN",
                n=min(count, 200),
                period=period_map[resolved_period],
                end_date=(pd.Timestamp(end.date()) if end is not None else None),
            )
            return _to_candle_rows(
                frame,
                symbol=resolved_symbol,
                market=resolved_market,
                source="kis",
                period=resolved_period,
            )

        if resolved_period in KR_INTRADAY_OHLCV_PERIODS:
            frame = await read_kr_intraday_candles(
                symbol=resolved_symbol,
                period=resolved_period,
                count=min(count, 200),
                end_date=end,
            )
        else:
            frame = await kis.inquire_minute_chart(
                code=resolved_symbol,
                market="UN",
                time_unit=60,
                n=min(count, 200),
                end_date=(pd.Timestamp(end.date()) if end is not None else None),
            )
        return _to_candle_rows(
            frame,
            symbol=resolved_symbol,
            market=resolved_market,
            source="kis",
            period=resolved_period,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


__all__ = [
    "get_quote",
    "get_orderbook",
    "get_short_interest",
    "get_ohlcv",
    "get_kr_volume_rank",
    "Quote",
    "Candle",
    "OrderbookLevel",
    "OrderbookSnapshot",
]
