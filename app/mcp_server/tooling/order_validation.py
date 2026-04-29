"""Order validation, price lookup, and preview logic."""

from __future__ import annotations

import datetime
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

import app.services.brokers.upbit.client as upbit_service
from app.core.config import settings
from app.mcp_server.caller_identity import get_caller_agent_id, get_caller_source
from app.mcp_server.tooling.market_data_quotes import (
    _fetch_quote_equity_kr,
    _fetch_quote_equity_us,
)
from app.mcp_server.tooling.portfolio_cash import (
    extract_usd_orderable_from_row as _extract_usd_orderable_from_row,
)
from app.mcp_server.tooling.portfolio_cash import (
    select_usd_row_for_us_order as _select_usd_row_for_us_order,
)
from app.mcp_server.tooling.shared import logger
from app.mcp_server.tooling.shared import to_float as _to_float
from app.services.brokers.kis import (
    KISClient,
    extract_domestic_cash_summary_from_integrated_margin,
)
from app.services.brokers.upbit.client import (
    parse_upbit_account_row as _parse_upbit_account_row,
)


def _create_kis_client(*, is_mock: bool) -> KISClient:
    if is_mock:
        return KISClient(is_mock=True)
    return KISClient()


async def _call_kis(method: Any, *args: Any, is_mock: bool, **kwargs: Any) -> Any:
    if is_mock:
        return await method(*args, **kwargs, is_mock=True)
    return await method(*args, **kwargs)


_DEFENSIVE_TRIM_APPROVAL_REGEX = re.compile(r"^[A-Z]+-\d+$")
_DEFENSIVE_TRIM_CACHE_TTL_SECONDS = 60.0
_defensive_trim_success_cache: dict[str, float] = {}
_TRADER_AGENT_ID_DEFAULT = "6b2192cc-14fa-4335-b572-2fe1e0cb54a7"


@dataclass(frozen=True)
class DefensiveTrimContext:
    approval_issue_id: str
    requester_agent_id: str
    approval_verified_at: datetime.datetime


def _is_cached_approved(approval_issue_id: str) -> bool:
    expires_at = _defensive_trim_success_cache.get(approval_issue_id)
    if expires_at is None:
        return False
    if expires_at <= time.time():
        _defensive_trim_success_cache.pop(approval_issue_id, None)
        return False
    return True


def _cache_approved(approval_issue_id: str) -> None:
    _defensive_trim_success_cache[approval_issue_id] = (
        time.time() + _DEFENSIVE_TRIM_CACHE_TTL_SECONDS
    )


def _log_defensive_trim_bypass(
    *,
    symbol: str,
    market_type: str,
    price: float,
    current_price: float,
    avg_price: float,
    min_sell_price: float,
    defensive_trim_ctx: DefensiveTrimContext,
    phase: str,
) -> None:
    logger.warning(
        "defensive_trim_bypass_active: sell floor bypassed",
        extra={
            "symbol": symbol,
            "market_type": market_type,
            "price": price,
            "current_price": current_price,
            "avg_price": avg_price,
            "avg_buy_price": avg_price,
            "min_sell_price": min_sell_price,
            "min_floor": min_sell_price,
            "approval_issue_id": defensive_trim_ctx.approval_issue_id,
            "requester_agent_id": defensive_trim_ctx.requester_agent_id,
            "phase": phase,
        },
    )


async def _fetch_approval_issue_status(approval_issue_id: str) -> str | None:
    api_url = getattr(settings, "paperclip_api_url", None)
    api_key = getattr(settings, "paperclip_api_key", None)
    if not api_url or not api_key:
        logger.warning(
            "defensive_trim disabled: missing PAPERCLIP_API_URL or PAPERCLIP_API_KEY"
        )
        return None

    issue_api_url = f"{api_url.rstrip('/')}/api/issues/{approval_issue_id}"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(issue_api_url, headers=headers)
    except Exception:
        return None

    if response.status_code != 200:
        return None

    try:
        payload = response.json()
    except ValueError:
        return None

    status = payload.get("status")
    return str(status) if status is not None else None


