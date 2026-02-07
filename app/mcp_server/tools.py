from __future__ import annotations

import asyncio
import datetime
import json
from typing import TYPE_CHECKING, Any, Literal

import finnhub
import httpx
import numpy as np
import pandas as pd
import yfinance as yf

if TYPE_CHECKING:
    from fastmcp import FastMCP

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.symbol import to_db_symbol
from app.mcp_server.env_utils import _env_int
from app.models.manual_holdings import MarketType
from app.services import naver_finance
from app.services import upbit as upbit_service
from app.services import yahoo as yahoo_service
from app.services.kis import KISClient
from app.services.manual_holdings_service import ManualHoldingsService
from data.coins_info import get_or_refresh_maps

# 마스터 데이터 (lazy loading)
from data.stocks_info import (
    get_kosdaq_name_to_code,
    get_kospi_name_to_code,
    get_us_stocks_data,
)


def _is_korean_equity_code(symbol: str) -> bool:
    """Check if symbol is a valid Korean equity code (6 alphanumeric characters).

    Korean stock codes are 6 characters:
    - Regular stocks: 6 digits (e.g., 005930)
    - ETF/ETN: 6 alphanumeric (e.g., 0123G0, 0117V0)
    """
    s = symbol.strip().upper()
    return len(s) == 6 and s.isalnum()


def _is_crypto_market(symbol: str) -> bool:
    s = symbol.strip().upper()
    return s.startswith("KRW-") or s.startswith("USDT-")


def _is_us_equity_symbol(symbol: str) -> bool:
    # Simple heuristic: has letters and no dash-prefix like KRW-
    s = symbol.strip().upper()
    return (not _is_crypto_market(s)) and any(c.isalpha() for c in s)


def _normalize_market(market: str | None) -> str | None:
    if not market:
        return None
    normalized = market.strip().lower()
    if not normalized:
        return None
    mapping = {
        "crypto": "crypto",
        "upbit": "crypto",
        "krw": "crypto",
        "usdt": "crypto",
        "kr": "equity_kr",
        "krx": "equity_kr",
        "korea": "equity_kr",
        "kospi": "equity_kr",
        "kosdaq": "equity_kr",
        "kis": "equity_kr",
        "equity_kr": "equity_kr",
        "us": "equity_us",
        "usa": "equity_us",
        "nyse": "equity_us",
        "nasdaq": "equity_us",
        "yahoo": "equity_us",
        "equity_us": "equity_us",
    }
    return mapping.get(normalized)


def _resolve_market_type(symbol: str, market: str | None) -> tuple[str, str]:
    """Resolve market type and validate symbol.

    Returns (market_type, normalized_symbol) or raises ValueError.
    """
    market_type = _normalize_market(market)

    # Explicit market specified - validate symbol format
    if market_type == "crypto":
        symbol = symbol.upper()
        if not _is_crypto_market(symbol):
            raise ValueError("crypto symbols must include KRW-/USDT- prefix")
        return "crypto", symbol

    if market_type == "equity_kr":
        if not _is_korean_equity_code(symbol):
            raise ValueError("korean equity symbols must be 6 alphanumeric characters")
        return "equity_kr", symbol

    if market_type == "equity_us":
        if _is_crypto_market(symbol):
            raise ValueError("us equity symbols must not include KRW-/USDT- prefix")
        return "equity_us", symbol

    # Auto-detect from symbol format
    if _is_crypto_market(symbol):
        return "crypto", symbol.upper()

    if _is_korean_equity_code(symbol):
        return "equity_kr", symbol

    if _is_us_equity_symbol(symbol):
        return "equity_us", symbol

    raise ValueError("Unsupported symbol format")


def _error_payload(
    *,
    source: str,
    message: str,
    symbol: str | None = None,
    instrument_type: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": message, "source": source}
    if symbol is not None:
        payload["symbol"] = symbol
    if instrument_type is not None:
        payload["instrument_type"] = instrument_type
    if query is not None:
        payload["query"] = query
    return payload


def _normalize_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, pd.Timedelta):
        return value.total_seconds()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value


def _normalize_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {str(key): _normalize_value(value) for key, value in row.items()}
        for row in df.to_dict(orient="records")
    ]


_MCP_USER_ID = _env_int("MCP_USER_ID", 1)
_DEFAULT_ACCOUNT_KEYS = {"default", "default_account", "기본계좌", "기본_계좌"}
_INSTRUMENT_TO_MARKET = {
    "equity_kr": "kr",
    "equity_us": "us",
    "crypto": "crypto",
}
_ACCOUNT_FILTER_ALIASES = {
    "kis": {"kis", "korea_investment", "한국투자", "한국투자증권"},
    "upbit": {"upbit", "업비트"},
    "toss": {"toss", "토스"},
    "samsung_pension": {"samsung_pension", "samsung_pension_account"},
    "isa": {"isa"},
}
_UPBIT_TICKER_BATCH_SIZE = 50


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _normalize_account_key(value: str | None) -> str:
    if not value:
        return ""
    normalized = value.strip().lower()
    return "".join(ch if ch.isalnum() else "_" for ch in normalized).strip("_")


def _canonical_account_id(broker: str, account_name: str | None) -> str:
    broker_key = _normalize_account_key(broker)
    account_key = _normalize_account_key(account_name)

    if not account_key or account_key in _DEFAULT_ACCOUNT_KEYS:
        return broker_key

    raw_name = (account_name or "").strip().lower()
    if "isa" in account_key:
        return "isa"
    if ("samsung" in account_key and "pension" in account_key) or (
        "삼성" in raw_name and "연금" in raw_name
    ):
        return "samsung_pension"

    return account_key


def _normalize_account_filter(account: str | None) -> str | None:
    key = _normalize_account_key(account)
    if not key:
        return None
    for canonical, aliases in _ACCOUNT_FILTER_ALIASES.items():
        if key == canonical or key in aliases:
            return canonical
    return key


def _match_account_filter(position: dict[str, Any], account_filter: str | None) -> bool:
    if not account_filter:
        return True

    account_keys = {
        _normalize_account_filter(position.get("account")),
        _normalize_account_filter(position.get("broker")),
        _normalize_account_filter(position.get("account_name")),
    }
    account_keys.discard(None)

    account_keys.add(
        _canonical_account_id(
            str(position.get("broker", "")),
            str(position.get("account_name", "")),
        )
    )

    return account_filter in account_keys


def _parse_holdings_market_filter(market: str | None) -> str | None:
    if market is None or not market.strip():
        return None
    market_type = _normalize_market(market)
    if market_type is None:
        raise ValueError("market must be one of: kr, us, crypto")
    return market_type


def _manual_market_to_instrument_type(market_type: MarketType) -> str:
    if market_type == MarketType.KR:
        return "equity_kr"
    if market_type == MarketType.US:
        return "equity_us"
    if market_type == MarketType.CRYPTO:
        return "crypto"
    raise ValueError(f"Unsupported market type: {market_type}")


def _instrument_to_manual_market_type(market_type: str | None) -> MarketType | None:
    if market_type == "equity_kr":
        return MarketType.KR
    if market_type == "equity_us":
        return MarketType.US
    if market_type == "crypto":
        return MarketType.CRYPTO
    return None


def _normalize_position_symbol(symbol: str, instrument_type: str) -> str:
    normalized = symbol.strip().upper()
    if instrument_type == "crypto" and normalized and "-" not in normalized:
        return f"KRW-{normalized}"
    if instrument_type == "equity_us":
        return to_db_symbol(normalized).upper()
    return normalized


def _position_to_output(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": position["symbol"],
        "name": position["name"],
        "market": position["market"],
        "quantity": position["quantity"],
        "avg_buy_price": position["avg_buy_price"],
        "current_price": position["current_price"],
        "evaluation_amount": position["evaluation_amount"],
        "profit_loss": position["profit_loss"],
        "profit_rate": position["profit_rate"],
    }


def _value_for_minimum_filter(position: dict[str, Any]) -> float:
    evaluation_amount = position.get("evaluation_amount")
    if evaluation_amount is not None:
        return _to_float(evaluation_amount, default=0.0)

    # Price lookup failed (current_price is None) -> treat as zero value for filtering.
    if position.get("current_price") is None:
        return 0.0

    quantity = _to_float(position.get("quantity"))
    current_price = _to_float(position.get("current_price"))
    return quantity * current_price


def _format_filter_threshold(value: float) -> str:
    return f"{value:g}"


def _is_position_symbol_match(
    *,
    position_symbol: str,
    query_symbol: str,
    instrument_type: str,
) -> bool:
    if instrument_type == "crypto":
        pos_norm = _normalize_position_symbol(position_symbol, "crypto")
        query_norm = _normalize_position_symbol(query_symbol, "crypto")
        if pos_norm == query_norm:
            return True
        pos_base = pos_norm.split("-", 1)[-1]
        query_base = query_norm.split("-", 1)[-1]
        return pos_base == query_base

    if instrument_type == "equity_us":
        return to_db_symbol(position_symbol).upper() == to_db_symbol(query_symbol).upper()

    return position_symbol.upper() == query_symbol.upper()


def _recalculate_profit_fields(position: dict[str, Any]) -> None:
    current_price = position.get("current_price")
    quantity = _to_float(position.get("quantity"))
    avg_buy_price = _to_float(position.get("avg_buy_price"))

    if current_price is None or quantity <= 0:
        position["current_price"] = None
        position["evaluation_amount"] = None
        position["profit_loss"] = None
        position["profit_rate"] = None
        return

    current_price = _to_float(current_price)
    position["current_price"] = current_price
    position["evaluation_amount"] = round(current_price * quantity, 2)

    if avg_buy_price > 0:
        profit_loss = (current_price - avg_buy_price) * quantity
        position["profit_loss"] = round(profit_loss, 2)
        position["profit_rate"] = round(
            ((current_price - avg_buy_price) / avg_buy_price) * 100, 2
        )
    else:
        position["profit_loss"] = None
        position["profit_rate"] = None


