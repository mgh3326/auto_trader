"""Order execution helpers extracted from orders."""

from __future__ import annotations

import datetime
import json
from typing import Any, Literal

from app.core.config import settings
from app.mcp_server.tick_size import adjust_tick_size_kr, get_tick_size_kr
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
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)
from app.mcp_server.tooling.shared import (
    to_float as _to_float,
)
from app.services import upbit as upbit_service
from app.services.kis import KISClient
from data.stocks_info.overseas_us_stocks import get_exchange_by_symbol


def _calculate_date_range(days: int) -> tuple[str, str]:
    """Calculate date range for order lookup."""
    today = datetime.datetime.now()
    start_date = (today - datetime.timedelta(days=days)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")
    return start_date, end_date


def _normalize_market_type_to_external(market_type: str) -> str:
    """Convert internal market_type to external contract values."""
    mapping = {
        "equity_kr": "kr",
        "equity_us": "us",
        "crypto": "crypto",
    }
    return mapping.get(market_type, market_type)


async def _get_current_price_for_order(symbol: str, market_type: str) -> float | None:
    if market_type == "crypto":
        prices = await upbit_service.fetch_multiple_current_prices([symbol])
        return prices.get(symbol)
    if market_type == "equity_kr":
        quote = await _fetch_quote_equity_kr(symbol)
        return float(quote.get("price")) if quote.get("price") else None

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
        return None

    kis = KISClient()
    if market_type == "equity_kr":
        stocks = await kis.fetch_my_stocks()
        for stock in stocks:
            stock_code = str(stock.get("pdno", "")).strip().upper()
            if stock_code != symbol.upper():
                continue
            return {
                "quantity": _to_float(stock.get("hldg_qty"), default=0.0),
                "avg_price": _to_float(stock.get("pchs_avg_pric"), default=0.0),
            }
        return None

    us_stocks = await kis.fetch_my_us_stocks()
    for stock in us_stocks:
        stock_code = str(stock.get("ovrs_pdno", "")).strip().upper()
        if stock_code != symbol.upper():
            continue
        return {
            "quantity": _to_float(stock.get("ovrs_cblc_qty"), default=0.0),
            "avg_price": _to_float(stock.get("pchs_avg_pric"), default=0.0),
        }
    return None


async def _get_balance_for_order(market_type: str) -> float:
    if market_type == "crypto":
        coins = await upbit_service.fetch_my_coins()
        for coin in coins:
            if coin.get("currency") == "KRW":
                return float(coin.get("balance", 0))
        return 0.0

    if market_type == "equity_kr":
        # 국내 주문은 통합증거금이 아니라 국내 현금 잔고만 사용한다.
        kis = KISClient()
        balance_data = await kis.inquire_domestic_cash_balance()
        orderable = balance_data.get("stck_cash_ord_psbl_amt")
        if orderable is None:
            orderable = balance_data.get("dnca_tot_amt")
        return float(orderable or 0)

    kis = KISClient()
    margin_data = await kis.inquire_overseas_margin()
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
            result["error"] = f"Buy price {price} exceeds current price {current_price}"
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
            result["error"] = f"Sell price {price} below current price {current_price}"
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
            volume_str = f"{quantity:.8f}"
            adjusted_price = upbit_service.adjust_price_to_upbit_unit(price)
            return await upbit_service.place_buy_order(
                symbol, adjusted_price, volume_str, "limit"
            )

        holdings = await _get_holdings_for_order(symbol, market_type)
        if not holdings:
            raise ValueError("No holdings found")

        volume = holdings["quantity"] if quantity is None else quantity
        volume_str = f"{volume:.8f}"
        if order_type == "market":
            return await upbit_service.place_market_sell_order(symbol, volume_str)

        adjusted_price = upbit_service.adjust_price_to_upbit_unit(price)
        return await upbit_service.place_sell_order(
            symbol, volume_str, f"{adjusted_price}"
        )

    if market_type == "equity_kr":
        kis = KISClient()
        stock_code = symbol
        order_quantity = int(quantity) if quantity else 0
        order_price = int(price) if price else 0

        original_price = order_price if order_price else None
        if order_type == "limit" and order_price > 0:
            tick_size = get_tick_size_kr(float(order_price))
            order_price = adjust_tick_size_kr(float(order_price), side)

            if original_price is not None and order_price != original_price:
                logger.info(
                    "KR limit order tick adjusted: symbol=%s side=%s original_price=%s tick_size=%s adjusted_price=%s",
                    symbol,
                    side,
                    original_price,
                    tick_size,
                    order_price,
                )
            else:
                logger.debug(
                    "KR limit order tick valid: symbol=%s side=%s price=%s tick_size=%s tick_adjusted=false",
                    symbol,
                    side,
                    original_price,
                    tick_size,
                )

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

        if original_price is not None and order_price != original_price:
            result["original_price"] = original_price
            result["adjusted_price"] = order_price
            result["tick_adjusted"] = True
        return result

    kis = KISClient()
    exchange_code = get_exchange_by_symbol(symbol) or "NASD"

    if side == "buy":
        return await kis.buy_overseas_stock(
            symbol=symbol,
            exchange_code=exchange_code,
            quantity=int(quantity) if quantity else 0,
            price=price if price else 0.0,
        )
    return await kis.sell_overseas_stock(
        symbol=symbol,
        exchange_code=exchange_code,
        quantity=int(quantity) if quantity else 0,
        price=price if price else 0.0,
    )


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
            "error": message,
            "source": source,
            "symbol": normalized_symbol,
            "instrument_type": market_type,
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

        order_quantity = quantity
        if side_lower == "buy" and amount is not None:
            if order_type_lower == "market" and market_type == "crypto":
                price = amount
            elif order_type_lower == "limit" and price is not None:
                order_quantity = amount / price
                if market_type != "crypto":
                    order_quantity = int(order_quantity)
            else:
                if current_price <= 0:
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
            try:
                balance = await _get_balance_for_order(market_type)
            except Exception as balance_exc:
                logger.error(
                    "balance_precheck 조회 실패: stage=balance_query, market_type=%s, symbol=%s, side=%s, error=%s",
                    market_type,
                    normalized_symbol,
                    side_lower,
                    balance_exc,
                )
                raise

            if balance < order_amount:
                logger.warning(
                    "balance_precheck 경고: stage=insufficient_balance_precheck, market_type=%s, symbol=%s, side=%s, balance=%s, order_amount=%s",
                    market_type,
                    normalized_symbol,
                    side_lower,
                    balance,
                    order_amount,
                )
                if market_type == "crypto":
                    balance_warning = (
                        f"Insufficient KRW balance: {balance:,.0f} KRW < {order_amount:,.0f} KRW. "
                        "Please deposit KRW from your bank account to Upbit, then retry."
                    )
                elif market_type == "equity_kr":
                    balance_warning = (
                        f"Insufficient KRW balance: {balance:,.0f} KRW < {order_amount:,.0f} KRW. "
                        "Please deposit funds to your KIS domestic account, then retry."
                    )
                else:
                    balance_warning = (
                        f"Insufficient USD balance: {balance:,.2f} USD < {order_amount:,.2f} USD. "
                        "Please deposit USD to your KIS overseas account, then retry."
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
            return _order_error(f"Daily order limit ({MAX_ORDERS_PER_DAY}) exceeded")

        try:
            execution_result = await _execute_order(
                symbol=normalized_symbol,
                side=side_lower,
                order_type=order_type_lower,
                quantity=order_quantity,
                price=price,
                market_type=market_type,
            )
        except Exception as exec_exc:
            logger.error(
                "execute_order 실패: stage=execute_order, market_type=%s, symbol=%s, side=%s, error=%s",
                market_type,
                normalized_symbol,
                side_lower,
                exec_exc,
            )
            raise

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


__all__ = [
    "_calculate_date_range",
    "_normalize_market_type_to_external",
    "_get_current_price_for_order",
    "_get_holdings_for_order",
    "_get_balance_for_order",
    "_check_daily_order_limit",
    "_record_order_history",
    "_preview_order",
    "_execute_order",
    "_place_order_impl",
]