async def _validate_defensive_trim_preconditions(
    *,
    defensive_trim: bool,
    approval_issue_id: str | None,
    side: str,
    order_type: str,
) -> DefensiveTrimContext | None:
    """Validate defensive_trim gates using middleware-extracted caller identity."""
    if not defensive_trim:
        return None

    if side != "sell":
        raise ValueError(
            "defensive_trim requires side='sell' (buy orders always use existing path)"
        )
    if order_type != "limit":
        raise ValueError(
            "defensive_trim requires order_type='limit' (market orders are blocked)"
        )
    if not approval_issue_id:
        raise ValueError("defensive_trim=True requires approval_issue_id")
    if not _DEFENSIVE_TRIM_APPROVAL_REGEX.match(approval_issue_id):
        raise ValueError("approval_issue_id format invalid (expected e.g. 'ROB-164')")

    caller_agent_id = get_caller_agent_id()
    if not caller_agent_id:
        raise ValueError(
            "caller identity unavailable — defensive_trim requires authenticated MCP caller"
        )

    trader_agent_id = getattr(settings, "trader_agent_id", _TRADER_AGENT_ID_DEFAULT)
    if caller_agent_id != trader_agent_id:
        raise ValueError(
            "defensive_trim requires Trader agent caller "
            f"(got caller_agent_id={caller_agent_id})"
        )

    approval_status: str | None
    if _is_cached_approved(approval_issue_id):
        approval_status = "done"
    else:
        try:
            approval_status = await _fetch_approval_issue_status(approval_issue_id)
        except Exception:
            approval_status = None
        if approval_status == "done":
            _cache_approved(approval_issue_id)

    if approval_status != "done":
        raise ValueError(
            f"approval_issue_id {approval_issue_id} not found or not in 'done' status"
        )

    return DefensiveTrimContext(
        approval_issue_id=approval_issue_id,
        requester_agent_id=caller_agent_id,
        approval_verified_at=datetime.datetime.now(datetime.UTC),
    )


async def _get_current_price_for_order(symbol: str, market_type: str) -> float | None:
    if market_type == "crypto":
        prices = await upbit_service.fetch_multiple_current_prices(
            [symbol], use_cache=False
        )
        return prices.get(symbol)
    if market_type == "equity_kr":
        quote = await _fetch_quote_equity_kr(symbol)
        return float(quote.get("price")) if quote.get("price") else None

    quote = await _fetch_quote_equity_us(symbol)
    return float(quote.get("price")) if quote.get("price") else None


async def _get_holdings_for_order(
    symbol: str, market_type: str, is_mock: bool = False
) -> dict[str, Any] | None:
    if market_type == "crypto":
        coins = await upbit_service.fetch_my_coins()
        currency = symbol.replace("KRW-", "")
        for coin in coins:
            if coin.get("currency") == currency:
                parsed = _parse_upbit_account_row(coin)
                return {
                    "quantity": parsed["orderable_quantity"],
                    "total_quantity": parsed["total_quantity"],
                    "locked": parsed["locked"],
                    "avg_price": parsed["avg_buy_price"],
                }
        return None

    kis = _create_kis_client(is_mock=is_mock)
    if market_type == "equity_kr":
        stocks = await _call_kis(kis.fetch_my_stocks, is_mock=is_mock)
        for stock in stocks:
            stock_code = str(stock.get("pdno", "")).strip().upper()
            if stock_code != symbol.upper():
                continue
            return {
                "quantity": _to_float(stock.get("hldg_qty"), default=0.0),
                "avg_price": _to_float(stock.get("pchs_avg_pric"), default=0.0),
            }
        return None

    us_stocks = await _call_kis(kis.fetch_my_us_stocks, is_mock=is_mock)
    for stock in us_stocks:
        stock_code = str(stock.get("ovrs_pdno", "")).strip().upper()
        if stock_code != symbol.upper():
            continue
        return {
            "quantity": _to_float(stock.get("ovrs_cblc_qty"), default=0.0),
            "avg_price": _to_float(stock.get("pchs_avg_pric"), default=0.0),
        }
    return None