async def _collect_kis_positions(
    market_filter: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if market_filter == "crypto":
        return [], []

    positions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    kis = KISClient()

    if market_filter in (None, "equity_kr"):
        try:
            kr_stocks = await kis.fetch_my_stocks()
            for stock in kr_stocks:
                quantity = _to_float(stock.get("hldg_qty"))
                if quantity <= 0:
                    continue

                positions.append(
                    {
                        "account": "kis",
                        "account_name": "기본 계좌",
                        "broker": "kis",
                        "source": "kis_api",
                        "instrument_type": "equity_kr",
                        "market": "kr",
                        "symbol": _normalize_position_symbol(
                            str(stock.get("pdno", "")),
                            "equity_kr",
                        ),
                        "name": stock.get("prdt_name") or stock.get("pdno"),
                        "quantity": quantity,
                        "avg_buy_price": _to_float(stock.get("pchs_avg_pric")),
                        "current_price": _to_float(stock.get("prpr"), default=0.0)
                        or None,
                        "evaluation_amount": _to_float(stock.get("evlu_amt")),
                        "profit_loss": _to_float(stock.get("evlu_pfls_amt")),
                        "profit_rate": _to_float(stock.get("evlu_pfls_rt")),
                    }
                )
        except Exception as exc:
            errors.append({"source": "kis", "market": "kr", "error": str(exc)})

    if market_filter in (None, "equity_us"):
        try:
            us_stocks = await kis.fetch_my_us_stocks()
            for stock in us_stocks:
                quantity = _to_float(stock.get("ovrs_cblc_qty"))
                if quantity <= 0:
                    continue

                positions.append(
                    {
                        "account": "kis",
                        "account_name": "기본 계좌",
                        "broker": "kis",
                        "source": "kis_api",
                        "instrument_type": "equity_us",
                        "market": "us",
                        "symbol": _normalize_position_symbol(
                            str(stock.get("ovrs_pdno", "")),
                            "equity_us",
                        ),
                        "name": stock.get("ovrs_item_name") or stock.get("ovrs_pdno"),
                        "quantity": quantity,
                        "avg_buy_price": _to_float(stock.get("pchs_avg_pric")),
                        "current_price": _to_float(stock.get("now_pric2"), default=0.0)
                        or None,
                        "evaluation_amount": _to_float(stock.get("ovrs_stck_evlu_amt")),
                        "profit_loss": _to_float(stock.get("frcr_evlu_pfls_amt")),
                        "profit_rate": _to_float(stock.get("evlu_pfls_rt")),
                    }
                )
        except Exception as exc:
            errors.append({"source": "kis", "market": "us", "error": str(exc)})

    return positions, errors


async def _collect_upbit_positions(
    market_filter: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if market_filter not in (None, "crypto"):
        return [], []

    positions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    try:
        coin_name_map: dict[str, str] = {}
        try:
            crypto_maps = await get_or_refresh_maps()
            coin_name_map = crypto_maps.get("COIN_TO_NAME_KR", {}) or {}
        except Exception:
            coin_name_map = {}

        coins = await upbit_service.fetch_my_coins()
        for coin in coins:
            currency = str(coin.get("currency", "")).upper().strip()
            if not currency or currency == "KRW":
                continue

            quantity = _to_float(coin.get("balance")) + _to_float(coin.get("locked"))
            if quantity <= 0:
                continue

            unit_currency = str(coin.get("unit_currency", "KRW")).upper().strip()
            symbol = _normalize_position_symbol(
                f"{unit_currency or 'KRW'}-{currency}", "crypto"
            )

            positions.append(
                {
                    "account": "upbit",
                    "account_name": "기본 계좌",
                    "broker": "upbit",
                    "source": "upbit_api",
                    "instrument_type": "crypto",
                    "market": "crypto",
                    "symbol": symbol,
                    "name": coin_name_map.get(currency, symbol),
                    "quantity": quantity,
                    "avg_buy_price": _to_float(coin.get("avg_buy_price")),
                    "current_price": None,
                    "evaluation_amount": None,
                    "profit_loss": None,
                    "profit_rate": None,
                }
            )
    except Exception as exc:
        errors.append({"source": "upbit", "market": "crypto", "error": str(exc)})

    return positions, errors


async def _collect_manual_positions(
    *,
    user_id: int,
    market_filter: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    positions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    try:
        manual_market = _instrument_to_manual_market_type(market_filter)
        async with AsyncSessionLocal() as db:
            service = ManualHoldingsService(db)
            holdings = await service.get_holdings_by_user(
                user_id=user_id, market_type=manual_market
            )

        for holding in holdings:
            instrument_type = _manual_market_to_instrument_type(holding.market_type)
            symbol = _normalize_position_symbol(holding.ticker, instrument_type)
            quantity = _to_float(holding.quantity)
            if quantity <= 0:
                continue

            broker = holding.broker_account.broker_type.value
            account_name = holding.broker_account.account_name
            account = _canonical_account_id(broker, account_name)

            positions.append(
                {
                    "account": account,
                    "account_name": account_name or "기본 계좌",
                    "broker": broker,
                    "source": "manual",
                    "instrument_type": instrument_type,
                    "market": _INSTRUMENT_TO_MARKET[instrument_type],
                    "symbol": symbol,
                    "name": holding.display_name or symbol,
                    "quantity": quantity,
                    "avg_buy_price": _to_float(holding.avg_price),
                    "current_price": None,
                    "evaluation_amount": None,
                    "profit_loss": None,
                    "profit_rate": None,
                }
            )
    except Exception as exc:
        errors.append({"source": "manual_holdings", "error": str(exc)})

    return positions, errors


async def _fetch_price_map_for_positions(
    positions: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str], float], list[dict[str, Any]]]:
    price_map: dict[tuple[str, str], float] = {}
    price_errors: list[dict[str, Any]] = []

    crypto_symbols = sorted(
        {
            _normalize_position_symbol(position["symbol"], "crypto")
            for position in positions
            if position["instrument_type"] == "crypto"
        }
    )

    if crypto_symbols:
        valid_symbols = list(crypto_symbols)
        try:
            tradable_markets = await upbit_service.fetch_all_market_codes(fiat=None)
            tradable_set = {str(market).upper() for market in tradable_markets}
            valid_symbols = [
                symbol for symbol in crypto_symbols if symbol.upper() in tradable_set
            ]
            invalid_symbols = [
                symbol for symbol in crypto_symbols if symbol.upper() not in tradable_set
            ]
            for symbol in invalid_symbols:
                price_errors.append(
                    {
                        "source": "upbit",
                        "market": "crypto",
                        "symbol": symbol,
                        "stage": "current_price",
                        "error": "market not tradable on upbit (possibly delisted)",
                    }
                )
        except Exception as exc:
            price_errors.append(
                {
                    "source": "upbit",
                    "market": "crypto",
                    "stage": "current_price",
                    "error": f"failed to load tradable market list: {exc}",
                }
            )

        for offset in range(0, len(valid_symbols), _UPBIT_TICKER_BATCH_SIZE):
            batch_symbols = valid_symbols[offset : offset + _UPBIT_TICKER_BATCH_SIZE]
            try:
                prices = await upbit_service.fetch_multiple_current_prices(batch_symbols)
                for symbol in batch_symbols:
                    price = prices.get(symbol)
                    if price is not None:
                        price_map[("crypto", symbol.upper())] = float(price)
                missing_symbols_in_batch = [
                    symbol
                    for symbol in batch_symbols
                    if ("crypto", symbol.upper()) not in price_map
                ]
                for symbol in missing_symbols_in_batch:
                    price_errors.append(
                        {
                            "source": "upbit",
                            "market": "crypto",
                            "symbol": symbol,
                            "stage": "current_price",
                            "error": "price missing in batch ticker response",
                        }
                    )
            except Exception as exc:
                for symbol in batch_symbols:
                    if ("crypto", symbol.upper()) in price_map:
                        continue
                    price_errors.append(
                        {
                            "source": "upbit",
                            "market": "crypto",
                            "symbol": symbol,
                            "stage": "current_price",
                            "error": str(exc),
                        }
                    )

        # Deduplicate in case the same symbol is repeated across sources.
        if price_errors:
            deduped: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            for item in price_errors:
                key = (item.get("symbol", ""), item.get("stage", ""))
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(item)
            price_errors = deduped

    async def fetch_equity_price(
        instrument_type: str, symbol: str
    ) -> tuple[str, str, float | None]:
        try:
            if instrument_type == "equity_kr":
                quote = await _fetch_quote_equity_kr(symbol)
            else:
                quote = await _fetch_quote_equity_us(symbol)
            price = quote.get("price")
            return instrument_type, symbol, float(price) if price is not None else None
        except Exception:
            return instrument_type, symbol, None

    equity_tasks = [
        fetch_equity_price(instrument_type, symbol)
        for instrument_type, symbol in sorted(
            {
                (position["instrument_type"], position["symbol"])
                for position in positions
                if position["instrument_type"] in {"equity_kr", "equity_us"}
            }
        )
    ]

    if equity_tasks:
        results = await asyncio.gather(*equity_tasks)
        for instrument_type, symbol, price in results:
            if price is not None:
                price_map[(instrument_type, symbol)] = price

    return price_map, price_errors


async def _collect_portfolio_positions(
    *,
    account: str | None,
    market: str | None,
    include_current_price: bool,
    user_id: int = _MCP_USER_ID,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None, str | None]:
    market_filter = _parse_holdings_market_filter(market)
    account_filter = _normalize_account_filter(account)

    tasks: list[asyncio.Future[Any] | asyncio.Task[Any] | Any] = []
    if market_filter != "crypto":
        tasks.append(_collect_kis_positions(market_filter))
    if market_filter in (None, "crypto"):
        tasks.append(_collect_upbit_positions(market_filter))
    tasks.append(_collect_manual_positions(user_id=user_id, market_filter=market_filter))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    positions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for result in results:
        if isinstance(result, Exception):
            errors.append({"source": "holdings", "error": str(result)})
            continue
        source_positions, source_errors = result
        positions.extend(source_positions)
        errors.extend(source_errors)

    if market_filter:
        positions = [
            position
            for position in positions
            if position["instrument_type"] == market_filter
        ]

    if account_filter:
        positions = [
            position
            for position in positions
            if _match_account_filter(position, account_filter)
        ]

    if include_current_price and positions:
        price_map, price_errors = await _fetch_price_map_for_positions(positions)
        errors.extend(price_errors)
        for position in positions:
            price = price_map.get((position["instrument_type"], position["symbol"]))
            if price is not None:
                position["current_price"] = price
            _recalculate_profit_fields(position)
    else:
        for position in positions:
            position["current_price"] = None
            position["evaluation_amount"] = None
            position["profit_loss"] = None
            position["profit_rate"] = None

    positions.sort(
        key=lambda position: (
            position["account"],
            position["market"],
            position["symbol"],
        )
    )

    return positions, errors, market_filter, account_filter


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
        n=1,  # J = 주식/ETF/ETN
    )
    if df.empty:
        raise ValueError(f"Symbol '{symbol}' not found")
    last = df.iloc[-1].to_dict()
    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "price": last.get("close"),
        "open": last.get("open"),
        "high": last.get("high"),
        "low": last.get("low"),
        "volume": last.get("volume"),
        "value": last.get("value"),
        "source": "kis",
    }


async def _fetch_quote_equity_us(symbol: str) -> dict[str, Any]:
    """Fetch US equity quote from Yahoo Finance."""
    import yfinance as yf

    from app.core.symbol import to_yahoo_symbol

    yahoo_ticker = to_yahoo_symbol(symbol)
    info = yf.Ticker(yahoo_ticker).fast_info

    price = getattr(info, "last_price", None)
    if price is None:
        raise ValueError(f"Symbol '{symbol}' not found")

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "price": price,
        "previous_close": getattr(info, "regular_market_previous_close", None),
        "open": getattr(info, "open", None),
        "high": getattr(info, "day_high", None),
        "low": getattr(info, "day_low", None),
        "volume": getattr(info, "last_volume", None),
        "source": "yahoo",
    }


async def _fetch_ohlcv_crypto(
    symbol: str, count: int, period: str, end_date: datetime.datetime | None
) -> dict[str, Any]:
    """Fetch crypto OHLCV from Upbit."""
    capped_count = min(count, 200)
    df = await upbit_service.fetch_ohlcv(
        market=symbol, days=capped_count, period=period, end_date=end_date
    )
    return {
        "symbol": symbol,
        "instrument_type": "crypto",
        "source": "upbit",
        "period": period,
        "count": capped_count,
        "rows": _normalize_rows(df),
    }


async def _fetch_ohlcv_equity_kr(
    symbol: str,
    count: int,
    period: str,
    end_date: datetime.datetime | None,
) -> dict[str, Any]:
    """Fetch Korean equity OHLCV from KIS."""
    capped_count = min(count, 200)
    # KIS uses D/W/M for period
    kis_period_map = {"day": "D", "week": "W", "month": "M"}
    kis = KISClient()
    df = await kis.inquire_daily_itemchartprice(
        code=symbol,
        market="J",  # J = 주식/ETF/ETN
        n=capped_count,
        period=kis_period_map.get(period, "D"),
        end_date=end_date.date() if end_date else None,
    )
    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "source": "kis",
        "period": period,
        "count": capped_count,
        "rows": _normalize_rows(df),
    }


async def _fetch_ohlcv_equity_us(
    symbol: str, count: int, period: str, end_date: datetime.datetime | None
) -> dict[str, Any]:
    """Fetch US equity OHLCV from Yahoo Finance."""
    capped_count = min(count, 100)
    df = await yahoo_service.fetch_ohlcv(
        ticker=symbol, days=capped_count, period=period, end_date=end_date
    )
    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "yahoo",
        "period": period,
        "count": capped_count,
        "rows": _normalize_rows(df),
    }


# ---------------------------------------------------------------------------
# Technical Indicator Calculations
# ---------------------------------------------------------------------------

IndicatorType = Literal["sma", "ema", "rsi", "macd", "bollinger", "atr", "pivot"]

DEFAULT_SMA_PERIODS = [5, 20, 60, 120, 200]
DEFAULT_EMA_PERIODS = [5, 20, 60, 120, 200]
DEFAULT_RSI_PERIOD = 14
DEFAULT_MACD_FAST = 12
DEFAULT_MACD_SLOW = 26
DEFAULT_MACD_SIGNAL = 9
DEFAULT_BOLLINGER_PERIOD = 20
DEFAULT_BOLLINGER_STD = 2.0
DEFAULT_ATR_PERIOD = 14


def _calculate_sma(
    close: pd.Series, periods: list[int] | None = None
) -> dict[str, float | None]:
    """Calculate Simple Moving Average for multiple periods."""
    periods = periods or DEFAULT_SMA_PERIODS
    result: dict[str, float | None] = {}
    for period in periods:
        if len(close) >= period:
            sma_value = close.iloc[-period:].mean()
            result[str(period)] = float(sma_value) if pd.notna(sma_value) else None
        else:
            result[str(period)] = None
    return result


def _calculate_ema(
    close: pd.Series, periods: list[int] | None = None
) -> dict[str, float | None]:
    """Calculate Exponential Moving Average for multiple periods."""
    periods = periods or DEFAULT_EMA_PERIODS
    result: dict[str, float | None] = {}
    for period in periods:
        if len(close) >= period:
            ema = close.ewm(span=period, adjust=False).mean()
            ema_value = ema.iloc[-1]
            result[str(period)] = float(ema_value) if pd.notna(ema_value) else None
        else:
            result[str(period)] = None
    return result


def _calculate_rsi(
    close: pd.Series, period: int = DEFAULT_RSI_PERIOD
) -> dict[str, float | None]:
    """Calculate Relative Strength Index."""
    if len(close) < period + 1:
        return {str(period): None}

    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    rsi_value = rsi.iloc[-1]
    return {str(period): round(float(rsi_value), 2) if pd.notna(rsi_value) else None}


def _calculate_macd(
    close: pd.Series,
    fast: int = DEFAULT_MACD_FAST,
    slow: int = DEFAULT_MACD_SLOW,
    signal: int = DEFAULT_MACD_SIGNAL,
) -> dict[str, float | None]:
    """Calculate MACD, Signal, and Histogram."""
    if len(close) < slow + signal:
        return {"macd": None, "signal": None, "histogram": None}

    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line

    macd_val = macd_line.iloc[-1]
    signal_val = signal_line.iloc[-1]
    hist_val = histogram.iloc[-1]

    return {
        "macd": float(macd_val) if pd.notna(macd_val) else None,
        "signal": float(signal_val) if pd.notna(signal_val) else None,
        "histogram": float(hist_val) if pd.notna(hist_val) else None,
    }


def _calculate_bollinger(
    close: pd.Series,
    period: int = DEFAULT_BOLLINGER_PERIOD,
    std: float = DEFAULT_BOLLINGER_STD,
) -> dict[str, float | None]:
    """Calculate Bollinger Bands (upper, middle, lower)."""
    if len(close) < period:
        return {"upper": None, "middle": None, "lower": None}

    sma = close.rolling(window=period).mean()
    rolling_std = close.rolling(window=period).std()

    upper = sma + (rolling_std * std)
    lower = sma - (rolling_std * std)

    sma_val = sma.iloc[-1]
    upper_val = upper.iloc[-1]
    lower_val = lower.iloc[-1]

    return {
        "upper": float(upper_val) if pd.notna(upper_val) else None,
        "middle": float(sma_val) if pd.notna(sma_val) else None,
        "lower": float(lower_val) if pd.notna(lower_val) else None,
    }


def _calculate_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = DEFAULT_ATR_PERIOD
) -> dict[str, float | None]:
    """Calculate Average True Range."""
    if len(close) < period + 1:
        return {str(period): None}

    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    atr_value = atr.iloc[-1]

    return {str(period): float(atr_value) if pd.notna(atr_value) else None}


def _calculate_pivot(
    high: pd.Series, low: pd.Series, close: pd.Series
) -> dict[str, float | None]:
    """Calculate Pivot Points (classic) based on previous day's HLC."""
    if len(close) < 2:
        return {
            "p": None,
            "r1": None,
            "r2": None,
            "r3": None,
            "s1": None,
            "s2": None,
            "s3": None,
        }

    # Use previous day's data
    prev_high = float(high.iloc[-2])
    prev_low = float(low.iloc[-2])
    prev_close = float(close.iloc[-2])

    # Classic pivot point formula
    p = (prev_high + prev_low + prev_close) / 3
    r1 = 2 * p - prev_low
    r2 = p + (prev_high - prev_low)
    r3 = prev_high + 2 * (p - prev_low)
    s1 = 2 * p - prev_high
    s2 = p - (prev_high - prev_low)
    s3 = prev_low - 2 * (prev_high - p)

    return {
        "p": round(p, 2),
        "r1": round(r1, 2),
        "r2": round(r2, 2),
        "r3": round(r3, 2),
        "s1": round(s1, 2),
        "s2": round(s2, 2),
        "s3": round(s3, 2),
    }


FIBONACCI_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]


