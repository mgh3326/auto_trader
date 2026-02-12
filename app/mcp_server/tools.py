from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import re
import time
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
from app.mcp_server.tick_size import adjust_tick_size_kr
from app.models.dca_plan import (
    DcaPlan,
    DcaPlanStep,
    DcaStepStatus,
)
from app.models.manual_holdings import MarketType
from app.services import naver_finance
from app.services import upbit as upbit_service
from app.services import yahoo as yahoo_service
from app.services.dca_service import DcaService
from app.services.disclosures.dart import list_filings
from app.services.kis import KISClient
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.screenshot_holdings_service import ScreenshotHoldingsService
from data.coins_info import get_or_refresh_maps
from data.stocks_info import (
    get_kosdaq_name_to_code,
    get_kospi_name_to_code,
    get_us_stocks_data,
)
from data.stocks_info.overseas_us_stocks import get_exchange_by_symbol

logger = logging.getLogger(__name__)


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
        symbol = symbol.upper()
        if _is_crypto_market(symbol):
            raise ValueError("us equity symbols must not include KRW-/USDT- prefix")
        if not _is_us_equity_symbol(symbol):
            raise ValueError("invalid US equity symbol")
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
_MCP_DCA_USER_ID = _env_int("MCP_DCA_USER_ID", _MCP_USER_ID)
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
_DEFAULT_MINIMUM_VALUES: dict[str, float] = {
    "equity_kr": 5000.0,
    "equity_us": 10.0,
    "crypto": 5000.0,
}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _to_optional_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _to_optional_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
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
    output = {
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
    if "price_error" in position:
        output["price_error"] = position["price_error"]
    return output


def _value_for_minimum_filter(position: dict[str, Any]) -> float:
    evaluation_amount = position.get("evaluation_amount")
    if evaluation_amount is not None:
        return _to_float(evaluation_amount, default=0.0)

    # Price lookup failed (current_price is None) -> return infinity to prevent filtering
    if position.get("current_price") is None:
        return float("inf")

    quantity = _to_float(position.get("quantity"))
    current_price = _to_float(position.get("current_price"))
    return quantity * current_price


def _format_filter_threshold(value: float) -> str:
    return f"{value:g}"


def _build_holdings_summary(
    positions: list[dict[str, Any]], include_current_price: bool
) -> dict[str, Any]:
    total_buy_amount = round(
        sum(
            _to_float(position.get("avg_buy_price"))
            * _to_float(position.get("quantity"))
            for position in positions
        ),
        2,
    )

    if not include_current_price:
        return {
            "total_buy_amount": total_buy_amount,
            "total_evaluation": None,
            "total_profit_loss": None,
            "total_profit_rate": None,
            "position_count": len(positions),
            "weights": None,
        }

    total_evaluation = round(
        sum(_to_float(position.get("evaluation_amount")) for position in positions),
        2,
    )
    total_profit_loss = round(
        sum(_to_float(position.get("profit_loss")) for position in positions),
        2,
    )
    total_profit_rate = (
        round((total_profit_loss / total_buy_amount) * 100, 2)
        if total_buy_amount > 0
        else None
    )

    weights: list[dict[str, Any]] = []
    if total_evaluation > 0:
        for position in positions:
            evaluation = _to_float(position.get("evaluation_amount"))
            if evaluation <= 0:
                continue
            weights.append(
                {
                    "symbol": position.get("symbol"),
                    "name": position.get("name"),
                    "weight_pct": round((evaluation / total_evaluation) * 100, 2),
                }
            )
        weights.sort(key=lambda item: _to_float(item.get("weight_pct")), reverse=True)

    return {
        "total_buy_amount": total_buy_amount,
        "total_evaluation": total_evaluation,
        "total_profit_loss": total_profit_loss,
        "total_profit_rate": total_profit_rate,
        "position_count": len(positions),
        "weights": weights,
    }


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
        return (
            to_db_symbol(position_symbol).upper() == to_db_symbol(query_symbol).upper()
        )

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

            broker = holding.broker_account.broker_type
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
) -> tuple[
    dict[tuple[str, str], float], list[dict[str, Any]], dict[tuple[str, str], str]
]:
    price_map: dict[tuple[str, str], float] = {}
    price_errors: list[dict[str, Any]] = []
    error_map: dict[tuple[str, str], str] = {}

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
                prices = await upbit_service.fetch_multiple_current_prices(
                    batch_symbols
                )
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
                    error_msg = "price missing in batch ticker response"
                    error_map[("crypto", symbol.upper())] = error_msg
                    price_errors.append(
                        {
                            "source": "upbit",
                            "market": "crypto",
                            "symbol": symbol,
                            "stage": "current_price",
                            "error": error_msg,
                        }
                    )
            except Exception as exc:
                for symbol in batch_symbols:
                    if ("crypto", symbol.upper()) in price_map:
                        continue
                    error_msg = str(exc)
                    error_map[("crypto", symbol.upper())] = error_msg
                    price_errors.append(
                        {
                            "source": "upbit",
                            "market": "crypto",
                            "symbol": symbol,
                            "stage": "current_price",
                            "error": error_msg,
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
    ) -> tuple[str, str, float | None, str | None]:
        try:
            if instrument_type == "equity_kr":
                quote = await _fetch_quote_equity_kr(symbol)
            else:
                quote = await _fetch_quote_equity_us(symbol)
            price = quote.get("price")
            return (
                instrument_type,
                symbol,
                float(price) if price is not None else None,
                None,
            )
        except Exception as exc:
            error_msg = str(exc)
            logger.debug(f"Failed to fetch equity price for {symbol}: {error_msg}")
            return instrument_type, symbol, None, error_msg

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
        for instrument_type, symbol, price, error in results:
            if price is not None:
                price_map[(instrument_type, symbol)] = price
            elif error is not None:
                error_map[(instrument_type, symbol)] = error
                price_errors.append(
                    {
                        "source": "yahoo" if instrument_type == "equity_us" else "kis",
                        "market": "us" if instrument_type == "equity_us" else "kr",
                        "symbol": symbol,
                        "stage": "current_price",
                        "error": error,
                    }
                )

    return price_map, price_errors, error_map


async def _collect_portfolio_positions(
    *,
    account: str | None,
    market: str | None,
    include_current_price: bool,
    account_name: str | None = None,
    user_id: int = _MCP_USER_ID,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None, str | None]:
    market_filter = _parse_holdings_market_filter(market)
    account_filter = _normalize_account_filter(account)

    tasks: list[asyncio.Future[Any] | asyncio.Task[Any] | Any] = []
    if market_filter != "crypto":
        tasks.append(_collect_kis_positions(market_filter))
    if market_filter in (None, "crypto"):
        tasks.append(_collect_upbit_positions(market_filter))
    tasks.append(
        _collect_manual_positions(user_id=user_id, market_filter=market_filter)
    )

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

    if account_name:
        account_name_filter = account_name.strip().lower()
        positions = [
            position
            for position in positions
            if account_name_filter in str(position.get("account_name", "")).lower()
        ]

    if include_current_price and positions:
        price_map, price_errors, error_map = await _fetch_price_map_for_positions(
            positions
        )
        errors.extend(price_errors)
        for position in positions:
            key = (position["instrument_type"], position["symbol"])
            price = price_map.get(key)
            if price is not None:
                position["current_price"] = price
                _recalculate_profit_fields(position)
            else:
                error = error_map.get(key)
                if error is not None:
                    position["price_error"] = error
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

    if df.empty:
        return {
            "symbol": symbol,
            "instrument_type": "crypto",
            "source": "upbit",
            "period": period,
            "count": 0,
            "rows": [],
            "message": f"No candle data available for {symbol}",
        }

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


def _calculate_fibonacci(df: pd.DataFrame, current_price: float) -> dict[str, Any]:
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
            str(lvl): round(
                swing_high_price - lvl * (swing_high_price - swing_low_price), 2
            )
            for lvl in FIBONACCI_LEVELS
        }
    else:
        trend = "bounce_from_low"
        # Levels go from low (0%) up to high (100%)
        levels = {
            str(lvl): round(
                swing_low_price + lvl * (swing_high_price - swing_low_price), 2
            )
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


def _format_fibonacci_source(level_key: str) -> str:
    level = _to_optional_float(level_key)
    if level is None:
        return f"fib_{level_key}"

    pct = level * 100
    if abs(pct - round(pct)) < 1e-9:
        pct_str = str(int(round(pct)))
    else:
        pct_str = f"{pct:.1f}".rstrip("0").rstrip(".")

    return f"fib_{pct_str}"


def _cluster_price_levels(
    levels: list[tuple[float, str]],
    tolerance_pct: float = 0.02,
) -> list[dict[str, Any]]:
    if not levels:
        return []

    clusters: list[dict[str, Any]] = []
    for price, source in sorted(levels, key=lambda item: item[0]):
        if price <= 0:
            continue

        matched_cluster: dict[str, Any] | None = None
        for cluster in clusters:
            center = _to_float(cluster.get("center"), default=0.0)
            if center <= 0:
                continue
            if abs(price - center) / center <= tolerance_pct:
                matched_cluster = cluster
                break

        if matched_cluster is None:
            clusters.append(
                {
                    "prices": [price],
                    "sources": [source],
                    "center": price,
                }
            )
            continue

        prices = matched_cluster["prices"]
        sources = matched_cluster["sources"]
        prices.append(price)
        if source not in sources:
            sources.append(source)
        matched_cluster["center"] = sum(prices) / len(prices)

    clustered: list[dict[str, Any]] = []
    for cluster in clusters:
        prices = cluster.get("prices", [])
        if not prices:
            continue

        level_sources = cluster.get("sources", [])
        source_count = len(level_sources)
        if source_count >= 3:
            strength = "strong"
        elif source_count == 2:
            strength = "moderate"
        else:
            strength = "weak"

        clustered.append(
            {
                "price": round(sum(prices) / len(prices), 2),
                "strength": strength,
                "sources": level_sources,
            }
        )

    return clustered


def _split_support_resistance_levels(
    clustered_levels: list[dict[str, Any]],
    current_price: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    supports: list[dict[str, Any]] = []
    resistances: list[dict[str, Any]] = []

    for level in clustered_levels:
        price = _to_float(level.get("price"), default=0.0)
        if price <= 0:
            continue
        level["distance_pct"] = round((price - current_price) / current_price * 100, 2)
        if price < current_price:
            supports.append(level)
        elif price > current_price:
            resistances.append(level)

    supports.sort(
        key=lambda item: _to_float(item.get("price"), default=0.0), reverse=True
    )
    resistances.sort(key=lambda item: _to_float(item.get("price"), default=0.0))
    return supports, resistances


def _compute_rsi_weights(rsi_value: float | None, splits: int) -> list[float]:
    """Compute DCA weight distribution based on RSI value.

    Args:
        rsi_value: RSI 14 value (0-100). None means no RSI data.
        splits: Number of DCA splits (e.g., 3 for 3-step buying)

    Returns:
        List of weights that sum to 1.0. RSI < 30 gives higher weight
        to early steps (linear decreasing), RSI > 50 gives higher weight
        to later steps (linear increasing), 30-50 or None gives equal weights.
    """
    if rsi_value is None:
        # No RSI data: equal distribution
        return [1.0 / splits] * splits

    if rsi_value < 30:
        # Oversold: front-weighted (buy more at early/closer steps)
        # splits=3: raw=[3,2,1] → [0.5, 0.333, 0.167]
        raw = [splits - i for i in range(splits)]
        total = sum(raw)
        return [r / total for r in raw]
    elif rsi_value > 50:
        # Overbought: back-weighted (buy more at later/lower steps)
        # splits=3: raw=[1,2,3] → [0.167, 0.333, 0.5]
        raw = [i + 1 for i in range(splits)]
        total = sum(raw)
        return [r / total for r in raw]
    else:
        # Neutral (30-50): equal distribution
        return [1.0 / splits] * splits


def _compute_dca_price_levels(
    strategy: str,
    splits: int,
    current_price: float,
    supports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compute DCA price levels based on strategy and support levels.

    Args:
        strategy: "support", "equal", or "aggressive"
        splits: Number of price levels to generate
        current_price: Current market price
        supports: List of support levels from get_support_resistance

    Returns:
        List of dicts with "price" and "source" keys, sorted by price
        descending (closest to current price first).
    """
    support_prices = sorted(
        [_to_float(level.get("price")) for level in supports if level.get("price")],
        reverse=True,  # Closest to current price first
    )

    if strategy == "support":
        # Use closest support levels, fill gaps with interpolation
        if len(support_prices) >= splits:
            # Enough supports: use closest splits
            return [
                {"price": price, "source": "support"}
                for price in support_prices[:splits]
            ]
        elif len(support_prices) > 0:
            # Fewer supports: interpolate between them
            support_levels: list[dict[str, Any]] = []
            start_price = current_price * 0.995
            end_price = min(support_prices)
            step = (end_price - start_price) / (splits - 1)
            used_supports: set[float] = set()
            for i in range(splits):
                price = start_price + step * i
                # Check if any support is near this price (within 2%)
                near_support = None
                for supp in support_prices:
                    if supp in used_supports:
                        continue
                    if abs(price - supp) / price < 0.02:
                        near_support = supp
                        break
                if near_support is not None:
                    price = near_support
                    used_supports.add(near_support)
                support_levels.append({"price": price, "source": "support"})
            return support_levels
        else:
            # No supports: synthetic levels (-2%, -4%, -6%...)
            return [
                {
                    "price": current_price * (1.0 - 0.02 * (i + 1)),
                    "source": "synthetic",
                }
                for i in range(splits)
            ]

    elif strategy == "equal":
        # Equal spacing between current_price and lowest support
        if support_prices:
            min_price = min(support_prices)
        else:
            # No supports: go down to -10%
            min_price = current_price * 0.90

        start_price = current_price * 0.995
        step = (min_price - start_price) / (splits - 1)
        return [
            {"price": start_price + step * i, "source": "equal_spaced"}
            for i in range(splits)
        ]

    elif strategy == "aggressive":
        first_price = current_price * 0.995
        levels: list[dict[str, Any]] = [
            {"price": first_price, "source": "aggressive_first"}
        ]

        if splits <= 1:
            return levels

        support_prices = [s["price"] for s in supports]
        if support_prices:
            end_price = min(support_prices)
        else:
            end_price = current_price * 0.98

        remaining = splits - 1
        used_supports: set[float] = set()

        if len(support_prices) >= remaining:
            for i in range(1, splits):
                price = first_price + ((end_price - first_price) / (splits - 1)) * i
                near_support = None
                for supp in support_prices:
                    if supp in used_supports:
                        continue
                    if abs(price - supp) / price < 0.02 and supp < price:
                        near_support = supp
                        break
                if near_support is not None:
                    price = near_support
                    used_supports.add(near_support)
                source = "support" if near_support else "interpolated"
                levels.append({"price": price, "source": source})
        else:
            step = (end_price - first_price) / remaining
            for i in range(1, splits):
                price = first_price + step * i
                levels.append({"price": price, "source": "interpolated"})

        return levels

    else:
        raise ValueError(
            f"Invalid strategy: {strategy}. Must be 'support', 'equal', or 'aggressive'"
        )


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
                np.clip(
                    np.searchsorted(bin_edges, low_i, side="right") - 1, 0, bins - 1
                )
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

    while covered_volume < target_volume and (left_index > 0 or right_index < bins - 1):
        left_vol = bin_volumes[left_index - 1] if left_index > 0 else -np.inf
        right_vol = bin_volumes[right_index + 1] if right_index < bins - 1 else -np.inf

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


async def _fetch_sector_peers_finnhub(symbol: str, limit: int) -> dict[str, Any]:
    """Fetch sector peers from Finnhub API.

    Args:
        symbol: US stock symbol (e.g., "AAPL")
        limit: Maximum number of peers to return

    Returns:
        Dictionary with sector name and peer companies
    """
    client = _get_finnhub_client()

    def fetch_sync() -> dict[str, Any]:
        peers_data = client.company_peers(symbol=symbol.upper())
        return peers_data

    peers = await asyncio.to_thread(fetch_sync)

    if not peers:
        raise ValueError(f"Sector peers not found for symbol '{symbol}'")

    # Get company profiles for peers to get sector info
    def fetch_profiles_sync() -> list[dict[str, Any]]:
        profiles = []
        for peer_symbol in peers[:limit]:
            profile = client.company_profile2(symbol=peer_symbol)
            if profile:
                profiles.append(profile)
        return profiles

    profiles = await asyncio.to_thread(fetch_profiles_sync)

    # Extract sector from first profile
    sector = None
    if profiles:
        sector = profiles[0].get("finnhubIndustry")

    # Transform to consistent format
    result_peers = []
    for profile in profiles:
        result_peers.append(
            {
                "symbol": profile.get("ticker", ""),
                "name": profile.get("name", ""),
                "market_cap": profile.get("marketCapitalization"),
                "exchange": profile.get("exchange", ""),
                "country": profile.get("country", ""),
            }
        )

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "finnhub",
        "sector": sector,
        "peers": result_peers,
    }


async def _fetch_financials_yfinance(
    symbol: str, statement: str, freq: str
) -> dict[str, Any]:
    """Fetch financial statements from yfinance for US stocks.

    Args:
        symbol: US stock symbol (e.g., "AAPL")
        statement: Statement type - "income", "balance", or "cashflow"
        freq: Frequency - "annual" or "quarterly"

    Returns:
        Dictionary with financial data
    """
    loop = asyncio.get_running_loop()
    ticker = yf.Ticker(symbol)

    def fetch_sync() -> dict[str, Any]:
        statement_map = {
            "income": "income_stmt",
            "balance": "balance_sheet",
            "cashflow": "cashflow",
        }
        yf_stmt_name = statement_map.get(statement)
        if not yf_stmt_name:
            raise ValueError(
                f"Invalid statement type '{statement}'. Use: income, balance, cashflow"
            )

        freq_attr = f"quarterly_{yf_stmt_name}" if freq == "quarterly" else yf_stmt_name

        if not hasattr(ticker, freq_attr):
            try:
                df = getattr(ticker, yf_stmt_name)
                if df is None or df.empty:
                    raise ValueError(f"No {statement} data available for '{symbol}'")
            except Exception as e:
                raise ValueError(f"Failed to fetch {statement} data: {e}")

        df = getattr(ticker, freq_attr)
        if df is None or df.empty:
            raise ValueError(f"No {statement} data available for '{symbol}'")

        financials = {}
        for col in df.columns:
            col_key = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)
            period_data = {}
            for row_label, val in df[col].items():
                if pd.notna(val):
                    period_data[str(row_label)] = _normalize_value(val)
            if period_data:
                financials[col_key] = period_data

        return financials

    financials = await loop.run_in_executor(None, fetch_sync)

    return {
        "symbol": symbol.upper(),
        "instrument_type": "equity_us",
        "source": "yfinance",
        "statement": statement,
        "freq": freq,
        "data": financials,
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


async def _get_quote_impl(symbol: str, market_type: str) -> dict[str, Any] | None:
    """Fetch quote data for any market type."""
    if market_type == "crypto":
        return await _fetch_quote_crypto(symbol)
    elif market_type == "equity_kr":
        return await _fetch_quote_equity_kr(symbol)
    elif market_type == "equity_us":
        return await _fetch_quote_equity_us(symbol)
    return None


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
            current_position_52w = round(
                (current_price - low_52w) / (high_52w - low_52w), 2
            )

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
    symbol: str, limit: int, manual_peers: list[str] | None = None
) -> dict[str, Any]:
    """Fetch sector peers for a Korean stock via Naver Finance.

    Args:
        symbol: Korean stock code (6 digits, e.g., "298040")
        limit: Max number of peers to return
        manual_peers: Optional list of peer tickers to use instead of Naver

    Returns:
        Dictionary with target info, peers list, and comparison metrics
    """
    data = await naver_finance.fetch_sector_peers(symbol, limit=limit)

    peers = data["peers"]

    # Build comparison metrics
    target_per = data.get("per")
    target_pbr = data.get("pbr")

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
    symbol: str, limit: int, manual_peers: list[str] | None = None
) -> dict[str, Any]:
    """Fetch sector peers for a US stock via Finnhub + yfinance.

    Args:
        symbol: US stock ticker (e.g., "AAPL")
        limit: Max number of peers to return
        manual_peers: Optional list of peer tickers to use instead of Finnhub

    Returns:
        Dictionary with target info, peers list, and comparison metrics
    """
    client = _get_finnhub_client()
    upper_symbol = symbol.upper()

    # Step 1: Get peer tickers from Finnhub or use manual peers
    if manual_peers:
        peer_tickers = [t.upper() for t in manual_peers if t.upper() != upper_symbol]
        peer_tickers = peer_tickers[:limit]
    else:
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
    # Cross-listed filtering: remove peers with same base ticker as any existing peer
    def get_base_ticker(ticker: str) -> str:
        if "." in ticker:
            return ticker.split(".")[0]
        return ticker

    target_base = get_base_ticker(upper_symbol)
    seen_bases = {target_base}
    filtered_tickers = []
    for ticker in peer_tickers:
        peer_base = get_base_ticker(ticker)
        if peer_base not in seen_bases:
            seen_bases.add(peer_base)
            filtered_tickers.append(ticker)

    peers: list[dict[str, Any]] = []
    for ticker in filtered_tickers:
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
                "same_industry": (
                    info.get("industry") == target_industry
                    if target_industry and info.get("industry")
                    else None
                ),
            }
        )

    peers.sort(
        key=lambda x: (
            x.get("same_industry") is True,
            x.get("market_cap") or 0,
        ),
        reverse=True,
    )
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

    same_industry_count = sum(1 for p in peers if p.get("same_industry"))

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
        "same_industry_count": same_industry_count,
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
DEFAULT_BATCH_CRYPTO_SYMBOLS = [
    "BTC",
    "ETH",
    "XRP",
    "SOL",
    "ADA",
    "DOGE",
    "AVAX",
    "DOT",
    "TRX",
    "LINK",
]

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_FUNDING_RATE_URL = "https://fapi.binance.com/fapi/v1/fundingRate"

# exchangerate-api.com (free, no key required)
EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"

COINGECKO_COINS_LIST_URL = "https://api.coingecko.com/api/v3/coins/list"
COINGECKO_COINS_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
COINGECKO_COIN_DETAIL_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}"
COINGECKO_CACHE_TTL_SECONDS = 300
COINGECKO_SYMBOL_ID_OVERRIDES = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "XRP": "ripple",
    "SOL": "solana",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
    "DOT": "polkadot",
    "TRX": "tron",
    "LINK": "chainlink",
}
_COINGECKO_LIST_CACHE: dict[str, Any] = {
    "expires_at": 0.0,
    "symbol_to_ids": {},
}
_COINGECKO_PROFILE_CACHE: dict[str, dict[str, Any]] = {}
_COINGECKO_LIST_LOCK = asyncio.Lock()
_COINGECKO_PROFILE_LOCK = asyncio.Lock()


def _normalize_crypto_base_symbol(symbol: str) -> str:
    normalized = (symbol or "").strip().upper()
    if not normalized:
        return ""

    if "-" in normalized:
        normalized = normalized.split("-", 1)[-1]
    if normalized.endswith("USDT") and len(normalized) > len("USDT"):
        normalized = normalized[: -len("USDT")]

    return normalized


def _coingecko_cache_valid(expires_at: Any, now: float) -> bool:
    try:
        return float(expires_at) > now
    except Exception:
        return False


async def _get_coingecko_symbol_to_ids() -> dict[str, list[str]]:
    now = time.time()
    if _coingecko_cache_valid(_COINGECKO_LIST_CACHE.get("expires_at"), now):
        cached = _COINGECKO_LIST_CACHE.get("symbol_to_ids")
        if isinstance(cached, dict):
            return cached

    async with _COINGECKO_LIST_LOCK:
        now = time.time()
        if _coingecko_cache_valid(_COINGECKO_LIST_CACHE.get("expires_at"), now):
            cached = _COINGECKO_LIST_CACHE.get("symbol_to_ids")
            if isinstance(cached, dict):
                return cached

        async with httpx.AsyncClient(timeout=15) as cli:
            response = await cli.get(
                COINGECKO_COINS_LIST_URL,
                params={"include_platform": "false", "status": "active"},
            )
            response.raise_for_status()
            data = response.json()

        symbol_to_ids: dict[str, list[str]] = {}
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                coin_id = str(item.get("id") or "").strip()
                coin_symbol = str(item.get("symbol") or "").strip().lower()
                if not coin_id or not coin_symbol:
                    continue
                symbol_to_ids.setdefault(coin_symbol, []).append(coin_id)

        _COINGECKO_LIST_CACHE["symbol_to_ids"] = symbol_to_ids
        _COINGECKO_LIST_CACHE["expires_at"] = now + COINGECKO_CACHE_TTL_SECONDS
        return symbol_to_ids


async def _choose_coingecko_id_by_market_cap(candidate_ids: list[str]) -> str | None:
    if not candidate_ids:
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            response = await cli.get(
                COINGECKO_COINS_MARKETS_URL,
                params={
                    "vs_currency": "krw",
                    "ids": ",".join(candidate_ids),
                    "order": "market_cap_desc",
                    "per_page": len(candidate_ids),
                    "page": 1,
                    "sparkline": "false",
                },
            )
            response.raise_for_status()
            markets = response.json()

        if isinstance(markets, list) and markets:
            first = markets[0]
            if isinstance(first, dict):
                top_id = str(first.get("id") or "").strip()
                if top_id:
                    return top_id
    except Exception:
        return None

    return None


async def _resolve_coingecko_coin_id(symbol: str) -> str:
    base_symbol = _normalize_crypto_base_symbol(symbol)
    if not base_symbol:
        raise ValueError("symbol is required")

    override = COINGECKO_SYMBOL_ID_OVERRIDES.get(base_symbol)
    if override:
        return override

    symbol_to_ids = await _get_coingecko_symbol_to_ids()
    candidates = symbol_to_ids.get(base_symbol.lower(), [])
    if not candidates:
        raise ValueError(f"CoinGecko id not found for symbol '{base_symbol}'")

    if len(candidates) == 1:
        return candidates[0]

    base_lower = base_symbol.lower()
    for coin_id in candidates:
        if coin_id == base_lower or coin_id.replace("-", "") == base_lower:
            return coin_id

    top_id = await _choose_coingecko_id_by_market_cap(candidates)
    if top_id:
        return top_id

    return sorted(candidates)[0]


async def _fetch_coingecko_coin_profile(coin_id: str) -> dict[str, Any]:
    cache_key = coin_id.strip().lower()
    if not cache_key:
        raise ValueError("coin_id is required")

    now = time.time()
    cached = _COINGECKO_PROFILE_CACHE.get(cache_key)
    if cached and _coingecko_cache_valid(cached.get("expires_at"), now):
        data = cached.get("data")
        if isinstance(data, dict):
            return data

    async with _COINGECKO_PROFILE_LOCK:
        now = time.time()
        cached = _COINGECKO_PROFILE_CACHE.get(cache_key)
        if cached and _coingecko_cache_valid(cached.get("expires_at"), now):
            data = cached.get("data")
            if isinstance(data, dict):
                return data

        async with httpx.AsyncClient(timeout=15) as cli:
            response = await cli.get(
                COINGECKO_COIN_DETAIL_URL.format(coin_id=cache_key),
                params={
                    "localization": "false",
                    "tickers": "false",
                    "market_data": "true",
                    "community_data": "false",
                    "developer_data": "false",
                    "sparkline": "false",
                    "include_categories_details": "false",
                },
            )
            response.raise_for_status()
            data = response.json()

        _COINGECKO_PROFILE_CACHE[cache_key] = {
            "expires_at": now + COINGECKO_CACHE_TTL_SECONDS,
            "data": data,
        }
        return data


def _to_optional_money(value: Any) -> int | None:
    numeric = _to_optional_float(value)
    if numeric is None:
        return None
    return int(round(numeric))


def _clean_description_one_line(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None

    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None

    if len(text) > 240:
        text = text[:240].rstrip() + "..."
    return text


def _map_coingecko_profile_to_output(profile: dict[str, Any]) -> dict[str, Any]:
    market_data = profile.get("market_data") or {}
    description_map = profile.get("description") or {}

    description = _clean_description_one_line(
        description_map.get("ko") or description_map.get("en")
    )

    market_cap_krw = _to_optional_money(
        (market_data.get("market_cap") or {}).get("krw")
    )
    total_volume_krw = _to_optional_money(
        (market_data.get("total_volume") or {}).get("krw")
    )
    ath_krw = _to_optional_money((market_data.get("ath") or {}).get("krw"))

    ath_change_pct = _to_optional_float(
        (market_data.get("ath_change_percentage") or {}).get("krw")
    )
    change_7d = _to_optional_float(
        (market_data.get("price_change_percentage_7d_in_currency") or {}).get("krw")
    )
    if change_7d is None:
        change_7d = _to_optional_float(market_data.get("price_change_percentage_7d"))

    change_30d = _to_optional_float(
        (market_data.get("price_change_percentage_30d_in_currency") or {}).get("krw")
    )
    if change_30d is None:
        change_30d = _to_optional_float(market_data.get("price_change_percentage_30d"))

    categories = profile.get("categories")
    if not isinstance(categories, list):
        categories = []

    return {
        "name": profile.get("name"),
        "symbol": str(profile.get("symbol") or "").upper() or None,
        "market_cap": market_cap_krw,
        "market_cap_rank": _to_optional_int(profile.get("market_cap_rank")),
        "total_volume_24h": total_volume_krw,
        "circulating_supply": _to_optional_float(market_data.get("circulating_supply")),
        "total_supply": _to_optional_float(market_data.get("total_supply")),
        "max_supply": _to_optional_float(market_data.get("max_supply")),
        "categories": categories,
        "description": description,
        "ath": ath_krw,
        "ath_change_percentage": ath_change_pct,
        "price_change_percentage_7d": change_7d,
        "price_change_percentage_30d": change_30d,
    }


async def _resolve_batch_crypto_symbols() -> list[str]:
    try:
        coins = await upbit_service.fetch_my_coins()
        held_symbols: list[str] = []
        for coin in coins:
            currency = str(coin.get("currency", "")).upper().strip()
            if not currency or currency == "KRW":
                continue
            quantity = _to_float(coin.get("balance")) + _to_float(coin.get("locked"))
            if quantity <= 0:
                continue
            held_symbols.append(currency)

        if held_symbols:
            try:
                tradable_markets = await upbit_service.fetch_all_market_codes(fiat=None)
                tradable_set = {str(market).upper() for market in tradable_markets}
                held_symbols = [
                    symbol for symbol in held_symbols if symbol.upper() in tradable_set
                ]
            except Exception:
                pass

            if held_symbols:
                return sorted(set(held_symbols))
    except Exception:
        pass

    return list(DEFAULT_BATCH_CRYPTO_SYMBOLS)


def _funding_interpretation_text(rate: float) -> str:
    if rate > 0:
        return "positive (롱이 숏에게 지불, 롱 과열)"
    if rate < 0:
        return "negative (숏이 롱에게 지불, 숏 과열)"
    return "neutral"


async def _fetch_funding_rate_batch(symbols: list[str]) -> list[dict[str, Any]]:
    if not symbols:
        return []

    pair_to_symbol = {f"{symbol.upper()}USDT": symbol.upper() for symbol in symbols}

    async with httpx.AsyncClient(timeout=10) as cli:
        response = await cli.get(BINANCE_PREMIUM_INDEX_URL)
        response.raise_for_status()
        payload = response.json()

    rows: list[dict[str, Any]] = []
    data_list: list[dict[str, Any]]
    if isinstance(payload, list):
        data_list = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict):
        data_list = [payload]
    else:
        data_list = []

    for row in data_list:
        pair = str(row.get("symbol") or "").upper()
        base_symbol = pair_to_symbol.get(pair)
        if not base_symbol:
            continue

        funding_rate = _to_optional_float(row.get("lastFundingRate"))
        next_ts = _to_optional_int(row.get("nextFundingTime"))
        if funding_rate is None or next_ts is None or next_ts <= 0:
            continue

        next_funding_time = datetime.datetime.fromtimestamp(
            next_ts / 1000,
            tz=datetime.UTC,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        rows.append(
            {
                "symbol": base_symbol,
                "funding_rate": funding_rate,
                "next_funding_time": next_funding_time,
                "interpretation": _funding_interpretation_text(funding_rate),
            }
        )

    rows.sort(key=lambda item: str(item.get("symbol", "")))
    return rows


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

    now = datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S")

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
        raw_df = yf.download(
            yf_ticker,
            start=start,
            end=end,
            interval=interval,
            progress=False,
            auto_adjust=False,
        )
        if raw_df is None or not isinstance(raw_df, pd.DataFrame):
            return pd.DataFrame()

        df = raw_df.copy()
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
        if isinstance(d, (datetime.date, datetime.datetime, pd.Timestamp)):
            date_str = d.strftime("%Y-%m-%d")
        else:
            date_str = str(d)[:10]
        history.append(
            {
                "date": date_str,
                "close": float(row["close"]) if pd.notna(row.get("close")) else None,
                "open": float(row["open"]) if pd.notna(row.get("open")) else None,
                "high": float(row["high"]) if pd.notna(row.get("high")) else None,
                "low": float(row["low"]) if pd.notna(row.get("low")) else None,
                "volume": (int(row["volume"]) if pd.notna(row.get("volume")) else None),
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

        # Parse current funding rate
        premium_data = premium_resp.json()
        current_rate = float(premium_data.get("lastFundingRate", 0))
        next_funding_ts = int(premium_data.get("nextFundingTime", 0))
        next_funding_time = datetime.datetime.fromtimestamp(
            next_funding_ts / 1000,
            tz=datetime.UTC,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Build history entries
        funding_history: list[dict[str, Any]] = []
        rates_for_avg: list[float] = []
        for entry in history_resp.json():
            rate = float(entry.get("fundingRate", 0))
            ts = int(entry.get("fundingTime", 0))
            time_str = datetime.datetime.fromtimestamp(
                ts / 1000,
                tz=datetime.UTC,
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            funding_history.append(
                {
                    "time": time_str,
                    "rate": rate,
                    "rate_pct": round(rate * 100, 4),
                }
            )
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


def _parse_change_rate(value: Any) -> float | None:
    val = _to_optional_float(value)
    if val is None:
        return None
    return val


def _normalize_change_rate_equity(value: Any) -> float:
    val = _parse_change_rate(value)
    if val is None:
        return 0.0
    return val


def _normalize_change_rate_crypto(value: Any) -> float:
    val = _parse_change_rate(value)
    if val is None:
        return 0.0
    return val * 100


def _classify_kr_asset_type(symbol: str, name: str | None = None) -> str:
    symbol = symbol.strip().upper()
    name = (name or "").strip().upper()

    etf_keywords = [
        "ETF",
        "ETN",
        "ETP",
        "KODEX",
        "TIGER",
        "KOSEF",
        "KINDEX",
        "ARIRANG",
        "KBSTAR",
        "HANARO",
        "ACE",
        "RISE",
        "SOL",
        "PLUS",
    ]

    is_etf = any(keyword in name for keyword in etf_keywords)
    return "etf" if is_etf else "stock"


def _map_kr_row(row: dict, rank: int) -> dict[str, Any]:
    symbol = row.get("stck_shrn_iscd") or row.get("mksc_shrn_iscd", "")
    name = row.get("hts_kor_isnm", "")
    price = _to_float(row.get("stck_prpr"))
    change_rate = _normalize_change_rate_equity(row.get("prdy_ctrt"))
    volume = _to_int(row.get("acml_vol") or row.get("frgn_ntby_qty"))
    market_cap = _to_float(row.get("hts_avls"))
    trade_amount = _to_float(row.get("acml_tr_pbmn") or row.get("frgn_ntby_tr_pbmn"))

    return {
        "rank": rank,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_rate": round(change_rate, 2) if change_rate is not None else None,
        "volume": volume,
        "market_cap": market_cap,
        "trade_amount": trade_amount,
    }


def _map_us_row(row: dict, rank: int) -> dict[str, Any]:
    symbol = row.get("symbol", "")
    name = row.get("longName", "") or row.get("shortName", symbol)
    price = _to_float(row.get("regularMarketPrice"))
    prev_close = _to_float(row.get("previousClose"))

    if price is not None and prev_close is not None and prev_close > 0:
        change_rate = ((price - prev_close) / prev_close) * 100
    else:
        change_rate = _to_float(row.get("regularMarketChangePercent", 0))

    volume = _to_int(row.get("regularMarketVolume"))
    market_cap = _to_float(row.get("marketCap"))
    trade_amount = None

    return {
        "rank": rank,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_rate": round(change_rate, 2) if change_rate is not None else None,
        "volume": volume,
        "market_cap": market_cap,
        "trade_amount": trade_amount,
    }


def _map_crypto_row(row: dict, rank: int) -> dict[str, Any]:
    symbol = row.get("market", "")
    name = symbol.replace("KRW-", "") if symbol.startswith("KRW-") else symbol
    price = _to_float(row.get("trade_price"))
    change_rate = _normalize_change_rate_crypto(row.get("signed_change_rate"))
    volume = _to_float(row.get("acc_trade_volume_24h"))
    market_cap = None
    trade_amount = _to_float(row.get("acc_trade_price_24h"))

    return {
        "rank": rank,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_rate": round(change_rate, 2) if change_rate is not None else None,
        "volume": volume,
        "market_cap": market_cap,
        "trade_amount": trade_amount,
    }


async def _get_us_rankings(
    ranking_type: str, limit: int
) -> tuple[list[dict[str, Any]], str]:
    screener_ids = {
        "volume": "most_actives",
        "gainers": "day_gainers",
        "losers": "day_losers",
    }

    screener_id = screener_ids.get(ranking_type)

    def fetch_sync():
        if ranking_type == "market_cap":
            query = yf.EquityQuery(
                "and",
                [
                    yf.EquityQuery("eq", ["region", "us"]),
                    yf.EquityQuery("gte", ["intradaymarketcap", 2000000000]),
                    yf.EquityQuery("gte", ["intradayprice", 5]),
                    yf.EquityQuery("gt", ["dayvolume", 15000]),
                ],
            )
            # Filters: 시총≥2B (유동성 확보), 가격≥$5 (페니스탁 제외), 거래량>15000 (비정상 저거래량 제외)
            return yf.screen(
                query, size=limit, sortField="intradaymarketcap", sortAsc=False
            )
        return yf.screen(screener_id)

    results = await asyncio.to_thread(fetch_sync)

    rankings = []
    if isinstance(results, dict):
        quotes = results.get("quotes", [])
        if not quotes:
            raise RuntimeError(
                f"Empty quotes response for ranking_type='{ranking_type}' from yfinance"
            )
        for i, row in enumerate(quotes[:limit], 1):
            rankings.append(_map_us_row(row, i))
    else:
        if results.empty:
            raise RuntimeError(
                f"Empty DataFrame response for ranking_type='{ranking_type}' from yfinance"
            )
        for i, row in enumerate(results.head(limit).to_dict(orient="records"), 1):
            rankings.append(_map_us_row(row, i))

    return rankings, "yfinance"


async def _get_crypto_rankings(
    ranking_type: str, limit: int
) -> tuple[list[dict[str, Any]], str]:
    coins = await upbit_service.fetch_top_traded_coins()

    if ranking_type == "volume":
        sorted_coins = coins
    elif ranking_type == "gainers":
        sorted_coins = sorted(
            coins, key=lambda x: float(x.get("signed_change_rate", 0)), reverse=True
        )
    elif ranking_type == "losers":
        sorted_coins = sorted(
            coins, key=lambda x: float(x.get("signed_change_rate", 0))
        )
    else:
        sorted_coins = coins

    rankings = []
    for i, coin in enumerate(sorted_coins[:limit], 1):
        rankings.append(_map_crypto_row(coin, i))

    return rankings, "upbit"


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
            "when include_current_price=True. When minimum_value is None (default), "
            "per-currency thresholds are applied: KRW=5000, USD=10. "
            "Explicit number uses uniform threshold. Response includes filtered_count, "
            "filter_reason, and per-symbol price lookup errors."
        ),
    )
    async def get_holdings(
        account: str | None = None,
        market: str | None = None,
        include_current_price: bool = True,
        minimum_value: float | None = None,  # None = per-currency defaults
        account_name: str | None = None,
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
            account_name=account_name,
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
        elif include_current_price and minimum_value is None:
            threshold_map = _DEFAULT_MINIMUM_VALUES.copy()
            filter_reason_parts = []
            for instrument_type, threshold in threshold_map.items():
                filter_reason_parts.append(
                    f"{instrument_type} < {_format_filter_threshold(threshold)}"
                )
            filter_reason = ", ".join(filter_reason_parts)
            filtered_positions: list[dict[str, Any]] = []
            for position in positions:
                value = _value_for_minimum_filter(position)
                instrument_type = position.get("instrument_type")
                threshold = threshold_map.get(instrument_type, 0.0)
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
        summary = _build_holdings_summary(positions, include_current_price)

        reported_minimum_value = (
            _DEFAULT_MINIMUM_VALUES.copy() if minimum_value is None else minimum_value
        )

        return {
            "filters": {
                "account": resolved_account_filter,
                "account_name": account_name,
                "market": _INSTRUMENT_TO_MARKET.get(resolved_market_filter),
                "include_current_price": include_current_price,
                "minimum_value": reported_minimum_value,
            },
            "filtered_count": filtered_count,
            "filter_reason": filter_reason,
            "total_accounts": len(accounts),
            "total_positions": len(positions),
            "summary": summary,
            "accounts": accounts,
            "errors": errors,
        }

    # Helper functions for order tools
    def _normalize_upbit_order(order: dict[str, Any]) -> dict[str, Any]:
        """Normalize Upbit order data to standard format."""
        side_code = order.get("side", "")
        side = "buy" if side_code == "bid" else "sell"

        state = order.get("state", "")
        remaining = float(order.get("remaining_volume", 0) or 0)
        filled = float(order.get("executed_volume", 0) or 0)
        ordered = remaining + filled

        ordered_price = float(order.get("price", 0) or 0)
        filled_price = float(order.get("avg_price", 0) or 0)

        status = _map_upbit_state(state, filled, remaining)

        return {
            "order_id": order.get("uuid", ""),
            "symbol": order.get("market", ""),
            "side": side,
            "status": status,
            "ordered_qty": ordered,
            "filled_qty": filled,
            "remaining_qty": remaining,
            "ordered_price": ordered_price,
            "filled_avg_price": filled_price,
            "ordered_at": order.get("created_at", ""),
            "filled_at": order.get("done_at", ""),
            "currency": "KRW",
        }

    def _map_upbit_state(state: str, filled: float, remaining: float) -> str:
        """Map Upbit state to standard status."""
        if state == "wait":
            return "pending"
        elif state == "done":
            if filled > 0:
                return "filled"
            else:
                return "cancelled"
        elif state == "cancelled":
            return "cancelled"
        else:
            return "partial"

    def _get_kis_field(order: dict[str, Any], *keys: str, default: Any = "") -> Any:
        """Get a field from KIS API response, trying multiple case variants.

        KIS API field names can be in lowercase, uppercase, or mixed case.
        This helper tries all provided key variants in order.

        Args:
            order: KIS API response dict
            *keys: Field name variants to try (e.g., "ord_no", "ORD_NO")
            default: Default value if none of keys exist

        Returns:
            First non-empty value found, or default
        """
        for key in keys:
            value = order.get(key)
            if value:
                return value
        return default

    def _extract_kis_order_number(order: dict[str, Any]) -> str:
        """Extract order number from KIS API response with priority.

        Priority order: odno/ODNO > ord_no/ORD_NO > orgn_odno/ORGN_ODNO.
        For KR domestic orders, ODNO is the actual order number from pending responses.

        Args:
            order: KIS API response dict

        Returns:
            First non-empty order number found, or empty string
        """
        value = _get_kis_field(
            order,
            "odno",
            "ODNO",
            "ord_no",
            "ORD_NO",
            "orgn_odno",
            "ORGN_ODNO",
            default="",
        )
        if value is None:
            return ""
        return str(value).strip()

    def _build_temp_kr_order_id(
        *,
        symbol: str,
        side: str,
        ordered_price: int,
        ordered_qty: int,
        ordered_at: str,
    ) -> str:
        """Build deterministic fallback order id for KR orders."""
        raw = "|".join(
            [
                symbol,
                side,
                str(ordered_price),
                str(ordered_qty),
                ordered_at.strip(),
            ]
        )
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12].upper()
        return f"TEMP_KR_{digest}"

    def _normalize_kis_domestic_order(order: dict[str, Any]) -> dict[str, Any]:
        """Normalize KIS domestic order data to standard format."""
        side_code = _get_kis_field(order, "sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD")
        side = "buy" if side_code == "02" else "sell"

        ordered = int(
            float(_get_kis_field(order, "ord_qty", "ORD_QTY", default=0) or 0)
        )
        filled = int(
            float(_get_kis_field(order, "ccld_qty", "CCLD_QTY", default=0) or 0)
        )
        remaining = ordered - filled

        ordered_price = int(
            float(_get_kis_field(order, "ord_unpr", "ORD_UNPR", default=0) or 0)
        )
        filled_price = int(
            float(_get_kis_field(order, "ccld_unpr", "CCLD_UNPR", default=0) or 0)
        )

        status = _map_kis_status(
            filled,
            remaining,
            _get_kis_field(order, "prcs_stat_name", "PRCS_STAT_NAME"),
        )
        symbol = str(_get_kis_field(order, "pdno", "PDNO"))
        ordered_at = (
            f"{_get_kis_field(order, 'ord_dt', 'ORD_DT')} "
            f"{_get_kis_field(order, 'ord_tmd', 'ORD_TMD')}"
        )
        order_id = _extract_kis_order_number(order)
        if not order_id:
            order_id = _build_temp_kr_order_id(
                symbol=symbol,
                side=side,
                ordered_price=ordered_price,
                ordered_qty=ordered,
                ordered_at=ordered_at,
            )
            logger.warning(
                "Missing order_id for KR order (symbol=%s, side=%s, qty=%s, "
                "price=%s, ordered_at=%s), generated %s",
                symbol,
                side,
                ordered,
                ordered_price,
                ordered_at,
                order_id,
            )

        return {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "status": status,
            "ordered_qty": ordered,
            "filled_qty": filled,
            "remaining_qty": remaining,
            "ordered_price": ordered_price,
            "filled_avg_price": filled_price,
            "ordered_at": ordered_at,
            "filled_at": "",
            "currency": "KRW",
        }

    def _normalize_kis_overseas_order(order: dict[str, Any]) -> dict[str, Any]:
        """Normalize KIS overseas order data to standard format."""
        side_code = _get_kis_field(order, "sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD")
        side = "buy" if side_code == "02" else "sell"

        ordered = int(
            float(_get_kis_field(order, "ft_ord_qty", "FT_ORD_QTY", default=0) or 0)
        )
        filled = int(
            float(_get_kis_field(order, "ft_ccld_qty", "FT_CCLD_QTY", default=0) or 0)
        )
        remaining = ordered - filled

        ordered_price = float(
            _get_kis_field(order, "ft_ord_unpr3", "FT_ORD_UNPR3", default=0) or 0
        )
        filled_price = float(
            _get_kis_field(order, "ft_ccld_unpr3", "FT_CCLD_UNPR3", default=0) or 0
        )

        status = _map_kis_status(
            filled,
            remaining,
            _get_kis_field(order, "prcs_stat_name", "PRCS_STAT_NAME"),
        )

        return {
            "order_id": _extract_kis_order_number(order),
            "symbol": _get_kis_field(order, "pdno", "PDNO"),
            "side": side,
            "status": status,
            "ordered_qty": ordered,
            "filled_qty": filled,
            "remaining_qty": remaining,
            "ordered_price": ordered_price,
            "filled_avg_price": filled_price,
            "ordered_at": (
                f"{_get_kis_field(order, 'ord_dt', 'ORD_DT')} "
                f"{_get_kis_field(order, 'ord_tmd', 'ORD_TMD')}"
            ),
            "filled_at": "",
            "currency": "USD",
        }

    def _map_kis_status(filled: int, remaining: int, status_name: str) -> str:
        """Map KIS status to standard status."""
        if status_name in ("접수", "주문접수"):
            return "pending"
        elif status_name == "주문취소":
            return "cancelled"
        elif status_name in ("체결", "미체결"):
            if remaining > 0:
                return "partial"
            return "filled"
        else:
            return "pending"

    def _calculate_order_summary(orders: list[dict[str, Any]]) -> dict[str, Any]:
        """Calculate summary statistics for orders."""
        total_orders = len(orders)
        filled = sum(1 for o in orders if o.get("status") == "filled")
        pending = sum(1 for o in orders if o.get("status") == "pending")
        partial = sum(1 for o in orders if o.get("status") == "partial")
        cancelled = sum(1 for o in orders if o.get("status") == "cancelled")

        return {
            "total_orders": total_orders,
            "filled": filled,
            "pending": pending,
            "partial": partial,
            "cancelled": cancelled,
        }

    def _calculate_date_range(days: int) -> tuple[str, str]:
        """Calculate date range for order lookup."""
        today = datetime.datetime.now()
        start_date = (today - datetime.timedelta(days=days)).strftime("%Y%m%d")
        end_date = today.strftime("%Y%m%d")
        return start_date, end_date

    def _normalize_market_type_to_external(market_type: str) -> str:
        """Convert internal market_type to external contract values.

        Internal types: equity_kr, equity_us, crypto
        External contract values: kr, us, crypto
        """
        mapping = {
            "equity_kr": "kr",
            "equity_us": "us",
            "crypto": "crypto",
        }
        return mapping.get(market_type, market_type)

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

    # ------------------------------------------------------------------
    # place_order
    # ------------------------------------------------------------------

    @mcp.tool(
        name="place_order",
        description=(
            "Place buy/sell orders for stocks or crypto. "
            "Supports Upbit (crypto) and KIS (KR/US equities). "
            "Always returns dry_run preview unless explicitly set to False. "
            "Safety limit: max 20 orders/day. "
            "dry_run=True by default for safety."
        ),
    )
    async def place_order(
        symbol: str,
        side: Literal["buy", "sell"],
        order_type: Literal["limit", "market"] = "limit",
        quantity: float | None = None,
        price: float | None = None,
        amount: float | None = None,
        dry_run: bool = True,
        reason: str = "",
    ) -> dict[str, Any]:
        """Place buy/sell order with safety checks."""
        return await _place_order_impl(
            symbol, side, order_type, quantity, price, amount, dry_run, reason
        )

    # ------------------------------------------------------------------
    # place_order helper functions (module-level for _place_order_impl access)
    # ------------------------------------------------------------------

    async def _get_current_price_for_order(
        symbol: str, market_type: str
    ) -> float | None:
        if market_type == "crypto":
            prices = await upbit_service.fetch_multiple_current_prices([symbol])
            return prices.get(symbol)
        elif market_type == "equity_kr":
            quote = await _fetch_quote_equity_kr(symbol)
            return float(quote.get("price")) if quote.get("price") else None
        else:
            quote = await _fetch_quote_equity_us(symbol)
            return float(quote.get("price")) if quote.get("price") else None

    async def _get_holdings_for_order(
        symbol: str, market_type: str
    ) -> dict[str, Any] | None:
        if market_type == "crypto":
            coins = await upbit_service.fetch_my_coins()
            currency = symbol.replace("KRW-", "")
            for coin in coins:
                if coin.get("currency") == currency:
                    balance = float(coin.get("balance", 0))
                    locked = float(coin.get("locked", 0))
                    avg_buy_price = float(coin.get("avg_buy_price", 0) or 0)
                    return {
                        "quantity": balance + locked,
                        "avg_price": avg_buy_price,
                    }
        else:
            positions, _, _, _ = await _collect_portfolio_positions(
                account=None,
                market="kr" if market_type == "equity_kr" else "us",
                include_current_price=False,
            )
            for position in positions:
                if position["symbol"] == symbol:
                    return {
                        "quantity": float(position.get("quantity", 0)),
                        "avg_price": float(position.get("avg_buy_price", 0) or 0),
                    }
        return None

    async def _get_balance_for_order(market_type: str) -> float:
        if market_type == "crypto":
            coins = await upbit_service.fetch_my_coins()
            for coin in coins:
                if coin.get("currency") == "KRW":
                    return float(coin.get("balance", 0))
        elif market_type == "equity_kr":
            kis = KISClient()
            if hasattr(kis, "inquire_integrated_margin"):
                margin = await kis.inquire_integrated_margin()
                if isinstance(margin, dict):
                    orderable = margin.get("stck_cash_ord_psbl_amt")
                    if orderable is None:
                        orderable = margin.get("dnca_tot_amt")
                    if orderable is not None:
                        return float(orderable or 0)
            balance_data = await kis.inquire_domestic_cash_balance()
            return float(balance_data.get("stck_cash_ord_psbl_amt", 0) or 0)
        elif market_type == "equity_us":
            kis = KISClient()
            if hasattr(kis, "inquire_integrated_margin"):
                margin = await kis.inquire_integrated_margin()
                if isinstance(margin, dict):
                    usd_orderable = (
                        margin.get("usd_ord_psbl_amt")
                        or margin.get("frcr_ord_psbl_amt")
                        or margin.get("usd_balance")
                    )
                    if usd_orderable is not None:
                        return float(usd_orderable or 0)
            margin_data = await kis.inquire_overseas_margin()
            usd_balance = next(
                (
                    row
                    for row in margin_data
                    if str(row.get("crcy_cd", "")).upper() == "USD"
                ),
                None,
            )
            if usd_balance is None:
                raise RuntimeError("USD margin data not found in KIS overseas margin")
            return float(usd_balance.get("frcr_ord_psbl_amt", 0) or 0)
        return 0.0

    async def _check_daily_order_limit(max_orders: int) -> bool:
        try:
            import redis.asyncio as redis_async

            redis_url = getattr(settings, "redis_url", None)
            if not redis_url:
                return True

            redis = await redis_async.from_url(redis_url)
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            key = f"order_count:{today}"

            count = await redis.get(key)
            if count is None:
                count = 0
            else:
                count = int(count)

            if count >= max_orders:
                return False

            return True
        except Exception:
            return True

    async def _record_order_history(
        symbol: str,
        side: str,
        order_type: str,
        quantity: float | None,
        price: float | None,
        amount: float,
        reason: str,
        dry_run: bool,
        error: str | None = None,
    ) -> None:
        try:
            import redis.asyncio as redis_async

            redis_url = getattr(settings, "redis_url", None)
            if not redis_url:
                return

            redis = await redis_async.from_url(redis_url)
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            key = f"order_history:{today}"
            record = {
                "timestamp": timestamp,
                "symbol": symbol,
                "side": side,
                "order_type": order_type,
                "quantity": quantity,
                "price": price,
                "amount": amount,
                "reason": reason,
                "dry_run": dry_run,
                "error": error,
            }

            await redis.rpush(key, json.dumps(record))
            await redis.expire(key, 86400)
        except Exception:
            pass

    async def _preview_order(
        symbol: str,
        side: str,
        order_type: str,
        quantity: float | None,
        price: float | None,
        current_price: float,
        market_type: str,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "current_price": current_price,
        }

        if order_type == "market":
            execution_price = current_price
            result["price"] = execution_price
        else:
            execution_price = price
            result["price"] = execution_price

        if side == "buy":
            if order_type == "market":
                if price is not None:
                    estimated_value = _to_float(price, default=0.0)
                elif quantity is not None:
                    estimated_value = current_price * quantity
                else:
                    balance = await _get_balance_for_order(market_type)
                    if market_type == "crypto":
                        min_market_buy_amount = _to_float(
                            getattr(settings, "upbit_buy_amount", 0), default=0.0
                        )
                    else:
                        min_market_buy_amount = 0.0
                    estimated_value = (
                        balance
                        if balance >= min_market_buy_amount
                        else min_market_buy_amount
                    )

                if estimated_value <= 0:
                    result["error"] = "order amount must be greater than 0"
                    return result

                order_quantity = estimated_value / current_price
                result["quantity"] = order_quantity
                result["estimated_value"] = estimated_value
                result["fee"] = estimated_value * 0.0005
                return result

            if price is None:
                result["error"] = "price is required for limit buy orders"
                return result
            if price > current_price:
                result["error"] = (
                    f"Buy price {price} exceeds current price {current_price}"
                )
                return result
            if quantity is None:
                result["error"] = "quantity is required for limit buy orders"
                return result

            order_quantity = quantity
            estimated_value = execution_price * order_quantity
            result["quantity"] = order_quantity
            result["estimated_value"] = estimated_value
            result["fee"] = estimated_value * 0.0005
            return result
        else:
            holdings = await _get_holdings_for_order(symbol, market_type)
            if not holdings:
                result["error"] = "No holdings found"
                return result

            avg_price = holdings["avg_price"]

            if order_type == "market":
                order_quantity = holdings["quantity"]
                execution_price = current_price
            else:
                if price is None:
                    result["error"] = "price is required for limit sell orders"
                    return result
                min_sell_price = avg_price * 1.01
                if price < min_sell_price:
                    result["error"] = (
                        f"Sell price {price} below minimum "
                        f"(avg_buy_price * 1.01 = {min_sell_price:.0f})"
                    )
                    return result
                if price < current_price:
                    result["error"] = (
                        f"Sell price {price} below current price {current_price}"
                    )
                    return result
                order_quantity = holdings["quantity"] if quantity is None else quantity
                execution_price = price

            estimated_value = execution_price * order_quantity
            realized_pnl = (execution_price - avg_price) * order_quantity

            result["quantity"] = order_quantity
            result["estimated_value"] = estimated_value
            result["fee"] = estimated_value * 0.0005
            result["realized_pnl"] = realized_pnl
            result["avg_buy_price"] = avg_price

        return result

    async def _execute_order(
        symbol: str,
        side: str,
        order_type: str,
        quantity: float | None,
        price: float | None,
        market_type: str,
    ) -> dict[str, Any]:
        if market_type == "crypto":
            if side == "buy":
                if order_type == "market":
                    price_str = f"{price:.0f}" if price else "0"
                    return await upbit_service.place_market_buy_order(symbol, price_str)
                else:
                    volume_str = f"{quantity:.8f}"
                    price_str = f"{price:.0f}"
                    adjusted_price = upbit_service.adjust_price_to_upbit_unit(price)
                    return await upbit_service.place_buy_order(
                        symbol, adjusted_price, volume_str, "limit"
                    )
            else:
                holdings = await _get_holdings_for_order(symbol, market_type)
                if not holdings:
                    raise ValueError("No holdings found")

                volume = holdings["quantity"] if quantity is None else quantity
                if order_type == "market":
                    volume_str = f"{volume:.8f}"
                    return await upbit_service.place_market_sell_order(
                        symbol, volume_str
                    )
                else:
                    volume_str = f"{volume:.8f}"
                    adjusted_price = upbit_service.adjust_price_to_upbit_unit(price)
                    price_str = f"{adjusted_price}"
                    return await upbit_service.place_sell_order(
                        symbol, volume_str, price_str
                    )
        elif market_type == "equity_kr":
            kis = KISClient()
            stock_code = symbol
            order_quantity = int(quantity) if quantity else 0
            order_price = int(price) if price else 0

            # Apply KRX tick size adjustment for limit orders
            original_price = order_price if order_price else None
            if order_type == "limit" and order_price > 0:
                order_price = adjust_tick_size_kr(float(order_price), side)

            if side == "buy":
                result = await kis.order_korea_stock(
                    stock_code=stock_code,
                    order_type="buy",
                    quantity=order_quantity,
                    price=order_price,
                )
            else:
                result = await kis.order_korea_stock(
                    stock_code=stock_code,
                    order_type="sell",
                    quantity=order_quantity,
                    price=order_price,
                )

            # Add tick adjustment info to response if adjustment occurred
            if original_price is not None and order_price != original_price:
                result["original_price"] = original_price
                result["adjusted_price"] = order_price
                result["tick_adjusted"] = True

            return result
        else:
            kis = KISClient()
            exchange_code = get_exchange_by_symbol(symbol) or "NASD"

            if side == "buy":
                return await kis.buy_overseas_stock(
                    symbol=symbol,
                    exchange_code=exchange_code,
                    quantity=int(quantity) if quantity else 0,
                    price=price if price else 0.0,
                )
            else:
                return await kis.sell_overseas_stock(
                    symbol=symbol,
                    exchange_code=exchange_code,
                    quantity=int(quantity) if quantity else 0,
                    price=price if price else 0.0,
                )

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

    async def _get_indicators_impl(
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

    @mcp.tool(
        name="get_indicators",
        description="Calculate technical indicators for a symbol. Available indicators: sma (Simple Moving Average), ema (Exponential Moving Average), rsi (Relative Strength Index), macd (MACD), bollinger (Bollinger Bands), atr (Average True Range), pivot (Pivot Points).",
    )
    async def get_indicators(
        symbol: str,
        indicators: list[str],
        market: str | None = None,
    ) -> dict[str, Any]:
        """Calculate technical indicators for a symbol."""
        return await _get_indicators_impl(symbol, indicators, market)

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
        name="get_crypto_profile",
        description=(
            "Get cryptocurrency profile data from CoinGecko. "
            "Accepts Upbit market code (e.g. KRW-BTC) or plain symbol (e.g. BTC)."
        ),
    )
    async def get_crypto_profile(symbol: str) -> dict[str, Any]:
        """Get crypto profile for a symbol via CoinGecko."""
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        normalized_symbol = _normalize_crypto_base_symbol(symbol)
        if not normalized_symbol:
            raise ValueError("symbol is required")

        try:
            coin_id = await _resolve_coingecko_coin_id(normalized_symbol)
            profile = await _fetch_coingecko_coin_profile(coin_id)
            result = _map_coingecko_profile_to_output(profile)
            if result.get("symbol") is None:
                result["symbol"] = normalized_symbol
            if result.get("name") is None:
                result["name"] = normalized_symbol
            return result
        except Exception as exc:
            return _error_payload(
                source="coingecko",
                message=str(exc),
                symbol=normalized_symbol,
                instrument_type="crypto",
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
                try:
                    return await _fetch_financials_finnhub(symbol, statement, freq)
                except (ValueError, Exception):
                    return await _fetch_financials_yfinance(symbol, statement, freq)
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
    async def get_valuation(symbol: str, market: str | None = None) -> dict[str, Any]:
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
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Get kimchi premium for cryptocurrencies.

        Args:
            symbol: Coin symbol (e.g., "BTC", "ETH"). If not specified,
                     returns data for held coins (fallback: major coins)

        Returns:
            Dictionary with kimchi premium data including exchange rate,
            Upbit/Binance prices, and premium percentage for each coin.
        """
        try:
            if symbol:
                sym = _normalize_crypto_base_symbol(symbol)
                if not sym:
                    raise ValueError("symbol is required")
                symbols = [sym]
                return await _fetch_kimchi_premium(symbols)

            symbols = await _resolve_batch_crypto_symbols()
            payload = await _fetch_kimchi_premium(symbols)
            rows: list[dict[str, Any]] = []
            for item in payload.get("data", []):
                if not isinstance(item, dict):
                    continue
                rows.append(
                    {
                        "symbol": item.get("symbol"),
                        "upbit_price": item.get("upbit_krw"),
                        "binance_price": item.get("binance_usdt"),
                        "premium_pct": item.get("premium_pct"),
                    }
                )
            return rows
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
        symbol: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Get futures funding rate for a cryptocurrency.

        Args:
            symbol: Coin symbol (e.g., "BTC", "ETH"). KRW-/USDT- prefix is stripped automatically.
                If omitted, returns batch snapshot for held coins (fallback: major coins).
            limit: Number of historical funding rate entries (default: 10, max: 100)

        Returns:
            Dictionary with current funding rate, next funding time, history, and interpretation.
        """
        if symbol is not None and not symbol.strip():
            raise ValueError("symbol is required")

        try:
            if symbol is None:
                symbols = await _resolve_batch_crypto_symbols()
                return await _fetch_funding_rate_batch(symbols)

            normalized_symbol = _normalize_crypto_base_symbol(symbol)
            if not normalized_symbol:
                raise ValueError("symbol is required")

            capped_limit = min(max(limit, 1), 100)
            return await _fetch_funding_rate(normalized_symbol, capped_limit)
        except Exception as exc:
            normalized_symbol = _normalize_crypto_base_symbol(symbol or "")
            return _error_payload(
                source="binance",
                message=str(exc),
                symbol=f"{normalized_symbol}USDT" if normalized_symbol else None,
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
                raise ValueError(
                    f"Unknown index symbol '{sym}'. Supported: {', '.join(sorted(_INDEX_META))}"
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
                        _fetch_index_us_current(meta["yf_ticker"], meta["name"], sym),
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
                    indices.append({"symbol": _DEFAULT_INDICES[i], "error": str(r)})
                else:
                    indices.append(r)

            return {"indices": indices}

    # ------------------------------------------------------------------
    # get_sector_peers
    # ------------------------------------------------------------------

    async def _get_support_resistance_impl(
        symbol: str,
        market: str | None = None,
    ) -> dict[str, Any]:
        """Get support/resistance zones from multi-indicator clustering."""
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        market_type, normalized_symbol = _resolve_market_type(symbol, market)
        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]

        try:
            df = await _fetch_ohlcv_for_indicators(
                normalized_symbol, market_type, count=60
            )
            if df.empty:
                raise ValueError(f"No data available for symbol '{normalized_symbol}'")

            for col in ("high", "low", "close"):
                if col not in df.columns:
                    raise ValueError(f"Missing required column: {col}")

            current_price = round(float(df["close"].iloc[-1]), 2)

            fib_result = _calculate_fibonacci(df, current_price)
            fib_result["symbol"] = normalized_symbol

            volume_profile_df = await _fetch_ohlcv_for_volume_profile(
                normalized_symbol, market_type, 60
            )
            volume_result = _calculate_volume_profile(volume_profile_df, bins=20)
            volume_result["symbol"] = normalized_symbol
            volume_result["period_days"] = 60

            indicator_result = _compute_indicators(df, ["bollinger"])
            indicator_result["symbol"] = normalized_symbol
            indicator_result["price"] = current_price
            indicator_result["instrument_type"] = market_type
            indicator_result["source"] = source

            if not fib_result.get("levels"):
                raise ValueError("Failed to calculate Fibonacci levels")
            if current_price is None or current_price <= 0:
                raise ValueError("failed to resolve current price")

            price_levels: list[tuple[float, str]] = []

            fib_levels = fib_result.get("levels", {})
            if isinstance(fib_levels, dict):
                for level_key, price in fib_levels.items():
                    level_price = _to_optional_float(price)
                    if level_price is None or level_price <= 0:
                        continue
                    price_levels.append(
                        (level_price, _format_fibonacci_source(str(level_key)))
                    )

            poc_price = _to_optional_float(
                (volume_result.get("poc") or {}).get("price")
            )
            if poc_price is not None and poc_price > 0:
                price_levels.append((poc_price, "volume_poc"))

            value_area = volume_result.get("value_area") or {}
            value_area_high = _to_optional_float(value_area.get("high"))
            value_area_low = _to_optional_float(value_area.get("low"))
            if value_area_high is not None and value_area_high > 0:
                price_levels.append((value_area_high, "volume_value_area_high"))
            if value_area_low is not None and value_area_low > 0:
                price_levels.append((value_area_low, "volume_value_area_low"))

            bollinger = indicator_result.get("bollinger")
            if not isinstance(bollinger, dict):
                bollinger = (indicator_result.get("indicators") or {}).get(
                    "bollinger"
                ) or {}
            bb_upper = _to_optional_float(bollinger.get("upper"))
            bb_middle = _to_optional_float(bollinger.get("middle"))
            bb_lower = _to_optional_float(bollinger.get("lower"))
            if bb_upper is not None and bb_upper > 0:
                price_levels.append((bb_upper, "bb_upper"))
            if bb_middle is not None and bb_middle > 0:
                price_levels.append((bb_middle, "bb_middle"))
            if bb_lower is not None and bb_lower > 0:
                price_levels.append((bb_lower, "bb_lower"))

            clustered_levels = _cluster_price_levels(price_levels, tolerance_pct=0.02)
            supports, resistances = _split_support_resistance_levels(
                clustered_levels,
                current_price,
            )

            return {
                "symbol": normalized_symbol,
                "current_price": round(current_price, 2),
                "supports": supports,
                "resistances": resistances,
            }
        except Exception as exc:
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=normalized_symbol,
                instrument_type=market_type,
            )

    @mcp.tool(
        name="get_support_resistance",
        description=(
            "Extract key support/resistance zones by combining Fibonacci levels, "
            "volume profile (POC/value area), and Bollinger Bands, then clustering "
            "nearby levels within +/-2%."
        ),
    )
    async def get_support_resistance(
        symbol: str,
        market: str | None = None,
    ) -> dict[str, Any]:
        """Get support/resistance zones from multi-indicator clustering."""
        return await _get_support_resistance_impl(symbol, market)

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
        manual_peers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get sector peer stocks for a stock.

        Args:
            symbol: Stock symbol (e.g., "005930" for Korean, "AAPL" for US)
            market: Market hint - "kr" or "us" (auto-detected if empty)
            limit: Number of peer stocks to return (default: 5, max: 20)
            manual_peers: Optional list of peer tickers to use instead of auto-discovery

        Returns:
            Dictionary with target stock info, peer stocks list, and
            comparison metrics (avg_per, avg_pbr, rankings).

        Examples:
            get_sector_peers("298040")            # 효성중공업 (Korean)
            get_sector_peers("AAPL")              # Apple (US)
            get_sector_peers("005930", limit=10)  # 삼성전자 with 10 peers
            get_sector_peers("AAPL", manual_peers=["MSFT", "GOOGL"])  # Manual peers
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if _is_crypto_market(symbol):
            raise ValueError("Sector peers are not available for cryptocurrencies")

        capped_limit = min(max(limit, 1), 20)

        # Determine market
        market_str = (market or "").strip().lower()
        if market_str in (
            "kr",
            "krx",
            "korea",
            "kospi",
            "kosdaq",
            "kis",
            "naver",
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
                return await _fetch_sector_peers_naver(
                    symbol, capped_limit, manual_peers
                )
            else:
                return await _fetch_sector_peers_us(symbol, capped_limit, manual_peers)
        except Exception as exc:
            source = "naver" if resolved_market == "kr" else "finnhub+yfinance"
            instrument_type = "equity_kr" if resolved_market == "kr" else "equity_us"
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

    # ------------------------------------------------------------------
    # place_order
    # ------------------------------------------------------------------

    async def _place_order_impl(
        symbol: str,
        side: Literal["buy", "sell"],
        order_type: Literal["limit", "market"] = "limit",
        quantity: float | None = None,
        price: float | None = None,
        amount: float | None = None,
        dry_run: bool = True,
        reason: str = "",
    ) -> dict[str, Any]:
        """Place buy/sell order with safety checks.

        Args:
            symbol: Stock/crypto symbol (e.g., "KRW-BTC", "005930", "AAPL")
            side: "buy" or "sell"
            order_type: "limit" or "market" (default: limit)
            quantity: Order quantity (required for limit orders, None for sell-all)
            price: Order price (required for limit orders, None for market orders)
            amount: Order amount in market currency (for buy orders only, mutually exclusive with quantity; e.g., KRW for KR/crypto, USD for US equities)
            dry_run: Preview order without execution (default: True)
            reason: Order reason for logging

        Returns:
            Order preview (dry_run=True) or execution result (dry_run=False)
        """
        MAX_ORDERS_PER_DAY = 20

        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        side_lower = side.lower().strip()
        if side_lower not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")

        order_type_lower = order_type.lower().strip()
        if order_type_lower not in ("limit", "market"):
            raise ValueError("order_type must be 'limit' or 'market'")

        if order_type_lower == "limit" and price is None:
            raise ValueError("price is required for limit orders")

        if amount is not None and quantity is not None:
            raise ValueError(
                "amount and quantity cannot both be specified. Use amount for notional-based buying or quantity for unit-based buying."
            )

        if amount is not None and side_lower != "buy":
            raise ValueError(
                "amount can only be used for buy orders. Use quantity for sell orders."
            )

        market_type, normalized_symbol = _resolve_market_type(symbol, None)
        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "kis"}
        source = source_map[market_type]

        def _order_error(message: str) -> dict[str, Any]:
            return {
                "success": False,
                **_error_payload(
                    source=source,
                    message=message,
                    symbol=normalized_symbol,
                    instrument_type=market_type,
                ),
            }

        try:
            try:
                current_price = await _get_current_price_for_order(
                    normalized_symbol, market_type
                )
            except Exception:
                if order_type_lower == "limit" and price is not None:
                    current_price = float(price)
                else:
                    raise

            if current_price is None:
                if order_type_lower == "limit" and price is not None:
                    current_price = float(price)
                else:
                    raise ValueError(f"Failed to get current price for {symbol}")

            holdings: dict[str, Any] | None = None
            order_quantity = quantity

            if side_lower == "buy" and amount is not None:
                if order_type_lower == "market" and market_type == "crypto":
                    price = amount
                elif order_type_lower == "limit" and price is not None:
                    order_quantity = amount / price
                    if market_type != "crypto":
                        order_quantity = int(order_quantity)
                else:
                    if current_price is None or current_price <= 0:
                        raise ValueError(f"Failed to get current price for {symbol}")
                    order_quantity = amount / current_price
                    if order_quantity <= 0:
                        raise ValueError(
                            f"Calculated quantity {order_quantity} is <= 0. "
                            f"Check amount ({amount}) and current price ({current_price})"
                        )
                    if market_type != "crypto":
                        order_quantity = int(order_quantity)
                        if order_quantity == 0:
                            raise ValueError(
                                f"Calculated quantity {order_quantity} is 0. "
                                f"Amount {amount} is insufficient for 1 unit at price {current_price}"
                            )

            if order_type_lower == "limit" and order_quantity is None:
                raise ValueError("quantity is required for limit orders")

            if side_lower == "sell":
                holdings = await _get_holdings_for_order(normalized_symbol, market_type)
                if not holdings:
                    return _order_error(f"No holdings found for {symbol}")

                available_quantity = _to_float(holdings.get("quantity"), default=0.0)
                order_quantity = (
                    available_quantity
                    if quantity is None
                    else min(quantity, available_quantity)
                )

                if order_type_lower == "limit" and price is not None:
                    avg_price = _to_float(holdings.get("avg_price"), default=0.0)
                    min_sell_price = avg_price * 1.01
                    if price < min_sell_price:
                        return _order_error(
                            f"Sell price {price} below minimum "
                            f"(avg_buy_price * 1.01 = {min_sell_price:.0f})"
                        )
                    if price < current_price:
                        return _order_error(
                            f"Sell price {price} below current price {current_price}"
                        )

            preview_fn = globals().get("_preview_order", _preview_order)
            dry_run_result = await preview_fn(
                symbol=normalized_symbol,
                side=side_lower,
                order_type=order_type_lower,
                quantity=order_quantity,
                price=price,
                current_price=current_price,
                market_type=market_type,
            )

            if not isinstance(dry_run_result, dict):
                raise ValueError("Order preview returned invalid result")

            if dry_run_result.get("error"):
                return _order_error(str(dry_run_result["error"]))

            if (
                side_lower == "sell"
                and order_quantity is not None
                and dry_run_result.get("quantity") is None
            ):
                dry_run_result["quantity"] = order_quantity

            dry_run_result.setdefault("symbol", normalized_symbol)
            dry_run_result.setdefault("side", side_lower)
            dry_run_result.setdefault("order_type", order_type_lower)
            if dry_run_result.get("price") is None:
                dry_run_result["price"] = (
                    current_price if order_type_lower == "market" else price
                )

            order_amount = _to_float(dry_run_result.get("estimated_value"), default=0.0)

            balance_warning: str | None = None

            if side_lower == "buy":
                balance = await _get_balance_for_order(market_type)
                if balance < order_amount:
                    if market_type == "crypto":
                        balance_warning = (
                            f"Insufficient KRW balance: {balance:,.0f} KRW < {order_amount:,.0f} KRW. "
                            f"Please deposit KRW from your bank account to Upbit, then retry."
                        )
                    elif market_type == "equity_kr":
                        balance_warning = (
                            f"Insufficient KRW balance: {balance:,.0f} KRW < {order_amount:,.0f} KRW. "
                            f"Please deposit funds to your KIS domestic account, then retry."
                        )
                    else:
                        balance_warning = (
                            f"Insufficient USD balance: {balance:,.2f} USD < {order_amount:,.2f} USD. "
                            f"Please deposit USD to your KIS overseas account, then retry."
                        )
                    if not dry_run:
                        return _order_error(balance_warning)

            if dry_run:
                result = {
                    "success": True,
                    "dry_run": True,
                    **dry_run_result,
                    "message": "Order preview (dry_run=True)",
                }
                if balance_warning:
                    result["warning"] = balance_warning
                return result

            if not await _check_daily_order_limit(MAX_ORDERS_PER_DAY):
                return _order_error(
                    f"Daily order limit ({MAX_ORDERS_PER_DAY}) exceeded"
                )

            execution_result = await _execute_order(
                symbol=normalized_symbol,
                side=side_lower,
                order_type=order_type_lower,
                quantity=order_quantity,
                price=price,
                market_type=market_type,
            )

            await _record_order_history(
                symbol=normalized_symbol,
                side=side_lower,
                order_type=order_type_lower,
                quantity=order_quantity,
                price=price,
                amount=order_amount,
                reason=reason,
                dry_run=False,
            )

            return {
                "success": True,
                "dry_run": False,
                "preview": dry_run_result,
                "execution": execution_result,
                "message": "Order placed successfully",
            }

        except Exception as exc:
            await _record_order_history(
                symbol=normalized_symbol,
                side=side_lower,
                order_type=order_type_lower,
                quantity=quantity,
                price=price,
                amount=0,
                reason=reason,
                dry_run=True,
                error=str(exc),
            )
            return _order_error(str(exc))

    # ------------------------------------------------------------------
    # get_cash_balance
    # ------------------------------------------------------------------
    # get_cash_balance
    # ------------------------------------------------------------------

    @mcp.tool(
        name="get_cash_balance",
        description=(
            "Query available cash balances from all accounts. "
            "Supports Upbit (KRW), KIS domestic (KRW), and KIS overseas (USD). "
            "Returns detailed balance information including orderable amounts."
        ),
    )
    async def get_cash_balance(account: str | None = None) -> dict[str, Any]:
        """Query available cash balances from all accounts.

        Args:
            account: Optional account filter ("upbit", "kis", "kis_domestic", "kis_overseas")
                      If None, returns all account balances.

        Returns:
            Dictionary with accounts list, summary, and errors.
        """
        accounts: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        total_krw = 0.0
        total_usd = 0.0

        account_filter = _normalize_account_filter(account)
        strict_mode = account_filter is not None

        if account_filter is None or account_filter in ("upbit",):
            try:
                krw_balance = await upbit_service.fetch_krw_balance()
                accounts.append(
                    {
                        "account": "upbit",
                        "account_name": "기본 계좌",
                        "broker": "upbit",
                        "currency": "KRW",
                        "balance": krw_balance,
                        "formatted": f"{int(krw_balance):,} KRW",
                    }
                )
                total_krw += krw_balance
            except Exception as exc:
                errors.append(
                    {"source": "upbit", "market": "crypto", "error": str(exc)}
                )

        if account_filter is None or account_filter in (
            "kis",
            "kis_domestic",
            "kis_overseas",
        ):
            kis = KISClient()

            if account_filter is None or account_filter in ("kis", "kis_domestic"):
                try:
                    domestic_data = await kis.inquire_domestic_cash_balance()
                    dncl_amt = float(domestic_data.get("dnca_tot_amt", 0) or 0)
                    orderable = float(
                        domestic_data.get("stck_cash_ord_psbl_amt", 0) or 0
                    )
                    accounts.append(
                        {
                            "account": "kis_domestic",
                            "account_name": "기본 계좌",
                            "broker": "kis",
                            "currency": "KRW",
                            "balance": dncl_amt,
                            "orderable": orderable,
                            "formatted": f"{int(dncl_amt):,} KRW",
                        }
                    )
                    total_krw += dncl_amt
                except Exception as exc:
                    if strict_mode:
                        raise RuntimeError(
                            f"KIS domestic cash balance query failed: {exc}"
                        ) from exc
                    errors.append({"source": "kis", "market": "kr", "error": str(exc)})

            if account_filter is None or account_filter in ("kis", "kis_overseas"):
                try:
                    overseas_margin_data = await kis.inquire_overseas_margin()
                    usd_margin = next(
                        (
                            row
                            for row in overseas_margin_data
                            if str(row.get("crcy_cd", "")).upper() == "USD"
                        ),
                        None,
                    )
                    if usd_margin is None:
                        raise RuntimeError(
                            "USD margin data not found in KIS overseas margin"
                        )

                    balance = float(usd_margin.get("frcr_dncl_amt_2", 0) or 0)
                    orderable = float(usd_margin.get("frcr_ord_psbl_amt", 0) or 0)
                    accounts.append(
                        {
                            "account": "kis_overseas",
                            "account_name": "기본 계좌",
                            "broker": "kis",
                            "currency": "USD",
                            "balance": balance,
                            "orderable": orderable,
                            "exchange_rate": None,
                            "formatted": f"${balance:.2f} USD",
                        }
                    )
                    total_usd += balance
                except Exception as exc:
                    if strict_mode:
                        raise RuntimeError(
                            f"KIS overseas cash balance query failed: {exc}"
                        ) from exc
                    errors.append({"source": "kis", "market": "us", "error": str(exc)})

        return {
            "accounts": accounts,
            "summary": {
                "total_krw": total_krw,
                "total_usd": total_usd,
            },
            "errors": errors,
        }

    # ------------------------------------------------------------------
    # cancel_order
    # ------------------------------------------------------------------

    @mcp.tool(
        name="cancel_order",
        description=(
            "Cancel a pending order. "
            "Supports Upbit (crypto) and KIS (KR/US equities). "
            "For KIS orders, automatically retrieves order details if not provided."
        ),
    )
    async def cancel_order(
        order_id: str,
        symbol: str | None = None,
        market: str | None = None,
    ) -> dict[str, Any]:
        """Cancel a pending order.

        Args:
            order_id: Order ID (UUID for Upbit, order number for KIS)
            symbol: Optional symbol (required for KIS cancel, auto-detected for Upbit UUID)
            market: Optional market ("crypto", "kr", "us"). Auto-detected if not specified.

        Returns:
            Cancellation result with success status and details.
        """
        order_id = (order_id or "").strip()
        if not order_id:
            raise ValueError("order_id is required")

        symbol = (symbol or "").strip() if symbol else None
        market_type = _parse_holdings_market_filter(market)

        if market_type is None:
            if symbol:
                market_type, _ = _resolve_market_type(symbol, None)
            elif "-" in order_id and len(order_id) == 36:
                market_type = "crypto"
            else:
                raise ValueError(
                    "market must be specified when symbol is not provided and order_id is not a UUID"
                )

        try:
            if market_type == "crypto":
                results = await upbit_service.cancel_orders([order_id])
                if results and len(results) > 0:
                    result = results[0]
                    if "error" in result:
                        return {
                            "success": False,
                            "order_id": order_id,
                            "error": result.get("error"),
                        }
                    return {
                        "success": True,
                        "order_id": order_id,
                        "cancelled_at": result.get("created_at", ""),
                    }
                return {
                    "success": False,
                    "order_id": order_id,
                    "error": "No result from Upbit",
                }

            elif market_type == "equity_kr":
                if not symbol:
                    try:
                        kis = KISClient()
                        open_orders = await kis.inquire_korea_orders()
                        for order in open_orders:
                            if (
                                str(
                                    _get_kis_field(
                                        order, "odno", "ODNO", "ord_no", "ORD_NO"
                                    )
                                )
                                == order_id
                            ):
                                symbol = str(_get_kis_field(order, "pdno", "PDNO"))
                                break
                    except Exception as exc:
                        return {
                            "success": False,
                            "order_id": order_id,
                            "error": f"Failed to auto-retrieve order details: {exc}",
                        }

                if not symbol:
                    return {
                        "success": False,
                        "order_id": order_id,
                        "error": "symbol not found in order",
                    }

                try:
                    kis = KISClient()
                    side_code = "02"  # Default to buy
                    price = 0
                    quantity = 1

                    open_orders = await kis.inquire_korea_orders()
                    for order in open_orders:
                        if (
                            str(
                                _get_kis_field(
                                    order, "odno", "ODNO", "ord_no", "ORD_NO"
                                )
                            )
                            == order_id
                        ):
                            side_code = _get_kis_field(
                                order,
                                "sll_buy_dvsn_cd",
                                "SLL_BUY_DVSN_CD",
                                default="02",
                            )
                            price = int(
                                float(
                                    _get_kis_field(
                                        order, "ord_unpr", "ORD_UNPR", default=0
                                    )
                                    or 0
                                )
                            )
                            quantity = int(
                                float(
                                    _get_kis_field(
                                        order, "ord_qty", "ORD_QTY", default=0
                                    )
                                    or 0
                                )
                            )
                            break

                    order_type_str = "buy" if side_code == "02" else "sell"
                    result = await kis.cancel_korea_order(
                        order_number=order_id,
                        stock_code=symbol,
                        quantity=quantity,
                        price=price,
                        order_type=order_type_str,
                    )
                    return {
                        "success": True,
                        "order_id": order_id,
                        "symbol": symbol,
                        "cancelled_at": result.get("ord_tmd", ""),
                    }
                except Exception as exc:
                    return {
                        "success": False,
                        "order_id": order_id,
                        "symbol": symbol,
                        "error": str(exc),
                    }

            elif market_type == "equity_us":
                if not symbol:
                    try:
                        kis = KISClient()
                        open_orders = await kis.inquire_overseas_orders("NASD")
                        for order in open_orders:
                            if str(_get_kis_field(order, "odno", "ODNO")) == order_id:
                                symbol = str(_get_kis_field(order, "pdno", "PDNO"))
                                break
                    except Exception as exc:
                        return {
                            "success": False,
                            "order_id": order_id,
                            "error": f"Failed to auto-retrieve order details: {exc}",
                        }

                if not symbol:
                    return {
                        "success": False,
                        "order_id": order_id,
                        "error": "symbol not found in order",
                    }

                try:
                    kis = KISClient()
                    quantity = 1

                    open_orders = await kis.inquire_overseas_orders("NASD")
                    for order in open_orders:
                        if str(_get_kis_field(order, "odno", "ODNO")) == order_id:
                            quantity = int(
                                float(
                                    _get_kis_field(
                                        order, "nccs_qty", "NCCS_QTY", default=0
                                    )
                                    or 0
                                )
                            )
                            break

                    result = await kis.cancel_overseas_order(
                        order_number=order_id,
                        symbol=symbol,
                        exchange_code="NASD",
                        quantity=quantity,
                    )
                    return {
                        "success": True,
                        "order_id": order_id,
                        "symbol": symbol,
                        "cancelled_at": result.get("ord_tmd", ""),
                    }
                except Exception as exc:
                    return {
                        "success": False,
                        "order_id": order_id,
                        "symbol": symbol,
                        "error": str(exc),
                    }

            return {
                "success": False,
                "order_id": order_id,
                "error": "Unsupported market type",
            }

        except Exception as exc:
            return {
                "success": False,
                "order_id": order_id,
                "error": str(exc),
            }

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
        if h_price is None:
            h_price = holdings.get("avg_price")
        h_qty = holdings.get("quantity")
        if h_price is None or h_qty is None:
            raise ValueError(
                "holdings must contain 'price' (or 'avg_price') and 'quantity'"
            )
        h_price = float(h_price)
        h_qty = float(h_qty)
        if h_price < 0 or h_qty < 0:
            raise ValueError("holdings price and quantity must be >= 0")

        # --- validate plans ---
        if not plans:
            raise ValueError("plans must contain at least one entry")
        validated_plans: list[tuple[float, float]] = []
        for i, p in enumerate(plans):
            pp = p.get("price")
            if pp is None:
                pp = p.get("avg_price")
            pq = p.get("quantity")
            if pp is None or pq is None:
                raise ValueError(
                    f"plans[{i}] must contain 'price' (or 'avg_price') and 'quantity'"
                )
            pp, pq = float(pp), float(pq)
            if pp <= 0 or pq <= 0:
                raise ValueError(f"plans[{i}] price and quantity must be > 0")
            validated_plans.append((pp, pq))

        mkt = float(current_market_price) if current_market_price is not None else None
        tp = float(target_price) if target_price is not None else None
        if tp is not None and tp <= 0:
            raise ValueError("target_price must be > 0")

        # --- current position ---
        total_qty = h_qty
        total_invested_raw = h_price * h_qty
        avg_price_raw = (total_invested_raw / total_qty) if total_qty > 0 else None
        avg_price = round(avg_price_raw, 2) if avg_price_raw is not None else None

        current_position: dict[str, Any] = {
            "avg_price": avg_price,
            "total_quantity": total_qty,
            "total_invested": round(total_invested_raw, 2),
        }
        if mkt is not None and avg_price is not None:
            pnl = round((mkt - avg_price) * total_qty, 2)
            pnl_pct = round((mkt / avg_price - 1) * 100, 2)
            current_position["unrealized_pnl"] = pnl
            current_position["unrealized_pnl_pct"] = pnl_pct
            current_position["pnl_vs_current"] = pnl
            current_position["pnl_vs_current_pct"] = pnl_pct

        if tp is not None and avg_price is not None:
            projected_profit = round((tp - avg_price) * total_qty, 2)
            target_return_pct = round((tp / avg_price - 1) * 100, 2)
            current_position["target_profit"] = projected_profit
            current_position["target_return_pct"] = target_return_pct

        # --- steps ---
        steps: list[dict[str, Any]] = []
        for idx, (bp, bq) in enumerate(validated_plans, start=1):
            total_invested_raw += bp * bq
            total_qty = round(total_qty + bq, 10)
            avg_price = round(total_invested_raw / total_qty, 2)

            step: dict[str, Any] = {
                "step": idx,
                "buy_price": bp,
                "buy_quantity": bq,
                "new_avg_price": avg_price,
                "total_quantity": total_qty,
                "total_invested": round(total_invested_raw, 2),
            }
            if mkt is not None:
                breakeven_pct = round((avg_price / mkt - 1) * 100, 2)
                pnl = round((mkt - avg_price) * total_qty, 2)
                pnl_pct = round((mkt / avg_price - 1) * 100, 2)
                step["breakeven_change_pct"] = breakeven_pct
                step["unrealized_pnl"] = pnl
                step["unrealized_pnl_pct"] = pnl_pct
                step["pnl_vs_current"] = pnl
                step["pnl_vs_current_pct"] = pnl_pct

            if tp is not None:
                target_profit = round((tp - avg_price) * total_qty, 2)
                target_return_pct = round((tp / avg_price - 1) * 100, 2)
                step["target_profit"] = target_profit
                step["target_return_pct"] = target_return_pct

            steps.append(step)

        # --- target analysis ---
        result: dict[str, Any] = {
            "current_position": current_position,
            "steps": steps,
        }
        if mkt is not None:
            result["current_market_price"] = mkt

        if tp is not None and steps:
            final_avg_price = float(steps[-1]["new_avg_price"])
            profit_per_unit = round(tp - final_avg_price, 2)
            total_profit = round(profit_per_unit * total_qty, 2)
            total_return_pct = round((tp / final_avg_price - 1) * 100, 2)
            result["target_analysis"] = {
                "target_price": tp,
                "final_avg_price": final_avg_price,
                "profit_per_unit": profit_per_unit,
                "total_profit": total_profit,
                "total_return_pct": total_return_pct,
            }

        return result

    @mcp.tool(
        name="update_manual_holdings",
        description=(
            "Update manual holdings from parsed securities app screenshot data. "
            "The LLM should first analyze the screenshot and extract holdings, "
            "then pass the structured data to this tool. "
            "Supports any broker (toss, samsung, etc.) and account type "
            "(기본 계좌, 퇴직연금, ISA). "
            "Each holding needs: stock_name, quantity, eval_amount, profit_loss, "
            "profit_rate, market_section (kr/us), and optional action (upsert/remove). "
            "Uses upsert by default: updates existing and adds new holdings. "
            "Use action='remove' to delete fully sold holdings. "
            "Holdings not in the input are left unchanged (safe for partial screenshots)."
        ),
    )
    async def update_manual_holdings(
        holdings: list[dict[str, Any]],
        broker: str = "toss",
        account_name: str = "기본 계좌",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if not holdings:
            return {
                "success": False,
                "error": "holdings list is required",
                "dry_run": dry_run,
            }

        try:
            async with AsyncSessionLocal() as db:
                service = ScreenshotHoldingsService(db)
                user_id = _env_int("MCP_USER_ID", 1)
                result = await service.resolve_and_update(
                    user_id=user_id,
                    holdings_data=holdings,
                    broker=broker,
                    account_name=account_name,
                    dry_run=dry_run,
                )
                return result
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "dry_run": dry_run,
                "broker": broker,
                "account_name": account_name,
            }

    @mcp.tool(
        name="create_dca_plan",
        description=(
            "Create a Dollar Cost Averaging (DCA) buying plan based on "
            "technical analysis. Uses support/resistance levels and RSI to "
            "determine optimal buying points. dry_run=True by default for safety. "
            "When dry_run=False, executes orders sequentially."
        ),
    )
    async def create_dca_plan(
        symbol: str,
        total_amount: float,
        splits: int = 3,
        strategy: str = "support",
        dry_run: bool = True,
        market: str | None = None,
        execute_steps: list[int] | None = None,
    ) -> dict[str, Any]:
        """Create DCA buying plan with technical analysis.

        Args:
            symbol: Trading symbol (e.g., "KRW-BTC", "005930", "AAPL")
            total_amount: Total amount to invest in market currency (e.g., KRW for KR/crypto, USD for US equities)
            splits: Number of buying steps (default: 3, range: 2-5)
            strategy: Strategy for price levels - "support", "equal", or "aggressive"
            dry_run: Preview only (default: True). Set False to execute orders.
            market: Market hint (optional, auto-detected from symbol)
            execute_steps: List of step numbers to execute (1-indexed, optional).
                If specified, overrides dry_run and executes only these steps.

        Returns:
            Dictionary with success status, plans array, and summary.
            When dry_run=False, includes execution_results for each step.
        """
        # Validation
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if total_amount <= 0:
            raise ValueError("total_amount must be greater than 0")

        if not (2 <= splits <= 5):
            raise ValueError("splits must be between 2 and 5")

        valid_strategies = {"support", "equal", "aggressive"}
        if strategy not in valid_strategies:
            raise ValueError(
                f"Invalid strategy '{strategy}'. Must be one of: {', '.join(sorted(valid_strategies))}"
            )

        if execute_steps is not None:
            invalid_steps = [s for s in execute_steps if not (1 <= s <= splits)]
            if invalid_steps:
                raise ValueError(
                    f"execute_steps must be between 1 and {splits}, got: {invalid_steps}"
                )

        market_type, normalized_symbol = _resolve_market_type(symbol, market)
        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]

        try:
            # Fetch support/resistance and RSI using _impl functions
            _sr_fn = globals().get(
                "_get_support_resistance_impl", _get_support_resistance_impl
            )
            sr_result = await _sr_fn(normalized_symbol, None)
            if "error" in sr_result:
                return {
                    "success": False,
                    "error": sr_result["error"],
                    "source": sr_result.get("source", "get_support_resistance"),
                    "dry_run": dry_run,
                }

            current_price = sr_result.get("current_price")
            supports = sr_result.get("supports", [])

            _ind_fn = globals().get("_get_indicators_impl", _get_indicators_impl)
            indicator_result = await _ind_fn(normalized_symbol, ["rsi"], None)
            if "error" in indicator_result:
                return {
                    "success": False,
                    "error": indicator_result["error"],
                    "source": indicator_result.get("source", "get_indicators"),
                    "dry_run": dry_run,
                }

            rsi_data = indicator_result.get("indicators", {}).get("rsi", {})
            rsi_value = rsi_data.get("14") if rsi_data else None

            # Compute price levels and weights
            price_levels = _compute_dca_price_levels(
                strategy, splits, current_price, supports
            )
            weights = _compute_rsi_weights(rsi_value, splits)

            # Calculate amounts and quantities
            plans: list[dict[str, Any]] = []
            total_quantity = 0.0

            for step, (level, weight) in enumerate(
                zip(price_levels, weights, strict=True), start=1
            ):
                step_amount = total_amount * weight
                step_price = level["price"]
                level_source = level["source"]

                # Apply tick size correction for Korean equity
                if market_type == "equity_kr":
                    original_price = step_price
                    step_price = adjust_tick_size_kr(step_price, "buy")
                    tick_adjusted = True
                else:
                    original_price = None
                    tick_adjusted = False

                # Calculate quantity (truncate for non-crypto)
                if market_type == "crypto":
                    quantity = step_amount / step_price
                else:
                    quantity = int(step_amount / step_price)
                    if quantity == 0:
                        return {
                            "success": False,
                            "error": f"Amount {step_amount:.0f} is insufficient for 1 unit at price {step_price}",
                            "dry_run": dry_run,
                        }

                total_quantity += quantity

                # Calculate distance from current price
                distance_pct = round(
                    (step_price - current_price) / current_price * 100, 2
                )

                plans.append(
                    {
                        "step": step,
                        "price": round(step_price, 2),
                        "distance_pct": distance_pct,
                        "amount": round(step_amount, 0),
                        "quantity": round(quantity, 8)
                        if market_type == "crypto"
                        else quantity,
                        "source": level_source,
                    }
                )

                # Add tick adjustment metadata if applied
                if tick_adjusted and original_price is not None:
                    plans[-1]["original_price"] = round(original_price, 2)
                    plans[-1]["tick_adjusted"] = True

            # Build summary
            avg_target_price = sum(p["price"] for p in plans) / len(plans)
            min_dist = min(p["distance_pct"] for p in plans)
            max_dist = max(p["distance_pct"] for p in plans)

            summary = {
                "symbol": normalized_symbol,
                "current_price": current_price,
                "rsi_14": rsi_value,
                "strategy": strategy,
                "total_amount": total_amount,
                "avg_target_price": round(avg_target_price, 2),
                "total_quantity": (
                    round(total_quantity, 8)
                    if market_type == "crypto"
                    else int(total_quantity)
                ),
                "price_range_pct": f"{min_dist:.2f}% ~ {max_dist:.2f}%",
                "weight_mode": (
                    "front_heavy"
                    if rsi_value is not None and rsi_value < 30
                    else "back_heavy"
                    if rsi_value is not None and rsi_value > 50
                    else "equal"
                ),
            }

            # Execute orders if not dry_run OR if execute_steps is specified
            execution_results: list[dict[str, Any]] = []
            executed_steps: list[int] = []
            should_execute = not dry_run or (execute_steps is not None)

            # Persist DCA plan to DB regardless of dry_run
            plan_id: int | None = None
            created_plan_steps: dict[int, DcaPlanStep] = {}
            try:
                async with AsyncSessionLocal() as db:
                    dca_service = DcaService(db)

                    # Convert plans to the format DcaService expects
                    plans_for_db = [
                        {
                            "step": p["step"],
                            "price": p["price"],
                            "amount": p["amount"],
                            "quantity": p["quantity"],
                            "source": p.get("source"),
                        }
                        for p in plans
                    ]

                    created_plan = await dca_service.create_plan(
                        user_id=_MCP_DCA_USER_ID,
                        symbol=normalized_symbol,
                        market=market_type,
                        total_amount=total_amount,
                        splits=splits,
                        strategy=strategy,
                        plans_data=plans_for_db,
                        rsi_14=rsi_value,
                    )

                    plan_id = created_plan.id
                    # Re-fetch plan from DB to ensure steps are properly loaded
                    try:
                        async with AsyncSessionLocal() as db:
                            dca_service = DcaService(db)
                            reloaded_plan = await dca_service.get_plan(
                                plan_id, _MCP_DCA_USER_ID
                            )
                            if not reloaded_plan:
                                raise ValueError(
                                    f"Plan {plan_id} not found after creation"
                                )
                            for step in reloaded_plan.steps or []:
                                created_plan_steps[step.step_number] = step
                    except Exception as reload_exc:
                        logger.error(f"Failed to reload DCA plan: {reload_exc}")
                        return {
                            "success": False,
                            "error": f"Failed to reload DCA plan: {reload_exc}",
                            "dry_run": not should_execute,
                            "executed": False,
                            "plan_id": plan_id,
                        }
            except Exception as exc:
                logger.error(f"Failed to persist DCA plan: {exc}")
                # Per policy: persist failure -> fail early (do not execute orders)
                return {
                    "success": False,
                    "error": f"Failed to persist DCA plan: {exc}",
                    "dry_run": not should_execute,
                    "executed": False,
                    "plan_id": None,
                }

            if should_execute:
                for plan_step in plans:
                    if (
                        execute_steps is not None
                        and plan_step["step"] not in execute_steps
                    ):
                        continue

                    order_amount = plan_step["amount"]
                    order_price = plan_step["price"]

                    # Safety check: max 1M KRW per step
                    if order_amount > 1_000_000:
                        return {
                            "success": False,
                            "error": f"Step {plan_step['step']} amount {order_amount:.0f} KRW exceeds limit 1,000,000 KRW",
                            "dry_run": not should_execute,
                            "executed": bool(executed_steps),
                            "plan_id": plan_id,
                            "summary": summary,
                        }

                    # Place order using _impl function
                    _po_fn = globals().get("_place_order_impl", _place_order_impl)
                    order_result = await _po_fn(
                        symbol=normalized_symbol,
                        side="buy",
                        order_type="limit",
                        amount=order_amount,
                        price=order_price,
                        dry_run=False,
                        reason=f"DCA plan step {plan_step['step']}/{splits}",
                    )

                    execution_results.append(
                        {
                            "step": plan_step["step"],
                            "success": order_result.get("success", False),
                            "result": order_result,
                        }
                    )
                    executed_steps.append(plan_step["step"])

                    # If order succeeded, mark step as ordered with order_id
                    if order_result.get("success") and plan_id is not None:
                        order_id = None
                        # Extract order_id from result with priority order
                        if "order_id" in order_result:
                            order_id = order_result["order_id"]
                        elif "execution" in order_result and isinstance(
                            order_result["execution"], dict
                        ):
                            order_id = (
                                order_result["execution"].get("uuid")
                                or order_result["execution"].get("ord_no")
                                or order_result["execution"].get("odno")
                            )

                        if order_id and plan_step["step"] in created_plan_steps:
                            try:
                                async with AsyncSessionLocal() as db:
                                    dca_service = DcaService(db)
                                    step = created_plan_steps[plan_step["step"]]
                                    await dca_service.mark_step_ordered(
                                        step.id, str(order_id)
                                    )
                            except Exception as exc:
                                logger.error(f"Failed to mark step ordered: {exc}")
                                return {
                                    "success": False,
                                    "error": f"Failed to mark step ordered: {exc}",
                                    "dry_run": not should_execute,
                                    "executed": bool(executed_steps),
                                    "plan_id": plan_id,
                                    "execution_results": execution_results,
                                    "summary": summary,
                                }
                        elif order_id:
                            logger.error(
                                f"Step {plan_step['step']} not found in plan {plan_id} - "
                                f"available steps: {list(created_plan_steps.keys())}"
                            )
                            return {
                                "success": False,
                                "error": f"Step {plan_step['step']} not found in plan {plan_id}",
                                "dry_run": not should_execute,
                                "executed": bool(executed_steps),
                                "plan_id": plan_id,
                                "execution_results": execution_results,
                                "summary": summary,
                            }

                    # Fail early if any order fails
                    if not order_result.get("success"):
                        return {
                            "success": False,
                            "error": f"Order failed at step {plan_step['step']}",
                            "failed_step": plan_step["step"],
                            "dry_run": not should_execute,
                            "executed": bool(executed_steps),
                            "plan_id": plan_id,
                            "execution_results": execution_results,
                            "summary": summary,
                        }

            response: dict[str, Any] = {
                "success": True,
                "dry_run": not should_execute,
                "executed": bool(executed_steps),
                "plan_id": plan_id,
                "plans": plans,
                "summary": summary,
            }

            if should_execute:
                response["execution_results"] = execution_results

            if should_execute and executed_steps:
                response["executed_steps"] = executed_steps

            return response

        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "source": source,
                "dry_run": dry_run,
            }

    async def _get_dca_status_impl(
        plan_id: int | None = None,
        symbol: str | None = None,
        status: str = "active",
        limit: int = 10,
    ) -> dict[str, Any]:
        """Internal implementation for get_dca_status.

        Args:
            plan_id: Specific plan ID (has priority)
            symbol: Filter by symbol (combined with optional status)
            status: Filter by status - "active", "completed", "cancelled", "expired", "all"
            limit: Max number of plans to return (1-1000)

        Returns:
            Dictionary with success status, plans array, and total_plans count.
        """
        # Validate status
        valid_statuses = {"active", "completed", "cancelled", "expired", "all"}
        if status not in valid_statuses:
            return {
                "success": False,
                "error": (
                    f"Invalid status '{status}'. Must be one of: "
                    f"{', '.join(sorted(valid_statuses))}"
                ),
                "plans": [],
                "total_plans": 0,
            }

        # Validate limit
        if limit < 1 or limit > 1000:
            return {
                "success": False,
                "error": f"limit must be between 1 and 1000, got: {limit}",
                "plans": [],
                "total_plans": 0,
            }

        try:
            async with AsyncSessionLocal() as db:
                dca_service = DcaService(db)

                plans: list[DcaPlan] = []

                # Filter priority: plan_id > symbol > status
                if plan_id is not None:
                    # Get single plan by ID
                    plan = await dca_service.get_plan(plan_id, _MCP_DCA_USER_ID)
                    if plan:
                        plans = [plan]
                elif symbol is not None:
                    # Get plans for symbol with optional status filter
                    symbol = symbol.strip()
                    if status == "all":
                        # Get all plans for symbol regardless of status
                        all_plans = await dca_service.get_plans_by_status(
                            user_id=_MCP_DCA_USER_ID,
                            symbol=symbol,
                            status=None,
                            limit=limit,
                        )
                        plans = all_plans
                    else:
                        # Get plans for symbol with specific status
                        filtered_plans = await dca_service.get_plans_by_status(
                            user_id=_MCP_DCA_USER_ID,
                            symbol=symbol,
                            status=status,
                            limit=limit,
                        )
                        plans = filtered_plans
                else:
                    # Get plans by status (all symbols for this status)
                    if status == "all":
                        # Get all plans for user
                        all_plans = await dca_service.get_plans_by_status(
                            user_id=_MCP_DCA_USER_ID,
                            status=None,
                            limit=limit,
                        )
                        plans = all_plans
                    else:
                        # Get plans with specific status
                        filtered_plans = await dca_service.get_plans_by_status(
                            user_id=_MCP_DCA_USER_ID,
                            status=status,
                            limit=limit,
                        )
                        plans = filtered_plans

                # Format plans for response
                def _format_dca_plan(plan: DcaPlan) -> dict[str, Any]:
                    """Format a DcaPlan (and its steps) into JSON-serializable dict with progress."""
                    p: dict[str, Any] = {
                        "plan_id": plan.id,
                        "id": plan.id,
                        "user_id": plan.user_id,
                        "symbol": plan.symbol,
                        "market": plan.market,
                        "status": plan.status.value
                        if hasattr(plan.status, "value")
                        else str(plan.status),
                        "total_amount": float(plan.total_amount)
                        if getattr(plan, "total_amount", None) is not None
                        else None,
                        "splits": plan.splits,
                        "strategy": plan.strategy,
                        "rsi_14": float(plan.rsi_14)
                        if plan.rsi_14 is not None
                        else None,
                        "created_at": plan.created_at.isoformat()
                        if plan.created_at
                        else None,
                        "updated_at": plan.updated_at.isoformat()
                        if plan.updated_at
                        else None,
                        "completed_at": plan.completed_at.isoformat()
                        if plan.completed_at
                        else None,
                    }

                    steps_list: list[dict[str, Any]] = []
                    total_steps = 0
                    counts = {
                        "filled": 0,
                        "ordered": 0,
                        "pending": 0,
                        "cancelled": 0,
                        "partial": 0,
                        "skipped": 0,
                    }
                    invested = 0.0
                    filled_qty_total = 0.0
                    filled_price_weighted = 0.0

                    if hasattr(plan, "steps") and plan.steps:
                        for step in plan.steps:
                            total_steps += 1
                            status_name = (
                                step.status.value
                                if hasattr(step.status, "value")
                                else str(step.status)
                            )
                            if status_name == DcaStepStatus.FILLED.value:
                                counts["filled"] += 1
                            elif status_name == DcaStepStatus.ORDERED.value:
                                counts["ordered"] += 1
                            elif status_name == DcaStepStatus.PENDING.value:
                                counts["pending"] += 1
                            elif status_name == DcaStepStatus.CANCELLED.value:
                                counts["cancelled"] += 1
                            elif status_name == DcaStepStatus.PARTIAL.value:
                                counts["partial"] += 1
                            elif status_name == DcaStepStatus.SKIPPED.value:
                                counts["skipped"] += 1

                            filled_amount = (
                                float(step.filled_amount)
                                if getattr(step, "filled_amount", None) is not None
                                else 0.0
                            )
                            filled_qty = (
                                float(step.filled_quantity)
                                if getattr(step, "filled_quantity", None) is not None
                                else 0.0
                            )
                            filled_price = (
                                float(step.filled_price)
                                if getattr(step, "filled_price", None) is not None
                                else None
                            )

                            invested += filled_amount
                            if filled_qty and filled_price is not None:
                                filled_qty_total += filled_qty
                                filled_price_weighted += filled_price * filled_qty

                            ordered_at_val = getattr(step, "ordered_at", None)
                            filled_at_val = getattr(step, "filled_at", None)
                            steps_list.append(
                                {
                                    "id": step.id,
                                    "plan_id": step.plan_id,
                                    "step": step.step_number,
                                    "step_number": step.step_number,
                                    "target_price": float(step.target_price)
                                    if getattr(step, "target_price", None) is not None
                                    else None,
                                    "target_amount": float(step.target_amount)
                                    if getattr(step, "target_amount", None) is not None
                                    else None,
                                    "target_quantity": float(step.target_quantity)
                                    if getattr(step, "target_quantity", None)
                                    is not None
                                    else None,
                                    "status": status_name,
                                    "order_id": step.order_id,
                                    "ordered_at": ordered_at_val.isoformat()
                                    if ordered_at_val is not None
                                    else None,
                                    "filled_price": float(step.filled_price)
                                    if getattr(step, "filled_price", None) is not None
                                    else None,
                                    "filled_quantity": float(step.filled_quantity)
                                    if getattr(step, "filled_quantity", None)
                                    is not None
                                    else None,
                                    "filled_amount": float(step.filled_amount)
                                    if getattr(step, "filled_amount", None) is not None
                                    else None,
                                    "filled_at": filled_at_val.isoformat()
                                    if filled_at_val is not None
                                    else None,
                                    "level_source": getattr(step, "level_source", None),
                                }
                            )

                    # Progress calculations
                    avg_filled_price = None
                    if filled_qty_total > 0:
                        avg_filled_price = filled_price_weighted / filled_qty_total

                    remaining = None
                    if p.get("total_amount") is not None:
                        remaining = float(p["total_amount"]) - invested

                    p["steps"] = steps_list
                    p["progress"] = {
                        "total_steps": total_steps,
                        "filled": counts["filled"],
                        "ordered": counts["ordered"],
                        "pending": counts["pending"],
                        "cancelled": counts["cancelled"],
                        "partial": counts["partial"],
                        "skipped": counts["skipped"],
                        "invested": round(invested, 2),
                        "remaining": round(remaining, 2)
                        if remaining is not None
                        else None,
                        "avg_filled_price": round(avg_filled_price, 8)
                        if avg_filled_price is not None
                        else None,
                    }

                    return p

                formatted_plans = [_format_dca_plan(plan) for plan in plans]

                return {
                    "success": True,
                    "plans": formatted_plans,
                    "total_plans": len(formatted_plans),
                }

        except ValueError as ve:
            return {
                "success": False,
                "error": str(ve),
                "plans": [],
                "total_plans": 0,
            }
        except Exception as exc:
            logger.error(f"Error fetching DCA status: {exc}")
            return {
                "success": False,
                "error": str(exc),
                "plans": [],
                "total_plans": 0,
            }

    @mcp.tool(
        name="get_dca_status",
        description=(
            "Get status of DCA (Dollar Cost Averaging) plans. "
            "Supports filtering by: plan_id (exact match), "
            "symbol + status (for symbol's plans), or just status. "
            "Response always includes total_plans count."
        ),
    )
    async def get_dca_status(
        plan_id: int | None = None,
        symbol: str | None = None,
        status: str = "active",
        limit: int = 10,
    ) -> dict[str, Any]:
        """Get DCA plan status.

        Args:
            plan_id: Specific plan ID (has priority)
            symbol: Filter by symbol (combined with optional status)
            status: Filter by status - "active", "completed", "cancelled", "expired", "all"
            limit: Max number of plans to return (1-1000)

        Returns:
            Dictionary with success status, plans array, and total_plans count.
        """
        _impl = globals().get("_get_dca_status_impl", _get_dca_status_impl)
        return await _impl(plan_id, symbol, status, limit)

    async def _get_quote_impl(symbol: str, market_type: str) -> dict[str, Any] | None:
        """Fetch quote data for any market type."""
        if market_type == "crypto":
            return await _fetch_quote_crypto(symbol)
        elif market_type == "equity_kr":
            return await _fetch_quote_equity_kr(symbol)
        elif market_type == "equity_us":
            return await _fetch_quote_equity_us(symbol)
        return None

    async def _analyze_stock_impl(
        symbol: str,
        market: str | None = None,
        include_peers: bool = False,
    ) -> dict[str, Any]:
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        market_type, normalized_symbol = _resolve_market_type(symbol, market)
        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]

        errors: list[str] = []
        analysis: dict[str, Any] = {
            "symbol": normalized_symbol,
            "market_type": market_type,
            "source": source,
        }

        tasks: list[asyncio.Task[Any]] = []

        quote_task = asyncio.create_task(
            _get_quote_impl(normalized_symbol, market_type),
        )
        tasks.append(quote_task)

        indicators_task = asyncio.create_task(
            _get_indicators_impl(
                normalized_symbol, ["rsi", "macd", "bollinger", "sma"], None
            ),
        )
        tasks.append(indicators_task)

        sr_task = asyncio.create_task(
            _get_support_resistance_impl(normalized_symbol, None),
        )
        tasks.append(sr_task)

        if market_type == "equity_kr":
            valuation_task = asyncio.create_task(
                _fetch_valuation_naver(normalized_symbol),
            )
            tasks.append(valuation_task)

            news_task = asyncio.create_task(
                _fetch_news_naver(normalized_symbol, 5),
            )
            tasks.append(news_task)

            opinions_task = asyncio.create_task(
                _fetch_investment_opinions_naver(normalized_symbol, 10),
            )
            tasks.append(opinions_task)

        elif market_type == "equity_us":
            valuation_task = asyncio.create_task(
                _fetch_valuation_yfinance(normalized_symbol),
            )
            tasks.append(valuation_task)

            profile_task = asyncio.create_task(
                _fetch_company_profile_finnhub(normalized_symbol),
            )
            tasks.append(profile_task)

            news_task = asyncio.create_task(
                _fetch_news_finnhub(normalized_symbol, "us", 5),
            )
            tasks.append(news_task)

            opinions_task = asyncio.create_task(
                _fetch_investment_opinions_yfinance(normalized_symbol, 10),
            )
            tasks.append(opinions_task)

        elif market_type == "crypto":
            news_task = asyncio.create_task(
                _fetch_news_finnhub(normalized_symbol, "crypto", 5),
            )
            tasks.append(news_task)

        if include_peers and market_type != "crypto":
            if market_type == "equity_kr":
                peers_task = asyncio.create_task(
                    _fetch_sector_peers_naver(normalized_symbol, 10),
                )
            else:
                peers_task = asyncio.create_task(
                    _fetch_sector_peers_us(normalized_symbol, 10),
                )
            tasks.append(peers_task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        quote = None
        if not isinstance(results[0], Exception):
            quote = results[0]

        indicators = None
        if not isinstance(results[1], Exception) and len(results) > 1:
            indicators = results[1]

        support_resistance = None
        if not isinstance(results[2], Exception) and len(results) > 2:
            support_resistance = results[2]

        if quote:
            analysis["quote"] = quote

        if indicators:
            analysis["indicators"] = indicators

        if support_resistance:
            analysis["support_resistance"] = support_resistance

        task_idx = 3
        if market_type == "equity_kr":
            if not isinstance(results[task_idx], Exception):
                analysis["valuation"] = results[task_idx]
            task_idx += 1

            if not isinstance(results[task_idx], Exception):
                analysis["news"] = results[task_idx]
            task_idx += 1

            if not isinstance(results[task_idx], Exception):
                analysis["opinions"] = results[task_idx]
            task_idx += 1

        elif market_type == "equity_us":
            if not isinstance(results[task_idx], Exception):
                analysis["valuation"] = results[task_idx]
            task_idx += 1

            if not isinstance(results[task_idx], Exception):
                analysis["profile"] = results[task_idx]
            task_idx += 1

            if not isinstance(results[task_idx], Exception):
                analysis["news"] = results[task_idx]
            task_idx += 1

            if not isinstance(results[task_idx], Exception):
                analysis["opinions"] = results[task_idx]
            task_idx += 1

        elif market_type == "crypto":
            if not isinstance(results[task_idx], Exception):
                analysis["news"] = results[task_idx]

        if include_peers and market_type != "crypto":
            if not isinstance(results[task_idx], Exception):
                analysis["sector_peers"] = results[task_idx]

        if errors:
            analysis["errors"] = errors
        else:
            analysis["errors"] = []

        return analysis

    @mcp.tool(
        name="analyze_stock",
        description=(
            "Comprehensive stock analysis tool. Fetches quote, indicators (RSI, MACD, BB, SMA), "
            "support/resistance, and market-specific data in parallel. "
            "For Korean stocks: valuation (Naver), news (Naver), opinions (Naver). "
            "For US stocks: valuation (yfinance), profile (Finnhub), news (Finnhub), opinions (yfinance). "
            "For crypto: news (Finnhub). "
            "Optionally includes sector peers. Returns errors array for failed sections."
        ),
    )
    async def analyze_stock(
        symbol: str,
        market: str | None = None,
        include_peers: bool = False,
    ) -> dict[str, Any]:
        """Comprehensive stock analysis with parallel data fetching.

        Args:
            symbol: Stock symbol (e.g., "005930", "AAPL", "KRW-BTC")
            market: Market type - "kr", "us", or "crypto" (auto-detected if not specified)
            include_peers: If True, include sector/industry peers comparison

        Returns:
            Dictionary with comprehensive analysis including quote, indicators, valuation,
            news, opinions, and optional sector peers. Failed sections are
            tracked in errors array.
        """
        return await globals()["_analyze_stock_impl"](symbol, market, include_peers)

    @mcp.tool(
        name="analyze_portfolio",
        description=(
            "Analyze multiple stocks in parallel. "
            "Returns individual analysis for each symbol plus portfolio summary. "
            "Maximum 5 concurrent analyses."
        ),
    )
    async def analyze_portfolio(
        symbols: list[str],
        market: str | None = None,
        include_peers: bool = False,
    ) -> dict[str, Any]:
        """Analyze multiple stocks in parallel with portfolio-level summary.

        Args:
            symbols: List of stock symbols to analyze (e.g., ["005930", "AAPL", "KRW-BTC"])
            market: Market hint (optional, auto-detected from symbols)
            include_peers: Include sector/industry peer comparison (optional, default False)

        Returns:
            Dictionary with individual results and portfolio summary.
        """
        if not symbols:
            raise ValueError("symbols must contain at least one entry")

        if len(symbols) > 10:
            raise ValueError("symbols must contain at most 10 entries")

        results: dict[str, Any] = {}
        errors: list[str] = []
        sem = asyncio.Semaphore(5)

        async def _analyze_one(sym: str) -> dict[str, Any]:
            async with sem:
                try:
                    return await globals()["_analyze_stock_impl"](
                        sym, market, include_peers
                    )
                except Exception as exc:
                    errors.append(f"{sym}: {str(exc)}")
                    return {"symbol": sym, "error": str(exc)}

        analyze_results = await asyncio.gather(*[_analyze_one(s) for s in symbols])

        success_count = 0
        fail_count = 0
        for sym, result in zip(symbols, analyze_results, strict=True):
            results[sym] = result
            if "error" not in result:
                success_count += 1
            else:
                fail_count += 1

        portfolio_summary = {
            "total_symbols": len(symbols),
            "successful": success_count,
            "failed": fail_count,
            "errors": errors,
        }

        return {
            "results": results,
            "summary": portfolio_summary,
        }

    @mcp.tool(
        name="get_disclosures",
        description=(
            "Get DART (OPENDART) disclosure filings for Korean corporations. "
            "Supports both 6-digit corp codes (e.g., '005930') and Korean company names (e.g., '삼성전자'). "
            "Returns filing date, report name, report number, and corporation name. "
            "Default lookback period: 30 days, default limit: 20 filings. "
            "Filter by report type: '정기', '주요사항', '발행', '지분', '기타'."
        ),
    )
    async def get_disclosures(
        symbol: str,
        days: int = 30,
        limit: int = 20,
        report_type: str | None = None,
    ) -> dict[str, Any]:
        """Get DART disclosure filings for a Korean corporation.

        Args:
            symbol: Korean company name or 6-digit corp code (e.g., '005930' or '삼성전자')
            days: Number of days to look back (default: 30, max: 365)
            limit: Maximum number of filings to return (default: 20, max: 100)
            report_type: Filter by report type ("정기", "주요사항", "발행", "지분", "기타")

        Returns:
            Dictionary with filings data or error message.
        """
        try:
            result = await list_filings(symbol, days, limit, report_type)
            # list_filings returns list[dict] or dict[str, Any], normalize to dict
            if isinstance(result, list):
                return {"success": True, "filings": result}
            return result
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "symbol": symbol,
            }

    @mcp.tool(
        name="get_correlation",
        description=(
            "Calculate Pearson correlation matrix between multiple assets. "
            "Supports Korean stocks (KIS), US stocks (yfinance), and crypto (Upbit). "
            "Uses daily closing prices over specified period."
        ),
    )
    async def get_correlation(
        symbols: list[str],
        period: int = 60,
    ) -> dict[str, Any]:
        """Calculate correlation matrix between multiple assets.

        Args:
            symbols: List of trading symbols (e.g., ["005930", "AAPL", "KRW-BTC"])
            period: Number of days to use for calculation (default: 60)

        Returns:
            Dictionary with correlation matrix and metadata.
        """
        if not symbols or len(symbols) < 2:
            raise ValueError("symbols must contain at least 2 assets")

        if len(symbols) > 10:
            raise ValueError("Maximum 10 symbols supported for correlation calculation")

        period = max(period, 30)
        if period > 365:
            raise ValueError("period must be between 30 and 365 days")

        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}

        errors: list[str] = []
        price_data: dict[str, list[float]] = {}
        market_types: dict[str, str] = {}

        async def fetch_prices(symbol: str) -> None:
            """Fetch daily closing prices for a symbol."""
            try:
                market_type, normalized_symbol = _resolve_market_type(symbol, None)
                market_types[normalized_symbol] = market_type

                df = await _fetch_ohlcv_for_indicators(
                    normalized_symbol, market_type, count=period
                )
                if df.empty:
                    raise ValueError(f"No data available for symbol '{symbol}'")

                if "close" not in df.columns:
                    raise ValueError(f"Missing close price data for symbol '{symbol}'")

                prices = df["close"].tolist()
                price_data[normalized_symbol] = prices
            except Exception as exc:
                errors.append(f"{symbol}: {str(exc)}")

        await asyncio.gather(*[fetch_prices(sym) for sym in symbols])

        if len(price_data) < 2:
            return {
                "success": False,
                "error": "Insufficient data to calculate correlation (need at least 2 symbols)",
            }

        correlation_matrix: list[list[float]] = []

        sorted_symbols = sorted(price_data.keys())

        for i, sym_a in enumerate(sorted_symbols):
            row: list[float] = []
            prices_a = price_data[sym_a]
            min_len = len(prices_a)

            for j, sym_b in enumerate(sorted_symbols):
                prices_b = price_data[sym_b]
                actual_len = min(len(prices_b), min_len)

                corr = 0.0
                if i <= j:
                    truncated_a = prices_a[-actual_len:]
                    truncated_b = prices_b[-actual_len:]
                    corr = (
                        _calculate_pearson_correlation(truncated_a, truncated_b)
                        if len(truncated_a) >= 2
                        else 0.0
                    )
                else:
                    corr = correlation_matrix[j][i]

                row.append(corr)
            correlation_matrix.append(row)

        metadata = {
            "period_days": period,
            "symbols": sorted_symbols,
            "market_types": {
                sym: market_types.get(sym, "unknown") for sym in sorted_symbols
            },
            "sources": {
                sym: source_map.get(market_types.get(sym, "equity_us"), "unknown")
                for sym in sorted_symbols
            },
        }

        if errors:
            return {
                "success": True,
                "correlation_matrix": correlation_matrix,
                "symbols": sorted_symbols,
                "metadata": metadata,
                "errors": errors,
            }

        return {
            "success": True,
            "correlation_matrix": correlation_matrix,
            "symbols": sorted_symbols,
            "metadata": metadata,
        }

    def _calculate_pearson_correlation(x: list[float], y: list[float]) -> float:
        """Calculate Pearson correlation coefficient between two lists."""
        n = len(x)
        if n != len(y) or n < 2:
            return 0.0

        sum_x = sum(x)
        sum_y = sum(y)
        sum_xy = sum(xi * yi for xi, yi in zip(x, y, strict=True))
        sum_x2 = sum(xi**2 for xi in x)
        sum_y2 = sum(yi**2 for yi in y)

        numerator = n * sum_xy - sum_x * sum_y
        denominator_x = n * sum_x2 - sum_x**2
        denominator_y = n * sum_y2 - sum_y**2

        denominator = (denominator_x * denominator_y) ** 0.5

        if denominator == 0:
            return 0.0

        return numerator / denominator

    @mcp.tool(
        name="get_top_stocks",
        description=(
            "Get top stocks by ranking type across different markets (KR/US/Crypto). "
            "KR: volume, market_cap, gainers, losers, foreigners "
            "US: volume, market_cap, gainers, losers "
            "Crypto: volume, gainers, losers. "
            "Supports asset_type filter for KR (stock/etf)."
        ),
    )
    async def get_top_stocks(
        market: str = "kr",
        ranking_type: str = "volume",
        asset_type: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        market = (market or "").strip().lower()
        ranking_type = (ranking_type or "").strip().lower()

        limit_clamped = max(1, min(limit, 50))

        supported_combinations = {
            ("kr", "volume"),
            ("kr", "market_cap"),
            ("kr", "gainers"),
            ("kr", "losers"),
            ("kr", "foreigners"),
            ("us", "volume"),
            ("us", "market_cap"),
            ("us", "gainers"),
            ("us", "losers"),
            ("crypto", "volume"),
            ("crypto", "gainers"),
            ("crypto", "losers"),
        }

        key = (market, ranking_type)
        if key not in supported_combinations:
            return _error_payload(
                source="validation",
                message=f"Unsupported combination: market={market}, ranking_type={ranking_type}",
                query=f"market={market}, ranking_type={ranking_type}",
            )

        asset_type_normalized = None
        if asset_type is not None:
            asset_type_normalized = asset_type.strip().lower()
            if asset_type_normalized not in ("stock", "etf"):
                return _error_payload(
                    source="validation",
                    message=f"asset_type must be 'stock' or 'etf', got '{asset_type}'",
                    query=f"asset_type={asset_type}",
                )

        fetch_limit = limit_clamped
        if asset_type_normalized is not None:
            fetch_limit = min(limit_clamped * 3, 50)

        rankings: list[dict[str, Any]] = []
        source = {
            "kr": "kis",
            "us": "yfinance",
            "crypto": "upbit",
        }.get(market, "")

        try:
            if market == "kr":
                kis = KISClient()

                if ranking_type == "volume":
                    data = await kis.volume_rank(market="J", limit=fetch_limit)
                    source = "kis"
                elif ranking_type == "market_cap":
                    data = await kis.market_cap_rank(market="J", limit=fetch_limit)
                    source = "kis"
                elif ranking_type in ("gainers", "losers"):
                    direction = "up" if ranking_type == "gainers" else "down"
                    data = await kis.fluctuation_rank(
                        market="J", direction=direction, limit=fetch_limit
                    )
                    source = "kis"
                elif ranking_type == "foreigners":
                    data = await kis.foreign_buying_rank(market="J", limit=fetch_limit)
                    source = "kis"
                else:
                    data = []

                filtered_rank = 1
                for row in data[:fetch_limit]:
                    if asset_type_normalized is not None:
                        symbol = row.get("stck_shrn_iscd") or row.get(
                            "mksc_shrn_iscd", ""
                        )
                        row_asset_type = _classify_kr_asset_type(
                            symbol, row.get("hts_kor_isnm", "")
                        )
                        if row_asset_type != asset_type_normalized:
                            continue

                    mapped = _map_kr_row(row, filtered_rank)
                    rankings.append(mapped)
                    filtered_rank += 1
                    if len(rankings) >= limit_clamped:
                        break

            elif market == "us":
                rankings, source = await _get_us_rankings(ranking_type, limit_clamped)

            elif market == "crypto":
                rankings, source = await _get_crypto_rankings(
                    ranking_type, limit_clamped
                )

            else:
                return _error_payload(
                    source="validation",
                    message=f"Unsupported market: {market}",
                    query=f"market={market}",
                )

        except Exception as exc:
            return _error_payload(
                source=source,
                message=str(exc),
            )

        kst_tz = datetime.timezone(datetime.timedelta(hours=9))
        return {
            "rankings": rankings,
            "total_count": len(rankings),
            "market": market,
            "ranking_type": ranking_type,
            "asset_type": asset_type,
            "timestamp": datetime.datetime.now(kst_tz).isoformat(),
            "source": source,
        }

    @mcp.tool(
        name="get_dividends",
        description=(
            "Get dividend information for US stocks (via yfinance). "
            "Returns dividend yield, payout date, and 52-week high/low."
        ),
    )
    async def get_dividends(
        symbol: str,
    ) -> dict[str, Any]:
        """Get dividend information for a US stock.

        Args:
            symbol: US stock symbol (e.g., "AAPL", "MSFT")

        Returns:
            Dictionary with dividend data.
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        ticker = yf.Ticker(symbol.upper())

        def fetch_sync() -> dict[str, Any]:
            try:
                info = ticker.info or {}

                dividend_yield = info.get("dividendYield")
                dividend_rate = info.get("dividendRate")
                ex_date = info.get("exDividendDate")

                divs = ticker.dividends
                last_div = None
                if divs is not None and not divs.empty:
                    last_date = divs.index[-1]
                    last_div = {
                        "date": last_date.strftime("%Y-%m-%d"),
                        "amount": float(divs.iloc[-1]),
                    }

                return {
                    "success": True,
                    "symbol": symbol.upper(),
                    "dividend_yield": round(dividend_yield, 4)
                    if dividend_yield
                    else None,
                    "dividend_rate": float(dividend_rate) if dividend_rate else None,
                    "ex_dividend_date": (
                        datetime.datetime.fromtimestamp(ex_date).strftime("%Y-%m-%d")
                        if ex_date
                        else None
                    ),
                    "last_dividend": last_div,
                }
            except Exception as exc:
                return {
                    "success": False,
                    "error": str(exc),
                    "symbol": symbol.upper(),
                }

        return await asyncio.to_thread(fetch_sync)

    @mcp.tool(
        name="get_fear_greed_index",
        description=(
            "Get the Crypto Fear & Greed Index from Alternative.me. "
            "Returns current value (0=Extreme Fear, 100=Extreme Greed), "
            "classification, and historical values. Useful for gauging "
            "overall crypto market sentiment."
        ),
    )
    async def get_fear_greed_index(days: int = 7) -> dict[str, Any]:
        """Get the Crypto Fear & Greed Index.

        Args:
            days: Number of days of historical data to fetch (1-365, default: 7)

        Returns:
            Dictionary with current index value, classification, and history.
        """
        # Validate and clamp days parameter
        capped_days = min(max(days, 1), 365)

        url = "https://api.alternative.me/fng/"
        params = {"limit": capped_days}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()

            if not data or "data" not in data:
                return _error_payload(
                    source="alternative.me",
                    message="No data received from Fear & Greed API",
                )

            history_data = data["data"]

            if not history_data:
                return _error_payload(
                    source="alternative.me",
                    message="Empty data array received from Fear & Greed API",
                )

            # Parse current value (first item is most recent)
            current = history_data[0]
            current_value = int(current["value"])
            current_classification = current["value_classification"]
            current_timestamp = current["timestamp"]
            current_date = (
                datetime.datetime.fromtimestamp(int(current_timestamp)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if current_timestamp
                else "Unknown"
            )

            # Parse history
            history = []
            for item in history_data:
                value = int(item["value"])
                classification = item["value_classification"]
                timestamp = item["timestamp"]
                date = (
                    datetime.datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d")
                    if timestamp
                    else "Unknown"
                )
                history.append(
                    {
                        "date": date,
                        "value": value,
                        "classification": classification,
                    }
                )

            return {
                "success": True,
                "source": "alternative.me",
                "current": {
                    "value": current_value,
                    "classification": current_classification,
                    "date": current_date,
                },
                "history": history,
            }

        except httpx.HTTPStatusError as exc:
            return _error_payload(
                source="alternative.me",
                message=f"HTTP error: {exc}",
            )
        except Exception as exc:
            return _error_payload(
                source="alternative.me",
                message=str(exc),
            )

    @mcp.tool(
        name="get_order_history",
        description=(
            "Get order history for a symbol. "
            "Supports Upbit (crypto) and KIS (KR/US equities). "
            "Returns normalized order information with summary statistics."
        ),
    )
    async def get_order_history(
        symbol: str | None = None,
        status: Literal["all", "pending", "filled", "cancelled"] = "all",
        order_id: str | None = None,
        market: str | None = None,
        side: str | None = None,
        days: int | None = None,
        limit: int | None = 50,
    ) -> dict[str, Any]:
        """Get order history or open orders.

        Args:
            symbol: Trading symbol (e.g., "KRW-BTC", "005930", "AAPL"). Required unless status="pending".
            status: Filter by order status ("all", "pending", "filled", "cancelled").
            order_id: Filter by specific order ID.
            market: Market hint (kr, us, crypto).
            side: Filter by side (buy, sell).
            days: Number of days to look back (default: 7).
            limit: Max orders (default: 50). 0 or -1 for unlimited.

        Returns:
            Dictionary with 'orders' list and metadata.
        """
        if status != "pending" and not symbol:
            raise ValueError(
                f"symbol is required when status='{status}'. "
                f"Use status='pending' for symbol-free queries, or provide a symbol (e.g. symbol='KRW-BTC')."
            )

        symbol = (symbol or "").strip() or None
        order_id = (order_id or "").strip() or None
        market_hint = (market or "").strip().lower() or None
        side = (side or "").strip().lower() or None

        if side and side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")

        if limit is None:
            limit = 50
        elif limit < -1:
            raise ValueError("limit must be >= -1")

        # 0 or -1 means unlimited
        limit_val = limit if limit not in (0, -1) else float("inf")

        # Date range handling (optional)
        # If days is None, we generally don't restrict.
        # But specific APIs (like KIS) might require a range.
        effective_days = days

        # --- Market Resolution ---
        market_types = []
        normalized_symbol = None

        if symbol:
            market_type, normalized_symbol = _resolve_market_type(symbol, market_hint)
            market_types = [market_type]
        elif market_hint:
            norm = _normalize_market(market_hint)
            if norm:
                market_types = [norm]

        # If still unknown, and status is pending (so symbol might be None), try all
        if not market_types and status == "pending":
            market_types = ["crypto", "equity_kr", "equity_us"]

        # If order_id provided but no symbol/market, we might need a general search or guess
        if not market_types and order_id:
            # Heuristic: UUID implies crypto
            if "-" in order_id and len(order_id) == 36:
                market_types = ["crypto"]
            else:
                # Default to checking all if we can't tell
                market_types = ["crypto", "equity_kr", "equity_us"]

        orders: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        # --- Fetching ---
        for m_type in market_types:
            try:
                fetched = []

                if m_type == "crypto":
                    # Upbit
                    # 1. Open/Pending
                    if status in ("all", "pending"):
                        open_ops = await upbit_service.fetch_open_orders(
                            market=normalized_symbol
                        )
                        fetched.extend([_normalize_upbit_order(o) for o in open_ops])

                    # 2. Closed (Filled/Cancelled)
                    if status in ("all", "filled", "cancelled") and normalized_symbol:
                        # fetch_closed_orders limit
                        # If unlimited, fetch max allowed by upstream or a reasonable high number
                        fetch_limit = (
                            100 if limit_val == float("inf") else max(limit, 20)
                        )
                        closed_ops = await upbit_service.fetch_closed_orders(
                            market=normalized_symbol,
                            limit=fetch_limit,
                        )
                        fetched.extend([_normalize_upbit_order(o) for o in closed_ops])

                elif m_type == "equity_kr":
                    kis = KISClient()
                    # KIS Domestic
                    # 1. Open/Pending
                    if status in ("all", "pending"):
                        logger.debug(
                            "Fetching KR pending orders, symbol=%s", normalized_symbol
                        )
                        open_ops = await kis.inquire_korea_orders()
                        if open_ops:
                            logger.debug(
                                "Raw API response keys: %s", list(open_ops[0].keys())
                            )
                        for o in open_ops:
                            o_sym = str(_get_kis_field(o, "pdno", "PDNO"))
                            if normalized_symbol and o_sym != normalized_symbol:
                                continue
                            fetched.append(_normalize_kis_domestic_order(o))

                    # 2. History
                    if status in ("all", "filled", "cancelled") and normalized_symbol:
                        # KIS requires a date range. If days is None, default to 30 days (or user provided).
                        lookup_days = (
                            effective_days if effective_days is not None else 30
                        )
                        start_dt, end_dt = _calculate_date_range(lookup_days)
                        hist_ops = await kis.inquire_daily_order_domestic(
                            start_date=start_dt,
                            end_date=end_dt,
                            stock_code=normalized_symbol,
                            side="00",  # All
                        )
                        fetched.extend(
                            [_normalize_kis_domestic_order(o) for o in hist_ops]
                        )

                elif m_type == "equity_us":
                    kis = KISClient()
                    # KIS Overseas
                    # 1. Open/Pending
                    if status in ("all", "pending"):
                        target_exchanges = ["NASD", "NYSE", "AMEX"]
                        if normalized_symbol:
                            ex = get_exchange_by_symbol(normalized_symbol)
                            if ex:
                                target_exchanges = [ex]

                        seen_oids = set()
                        for ex in target_exchanges:
                            try:
                                ops = await kis.inquire_overseas_orders(ex)
                                for o in ops:
                                    oid = _extract_kis_order_number(o)
                                    if oid in seen_oids:
                                        continue
                                    seen_oids.add(oid)

                                    o_sym = str(_get_kis_field(o, "pdno", "PDNO"))
                                    if normalized_symbol and o_sym != normalized_symbol:
                                        continue
                                    fetched.append(_normalize_kis_overseas_order(o))
                            except Exception:
                                pass

                    # 2. History
                    if status in ("all", "filled", "cancelled") and normalized_symbol:
                        lookup_days = (
                            effective_days if effective_days is not None else 30
                        )
                        start_dt, end_dt = _calculate_date_range(lookup_days)
                        ex = get_exchange_by_symbol(normalized_symbol) or "NASD"
                        hist_ops = await kis.inquire_daily_order_overseas(
                            start_date=start_dt,
                            end_date=end_dt,
                            symbol=normalized_symbol,
                            exchange_code=ex,
                            side="00",
                        )
                        fetched.extend(
                            [_normalize_kis_overseas_order(o) for o in hist_ops]
                        )

                source_market = _normalize_market_type_to_external(m_type)
                for f in fetched:
                    f["_source_market"] = source_market
                orders.extend(fetched)

            except Exception as e:
                errors.append({"market": m_type, "error": str(e)})

        # --- Filtering & Sorting ---
        original_order_count = len(orders)
        unique_orders = {}
        for o in orders:
            oid = str(o.get("order_id") or "").strip()
            source_market = o.get("_source_market") or o.get("market") or "unknown"
            if oid:
                key = (source_market, oid)
                unique_orders[key] = o
            else:
                key = (
                    source_market,
                    o.get("symbol"),
                    o.get("side"),
                    o.get("ordered_price"),
                    o.get("ordered_qty"),
                    o.get("ordered_at"),
                    o.get("status"),
                    o.get("currency"),
                )
                unique_orders[key] = o

        orders = list(unique_orders.values())
        removed_duplicates = original_order_count - len(orders)
        if removed_duplicates > 0:
            logger.info("Removed %s duplicate orders", removed_duplicates)

        filtered_orders = []
        for o in orders:
            # Status
            o_status = o.get("status")
            if status == "pending":
                if o_status not in ("pending", "partial"):
                    continue
            elif status == "filled":
                if o_status != "filled":
                    continue
            elif status == "cancelled":
                if o_status != "cancelled":
                    continue

            # Order ID
            if order_id and o.get("order_id") != order_id:
                continue

            # Side
            if side and o.get("side") != side:
                continue

            filtered_orders.append(o)

        # Sort by date desc
        def _get_sort_key(o: dict[str, Any]) -> str:
            val = o.get("ordered_at") or o.get("created_at") or ""
            return str(val)

        filtered_orders.sort(key=_get_sort_key, reverse=True)

        # Limit & Truncation
        total_available = len(filtered_orders)
        truncated = False
        if limit_val != float("inf") and total_available > limit_val:
            filtered_orders = filtered_orders[: int(limit_val)]
            truncated = True

        response_orders = []
        for o in filtered_orders:
            cleaned = dict(o)
            cleaned.pop("_source_market", None)
            response_orders.append(cleaned)

        # Summary
        summary = _calculate_order_summary(response_orders)

        # Determine returned market string
        ret_market = "mixed"
        if len(market_types) == 1:
            ret_market = _normalize_market_type_to_external(market_types[0])
        elif normalized_symbol:
            # If symbol was resolved, we know the market
            m, _ = _resolve_market_type(normalized_symbol, None)
            ret_market = _normalize_market_type_to_external(m)

        return {
            "success": bool(response_orders) or not errors,
            "symbol": normalized_symbol,
            "market": ret_market,
            "status": status,
            "filters": {
                "symbol": symbol,
                "status": status,
                "order_id": order_id,
                "market": market_hint,
                "side": side,
                "days": days,
                "limit": limit,
            },
            "orders": response_orders,
            "summary": summary,
            "truncated": truncated,
            "total_available": total_available,
            "errors": errors,
        }

    @mcp.tool(
        name="modify_order",
        description=(
            "Modify a pending order (price/quantity). "
            "Supports Upbit (crypto) and KIS (KR/US equities). "
            "dry_run=True by default for safety. "
            "Upbit: only limit orders in wait state. "
            "KIS: uses API modify endpoint."
        ),
    )
    async def modify_order(
        order_id: str,
        symbol: str,
        market: str | None = None,
        new_price: float | None = None,
        new_quantity: float | None = None,
        dry_run: bool = True,
        reason: str = "",
    ) -> dict[str, Any]:
        """Modify a pending order (price/quantity).

        Args:
            order_id: Order ID to modify
            symbol: Trading symbol (e.g., "KRW-BTC", "005930", "AAPL")
            market: Market filter ("crypto", "kr", "us"). Auto-detected if not specified.
            new_price: New price (None to keep original)
            new_quantity: New quantity (None to keep original)
            dry_run: Preview changes without executing (default: True)
            reason: Optional reason for modification

        Returns:
            Dictionary with modification result, changes, and method used.
        """
        if new_price is None and new_quantity is None:
            raise ValueError(
                "At least one of new_price or new_quantity must be specified"
            )
        if new_price is not None and new_price <= 0:
            raise ValueError("new_price must be a positive number")
        if new_quantity is not None and new_quantity <= 0:
            raise ValueError("new_quantity must be a positive number")

        order_id = order_id.strip()
        symbol = symbol.strip()

        market_type, normalized_symbol = _resolve_market_type(symbol, market)

        original_order = None

        if dry_run:
            changes: dict[str, Any] = {
                "price": {"from": None, "to": new_price} if new_price else None,
                "quantity": {"from": None, "to": new_quantity}
                if new_quantity
                else None,
            }

            return {
                "success": True,
                "status": "simulated",
                "order_id": order_id,
                "symbol": normalized_symbol,
                "market": _normalize_market_type_to_external(market_type),
                "changes": changes,
                "method": "dry_run",
                "dry_run": dry_run,
                "message": f"Dry run - Preview changes for order {order_id}",
            }

        if market_type == "crypto":
            try:
                original_order = await upbit_service.fetch_order_detail(order_id)

                if original_order.get("state") != "wait":
                    return {
                        "success": False,
                        "status": "failed",
                        "order_id": order_id,
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "error": "Order not in wait state (cannot modify non-pending orders)",
                        "dry_run": dry_run,
                    }

                if original_order.get("ord_type") != "limit":
                    return {
                        "success": False,
                        "status": "failed",
                        "order_id": order_id,
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "error": "Only limit orders can be modified (not market orders)",
                        "dry_run": dry_run,
                    }

                original_price = float(original_order.get("price", 0) or 0)
                original_quantity = float(
                    original_order.get("remaining_volume", 0) or 0
                )

                final_price = new_price if new_price is not None else original_price
                final_quantity = (
                    new_quantity if new_quantity is not None else original_quantity
                )

                result = await upbit_service.cancel_and_reorder(
                    order_id, final_price, final_quantity
                )

                changes = {
                    "price": {"from": original_price, "to": final_price}
                    if final_price != original_price
                    else None,
                    "quantity": {"from": original_quantity, "to": final_quantity}
                    if final_quantity != original_quantity
                    else None,
                }

                if result.get("new_order") and "uuid" in result["new_order"]:
                    return {
                        "success": True,
                        "status": "modified",
                        "order_id": order_id,
                        "new_order_id": result["new_order"]["uuid"],
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "changes": changes,
                        "method": "cancel_reorder",
                        "dry_run": dry_run,
                        "message": "Order modified via cancel and reorder",
                    }
                else:
                    return {
                        "success": False,
                        "status": "failed",
                        "order_id": order_id,
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "error": result.get("cancel_result", {}).get(
                            "error", "Unknown error"
                        ),
                        "changes": changes,
                        "method": "cancel_reorder",
                        "dry_run": dry_run,
                    }
            except Exception as exc:
                return {
                    "success": False,
                    "status": "failed",
                    "order_id": order_id,
                    "symbol": normalized_symbol,
                    "market": _normalize_market_type_to_external(market_type),
                    "error": str(exc),
                    "changes": None,
                    "method": "cancel_reorder",
                    "dry_run": dry_run,
                }

        elif market_type == "equity_kr":
            try:
                kis = KISClient()
                open_orders = await kis.inquire_korea_orders()

                target_order = None
                for order in open_orders:
                    if (
                        str(_get_kis_field(order, "odno", "ODNO", "ord_no", "ORD_NO"))
                        == order_id
                    ):
                        target_order = order
                        break

                if not target_order:
                    return {
                        "success": False,
                        "status": "failed",
                        "order_id": order_id,
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "error": "Order not found in open orders",
                        "dry_run": dry_run,
                    }

                original_price = int(
                    float(
                        _get_kis_field(target_order, "ord_unpr", "ORD_UNPR", default=0)
                        or 0
                    )
                )
                original_quantity = int(
                    float(
                        _get_kis_field(target_order, "ord_qty", "ORD_QTY", default=0)
                        or 0
                    )
                )
                side_code = _get_kis_field(
                    target_order, "sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD"
                )
                side = "buy" if side_code == "02" else "sell"

                final_price_raw = (
                    int(new_price) if new_price is not None else original_price
                )
                final_price = int(adjust_tick_size_kr(float(final_price_raw), side))
                final_quantity = (
                    int(new_quantity) if new_quantity is not None else original_quantity
                )

                result = await kis.modify_korea_order(
                    order_id, normalized_symbol, final_quantity, final_price
                )

                changes = {
                    "price": {"from": original_price, "to": final_price}
                    if final_price != original_price
                    else None,
                    "quantity": {"from": original_quantity, "to": final_quantity}
                    if final_quantity != original_quantity
                    else None,
                }

                if result.get("odno"):
                    return {
                        "success": True,
                        "status": "modified",
                        "order_id": order_id,
                        "new_order_id": result["odno"],
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "changes": changes,
                        "method": "api_modify",
                        "dry_run": dry_run,
                        "message": "Order modified via KIS API",
                    }
                else:
                    return {
                        "success": False,
                        "status": "failed",
                        "order_id": order_id,
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "error": result.get("msg", "Unknown error"),
                        "changes": changes,
                        "method": "api_modify",
                        "dry_run": dry_run,
                    }
            except Exception as exc:
                return {
                    "success": False,
                    "status": "failed",
                    "order_id": order_id,
                    "symbol": normalized_symbol,
                    "market": _normalize_market_type_to_external(market_type),
                    "error": str(exc),
                    "changes": None,
                    "method": "api_modify",
                    "dry_run": dry_run,
                }

        elif market_type == "equity_us":
            try:
                kis = KISClient()
                target_order = None
                target_exchange = None
                preferred_exchange = get_exchange_by_symbol(normalized_symbol) or "NASD"
                exchange_candidates: list[str] = []
                for exchange in [preferred_exchange, "NASD", "NYSE", "AMEX"]:
                    if exchange and exchange not in exchange_candidates:
                        exchange_candidates.append(exchange)

                for exchange in exchange_candidates:
                    try:
                        open_orders = await kis.inquire_overseas_orders(exchange)
                    except Exception:
                        continue
                    for order in open_orders:
                        if str(_get_kis_field(order, "odno", "ODNO")) == order_id:
                            target_order = order
                            target_exchange = exchange
                            break
                    if target_order:
                        break

                if not target_order:
                    return {
                        "success": False,
                        "status": "failed",
                        "order_id": order_id,
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "error": f"Order not found in open orders (checked: {', '.join(exchange_candidates)})",
                        "dry_run": dry_run,
                    }

                original_price = float(
                    _get_kis_field(
                        target_order, "ft_ord_unpr3", "FT_ORD_UNPR3", default=0
                    )
                    or 0
                )
                original_quantity = int(
                    float(
                        _get_kis_field(
                            target_order, "ft_ord_qty", "FT_ORD_QTY", default=0
                        )
                        or 0
                    )
                )

                exchange_code = target_exchange or preferred_exchange
                final_price = (
                    float(new_price) if new_price is not None else original_price
                )
                final_quantity = (
                    int(new_quantity) if new_quantity is not None else original_quantity
                )

                result = await kis.modify_overseas_order(
                    order_id,
                    normalized_symbol,
                    exchange_code,
                    final_quantity,
                    final_price,
                )

                changes = {
                    "price": {"from": original_price, "to": final_price}
                    if final_price != original_price
                    else None,
                    "quantity": {"from": original_quantity, "to": final_quantity}
                    if final_quantity != original_quantity
                    else None,
                }

                if result.get("odno"):
                    return {
                        "success": True,
                        "status": "modified",
                        "order_id": order_id,
                        "new_order_id": result["odno"],
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "changes": changes,
                        "method": "api_modify",
                        "dry_run": dry_run,
                        "message": "Order modified via KIS API",
                    }
                else:
                    return {
                        "success": False,
                        "status": "failed",
                        "order_id": order_id,
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "error": result.get("msg", "Unknown error"),
                        "changes": changes,
                        "method": "api_modify",
                        "dry_run": dry_run,
                    }
            except Exception as exc:
                return {
                    "success": False,
                    "status": "failed",
                    "order_id": order_id,
                    "symbol": normalized_symbol,
                    "market": _normalize_market_type_to_external(market_type),
                    "error": str(exc),
                    "changes": None,
                    "method": "api_modify",
                    "dry_run": dry_run,
                }

        else:
            return {
                "success": False,
                "status": "failed",
                "order_id": order_id,
                "symbol": normalized_symbol,
                "market": _normalize_market_type_to_external(market_type),
                "error": "Unknown market type",
                "changes": None,
                "method": "none",
                "dry_run": dry_run,
            }

    # Export nested functions to module globals for testability
    globals()["_preview_order"] = _preview_order
    globals()["_get_support_resistance_impl"] = _get_support_resistance_impl
    globals()["_get_indicators_impl"] = _get_indicators_impl
    globals()["_place_order_impl"] = _place_order_impl
    globals()["_analyze_stock_impl"] = _analyze_stock_impl
    globals()["_get_dca_status_impl"] = _get_dca_status_impl
    globals()["_normalize_upbit_order"] = _normalize_upbit_order
    globals()["_map_upbit_state"] = _map_upbit_state
    globals()["_normalize_kis_domestic_order"] = _normalize_kis_domestic_order
    globals()["_normalize_kis_overseas_order"] = _normalize_kis_overseas_order
    globals()["_map_kis_status"] = _map_kis_status
    globals()["_calculate_order_summary"] = _calculate_order_summary
    globals()["_normalize_market_type_to_external"] = _normalize_market_type_to_external