async def _get_balance_for_order(market_type: str, is_mock: bool = False) -> float:
    if market_type == "crypto":
        coins = await upbit_service.fetch_my_coins()
        for coin in coins:
            if coin.get("currency") == "KRW":
                return float(coin.get("balance", 0))
        return 0.0

    if market_type == "equity_kr":
        kis = _create_kis_client(is_mock=is_mock)
        if is_mock:
            cash_summary = await _call_kis(
                kis.inquire_domestic_cash_balance,
                is_mock=is_mock,
            )
            return float(cash_summary.get("stck_cash_ord_psbl_amt") or 0)
        margin_data = await _call_kis(
            kis.inquire_integrated_margin,
            is_mock=is_mock,
        )
        domestic_cash = extract_domestic_cash_summary_from_integrated_margin(
            margin_data
        )
        return float(domestic_cash.get("orderable") or 0)

    kis = _create_kis_client(is_mock=is_mock)
    margin_data = await _call_kis(kis.inquire_overseas_margin, is_mock=is_mock)
    usd_row = _select_usd_row_for_us_order(margin_data)
    if usd_row is None:
        raise RuntimeError("USD margin data not found in KIS overseas margin")
    return _extract_usd_orderable_from_row(usd_row)


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
    defensive_trim: bool = False,
    approval_issue_id: str | None = None,
    requester_agent_id: str | None = None,
    caller_source: str | None = None,
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
            "defensive_trim": defensive_trim,
            "approval_issue_id": approval_issue_id,
            "requester_agent_id": requester_agent_id,
            "caller_source": caller_source or get_caller_source(),
        }

        await redis.rpush(key, json.dumps(record))
        await redis.expire(key, 86400)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Preview helpers (extracted from the monolithic _preview_order)
# ---------------------------------------------------------------------------


async def _preview_buy(
    *,
    symbol: str,
    order_type: str,
    quantity: float | None,
    price: float | None,
    current_price: float,
    market_type: str,
) -> dict[str, Any]:
    """Build a dry-run preview dict for a buy order."""
    result: dict[str, Any] = {
        "symbol": symbol,
        "side": "buy",
        "order_type": order_type,
        "current_price": current_price,
    }

    if order_type == "market":
        result["price"] = current_price
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
                balance if balance >= min_market_buy_amount else min_market_buy_amount
            )

        if estimated_value <= 0:
            result["error"] = "order amount must be greater than 0"
            return result

        result["quantity"] = estimated_value / current_price
        result["estimated_value"] = estimated_value
        result["fee"] = estimated_value * 0.0005
        return result

    # Limit buy
    result["price"] = price
    if price is None:
        result["error"] = "price is required for limit buy orders"
        return result
    if price > current_price:
        result["error"] = f"Buy price {price} exceeds current price {current_price}"
        return result
    if quantity is None:
        result["error"] = "quantity is required for limit buy orders"
        return result

    estimated_value = price * quantity
    result["quantity"] = quantity
    result["estimated_value"] = estimated_value
    result["fee"] = estimated_value * 0.0005
    return result