def _calculate_fibonacci(
    df: pd.DataFrame, current_price: float
) -> dict[str, Any]:
    """Calculate Fibonacci retracement levels from OHLCV DataFrame.

    Detects swing high/low, determines trend direction, and computes
    retracement levels with nearest support/resistance.
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    swing_high_price = round(float(high.max()), 2)
    swing_low_price = round(float(low.min()), 2)

    # Use positional index for ordering comparison
    swing_high_pos = int(high.values.argmax())
    swing_low_pos = int(low.values.argmin())

    # Determine dates
    def _to_date_str(row: pd.Series) -> str:
        d = row.get("date")
        if d is None:
            return ""
        if isinstance(d, str):
            return d[:10]
        if isinstance(d, (datetime.date, datetime.datetime, pd.Timestamp)):
            return d.strftime("%Y-%m-%d")
        return str(d)[:10]

    swing_high_date = _to_date_str(df.iloc[swing_high_pos])
    swing_low_date = _to_date_str(df.iloc[swing_low_pos])

    # Trend: if high came after low → retracement from high, else bounce from low
    if swing_high_pos > swing_low_pos:
        trend = "retracement_from_high"
        # Levels go from high (0%) down to low (100%)
        levels = {
            str(lvl): round(swing_high_price - lvl * (swing_high_price - swing_low_price), 2)
            for lvl in FIBONACCI_LEVELS
        }
    else:
        trend = "bounce_from_low"
        # Levels go from low (0%) up to high (100%)
        levels = {
            str(lvl): round(swing_low_price + lvl * (swing_high_price - swing_low_price), 2)
            for lvl in FIBONACCI_LEVELS
        }

    # Find nearest support (level price just below current) and resistance (just above)
    nearest_support: dict[str, Any] | None = None
    nearest_resistance: dict[str, Any] | None = None

    sorted_levels = sorted(levels.items(), key=lambda x: x[1])
    for level_str, price in sorted_levels:
        if price < current_price:
            nearest_support = {"level": level_str, "price": price}
        elif price > current_price and nearest_resistance is None:
            nearest_resistance = {"level": level_str, "price": price}

    return {
        "swing_high": {"price": swing_high_price, "date": swing_high_date},
        "swing_low": {"price": swing_low_price, "date": swing_low_date},
        "trend": trend,
        "current_price": current_price,
        "levels": levels,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
    }


def _compute_indicators(
    df: pd.DataFrame, indicators: list[IndicatorType]
) -> dict[str, dict[str, float | None]]:
    """Compute requested indicators from OHLCV DataFrame.

    Args:
        df: DataFrame with columns: open, high, low, close, volume
        indicators: List of indicator types to compute

    Returns:
        Dictionary with indicator results
    """
    results: dict[str, dict[str, float | None]] = {}

    # Ensure we have required columns
    required = {"close"}
    if "atr" in indicators or "pivot" in indicators:
        required |= {"high", "low"}

    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    close = df["close"].astype(float)
    high = df["high"].astype(float) if "high" in df.columns else None
    low = df["low"].astype(float) if "low" in df.columns else None

    for indicator in indicators:
        if indicator == "sma":
            results["sma"] = _calculate_sma(close)
        elif indicator == "ema":
            results["ema"] = _calculate_ema(close)
        elif indicator == "rsi":
            results["rsi"] = _calculate_rsi(close)
        elif indicator == "macd":
            results["macd"] = _calculate_macd(close)
        elif indicator == "bollinger":
            results["bollinger"] = _calculate_bollinger(close)
        elif indicator == "atr":
            if high is not None and low is not None:
                results["atr"] = _calculate_atr(high, low, close)
            else:
                results["atr"] = {str(DEFAULT_ATR_PERIOD): None}
        elif indicator == "pivot":
            if high is not None and low is not None:
                results["pivot"] = _calculate_pivot(high, low, close)
            else:
                results["pivot"] = {
                    "p": None,
                    "r1": None,
                    "r2": None,
                    "r3": None,
                    "s1": None,
                    "s2": None,
                    "s3": None,
                }

    return results


async def _fetch_ohlcv_crypto_paginated(
    symbol: str, count: int, period: str = "day"
) -> pd.DataFrame:
    """Fetch crypto OHLCV with pagination to overcome Upbit's 200 limit.

    Args:
        symbol: Market symbol (e.g., "KRW-BTC")
        count: Total number of candles to fetch
        period: Candle period ("day", "week", "month")

    Returns:
        DataFrame with requested number of candles
    """
    max_per_request = 200
    all_dfs: list[pd.DataFrame] = []
    remaining = count
    end_date: datetime.datetime | None = None

    while remaining > 0:
        batch_size = min(remaining, max_per_request)
        df_batch = await upbit_service.fetch_ohlcv(
            market=symbol, days=batch_size, period=period, end_date=end_date
        )

        if df_batch.empty:
            break

        all_dfs.append(df_batch)
        remaining -= len(df_batch)

        if remaining > 0 and len(df_batch) > 0:
            # Get the earliest date from this batch for next pagination
            earliest_date = df_batch["date"].min()
            # Set end_date to the day before the earliest date
            end_date = datetime.datetime.combine(
                earliest_date - datetime.timedelta(days=1),
                datetime.time(23, 59, 59),
            )

    if not all_dfs:
        return pd.DataFrame()

    # Concatenate all batches, sort by date, and remove duplicates
    combined = pd.concat(all_dfs, ignore_index=True)
    combined = (
        combined.drop_duplicates(subset=["date"])
        .sort_values("date")
        .reset_index(drop=True)
    )

    return combined


async def _fetch_ohlcv_for_indicators(
    symbol: str, market_type: str, count: int = 250
) -> pd.DataFrame:
    """Fetch OHLCV data for indicator calculation.

    Fetches enough data for long-term indicators (200-day SMA needs 200+ candles).
    """
    if market_type == "crypto":
        # Use pagination for crypto to overcome Upbit's 200 limit
        df = await _fetch_ohlcv_crypto_paginated(symbol, count=count, period="day")
    elif market_type == "equity_kr":
        capped_count = min(count, 250)
        kis = KISClient()
        df = await kis.inquire_daily_itemchartprice(
            code=symbol, market="J", n=capped_count, period="D"
        )
    else:  # equity_us
        capped_count = min(count, 250)
        df = await yahoo_service.fetch_ohlcv(
            ticker=symbol, days=capped_count, period="day"
        )

    return df


# ---------------------------------------------------------------------------
# Volume Profile Calculations
# ---------------------------------------------------------------------------


def _normalize_number(value: float, decimals: int = 6) -> float | int:
    """Normalize float output to readable numeric values."""
    rounded = round(float(value), decimals)
    if abs(rounded - round(rounded)) < 10 ** (-decimals):
        return int(round(rounded))
    return rounded


async def _fetch_ohlcv_for_volume_profile(
    symbol: str, market_type: str, period_days: int
) -> pd.DataFrame:
    """Fetch daily OHLCV data for volume profile analysis."""
    if market_type == "crypto":
        return await _fetch_ohlcv_crypto_paginated(
            symbol=symbol, count=period_days, period="day"
        )
    if market_type == "equity_kr":
        kis = KISClient()
        return await kis.inquire_daily_itemchartprice(
            code=symbol, market="J", n=period_days, period="D"
        )
    return await yahoo_service.fetch_ohlcv(
        ticker=symbol, days=period_days, period="day"
    )


def _calculate_volume_profile(
    df: pd.DataFrame,
    bins: int,
    value_area_ratio: float = 0.70,
) -> dict[str, Any]:
    """Calculate price-by-volume distribution from OHLCV candles."""
    if bins < 2:
        raise ValueError("bins must be >= 2")
    if not 0 < value_area_ratio <= 1:
        raise ValueError("value_area_ratio must be between 0 and 1")

    required = {"low", "high", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    if df.empty:
        raise ValueError("No OHLCV data available")

    low = pd.to_numeric(df["low"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce")

    valid_mask = (~low.isna()) & (~high.isna()) & (~volume.isna())
    if not valid_mask.any():
        raise ValueError("No valid OHLCV rows with low/high/volume")

    low_values = low[valid_mask].astype(float).to_numpy()
    high_values = high[valid_mask].astype(float).to_numpy()
    candle_low = np.minimum(low_values, high_values)
    candle_high = np.maximum(low_values, high_values)
    candle_volume = volume[valid_mask].astype(float).to_numpy()

    price_low = float(candle_low.min())
    price_high = float(candle_high.max())

    if price_high <= price_low:
        # Flat-price edge case: make a tiny synthetic range for binning math.
        epsilon = max(abs(price_low) * 1e-6, 1e-6)
        bin_edges = np.linspace(
            price_low - epsilon / 2,
            price_high + epsilon / 2,
            bins + 1,
        )
    else:
        bin_edges = np.linspace(price_low, price_high, bins + 1)

    bin_volumes = np.zeros(bins, dtype=float)

    for low_i, high_i, vol_i in zip(
        candle_low, candle_high, candle_volume, strict=False
    ):
        if vol_i <= 0:
            continue

        if high_i <= low_i:
            idx = int(
                np.clip(np.searchsorted(bin_edges, low_i, side="right") - 1, 0, bins - 1)
            )
            bin_volumes[idx] += vol_i
            continue

        overlaps = np.minimum(bin_edges[1:], high_i) - np.maximum(bin_edges[:-1], low_i)
        overlaps = np.clip(overlaps, 0.0, None)
        overlap_sum = float(overlaps.sum())

        if overlap_sum <= 0:
            mid_price = (low_i + high_i) / 2
            idx = int(
                np.clip(
                    np.searchsorted(bin_edges, mid_price, side="right") - 1,
                    0,
                    bins - 1,
                )
            )
            bin_volumes[idx] += vol_i
            continue

        bin_volumes += vol_i * (overlaps / overlap_sum)

    total_volume = float(bin_volumes.sum())
    if total_volume <= 0:
        raise ValueError("Total volume is zero for the selected period")

    bin_volume_pct = (bin_volumes / total_volume) * 100
    poc_index = int(np.argmax(bin_volumes))

    target_volume = total_volume * value_area_ratio
    covered_volume = float(bin_volumes[poc_index])
    left_index = poc_index
    right_index = poc_index

    while covered_volume < target_volume and (
        left_index > 0 or right_index < bins - 1
    ):
        left_vol = bin_volumes[left_index - 1] if left_index > 0 else -np.inf
        right_vol = (
            bin_volumes[right_index + 1] if right_index < bins - 1 else -np.inf
        )

        if right_vol > left_vol:
            right_index += 1
            covered_volume += float(bin_volumes[right_index])
        else:
            if left_index > 0:
                left_index -= 1
                covered_volume += float(bin_volumes[left_index])
            elif right_index < bins - 1:
                right_index += 1
                covered_volume += float(bin_volumes[right_index])
            else:
                break

    profile = [
        {
            "price_low": _normalize_number(bin_edges[idx], decimals=6),
            "price_high": _normalize_number(bin_edges[idx + 1], decimals=6),
            "volume": _normalize_number(bin_volumes[idx], decimals=2),
            "volume_pct": _normalize_number(bin_volume_pct[idx], decimals=2),
        }
        for idx in range(bins)
    ]

    return {
        "price_range": {
            "low": _normalize_number(price_low, decimals=6),
            "high": _normalize_number(price_high, decimals=6),
        },
        "poc": {
            "price": _normalize_number(
                (bin_edges[poc_index] + bin_edges[poc_index + 1]) / 2,
                decimals=6,
            ),
            "volume": _normalize_number(bin_volumes[poc_index], decimals=2),
        },
        "value_area": {
            "high": _normalize_number(bin_edges[right_index + 1], decimals=6),
            "low": _normalize_number(bin_edges[left_index], decimals=6),
            "volume_pct": _normalize_number(
                (covered_volume / total_volume) * 100, decimals=2
            ),
        },
        "profile": profile,
    }


# ---------------------------------------------------------------------------
# Finnhub API Helpers
# ---------------------------------------------------------------------------


def _get_finnhub_client() -> finnhub.Client:
    """Get Finnhub client with API key from settings."""
    api_key = settings.finnhub_api_key
    if not api_key:
        raise ValueError("FINNHUB_API_KEY environment variable is not set")
    return finnhub.Client(api_key=api_key)


async def _fetch_news_finnhub(symbol: str, market: str, limit: int) -> dict[str, Any]:
    """Fetch news from Finnhub API.

    Args:
        symbol: Stock symbol (e.g., "AAPL") or crypto symbol (e.g., "BINANCE:BTCUSDT")
        market: Market type - "us" or "crypto"
        limit: Maximum number of news items to return

    Returns:
        Dictionary with news data
    """
    client = _get_finnhub_client()

    # Calculate date range (last 7 days for company news)
    to_date = datetime.date.today()
    from_date = to_date - datetime.timedelta(days=7)

    def fetch_sync() -> list[dict[str, Any]]:
        if market == "crypto":
            # For crypto, use general news with crypto category
            news = client.general_news("crypto", min_id=0)
        else:
            # For US stocks, use company news
            news = client.company_news(
                symbol.upper(),
                _from=from_date.strftime("%Y-%m-%d"),
                to=to_date.strftime("%Y-%m-%d"),
            )
        return news[:limit] if news else []

    news_items = await asyncio.to_thread(fetch_sync)

    # Transform to consistent format
    result_items = []
    for item in news_items:
        result_items.append(
            {
                "title": item.get("headline", ""),
                "source": item.get("source", ""),
                "datetime": datetime.datetime.fromtimestamp(
                    item.get("datetime", 0)
                ).isoformat()
                if item.get("datetime")
                else None,
                "url": item.get("url", ""),
                "summary": item.get("summary", ""),
                "sentiment": item.get("sentiment"),  # May be None
                "related": item.get("related", ""),
            }
        )

    return {
        "symbol": symbol,
        "market": market,
        "source": "finnhub",
        "count": len(result_items),
        "news": result_items,
    }


async def _fetch_company_profile_finnhub(symbol: str) -> dict[str, Any]:
    """Fetch company profile from Finnhub API.

    Args:
        symbol: US stock symbol (e.g., "AAPL")

    Returns:
        Dictionary with company profile data
    """
    client = _get_finnhub_client()

    def fetch_sync() -> dict[str, Any]:
        return client.company_profile2(symbol=symbol.upper())

    profile = await asyncio.to_thread(fetch_sync)

    if not profile:
        raise ValueError(f"Company profile not found for symbol '{symbol}'")

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "finnhub",
        "name": profile.get("name", ""),
        "ticker": profile.get("ticker", ""),
        "country": profile.get("country", ""),
        "currency": profile.get("currency", ""),
        "exchange": profile.get("exchange", ""),
        "ipo_date": profile.get("ipo", ""),
        "market_cap": profile.get("marketCapitalization"),
        "shares_outstanding": profile.get("shareOutstanding"),
        "sector": profile.get("finnhubIndustry", ""),
        "website": profile.get("weburl", ""),
        "logo": profile.get("logo", ""),
        "phone": profile.get("phone", ""),
    }


async def _fetch_financials_finnhub(
    symbol: str, statement: str, freq: str
) -> dict[str, Any]:
    """Fetch financial statements from Finnhub API.

    Args:
        symbol: US stock symbol (e.g., "AAPL")
        statement: Statement type - "income", "balance", or "cashflow"
        freq: Frequency - "annual" or "quarterly"

    Returns:
        Dictionary with financial data
    """
    client = _get_finnhub_client()

    # Map statement types to Finnhub format
    statement_map = {
        "income": "ic",
        "balance": "bs",
        "cashflow": "cf",
    }
    finnhub_statement = statement_map.get(statement)
    if not finnhub_statement:
        raise ValueError(
            f"Invalid statement type '{statement}'. Use: income, balance, cashflow"
        )

    def fetch_sync() -> dict[str, Any]:
        return client.financials_reported(
            symbol=symbol.upper(),
            freq=freq,
        )

    result = await asyncio.to_thread(fetch_sync)

    if not result or not result.get("data"):
        raise ValueError(f"Financial data not found for symbol '{symbol}'")

    # Extract relevant financial data
    reports = []
    for report in result.get("data", [])[:4]:  # Last 4 reports
        report_data = report.get("report", {})
        statement_data = report_data.get(finnhub_statement, [])

        # Convert list of dicts to a single dict
        financials = {}
        for item in statement_data:
            label = item.get("label", item.get("concept", ""))
            value = item.get("value")
            if label and value is not None:
                financials[label] = value

        reports.append(
            {
                "year": report.get("year"),
                "quarter": report.get("quarter"),
                "filed_date": report.get("filedDate"),
                "period_start": report.get("startDate"),
                "period_end": report.get("endDate"),
                "data": financials,
            }
        )

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "finnhub",
        "statement": statement,
        "freq": freq,
        "reports": reports,
    }


async def _fetch_insider_transactions_finnhub(
    symbol: str, limit: int
) -> dict[str, Any]:
    """Fetch insider transactions from Finnhub API.

    Args:
        symbol: US stock symbol (e.g., "AAPL")
        limit: Maximum number of transactions to return

    Returns:
        Dictionary with insider transaction data
    """
    client = _get_finnhub_client()

    def fetch_sync() -> dict[str, Any]:
        return client.stock_insider_transactions(symbol=symbol.upper())

    result = await asyncio.to_thread(fetch_sync)

    if not result or not result.get("data"):
        return {
            "symbol": symbol,
            "instrument_type": "equity_us",
            "source": "finnhub",
            "count": 0,
            "transactions": [],
        }

    transactions = []
    for txn in result.get("data", [])[:limit]:
        # Transaction codes: P=Purchase, S=Sale, A=Grant, D=Sale to issuer, etc.
        txn_code = txn.get("transactionCode", "")
        txn_type_map = {
            "P": "Purchase",
            "S": "Sale",
            "A": "Grant/Award",
            "D": "Sale to Issuer",
            "F": "Tax Payment",
            "M": "Option Exercise",
            "G": "Gift",
            "C": "Conversion",
            "J": "Other",
        }
        transactions.append(
            {
                "name": txn.get("name", ""),
                "transaction_type": txn_type_map.get(txn_code, txn_code),
                "transaction_code": txn_code,
                "shares": txn.get("share"),
                "change": txn.get("change"),  # Net change in shares
                "price": txn.get("transactionPrice"),
                "date": txn.get("transactionDate"),
                "filing_date": txn.get("filingDate"),
            }
        )

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "finnhub",
        "count": len(transactions),
        "transactions": transactions,
    }


async def _fetch_earnings_calendar_finnhub(
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """Fetch earnings calendar from Finnhub API.

    Args:
        symbol: US stock symbol (optional, e.g., "AAPL")
        from_date: Start date in ISO format (optional)
        to_date: End date in ISO format (optional)

    Returns:
        Dictionary with earnings calendar data
    """
    client = _get_finnhub_client()

    # Default to next 30 days if no dates provided
    if not from_date:
        from_date = datetime.date.today().isoformat()
    if not to_date:
        to_date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()

    def fetch_sync() -> dict[str, Any]:
        # Finnhub API accepts empty string for symbol to get all earnings
        return client.earnings_calendar(
            symbol=symbol.upper() if symbol else "",
            _from=from_date,
            to=to_date,
        )

    result = await asyncio.to_thread(fetch_sync)

    if not result or not result.get("earningsCalendar"):
        return {
            "symbol": symbol,
            "instrument_type": "equity_us",
            "source": "finnhub",
            "from_date": from_date,
            "to_date": to_date,
            "count": 0,
            "earnings": [],
        }

    earnings = []
    for item in result.get("earningsCalendar", []):
        earnings.append(
            {
                "symbol": item.get("symbol", ""),
                "date": item.get("date"),
                "hour": item.get(
                    "hour", ""
                ),  # "bmo" (before market open), "amc" (after market close)
                "eps_estimate": item.get("epsEstimate"),
                "eps_actual": item.get("epsActual"),
                "revenue_estimate": item.get("revenueEstimate"),
                "revenue_actual": item.get("revenueActual"),
                "quarter": item.get("quarter"),
                "year": item.get("year"),
            }
        )

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "finnhub",
        "from_date": from_date,
        "to_date": to_date,
        "count": len(earnings),
        "earnings": earnings,
    }


# ---------------------------------------------------------------------------
# Naver Finance Helpers (Korean Stocks)
# ---------------------------------------------------------------------------


async def _fetch_news_naver(symbol: str, limit: int) -> dict[str, Any]:
    """Fetch news from Naver Finance for Korean stocks.

    Args:
        symbol: Korean stock code (6 digits, e.g., "005930")
        limit: Maximum number of news items to return

    Returns:
        Dictionary with news data
    """
    news_items = await naver_finance.fetch_news(symbol, limit=limit)

    return {
        "symbol": symbol,
        "market": "kr",
        "source": "naver",
        "count": len(news_items),
        "news": news_items,
    }


async def _fetch_company_profile_naver(symbol: str) -> dict[str, Any]:
    """Fetch company profile from Naver Finance for Korean stocks.

    Args:
        symbol: Korean stock code (6 digits, e.g., "005930")

    Returns:
        Dictionary with company profile data
    """
    profile = await naver_finance.fetch_company_profile(symbol)

    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **profile,
    }


async def _fetch_financials_naver(
    symbol: str, statement: str, freq: str
) -> dict[str, Any]:
    """Fetch financial statements from Naver Finance for Korean stocks.

    Args:
        symbol: Korean stock code (6 digits, e.g., "005930")
        statement: Statement type - "income", "balance", or "cashflow"
        freq: Frequency - "annual" or "quarterly"

    Returns:
        Dictionary with financial statement data
    """
    financials = await naver_finance.fetch_financials(symbol, statement, freq)

    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **financials,
    }


async def _fetch_investor_trends_naver(symbol: str, days: int) -> dict[str, Any]:
    """Fetch investor trends from Naver Finance for Korean stocks.

    Args:
        symbol: Korean stock code (6 digits, e.g., "005930")
        days: Number of days of data to fetch

    Returns:
        Dictionary with investor trend data
    """
    trends = await naver_finance.fetch_investor_trends(symbol, days=days)

    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **trends,
    }


async def _fetch_investment_opinions_naver(symbol: str, limit: int) -> dict[str, Any]:
    """Fetch investment opinions from Naver Finance for Korean stocks.

    Args:
        symbol: Korean stock code (6 digits, e.g., "005930")
        limit: Maximum number of opinions to return

    Returns:
        Dictionary with investment opinion data
    """
    opinions = await naver_finance.fetch_investment_opinions(symbol, limit=limit)

    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **opinions,
    }


async def _fetch_investment_opinions_yfinance(
    symbol: str, limit: int
) -> dict[str, Any]:
    """Fetch analyst opinions from yfinance for US stocks.

    Uses Ticker.analyst_price_targets for consensus targets
    and Ticker.upgrades_downgrades for individual firm recommendations.

    Args:
        symbol: US stock ticker (e.g., "AAPL")
        limit: Maximum number of recommendations to return

    Returns:
        Dictionary with analyst opinion data
    """
    loop = asyncio.get_running_loop()
    ticker = yf.Ticker(symbol)

    def _collect() -> tuple[dict | None, Any, dict | None]:
        targets = None
        try:
            targets = ticker.analyst_price_targets
        except Exception:
            pass

        ud = None
        try:
            ud = ticker.upgrades_downgrades
        except Exception:
            pass

        info = None
        try:
            info = ticker.info
        except Exception:
            pass
        return targets, ud, info

    targets, ud, info = await loop.run_in_executor(None, _collect)

    current_price = (info or {}).get("currentPrice")

    # --- price targets ---
    avg_target: float | None = None
    max_target: float | None = None
    min_target: float | None = None
    if isinstance(targets, dict):
        avg_target = targets.get("mean") or targets.get("median")
        max_target = targets.get("high")
        min_target = targets.get("low")
        if current_price is None:
            current_price = targets.get("current")

    upside: float | None = None
    if current_price and avg_target:
        upside = round((avg_target - current_price) / current_price * 100, 2)

    # --- recent recommendations ---
    recommendations: list[dict[str, Any]] = []
    if ud is not None and not ud.empty:
        df = ud.head(limit).reset_index()
        for _, row in df.iterrows():
            rec: dict[str, Any] = {
                "firm": row.get("Firm"),
                "rating": row.get("ToGrade"),
                "date": (
                    row["GradeDate"].strftime("%Y-%m-%d")
                    if hasattr(row.get("GradeDate", None), "strftime")
                    else str(row.get("GradeDate", ""))[:10]
                ),
            }
            pt = row.get("currentPriceTarget")
            if pt and pt > 0:
                rec["target_price"] = float(pt)
            recommendations.append(rec)

    return {
        "instrument_type": "equity_us",
        "source": "yfinance",
        "symbol": symbol.upper(),
        "current_price": current_price,
        "avg_target_price": avg_target,
        "max_target_price": max_target,
        "min_target_price": min_target,
        "upside_potential": upside,
        "count": len(recommendations),
        "recommendations": recommendations,
    }


async def _fetch_valuation_naver(symbol: str) -> dict[str, Any]:
    """Fetch valuation metrics from Naver Finance for Korean stocks.

    Args:
        symbol: Korean stock code (6 digits, e.g., "005930")

    Returns:
        Dictionary with valuation metrics (PER, PBR, ROE, dividend_yield,
        52-week high/low, current price, current_position_52w)
    """
    valuation = await naver_finance.fetch_valuation(symbol)

    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **valuation,
    }


async def _fetch_valuation_yfinance(symbol: str) -> dict[str, Any]:
    """Fetch valuation metrics from yfinance for US stocks.

    Args:
        symbol: US stock ticker (e.g., "AAPL", "MSFT")

    Returns:
        Dictionary with valuation metrics (PER, PBR, ROE, dividend_yield,
        52-week high/low, current price, current_position_52w)
    """
    loop = asyncio.get_running_loop()
    ticker = yf.Ticker(symbol)
    info: dict[str, Any] = await loop.run_in_executor(None, lambda: ticker.info)

    current_price = info.get("currentPrice")
    high_52w = info.get("fiftyTwoWeekHigh")
    low_52w = info.get("fiftyTwoWeekLow")

    # Calculate 52-week position
    current_position_52w = None
    if current_price is not None and high_52w is not None and low_52w is not None:
        if high_52w > low_52w:
            current_position_52w = round((current_price - low_52w) / (high_52w - low_52w), 2)

    # ROE is returned as a decimal (e.g. 1.47 = 147%), convert to percentage
    roe_raw = info.get("returnOnEquity")
    roe = round(roe_raw * 100, 2) if roe_raw is not None else None

    return {
        "instrument_type": "equity_us",
        "source": "yfinance",
        "symbol": symbol.upper(),
        "name": info.get("shortName") or info.get("longName"),
        "current_price": current_price,
        "per": info.get("trailingPE"),
        "pbr": info.get("priceToBook"),
        "roe": roe,
        "dividend_yield": info.get("dividendYield"),
        "high_52w": high_52w,
        "low_52w": low_52w,
        "current_position_52w": current_position_52w,
    }


async def _fetch_sector_peers_naver(
    symbol: str, limit: int
) -> dict[str, Any]:
    """Fetch sector peers for a Korean stock via Naver Finance.

    Args:
        symbol: Korean stock code (6 digits, e.g., "298040")
        limit: Max number of peers to return

    Returns:
        Dictionary with target info, peers list, and comparison metrics
    """
    data = await naver_finance.fetch_sector_peers(symbol, limit=limit)

    peers = data["peers"]

    # Build comparison metrics
    target_per = data.get("per")
    target_pbr = data.get("pbr")

    all_pers = [v for v in [target_per] + [p.get("per") for p in peers] if v is not None and v > 0]
    all_pbrs = [v for v in [target_pbr] + [p.get("pbr") for p in peers] if v is not None and v > 0]

    avg_per = round(sum(all_pers) / len(all_pers), 2) if all_pers else None
    avg_pbr = round(sum(all_pbrs) / len(all_pbrs), 2) if all_pbrs else None

    target_per_rank = None
    if target_per is not None and target_per > 0 and all_pers:
        sorted_pers = sorted(all_pers)
        target_per_rank = f"{sorted_pers.index(target_per) + 1}/{len(sorted_pers)}"

    target_pbr_rank = None
    if target_pbr is not None and target_pbr > 0 and all_pbrs:
        sorted_pbrs = sorted(all_pbrs)
        target_pbr_rank = f"{sorted_pbrs.index(target_pbr) + 1}/{len(sorted_pbrs)}"

    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        "symbol": symbol,
        "name": data.get("name"),
        "sector": data.get("sector"),
        "current_price": data.get("current_price"),
        "change_pct": data.get("change_pct"),
        "per": target_per,
        "pbr": target_pbr,
        "market_cap": data.get("market_cap"),
        "peers": peers,
        "comparison": {
            "avg_per": avg_per,
            "avg_pbr": avg_pbr,
            "target_per_rank": target_per_rank,
            "target_pbr_rank": target_pbr_rank,
        },
    }


async def _fetch_sector_peers_us(
    symbol: str, limit: int
) -> dict[str, Any]:
    """Fetch sector peers for a US stock via Finnhub + yfinance.

    Args:
        symbol: US stock ticker (e.g., "AAPL")
        limit: Max number of peers to return

    Returns:
        Dictionary with target info, peers list, and comparison metrics
    """
    client = _get_finnhub_client()
    upper_symbol = symbol.upper()

    # Step 1: Get peer tickers from Finnhub
    peer_tickers: list[str] = await asyncio.to_thread(
        client.company_peers, upper_symbol
    )
    peer_tickers = [t for t in peer_tickers if t.upper() != upper_symbol]
    peer_tickers = peer_tickers[: limit + 5]

    # Step 2: Fetch yfinance info concurrently for target + peers
    all_tickers = [upper_symbol] + peer_tickers

    async def _fetch_yf_info(ticker: str) -> tuple[str, dict[str, Any] | None]:
        try:
            info: dict[str, Any] = await asyncio.to_thread(
                lambda t=ticker: yf.Ticker(t).info
            )
            return (ticker, info)
        except Exception:
            return (ticker, None)

    results = await asyncio.gather(*[_fetch_yf_info(t) for t in all_tickers])
    info_map = {t: info for t, info in results if info}

    # Step 3: Build target info
    target_info = info_map.get(upper_symbol, {})
    target_name = target_info.get("shortName") or target_info.get("longName")
    target_sector = target_info.get("sector")
    target_industry = target_info.get("industry")
    target_price = target_info.get("currentPrice")
    target_prev = target_info.get("previousClose") or target_info.get(
        "regularMarketPreviousClose"
    )
    target_change_pct = (
        round((target_price - target_prev) / target_prev * 100, 2)
        if target_price and target_prev and target_prev > 0
        else None
    )
    target_per = target_info.get("trailingPE")
    target_pbr = target_info.get("priceToBook")
    target_mcap = target_info.get("marketCap")

    # Step 4: Build peers
    peers: list[dict[str, Any]] = []
    for ticker in peer_tickers:
        info = info_map.get(ticker)
        if info is None:
            continue
        price = info.get("currentPrice")
        prev = info.get("previousClose") or info.get("regularMarketPreviousClose")
        change_pct = (
            round((price - prev) / prev * 100, 2)
            if price and prev and prev > 0
            else None
        )
        peers.append(
            {
                "symbol": ticker,
                "name": info.get("shortName") or info.get("longName"),
                "current_price": price,
                "change_pct": change_pct,
                "per": info.get("trailingPE"),
                "pbr": info.get("priceToBook"),
                "market_cap": info.get("marketCap"),
            }
        )

    peers.sort(key=lambda x: x.get("market_cap") or 0, reverse=True)
    peers = peers[:limit]

    # Step 5: Comparison metrics
    all_pers = [
        v
        for v in [target_per] + [p.get("per") for p in peers]
        if v is not None and v > 0
    ]
    all_pbrs = [
        v
        for v in [target_pbr] + [p.get("pbr") for p in peers]
        if v is not None and v > 0
    ]

    avg_per = round(sum(all_pers) / len(all_pers), 2) if all_pers else None
    avg_pbr = round(sum(all_pbrs) / len(all_pbrs), 2) if all_pbrs else None

    target_per_rank = None
    if target_per is not None and target_per > 0 and all_pers:
        sorted_pers = sorted(all_pers)
        target_per_rank = f"{sorted_pers.index(target_per) + 1}/{len(sorted_pers)}"

    target_pbr_rank = None
    if target_pbr is not None and target_pbr > 0 and all_pbrs:
        sorted_pbrs = sorted(all_pbrs)
        target_pbr_rank = f"{sorted_pbrs.index(target_pbr) + 1}/{len(sorted_pbrs)}"

    return {
        "instrument_type": "equity_us",
        "source": "finnhub+yfinance",
        "symbol": upper_symbol,
        "name": target_name,
        "sector": target_sector,
        "industry": target_industry,
        "current_price": target_price,
        "change_pct": target_change_pct,
        "per": target_per,
        "pbr": target_pbr,
        "market_cap": target_mcap,
        "peers": peers,
        "comparison": {
            "avg_per": avg_per,
            "avg_pbr": avg_pbr,
            "target_per_rank": target_per_rank,
            "target_pbr_rank": target_pbr_rank,
        },
    }


async def _search_master_data(
    query: str, limit: int, instrument_type: str | None = None
) -> list[dict[str, Any]]:
    """마스터 데이터에서 종목 검색 (KRX, US, Crypto)

    Args:
        query: 검색어 (심볼 또는 이름)
        limit: 최대 결과 개수
        instrument_type: 필터링할 상품 유형 (equity_kr, equity_us, crypto, None=전체)
    """
    results: list[dict[str, Any]] = []
    query_lower = query.lower()
    query_upper = query.upper()

    # 1. KRX (KOSPI + KOSDAQ) 검색
    if instrument_type is None or instrument_type == "equity_kr":
        kospi = get_kospi_name_to_code()
        kosdaq = get_kosdaq_name_to_code()

        for name, code in kospi.items():
            if query_lower in name.lower() or query_upper in code:
                results.append(
                    {
                        "symbol": code,
                        "name": name,
                        "instrument_type": "equity_kr",
                        "exchange": "KOSPI",
                        "is_active": True,
                    }
                )
                if len(results) >= limit:
                    return results

        for name, code in kosdaq.items():
            if query_lower in name.lower() or query_upper in code:
                results.append(
                    {
                        "symbol": code,
                        "name": name,
                        "instrument_type": "equity_kr",
                        "exchange": "KOSDAQ",
                        "is_active": True,
                    }
                )
                if len(results) >= limit:
                    return results

    # 2. US Stocks 검색
    if instrument_type is None or instrument_type == "equity_us":
        us_data = get_us_stocks_data()
        symbol_to_exchange = us_data.get("symbol_to_exchange", {})
        symbol_to_name_kr = us_data.get("symbol_to_name_kr", {})
        symbol_to_name_en = us_data.get("symbol_to_name_en", {})

        for symbol, exchange in symbol_to_exchange.items():
            name_kr = symbol_to_name_kr.get(symbol, "")
            name_en = symbol_to_name_en.get(symbol, "")
            if (
                query_upper in symbol.upper()
                or query_lower in name_kr.lower()
                or query_lower in name_en.lower()
            ):
                results.append(
                    {
                        "symbol": symbol,
                        "name": name_kr or name_en or symbol,
                        "instrument_type": "equity_us",
                        "exchange": exchange,
                        "is_active": True,
                    }
                )
                if len(results) >= limit:
                    return results

    # 3. Crypto 검색
    if instrument_type is None or instrument_type == "crypto":
        try:
            crypto_maps = await get_or_refresh_maps()
            name_to_pair = crypto_maps.get("NAME_TO_PAIR_KR", {})
            for name, pair in name_to_pair.items():
                if query_lower in name.lower() or query_upper in pair.upper():
                    results.append(
                        {
                            "symbol": pair,
                            "name": name,
                            "instrument_type": "crypto",
                            "exchange": "Upbit",
                            "is_active": True,
                        }
                    )
                    if len(results) >= limit:
                        return results
        except Exception:
            pass  # crypto 데이터 로드 실패 시 무시

    return results


DEFAULT_KIMCHI_SYMBOLS = ["BTC", "ETH", "XRP", "SOL", "DOGE", "ADA", "AVAX", "DOT"]

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_FUNDING_RATE_URL = "https://fapi.binance.com/fapi/v1/fundingRate"

# exchangerate-api.com (free, no key required)
EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"


async def _fetch_exchange_rate_usd_krw() -> float:
    """Fetch current USD/KRW exchange rate."""
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.get(EXCHANGE_RATE_URL)
        r.raise_for_status()
        data = r.json()
        rate = data["rates"]["KRW"]
        return float(rate)


async def _fetch_binance_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch USDT prices from Binance for given symbols.

    Returns dict like {"BTC": 102000.5, "ETH": 3050.2}.
    """
    pairs = [f"{s}USDT" for s in symbols]
    async with httpx.AsyncClient(timeout=10) as cli:
        # Binance expects compact JSON without spaces for the symbols param
        symbols_json = json.dumps(pairs, separators=(",", ":"))
        r = await cli.get(
            BINANCE_TICKER_URL,
            params={"symbols": symbols_json},
        )
        r.raise_for_status()
        data = r.json()

    result: dict[str, float] = {}
    for item in data:
        pair: str = item["symbol"]  # e.g. "BTCUSDT"
        if pair.endswith("USDT"):
            sym = pair[: -len("USDT")]
            result[sym] = float(item["price"])
    return result


async def _fetch_kimchi_premium(symbols: list[str]) -> dict[str, Any]:
    """Calculate kimchi premium for given crypto symbols.

    Compares Upbit KRW prices with Binance USDT prices * USD/KRW rate.
    """
    upbit_markets = [f"KRW-{s}" for s in symbols]

    # Fetch all three data sources concurrently
    upbit_prices, binance_prices, exchange_rate = await asyncio.gather(
        upbit_service.fetch_multiple_current_prices(upbit_markets),
        _fetch_binance_prices(symbols),
        _fetch_exchange_rate_usd_krw(),
    )

    data: list[dict[str, Any]] = []
    for sym in symbols:
        upbit_key = f"KRW-{sym}"
        upbit_krw = upbit_prices.get(upbit_key)
        binance_usdt = binance_prices.get(sym)

        if upbit_krw is None or binance_usdt is None:
            continue

        binance_krw = binance_usdt * exchange_rate
        premium_pct = round((upbit_krw - binance_krw) / binance_krw * 100, 2)

        data.append(
            {
                "symbol": sym,
                "upbit_krw": upbit_krw,
                "binance_usdt": binance_usdt,
                "binance_krw": round(binance_krw, 0),
                "premium_pct": premium_pct,
            }
        )

    now = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    return {
        "instrument_type": "crypto",
        "source": "upbit+binance",
        "timestamp": now,
        "exchange_rate": exchange_rate,
        "count": len(data),
        "data": data,
    }