async def _preview_sell(
    *,
    symbol: str,
    order_type: str,
    quantity: float | None,
    price: float | None,
    current_price: float,
    market_type: str,
    defensive_trim_ctx: DefensiveTrimContext | None = None,
    is_mock: bool = False,
) -> dict[str, Any]:
    """Build a dry-run preview dict for a sell order."""
    result: dict[str, Any] = {
        "symbol": symbol,
        "side": "sell",
        "order_type": order_type,
        "current_price": current_price,
    }

    holdings = await _get_holdings_for_order(symbol, market_type, is_mock=is_mock)
    if not holdings:
        result["error"] = "No holdings found"
        return result

    avg_price = holdings["avg_price"]
    if order_type == "market":
        order_quantity = holdings["quantity"]
        execution_price = current_price
        result["price"] = execution_price
    else:
        if price is None:
            result["error"] = "price is required for limit sell orders"
            return result
        min_sell_price = avg_price * 1.01
        if price < min_sell_price and defensive_trim_ctx is None:
            result["error"] = (
                f"Sell price {price} below minimum "
                f"(avg_buy_price * 1.01 = {min_sell_price:.0f})"
            )
            return result
        if price < min_sell_price and defensive_trim_ctx is not None:
            _log_defensive_trim_bypass(
                symbol=symbol,
                market_type=market_type,
                price=price,
                current_price=current_price,
                avg_price=avg_price,
                min_sell_price=min_sell_price,
                defensive_trim_ctx=defensive_trim_ctx,
                phase="preview",
            )
        if price < current_price:
            result["error"] = f"Sell price {price} below current price {current_price}"
            return result
        order_quantity = holdings["quantity"] if quantity is None else quantity
        execution_price = price
        result["price"] = execution_price

    if defensive_trim_ctx is not None:
        result["defensive_trim"] = True
        result["approval_issue_id"] = defensive_trim_ctx.approval_issue_id

    estimated_value = execution_price * order_quantity
    realized_pnl = (execution_price - avg_price) * order_quantity

    result["quantity"] = order_quantity
    result["estimated_value"] = estimated_value
    result["fee"] = estimated_value * 0.0005
    result["realized_pnl"] = realized_pnl
    result["avg_buy_price"] = avg_price
    return result


async def _preview_order(
    symbol: str,
    side: str,
    order_type: str,
    quantity: float | None,
    price: float | None,
    current_price: float,
    market_type: str,
    defensive_trim_ctx: DefensiveTrimContext | None = None,
    is_mock: bool = False,
) -> dict[str, Any]:
    """Validate order and return a dry-run simulation dict.

    Delegates to _preview_buy / _preview_sell for clarity.
    """
    if side == "buy":
        return await _preview_buy(
            symbol=symbol,
            order_type=order_type,
            quantity=quantity,
            price=price,
            current_price=current_price,
            market_type=market_type,
        )
    return await _preview_sell(
        symbol=symbol,
        order_type=order_type,
        quantity=quantity,
        price=price,
        current_price=current_price,
        market_type=market_type,
        defensive_trim_ctx=defensive_trim_ctx,
        is_mock=is_mock,
    )


# ---------------------------------------------------------------------------
# Helpers extracted from _place_order_impl
# ---------------------------------------------------------------------------


def _resolve_buy_quantity(
    *,
    amount: float | None,
    quantity: float | None,
    order_type: str,
    market_type: str,
    price: float | None,
    current_price: float,
) -> tuple[float | None, float | None]:
    """Convert amount to quantity for buy orders.

    Returns (resolved_quantity, resolved_price).
    resolved_price may be updated for crypto market buys.
    """
    if amount is None:
        return quantity, price

    if order_type == "market" and market_type == "crypto":
        return quantity, amount

    if order_type == "limit" and price is not None:
        qty = amount / price
        if market_type != "crypto":
            qty = int(qty)
        return qty, price

    if current_price <= 0:
        raise ValueError("Failed to get current price for amount conversion")
    qty = amount / current_price
    if qty <= 0:
        raise ValueError(
            f"Calculated quantity {qty} is <= 0. "
            f"Check amount ({amount}) and current price ({current_price})"
        )
    if market_type != "crypto":
        qty = int(qty)
        if qty == 0:
            raise ValueError(
                f"Calculated quantity {qty} is 0. "
                f"Amount {amount} is insufficient for 1 unit at price {current_price}"
            )
    return qty, price