# ---------------------------------------------------------------------------
# Market Index Constants & Helpers
# ---------------------------------------------------------------------------

_INDEX_META: dict[str, dict[str, str]] = {
    # Korean indices (naver mobile API)
    "KOSPI": {"name": "코스피", "source": "naver", "naver_code": "KOSPI"},
    "KOSDAQ": {"name": "코스닥", "source": "naver", "naver_code": "KOSDAQ"},
    # US indices (yfinance)
    "SPX": {"name": "S&P 500", "source": "yfinance", "yf_ticker": "^GSPC"},
    "SP500": {"name": "S&P 500", "source": "yfinance", "yf_ticker": "^GSPC"},
    "NASDAQ": {"name": "NASDAQ Composite", "source": "yfinance", "yf_ticker": "^IXIC"},
    "DJI": {"name": "다우존스", "source": "yfinance", "yf_ticker": "^DJI"},
    "DOW": {"name": "다우존스", "source": "yfinance", "yf_ticker": "^DJI"},
}

_DEFAULT_INDICES = ["KOSPI", "KOSDAQ", "SPX", "NASDAQ"]

NAVER_INDEX_BASIC_URL = "https://m.stock.naver.com/api/index/{code}/basic"
NAVER_INDEX_PRICE_URL = "https://m.stock.naver.com/api/index/{code}/price"


def _parse_naver_num(value: Any) -> float | None:
    """Parse a naver number which may be a string with commas."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_naver_int(value: Any) -> int | None:
    """Parse a naver integer which may be a string with commas."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).replace(",", "")))
    except (ValueError, TypeError):
        return None


async def _fetch_index_kr_current(naver_code: str, name: str) -> dict[str, Any]:
    """Fetch current Korean index data from Naver Finance mobile API.

    Combines /basic (realtime close/change) with the latest /price record
    (open/high/low) since the basic endpoint does not include OHLV for indices.
    """
    basic_url = NAVER_INDEX_BASIC_URL.format(code=naver_code)
    price_url = NAVER_INDEX_PRICE_URL.format(code=naver_code)

    async with httpx.AsyncClient(timeout=10) as cli:
        basic_resp, price_resp = await asyncio.gather(
            cli.get(basic_url, headers={"User-Agent": "Mozilla/5.0"}),
            cli.get(
                price_url,
                params={"pageSize": 1, "page": 1},
                headers={"User-Agent": "Mozilla/5.0"},
            ),
        )
        basic_resp.raise_for_status()
        price_resp.raise_for_status()

        basic = basic_resp.json()
        price_list = price_resp.json()

    latest = price_list[0] if price_list else {}

    return {
        "symbol": naver_code,
        "name": name,
        "current": _parse_naver_num(basic.get("closePrice")),
        "change": _parse_naver_num(basic.get("compareToPreviousClosePrice")),
        "change_pct": _parse_naver_num(basic.get("fluctuationsRatio")),
        "open": _parse_naver_num(latest.get("openPrice")),
        "high": _parse_naver_num(latest.get("highPrice")),
        "low": _parse_naver_num(latest.get("lowPrice")),
        "volume": _parse_naver_int(latest.get("accumulatedTradingVolume")),
        "source": "naver",
    }


async def _fetch_index_kr_history(
    naver_code: str, count: int, period: str
) -> list[dict[str, Any]]:
    """Fetch Korean index OHLCV history from Naver Finance mobile API."""
    url = NAVER_INDEX_PRICE_URL.format(code=naver_code)
    period_map = {"day": "day", "week": "week", "month": "month"}
    timeframe = period_map.get(period, "day")

    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.get(
            url,
            params={"pageSize": count, "page": 1, "timeframe": timeframe},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        data = r.json()

    history: list[dict[str, Any]] = []
    for item in data:
        history.append(
            {
                "date": item.get("localTradedAt", ""),
                "close": _parse_naver_num(item.get("closePrice")),
                "open": _parse_naver_num(item.get("openPrice")),
                "high": _parse_naver_num(item.get("highPrice")),
                "low": _parse_naver_num(item.get("lowPrice")),
                "volume": _parse_naver_int(item.get("accumulatedTradingVolume")),
            }
        )
    return history


async def _fetch_index_us_current(
    yf_ticker: str, name: str, symbol: str
) -> dict[str, Any]:
    """Fetch current US index data from yfinance."""
    loop = asyncio.get_running_loop()
    ticker_obj = yf.Ticker(yf_ticker)
    info = await loop.run_in_executor(None, lambda: ticker_obj.fast_info)

    current = getattr(info, "last_price", None)
    previous_close = getattr(info, "regular_market_previous_close", None)

    change: float | None = None
    change_pct: float | None = None
    if current is not None and previous_close is not None and previous_close != 0:
        change = round(current - previous_close, 2)
        change_pct = round((current - previous_close) / previous_close * 100, 2)

    return {
        "symbol": symbol,
        "name": name,
        "current": current,
        "change": change,
        "change_pct": change_pct,
        "open": getattr(info, "open", None),
        "high": getattr(info, "day_high", None),
        "low": getattr(info, "day_low", None),
        "volume": getattr(info, "last_volume", None),
        "source": "yfinance",
    }


async def _fetch_index_us_history(
    yf_ticker: str, count: int, period: str
) -> list[dict[str, Any]]:
    """Fetch US index OHLCV history from yfinance."""
    loop = asyncio.get_running_loop()
    period_map = {"day": "1d", "week": "1wk", "month": "1mo"}
    interval = period_map.get(period, "1d")

    multiplier = {"day": 2, "week": 10, "month": 40}.get(period, 2)
    end = datetime.date.today() + datetime.timedelta(days=1)
    start = end - datetime.timedelta(days=count * multiplier)

    def download() -> pd.DataFrame:
        df = yf.download(
            yf_ticker,
            start=start,
            end=end,
            interval=interval,
            progress=False,
            auto_adjust=False,
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        return df.reset_index(names="date")

    df = await loop.run_in_executor(None, download)

    if df.empty:
        return []

    df = df.tail(count).reset_index(drop=True)

    history: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        d = row.get("date")
        date_str = (
            d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
        )
        history.append(
            {
                "date": date_str,
                "close": float(row["close"]) if pd.notna(row.get("close")) else None,
                "open": float(row["open"]) if pd.notna(row.get("open")) else None,
                "high": float(row["high"]) if pd.notna(row.get("high")) else None,
                "low": float(row["low"]) if pd.notna(row.get("low")) else None,
                "volume": (
                    int(row["volume"]) if pd.notna(row.get("volume")) else None
                ),
            }
        )
    return history


async def _fetch_funding_rate(symbol: str, limit: int) -> dict[str, Any]:
    """Fetch current funding rate and history from Binance Futures API.

    Args:
        symbol: Coin symbol (e.g., "BTC", "ETH") — USDT suffix is appended automatically.
        limit: Number of historical funding rate entries to return.

    Returns:
        Dictionary with current funding rate, next funding time, and history.
    """
    pair = f"{symbol.upper()}USDT"

    async with httpx.AsyncClient(timeout=10) as cli:
        # Fetch current premium index and funding rate history concurrently
        premium_resp, history_resp = await asyncio.gather(
            cli.get(BINANCE_PREMIUM_INDEX_URL, params={"symbol": pair}),
            cli.get(BINANCE_FUNDING_RATE_URL, params={"symbol": pair, "limit": limit}),
        )
        premium_resp.raise_for_status()
        history_resp.raise_for_status()

        premium: dict[str, Any] = premium_resp.json()
        history: list[dict[str, Any]] = history_resp.json()

    # Parse current funding rate
    current_rate = float(premium.get("lastFundingRate", 0))
    next_funding_ts = int(premium.get("nextFundingTime", 0))
    next_funding_time = (
        datetime.datetime.fromtimestamp(
            next_funding_ts / 1000, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        if next_funding_ts
        else None
    )

    # Build history entries
    funding_history: list[dict[str, Any]] = []
    rates_for_avg: list[float] = []
    for entry in history:
        rate = float(entry.get("fundingRate", 0))
        ts = int(entry.get("fundingTime", 0))
        time_str = (
            datetime.datetime.fromtimestamp(
                ts / 1000, tz=datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            if ts
            else None
        )
        funding_history.append({
            "time": time_str,
            "rate": rate,
            "rate_pct": round(rate * 100, 4),
        })
        rates_for_avg.append(rate)

    avg_rate = (
        round(sum(rates_for_avg) / len(rates_for_avg) * 100, 4)
        if rates_for_avg
        else None
    )

    return {
        "symbol": pair,
        "current_funding_rate": current_rate,
        "current_funding_rate_pct": round(current_rate * 100, 4),
        "next_funding_time": next_funding_time,
        "funding_history": funding_history,
        "avg_funding_rate_pct": avg_rate,
        "interpretation": {
            "positive": "롱이 숏에게 지불 (롱 과열 — 시장이 과도하게 강세)",
            "negative": "숏이 롱에게 지불 (숏 과열 — 시장이 과도하게 약세)",
        },
    }


def register_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="search_symbol",
        description="Search symbols by query (symbol or name). Use market to filter: kr/kospi/kosdaq (Korean stocks), us/nasdaq/nyse (US stocks), crypto/upbit (cryptocurrencies).",
    )
    async def search_symbol(
        query: str, limit: int = 20, market: str | None = None
    ) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []

        # market 정규화 (get_quote, get_ohlcv와 동일한 로직)
        instrument_type = _normalize_market(market)

        try:
            capped_limit = min(max(limit, 1), 100)
            return await _search_master_data(query, capped_limit, instrument_type)
        except Exception as exc:
            return [_error_payload(source="master", message=str(exc), query=query)]

    @mcp.tool(
        name="get_quote",
        description="Get latest quote/last price for a symbol (KR equity / US equity / crypto).",
    )
    async def get_quote(symbol: str, market: str | None = None) -> dict[str, Any]:
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        market_type, symbol = _resolve_market_type(symbol, market)

        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]

        try:
            if market_type == "crypto":
                return await _fetch_quote_crypto(symbol)
            elif market_type == "equity_kr":
                return await _fetch_quote_equity_kr(symbol)
            else:  # equity_us
                return await _fetch_quote_equity_us(symbol)
        except Exception as exc:
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=market_type,
            )

    @mcp.tool(
        name="get_holdings",
        description=(
            "Get holdings grouped by account. Supports account filter "
            "(kis/upbit/toss/samsung_pension/isa) and market filter (kr/us/crypto). "
            "Cash balances are excluded. minimum_value filters out low-value positions "
            "when include_current_price=True. Response includes filtered_count, "
            "filter_reason, and per-symbol price lookup errors."
        ),
    )
    async def get_holdings(
        account: str | None = None,
        market: str | None = None,
        include_current_price: bool = True,
        minimum_value: float | None = 1000.0,
    ) -> dict[str, Any]:
        if minimum_value is not None and minimum_value < 0:
            raise ValueError("minimum_value must be >= 0")

        (
            positions,
            errors,
            resolved_market_filter,
            resolved_account_filter,
        ) = await _collect_portfolio_positions(
            account=account,
            market=market,
            include_current_price=include_current_price,
        )

        filtered_count = 0
        filter_reason: str | None = None

        if include_current_price and minimum_value is not None:
            threshold = float(minimum_value)
            filter_reason = f"minimum_value < {_format_filter_threshold(threshold)}"
            filtered_positions: list[dict[str, Any]] = []
            for position in positions:
                value = _value_for_minimum_filter(position)
                if value < threshold:
                    filtered_count += 1
                    continue
                filtered_positions.append(position)
            positions = filtered_positions
        elif not include_current_price:
            filter_reason = "minimum_value filter skipped (include_current_price=False)"
        else:
            filter_reason = "minimum_value filter disabled"

        grouped_accounts: dict[str, dict[str, Any]] = {}
        for position in positions:
            account_id = position["account"]
            grouped = grouped_accounts.setdefault(
                account_id,
                {
                    "account": account_id,
                    "broker": position["broker"],
                    "account_name": position["account_name"],
                    "positions": [],
                },
            )
            grouped["positions"].append(_position_to_output(position))

        accounts = [grouped_accounts[key] for key in sorted(grouped_accounts.keys())]

        return {
            "filters": {
                "account": resolved_account_filter,
                "market": _INSTRUMENT_TO_MARKET.get(resolved_market_filter),
                "include_current_price": include_current_price,
                "minimum_value": minimum_value,
            },
            "filtered_count": filtered_count,
            "filter_reason": filter_reason,
            "total_accounts": len(accounts),
            "total_positions": len(positions),
            "accounts": accounts,
            "errors": errors,
        }

    @mcp.tool(
        name="get_position",
        description=(
            "Check whether a symbol is currently held and return detailed positions "
            "across all accounts. If no position exists, returns status='미보유'."
        ),
    )
    async def get_position(
        symbol: str,
        market: str | None = None,
    ) -> dict[str, Any]:
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        parsed_market = _parse_holdings_market_filter(market)
        if parsed_market == "equity_us":
            query_symbol = _normalize_position_symbol(symbol, "equity_us")
        elif parsed_market == "equity_kr":
            query_symbol = _normalize_position_symbol(symbol, "equity_kr")
        elif parsed_market == "crypto":
            query_symbol = _normalize_position_symbol(symbol, "crypto")
        else:
            query_symbol = symbol.strip().upper()

        positions, errors, _, _ = await _collect_portfolio_positions(
            account=None,
            market=market,
            include_current_price=True,
        )

        matched_positions = [
            position
            for position in positions
            if _is_position_symbol_match(
                position_symbol=position["symbol"],
                query_symbol=query_symbol,
                instrument_type=position["instrument_type"],
            )
        ]

        if not matched_positions:
            return {
                "symbol": query_symbol,
                "market": _INSTRUMENT_TO_MARKET.get(parsed_market),
                "has_position": False,
                "status": "미보유",
                "position_count": 0,
                "positions": [],
                "errors": errors,
            }

        matched_positions.sort(
            key=lambda position: (
                position["account"],
                position["market"],
                position["symbol"],
            )
        )

        return {
            "symbol": query_symbol,
            "market": _INSTRUMENT_TO_MARKET.get(parsed_market),
            "has_position": True,
            "status": "보유",
            "position_count": len(matched_positions),
            "accounts": sorted({position["account"] for position in matched_positions}),
            "positions": [
                {
                    "account": position["account"],
                    "broker": position["broker"],
                    "account_name": position["account_name"],
                    **_position_to_output(position),
                }
                for position in matched_positions
            ],
            "errors": errors,
        }

    @mcp.tool(
        name="get_ohlcv",
        description="Get OHLCV candles for a symbol. Supports daily/weekly/monthly periods and date-based pagination.",
    )
    async def get_ohlcv(
        symbol: str,
        count: int = 100,
        period: str = "day",
        end_date: str | None = None,
        market: str | None = None,
    ) -> dict[str, Any]:
        """Get OHLCV candles.

        Args:
            symbol: Symbol to query (e.g., "005930", "AAPL", "KRW-BTC")
            count: Number of candles to return (max 200 for crypto/kr, 100 for us)
            period: Candle period - "day", "week", or "month"
            end_date: End date for pagination (ISO format: "2024-01-15"). None = latest
            market: Market hint - kr/us/crypto (optional, auto-detected from symbol)
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")
        count = int(count)
        if count <= 0:
            raise ValueError("count must be > 0")

        period = (period or "day").strip().lower()
        if period not in ("day", "week", "month"):
            raise ValueError("period must be 'day', 'week', or 'month'")

        parsed_end_date: datetime.datetime | None = None
        if end_date:
            try:
                parsed_end_date = datetime.datetime.fromisoformat(end_date)
            except ValueError:
                raise ValueError("end_date must be ISO format (e.g., '2024-01-15')")

        market_type, symbol = _resolve_market_type(symbol, market)

        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]

        try:
            if market_type == "crypto":
                return await _fetch_ohlcv_crypto(symbol, count, period, parsed_end_date)
            elif market_type == "equity_kr":
                return await _fetch_ohlcv_equity_kr(
                    symbol, count, period, parsed_end_date
                )
            else:  # equity_us
                return await _fetch_ohlcv_equity_us(
                    symbol, count, period, parsed_end_date
                )
        except Exception as exc:
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=market_type,
            )

    @mcp.tool(
        name="get_volume_profile",
        description="Get volume profile (price-by-volume distribution) for a symbol. Distributes each candle volume across low-high price range and computes POC and 70% value area.",
    )
    async def get_volume_profile(
        symbol: str,
        market: str | None = None,
        period: int = 60,
        bins: int = 20,
    ) -> dict[str, Any]:
        """Get volume profile from daily OHLCV.

        Args:
            symbol: Symbol to query (e.g., "298040", "PLTR", "KRW-BTC")
            market: Market hint - kr/us/crypto (optional, auto-detected from symbol)
            period: Analysis period in days (default: 60)
            bins: Number of price bins (default: 20)

        Returns:
            Dictionary with price range, POC, value area, and per-bin profile
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        period = int(period)
        bins = int(bins)
        if period <= 0:
            raise ValueError("period must be > 0")
        if bins < 2:
            raise ValueError("bins must be >= 2")
        if bins > 200:
            raise ValueError("bins must be <= 200")

        market_type, symbol = _resolve_market_type(symbol, market)

        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]

        try:
            df = await _fetch_ohlcv_for_volume_profile(
                symbol=symbol,
                market_type=market_type,
                period_days=period,
            )
            if df.empty:
                raise ValueError(f"No OHLCV data available for symbol '{symbol}'")

            profile_data = _calculate_volume_profile(
                df, bins=bins, value_area_ratio=0.70
            )
            return {
                "symbol": symbol,
                "period_days": period,
                **profile_data,
            }
        except Exception as exc:
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=market_type,
            )

    @mcp.tool(
        name="get_indicators",
        description="Calculate technical indicators for a symbol. Available indicators: sma (Simple Moving Average), ema (Exponential Moving Average), rsi (Relative Strength Index), macd (MACD), bollinger (Bollinger Bands), atr (Average True Range), pivot (Pivot Points).",
    )
    async def get_indicators(
        symbol: str,
        indicators: list[str],
        market: str | None = None,
    ) -> dict[str, Any]:
        """Calculate technical indicators for a symbol.

        Args:
            symbol: Symbol to query (e.g., "005930", "AAPL", "KRW-BTC")
            indicators: List of indicators to calculate. Options:
                - "sma": Simple Moving Average (periods: 5, 20, 60, 120, 200)
                - "ema": Exponential Moving Average (periods: 5, 20, 60, 120, 200)
                - "rsi": RSI (period: 14)
                - "macd": MACD (fast: 12, slow: 26, signal: 9)
                - "bollinger": Bollinger Bands (period: 20, std: 2)
                - "atr": Average True Range (period: 14)
                - "pivot": Pivot Points (classic formula)
            market: Market hint - kr/us/crypto (optional, auto-detected from symbol)

        Returns:
            Dictionary with symbol, current price, instrument_type, source, and indicators
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if not indicators:
            raise ValueError("indicators list is required and cannot be empty")

        # Validate indicator names
        valid_indicators: set[IndicatorType] = {
            "sma",
            "ema",
            "rsi",
            "macd",
            "bollinger",
            "atr",
            "pivot",
        }
        normalized_indicators: list[IndicatorType] = []
        for ind in indicators:
            ind_lower = ind.lower().strip()
            if ind_lower not in valid_indicators:
                raise ValueError(
                    f"Invalid indicator '{ind}'. Valid options: {', '.join(sorted(valid_indicators))}"
                )
            normalized_indicators.append(ind_lower)  # type: ignore[arg-type]

        market_type, symbol = _resolve_market_type(symbol, market)

        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]

        try:
            # Fetch enough data for long-term indicators (200-day SMA needs 200+ candles)
            df = await _fetch_ohlcv_for_indicators(symbol, market_type, count=250)

            if df.empty:
                raise ValueError(f"No data available for symbol '{symbol}'")

            # Get current price from the latest row
            current_price = (
                float(df["close"].iloc[-1]) if "close" in df.columns else None
            )

            # Compute requested indicators
            indicator_results = _compute_indicators(df, normalized_indicators)

            return {
                "symbol": symbol,
                "price": current_price,
                "instrument_type": market_type,
                "source": source,
                "indicators": indicator_results,
            }

        except Exception as exc:
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=market_type,
            )

    # ---------------------------------------------------------------------------
    # Finnhub Tools (News & Fundamentals)
    # ---------------------------------------------------------------------------

    @mcp.tool(
        name="get_news",
        description="Get recent news for a stock or cryptocurrency. Supports US stocks (Finnhub), Korean stocks (Naver Finance), and crypto (Finnhub).",
    )
    async def get_news(
        symbol: str,
        market: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Get recent news for a symbol.

        Args:
            symbol: Stock symbol (e.g., "AAPL" for US, "005930" for Korean) or "crypto"
            market: Market type - "us", "kr", or "crypto" (auto-detected if not specified)
            limit: Maximum number of news items (default: 10, max: 50)

        Returns:
            Dictionary with news items including title, source, datetime, url
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        # Auto-detect market if not specified
        if market is None:
            if _is_korean_equity_code(symbol):
                market = "kr"
            elif _is_crypto_market(symbol):
                market = "crypto"
            else:
                market = "us"

        # Normalize market type
        normalized_market = market.strip().lower()
        if normalized_market in ("crypto", "upbit", "krw", "usdt"):
            normalized_market = "crypto"
        elif normalized_market in (
            "kr",
            "krx",
            "korea",
            "kospi",
            "kosdaq",
            "kis",
            "equity_kr",
            "naver",
        ):
            normalized_market = "kr"
        elif normalized_market in ("us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"):
            normalized_market = "us"
        else:
            raise ValueError("market must be 'us', 'kr', or 'crypto'")

        capped_limit = min(max(limit, 1), 50)

        try:
            if normalized_market == "kr":
                return await _fetch_news_naver(symbol, capped_limit)
            else:
                return await _fetch_news_finnhub(
                    symbol, normalized_market, capped_limit
                )
        except Exception as exc:
            source = "naver" if normalized_market == "kr" else "finnhub"
            instrument_type = {
                "kr": "equity_kr",
                "us": "equity_us",
                "crypto": "crypto",
            }.get(normalized_market, "equity_us")
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

    @mcp.tool(
        name="get_company_profile",
        description="Get company profile for a US or Korean stock. Returns name, sector, industry, market cap, and financial ratios.",
    )
    async def get_company_profile(
        symbol: str, market: str | None = None
    ) -> dict[str, Any]:
        """Get company profile for a stock.

        Args:
            symbol: Stock symbol (e.g., "AAPL" for US, "005930" for Korean)
            market: Market type - "us" or "kr" (auto-detected if not specified)

        Returns:
            Dictionary with company profile including name, sector, market_cap
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        # Crypto not supported
        if _is_crypto_market(symbol):
            raise ValueError("Company profile is not available for cryptocurrencies")

        # Auto-detect market if not specified
        if market is None:
            if _is_korean_equity_code(symbol):
                market = "kr"
            else:
                market = "us"

        # Normalize market type
        normalized_market = market.strip().lower()
        if normalized_market in (
            "kr",
            "krx",
            "korea",
            "kospi",
            "kosdaq",
            "kis",
            "equity_kr",
            "naver",
        ):
            normalized_market = "kr"
        elif normalized_market in ("us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"):
            normalized_market = "us"
        else:
            raise ValueError("market must be 'us' or 'kr'")

        try:
            if normalized_market == "kr":
                return await _fetch_company_profile_naver(symbol)
            else:
                return await _fetch_company_profile_finnhub(symbol)
        except Exception as exc:
            source = "naver" if normalized_market == "kr" else "finnhub"
            instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

    @mcp.tool(
        name="get_financials",
        description="Get financial statements for a US or Korean stock. Supports income statement, balance sheet, and cash flow.",
    )
    async def get_financials(
        symbol: str,
        statement: str = "income",
        freq: str = "annual",
        market: str | None = None,
    ) -> dict[str, Any]:
        """Get financial statements for a stock.

        Args:
            symbol: Stock symbol (e.g., "AAPL" for US, "005930" for Korean)
            statement: Statement type - "income", "balance", or "cashflow" (default: "income")
            freq: Frequency - "annual" or "quarterly" (default: "annual")
            market: Market type - "us" or "kr" (auto-detected if not specified)

        Returns:
            Dictionary with financial statement data
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        statement = (statement or "income").strip().lower()
        if statement not in ("income", "balance", "cashflow"):
            raise ValueError("statement must be 'income', 'balance', or 'cashflow'")

        freq = (freq or "annual").strip().lower()
        if freq not in ("annual", "quarterly"):
            raise ValueError("freq must be 'annual' or 'quarterly'")

        # Crypto not supported
        if _is_crypto_market(symbol):
            raise ValueError(
                "Financial statements are not available for cryptocurrencies"
            )

        # Auto-detect market if not specified
        if market is None:
            if _is_korean_equity_code(symbol):
                market = "kr"
            else:
                market = "us"

        # Normalize market type
        normalized_market = market.strip().lower()
        if normalized_market in (
            "kr",
            "krx",
            "korea",
            "kospi",
            "kosdaq",
            "kis",
            "equity_kr",
            "naver",
        ):
            normalized_market = "kr"
        elif normalized_market in ("us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"):
            normalized_market = "us"
        else:
            raise ValueError("market must be 'us' or 'kr'")

        try:
            if normalized_market == "kr":
                return await _fetch_financials_naver(symbol, statement, freq)
            else:
                return await _fetch_financials_finnhub(symbol, statement, freq)
        except Exception as exc:
            source = "naver" if normalized_market == "kr" else "finnhub"
            instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

    @mcp.tool(
        name="get_insider_transactions",
        description="Get insider transactions for a US stock. Returns name, transaction type, shares, price, date. US stocks only.",
    )
    async def get_insider_transactions(
        symbol: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Get insider transactions for a US stock.

        Args:
            symbol: US stock symbol (e.g., "AAPL")
            limit: Maximum number of transactions (default: 20, max: 100)

        Returns:
            Dictionary with insider transaction data
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        capped_limit = min(max(limit, 1), 100)

        # Validate this is a US equity symbol
        if _is_crypto_market(symbol):
            raise ValueError("Insider transactions are only available for US stocks")
        if _is_korean_equity_code(symbol):
            raise ValueError("Insider transactions are only available for US stocks")

        try:
            return await _fetch_insider_transactions_finnhub(symbol, capped_limit)
        except Exception as exc:
            return _error_payload(
                source="finnhub",
                message=str(exc),
                symbol=symbol,
                instrument_type="equity_us",
            )

    @mcp.tool(
        name="get_earnings_calendar",
        description="Get earnings calendar for a US stock or date range. Returns earnings dates, EPS estimates and actuals. US stocks only.",
    )
    async def get_earnings_calendar(
        symbol: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get earnings calendar.

        Args:
            symbol: US stock symbol (optional, e.g., "AAPL"). If not provided, returns all earnings in date range.
            from_date: Start date in ISO format (optional, default: today)
            to_date: End date in ISO format (optional, default: 30 days from now)

        Returns:
            Dictionary with earnings calendar including dates, EPS estimates and actuals
        """
        symbol = (symbol or "").strip() if symbol else None

        # Validate symbol if provided
        if symbol:
            if _is_crypto_market(symbol):
                raise ValueError("Earnings calendar is only available for US stocks")
            if _is_korean_equity_code(symbol):
                raise ValueError("Earnings calendar is only available for US stocks")

        # Validate date formats if provided
        if from_date:
            try:
                datetime.date.fromisoformat(from_date)
            except ValueError:
                raise ValueError("from_date must be ISO format (e.g., '2024-01-15')")

        if to_date:
            try:
                datetime.date.fromisoformat(to_date)
            except ValueError:
                raise ValueError("to_date must be ISO format (e.g., '2024-01-15')")

        try:
            return await _fetch_earnings_calendar_finnhub(symbol, from_date, to_date)
        except Exception as exc:
            return _error_payload(
                source="finnhub",
                message=str(exc),
                symbol=symbol,
                instrument_type="equity_us",
            )

    # ---------------------------------------------------------------------------
    # Naver Finance Tools (Korean Stocks Only)
    # ---------------------------------------------------------------------------

    @mcp.tool(
        name="get_investor_trends",
        description="Get foreign and institutional investor trading trends for a Korean stock. Returns daily net buy/sell data. Korean stocks only.",
    )
    async def get_investor_trends(
        symbol: str,
        days: int = 20,
    ) -> dict[str, Any]:
        """Get investor trading trends for a Korean stock.

        Args:
            symbol: Korean stock code (6 digits, e.g., "005930")
            days: Number of days of data (default: 20, max: 60)

        Returns:
            Daily investor flow data including foreign, institutional net trades
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if not _is_korean_equity_code(symbol):
            raise ValueError(
                "Investor trends are only available for Korean stocks "
                "(6-digit codes like '005930')"
            )

        capped_days = min(max(days, 1), 60)

        try:
            return await _fetch_investor_trends_naver(symbol, capped_days)
        except Exception as exc:
            return _error_payload(
                source="naver",
                message=str(exc),
                symbol=symbol,
                instrument_type="equity_kr",
            )

    @mcp.tool(
        name="get_investment_opinions",
        description="Get securities firm investment opinions and target prices for a US or Korean stock. Returns analyst ratings, price targets, and upside potential.",
    )
    async def get_investment_opinions(
        symbol: str,
        limit: int = 10,
        market: str | None = None,
    ) -> dict[str, Any]:
        """Get investment opinions for a stock.

        Args:
            symbol: Stock symbol (e.g., "AAPL" for US, "005930" for Korean)
            limit: Maximum number of opinions (default: 10, max: 30)
            market: Market type - "us" or "kr" (auto-detected if not specified)

        Returns:
            Investment opinions including firm name, target price, rating, date
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if _is_crypto_market(symbol):
            raise ValueError(
                "Investment opinions are not available for cryptocurrencies"
            )

        # Auto-detect market if not specified
        if market is None:
            if _is_korean_equity_code(symbol):
                market = "kr"
            else:
                market = "us"

        normalized_market = market.strip().lower()
        if normalized_market in (
            "kr", "krx", "korea", "kospi", "kosdaq", "kis", "equity_kr", "naver",
        ):
            normalized_market = "kr"
        elif normalized_market in ("us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"):
            normalized_market = "us"
        else:
            raise ValueError("market must be 'us' or 'kr'")

        capped_limit = min(max(limit, 1), 30)

        try:
            if normalized_market == "kr":
                return await _fetch_investment_opinions_naver(symbol, capped_limit)
            else:
                return await _fetch_investment_opinions_yfinance(symbol, capped_limit)
        except Exception as exc:
            source = "naver" if normalized_market == "kr" else "yfinance"
            instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

    @mcp.tool(
        name="get_valuation",
        description="Get valuation metrics for a US or Korean stock. Returns PER, PBR, ROE, dividend yield, 52-week high/low, current price, and position within 52-week range.",
    )
    async def get_valuation(
        symbol: str, market: str | None = None
    ) -> dict[str, Any]:
        """Get valuation metrics for a stock.

        Args:
            symbol: Stock symbol (e.g., "AAPL" for US, "005930" for Korean)
            market: Market type - "us" or "kr" (auto-detected if not specified)

        Returns:
            Dictionary with valuation metrics:
            - symbol: Stock code
            - name: Company name
            - current_price: Current stock price
            - per: Price-to-Earnings Ratio
            - pbr: Price-to-Book Ratio
            - roe: Return on Equity (%)
            - dividend_yield: Dividend yield (as decimal, e.g., 0.02 for 2%)
            - high_52w: 52-week high price
            - low_52w: 52-week low price
            - current_position_52w: Position within 52-week range (0=low, 1=high)
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if _is_crypto_market(symbol):
            raise ValueError("Valuation metrics are not available for cryptocurrencies")

        # Auto-detect market if not specified
        if market is None:
            if _is_korean_equity_code(symbol):
                market = "kr"
            else:
                market = "us"

        normalized_market = market.strip().lower()
        if normalized_market in (
            "kr", "krx", "korea", "kospi", "kosdaq", "kis", "equity_kr", "naver",
        ):
            normalized_market = "kr"
        elif normalized_market in ("us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"):
            normalized_market = "us"
        else:
            raise ValueError("market must be 'us' or 'kr'")

        try:
            if normalized_market == "kr":
                return await _fetch_valuation_naver(symbol)
            else:
                return await _fetch_valuation_yfinance(symbol)
        except Exception as exc:
            source = "naver" if normalized_market == "kr" else "yfinance"
            instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

    @mcp.tool(
        name="get_short_interest",
        description="Get short selling data for a Korean stock. Returns daily short selling volume, amount, ratio, and balance. Korean stocks only.",
    )
    async def get_short_interest(
        symbol: str,
        days: int = 20,
    ) -> dict[str, Any]:
        """Get short selling data for a Korean stock.

        Args:
            symbol: Korean stock code (6 digits, e.g., "005930" for Samsung Electronics)
            days: Number of days of data to fetch (default: 20, max: 60)

        Returns:
            Dictionary with short selling data:
            - symbol: Stock code
            - name: Company name
            - short_data: List of daily short selling data
                - date: Trading date (ISO format)
                - short_volume: Short selling volume (shares, if available)
                - short_amount: Short selling amount (KRW)
                - short_ratio: Short selling ratio (%)
                - total_volume: Total trading volume (shares, if available)
                - total_amount: Total trading amount (KRW)
            - avg_short_ratio: Average short ratio over the period
            - short_balance: Short balance data (if available)
                - balance_shares: Outstanding short shares
                - balance_amount: Outstanding short amount (KRW)
                - balance_ratio: Balance ratio (%)
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if not _is_korean_equity_code(symbol):
            raise ValueError(
                "Short selling data is only available for Korean stocks "
                "(6-digit codes like '005930')"
            )

        capped_days = min(max(days, 1), 60)

        try:
            return await naver_finance.fetch_short_interest(symbol, capped_days)
        except Exception as exc:
            return _error_payload(
                source="krx",
                message=str(exc),
                symbol=symbol,
                instrument_type="equity_kr",
            )

    @mcp.tool(
        name="get_kimchi_premium",
        description="Get kimchi premium (김치 프리미엄) for cryptocurrencies. Compares Upbit KRW prices with Binance USDT prices to calculate the Korean exchange premium percentage.",
    )
    async def get_kimchi_premium(
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Get kimchi premium for cryptocurrencies.

        Args:
            symbol: Coin symbol (e.g., "BTC", "ETH"). If not specified,
                     returns data for major coins (BTC, ETH, XRP, SOL, etc.)

        Returns:
            Dictionary with kimchi premium data including exchange rate,
            Upbit/Binance prices, and premium percentage for each coin.
        """
        if symbol:
            sym = symbol.strip().upper()
            # Strip KRW- or USDT- prefix if provided
            if sym.startswith("KRW-"):
                sym = sym[4:]
            elif sym.startswith("USDT-"):
                sym = sym[5:]
            symbols = [sym]
        else:
            symbols = list(DEFAULT_KIMCHI_SYMBOLS)

        try:
            return await _fetch_kimchi_premium(symbols)
        except Exception as exc:
            return _error_payload(
                source="upbit+binance",
                message=str(exc),
                instrument_type="crypto",
            )

    @mcp.tool(
        name="get_funding_rate",
        description="Get futures funding rate for a cryptocurrency from Binance. Returns current funding rate, next funding time, historical rates, and interpretation. Positive = longs pay shorts (long overheated), Negative = shorts pay longs (short overheated).",
    )
    async def get_funding_rate(
        symbol: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Get futures funding rate for a cryptocurrency.

        Args:
            symbol: Coin symbol (e.g., "BTC", "ETH"). KRW-/USDT- prefix is stripped automatically.
            limit: Number of historical funding rate entries (default: 10, max: 100)

        Returns:
            Dictionary with current funding rate, next funding time, history, and interpretation.
        """
        symbol = (symbol or "").strip().upper()
        if not symbol:
            raise ValueError("symbol is required")

        # Strip common prefixes
        if symbol.startswith("KRW-"):
            symbol = symbol[4:]
        elif symbol.startswith("USDT-"):
            symbol = symbol[5:]
        # Strip USDT suffix if user passed e.g. "BTCUSDT"
        if symbol.endswith("USDT"):
            symbol = symbol[: -len("USDT")]

        capped_limit = min(max(limit, 1), 100)

        try:
            return await _fetch_funding_rate(symbol, capped_limit)
        except Exception as exc:
            return _error_payload(
                source="binance",
                message=str(exc),
                symbol=f"{symbol}USDT",
                instrument_type="crypto",
            )

    @mcp.tool(
        name="get_market_index",
        description="Get market index data. Supports KOSPI, KOSDAQ (Naver Finance) and SPX/SP500, NASDAQ, DJI/DOW (yfinance). Without symbol returns current data for all major indices. With symbol returns current data + OHLCV history.",
    )
    async def get_market_index(
        symbol: str | None = None,
        period: str = "day",
        count: int = 20,
    ) -> dict[str, Any]:
        """Get market index data.

        Args:
            symbol: Index symbol (e.g., "KOSPI", "KOSDAQ", "SPX", "NASDAQ", "DJI").
                    If not specified, returns current data for major indices.
            period: OHLCV period - "day" (default), "week", "month"
            count: Number of OHLCV history records (default: 20, max: 100)

        Returns:
            Dictionary with indices (current data) and optionally history (OHLCV)
        """
        period = (period or "day").strip().lower()
        if period not in ("day", "week", "month"):
            raise ValueError("period must be 'day', 'week', or 'month'")

        capped_count = min(max(count, 1), 100)

        if symbol:
            sym = symbol.strip().upper()
            meta = _INDEX_META.get(sym)
            if meta is None:
                valid = sorted({k for k in _INDEX_META})
                raise ValueError(
                    f"Unknown index symbol '{sym}'. Supported: {', '.join(valid)}"
                )

            try:
                if meta["source"] == "naver":
                    current_data, history = await asyncio.gather(
                        _fetch_index_kr_current(meta["naver_code"], meta["name"]),
                        _fetch_index_kr_history(
                            meta["naver_code"], capped_count, period
                        ),
                    )
                else:
                    current_data, history = await asyncio.gather(
                        _fetch_index_us_current(
                            meta["yf_ticker"], meta["name"], sym
                        ),
                        _fetch_index_us_history(
                            meta["yf_ticker"], capped_count, period
                        ),
                    )
                return {"indices": [current_data], "history": history}
            except Exception as exc:
                return _error_payload(
                    source=meta["source"], message=str(exc), symbol=sym
                )
        else:
            tasks = []
            for idx_sym in _DEFAULT_INDICES:
                meta = _INDEX_META[idx_sym]
                if meta["source"] == "naver":
                    tasks.append(
                        _fetch_index_kr_current(meta["naver_code"], meta["name"])
                    )
                else:
                    tasks.append(
                        _fetch_index_us_current(
                            meta["yf_ticker"], meta["name"], idx_sym
                        )
                    )

            results = await asyncio.gather(*tasks, return_exceptions=True)

            indices: list[dict[str, Any]] = []
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    indices.append(
                        {"symbol": _DEFAULT_INDICES[i], "error": str(r)}
                    )
                else:
                    indices.append(r)

            return {"indices": indices}

    @mcp.tool(
        name="get_fibonacci",
        description="Calculate Fibonacci retracement levels for a symbol. Automatically detects swing high/low within the period and computes 0%, 23.6%, 38.2%, 50%, 61.8%, 78.6%, 100% levels with nearest support/resistance relative to the current price.",
    )
    async def get_fibonacci(
        symbol: str,
        market: str | None = None,
        period: int = 60,
    ) -> dict[str, Any]:
        """Calculate Fibonacci retracement levels.

        Args:
            symbol: Symbol to query (e.g., "005930", "AAPL", "KRW-BTC")
            market: Market hint - kr/us/crypto (optional, auto-detected from symbol)
            period: Number of days to search for swing high/low (default: 60)

        Returns:
            Dictionary with swing high/low, trend, Fibonacci levels,
            nearest support and resistance relative to current price.
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        period = int(period)
        if period <= 0:
            raise ValueError("period must be > 0")

        market_type, symbol = _resolve_market_type(symbol, market)

        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]

        try:
            df = await _fetch_ohlcv_for_indicators(
                symbol, market_type, count=period
            )

            if df.empty:
                raise ValueError(f"No data available for symbol '{symbol}'")

            for col in ("high", "low", "close"):
                if col not in df.columns:
                    raise ValueError(f"Missing required column: {col}")

            current_price = round(float(df["close"].iloc[-1]), 2)
            fib = _calculate_fibonacci(df, current_price)
            fib["symbol"] = symbol

            return fib

        except Exception as exc:
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=market_type,
            )

    # ------------------------------------------------------------------
    # get_sector_peers
    # ------------------------------------------------------------------

    @mcp.tool(
        name="get_sector_peers",
        description=(
            "Get sector peer stocks for comparison. "
            "Returns peer stocks in the same sector/industry with current price, "
            "PER, PBR, market cap, and comparison metrics (avg PER/PBR, ranking). "
            "Supports Korean stocks (via Naver Finance) and US stocks (via Finnhub + yfinance). "
            "Not available for cryptocurrencies."
        ),
    )
    async def get_sector_peers(
        symbol: str,
        market: str = "",
        limit: int = 5,
    ) -> dict[str, Any]:
        """Get sector peer stocks for a stock.

        Args:
            symbol: Stock symbol (e.g., "005930" for Korean, "AAPL" for US)
            market: Market hint - "kr" or "us" (auto-detected if empty)
            limit: Number of peer stocks to return (default: 5, max: 20)

        Returns:
            Dictionary with target stock info, peer stocks list, and
            comparison metrics (avg_per, avg_pbr, rankings).

        Examples:
            get_sector_peers("298040")            # 효성중공업 (Korean)
            get_sector_peers("AAPL")              # Apple (US)
            get_sector_peers("005930", limit=10)  # 삼성전자 with 10 peers
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if _is_crypto_market(symbol):
            raise ValueError(
                "Sector peers are not available for cryptocurrencies"
            )

        capped_limit = min(max(limit, 1), 20)

        # Determine market
        market_str = (market or "").strip().lower()
        if market_str in (
            "kr", "krx", "korea", "kospi", "kosdaq", "kis", "naver",
        ):
            resolved_market = "kr"
        elif market_str in ("us", "usa", "nyse", "nasdaq", "yahoo"):
            resolved_market = "us"
        elif market_str == "":
            # Auto-detect
            if _is_korean_equity_code(symbol):
                resolved_market = "kr"
            elif _is_us_equity_symbol(symbol):
                resolved_market = "us"
            else:
                raise ValueError(
                    f"Cannot auto-detect market for symbol '{symbol}'. "
                    "Please specify market='kr' or market='us'."
                )
        else:
            raise ValueError("market must be 'kr' or 'us'")

        try:
            if resolved_market == "kr":
                return await _fetch_sector_peers_naver(symbol, capped_limit)
            else:
                return await _fetch_sector_peers_us(symbol, capped_limit)
        except Exception as exc:
            source = "naver" if resolved_market == "kr" else "finnhub+yfinance"
            instrument_type = (
                "equity_kr" if resolved_market == "kr" else "equity_us"
            )
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

    # ------------------------------------------------------------------
    # simulate_avg_cost
    # ------------------------------------------------------------------

    @mcp.tool(
        name="simulate_avg_cost",
        description=(
            "Simulate dollar-cost averaging / adding to a position. "
            "Given current holdings and a list of additional buy plans, "
            "calculates the new average cost, breakeven change %, and "
            "unrealised P&L at each step.  Optionally computes return "
            "at a target price.  Works for any asset (stocks, crypto, etc.)."
        ),
    )
    async def simulate_avg_cost(
        holdings: dict[str, float],
        plans: list[dict[str, float]],
        current_market_price: float | None = None,
        target_price: float | None = None,
    ) -> dict[str, Any]:
        """Simulate averaging-down / dollar-cost averaging.

        Args:
            holdings: Current position - {"price": buy_price, "quantity": qty}
            plans: List of planned buys - [{"price": ..., "quantity": ...}, ...]
            current_market_price: Current market price (optional).
                Used to calculate unrealised P&L and breakeven change %.
                If omitted, those fields are null.
            target_price: Target sell price (optional).
                If given, calculates projected return at that price.

        Returns:
            Dictionary with current_position, steps, and optional target_analysis.
        """
        # --- validate holdings ---
        h_price = holdings.get("price")
        h_qty = holdings.get("quantity")
        if h_price is None or h_qty is None:
            raise ValueError(
                "holdings must contain 'price' and 'quantity'"
            )
        h_price = float(h_price)
        h_qty = float(h_qty)
        if h_price <= 0 or h_qty <= 0:
            raise ValueError("holdings price and quantity must be > 0")

        # --- validate plans ---
        if not plans:
            raise ValueError("plans must contain at least one entry")
        validated_plans: list[tuple[float, float]] = []
        for i, p in enumerate(plans):
            pp = p.get("price")
            pq = p.get("quantity")
            if pp is None or pq is None:
                raise ValueError(
                    f"plans[{i}] must contain 'price' and 'quantity'"
                )
            pp, pq = float(pp), float(pq)
            if pp <= 0 or pq <= 0:
                raise ValueError(
                    f"plans[{i}] price and quantity must be > 0"
                )
            validated_plans.append((pp, pq))

        mkt = float(current_market_price) if current_market_price is not None else None

        # --- current position ---
        total_qty = h_qty
        total_invested = round(h_price * h_qty, 2)
        avg_price = round(total_invested / total_qty, 2)

        current_position: dict[str, Any] = {
            "avg_price": avg_price,
            "total_quantity": total_qty,
            "total_invested": total_invested,
        }
        if mkt is not None:
            pnl = round((mkt - avg_price) * total_qty, 2)
            pnl_pct = round((mkt / avg_price - 1) * 100, 2)
            current_position["unrealized_pnl"] = pnl
            current_position["unrealized_pnl_pct"] = pnl_pct

        # --- steps ---
        steps: list[dict[str, Any]] = []
        for idx, (bp, bq) in enumerate(validated_plans, start=1):
            total_invested = round(total_invested + bp * bq, 2)
            total_qty = round(total_qty + bq, 10)
            avg_price = round(total_invested / total_qty, 2)

            step: dict[str, Any] = {
                "step": idx,
                "buy_price": bp,
                "buy_quantity": bq,
                "new_avg_price": avg_price,
                "total_quantity": total_qty,
                "total_invested": total_invested,
            }
            if mkt is not None:
                breakeven_pct = round((avg_price / mkt - 1) * 100, 2)
                pnl = round((mkt - avg_price) * total_qty, 2)
                pnl_pct = round((mkt / avg_price - 1) * 100, 2)
                step["breakeven_change_pct"] = breakeven_pct
                step["unrealized_pnl"] = pnl
                step["unrealized_pnl_pct"] = pnl_pct

            steps.append(step)

        # --- target analysis ---
        result: dict[str, Any] = {
            "current_position": current_position,
            "steps": steps,
        }
        if mkt is not None:
            result["current_market_price"] = mkt

        if target_price is not None:
            tp = float(target_price)
            if tp <= 0:
                raise ValueError("target_price must be > 0")
            profit_per_unit = round(tp - avg_price, 2)
            total_profit = round(profit_per_unit * total_qty, 2)
            total_return_pct = round((tp / avg_price - 1) * 100, 2)
            result["target_analysis"] = {
                "target_price": tp,
                "final_avg_price": avg_price,
                "profit_per_unit": profit_per_unit,
                "total_profit": total_profit,
                "total_return_pct": total_return_pct,
            }

        return result