async def _validate_sell_side(
    *,
    symbol: str,
    normalized_symbol: str,
    market_type: str,
    quantity: float | None,
    order_type: str,
    price: float | None,
    current_price: float,
    order_error_fn: Any,
    defensive_trim_ctx: DefensiveTrimContext | None = None,
    is_mock: bool = False,
) -> tuple[float, float, dict[str, Any] | None]:
    """Validate sell-side: check holdings, locked, price constraints.

    Returns (order_quantity, avg_price, error_dict_or_None).
    """
    holdings = await _get_holdings_for_order(
        normalized_symbol,
        market_type,
        is_mock=is_mock,
    )
    if not holdings:
        return 0.0, 0.0, order_error_fn(f"No holdings found for {symbol}")

    available_quantity = _to_float(holdings.get("quantity"), default=0.0)
    locked_quantity = _to_float(holdings.get("locked"), default=0.0)

    if quantity is not None and quantity > available_quantity:
        return (
            0.0,
            0.0,
            order_error_fn(
                f"Requested sell quantity {quantity} exceeds orderable balance {available_quantity}. "
                f"locked={locked_quantity} (in open orders, not sellable)."
            ),
        )

    order_quantity = available_quantity if quantity is None else quantity
    avg_price = _to_float(holdings.get("avg_price"), default=0.0)

    if order_type == "limit" and price is not None:
        min_sell_price = avg_price * 1.01
        if price < min_sell_price and defensive_trim_ctx is None:
            return (
                0.0,
                0.0,
                order_error_fn(
                    f"Sell price {price} below minimum "
                    f"(avg_buy_price * 1.01 = {min_sell_price:.0f})"
                ),
            )
        if price < min_sell_price and defensive_trim_ctx is not None:
            _log_defensive_trim_bypass(
                symbol=normalized_symbol,
                market_type=market_type,
                price=price,
                current_price=current_price,
                avg_price=avg_price,
                min_sell_price=min_sell_price,
                defensive_trim_ctx=defensive_trim_ctx,
                phase="execution",
            )
        if price < current_price:
            return (
                0.0,
                0.0,
                order_error_fn(
                    f"Sell price {price} below current price {current_price}"
                ),
            )

    return order_quantity, avg_price, None


async def _check_balance_and_warn(
    *,
    market_type: str,
    normalized_symbol: str,
    side: str,
    order_amount: float,
    dry_run: bool,
    order_error_fn: Any,
    is_mock: bool = False,
) -> tuple[str | None, dict[str, Any] | None]:
    """Pre-check cash balance for buy orders.

    Returns (warning_message_or_None, error_dict_or_None).
    If error_dict is not None, the caller should return it immediately.
    """
    try:
        balance = await _get_balance_for_order(market_type, is_mock=is_mock)
    except Exception as balance_exc:
        logger.error(
            "balance_precheck 조회 실패: stage=balance_query, market_type=%s, symbol=%s, side=%s, error=%s",
            market_type,
            normalized_symbol,
            side,
            balance_exc,
        )
        raise

    if balance >= order_amount:
        return None, None

    logger.warning(
        "balance_precheck 경고: stage=insufficient_balance_precheck, market_type=%s, symbol=%s, side=%s, balance=%s, order_amount=%s",
        market_type,
        normalized_symbol,
        side,
        balance,
        order_amount,
    )

    messages = {
        "crypto": (
            f"Insufficient KRW balance: {balance:,.0f} KRW < {order_amount:,.0f} KRW. "
            "Please deposit KRW from your bank account to Upbit, then retry."
        ),
        "equity_kr": (
            f"Insufficient KRW balance: {balance:,.0f} KRW < {order_amount:,.0f} KRW. "
            "Please deposit funds to your KIS domestic account, then retry."
        ),
        "equity_us": (
            f"Insufficient USD balance: {balance:,.2f} USD < {order_amount:,.2f} USD. "
            "Please deposit USD to your KIS overseas account, then retry."
        ),
    }
    warning = messages.get(market_type, messages["equity_us"])

    if not dry_run:
        return None, order_error_fn(warning)
    return warning, None
