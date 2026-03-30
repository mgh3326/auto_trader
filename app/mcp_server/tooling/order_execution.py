"""Order execution helpers extracted from orders."""

from __future__ import annotations

import datetime
import json
from typing import Any, Literal
from typing import cast as typing_cast

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.brokers.upbit.client as upbit_service
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
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
from app.models.review import Trade
from app.models.trade_journal import JournalStatus, TradeJournal
from app.services.brokers.kis import (
    KISClient,
    extract_domestic_cash_summary_from_integrated_margin,
)
from app.services.brokers.upbit.client import (
    parse_upbit_account_row as _parse_upbit_account_row,
)
from app.services.crypto_trade_cooldown_service import (
    CryptoTradeCooldownService,
)
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

# Phase 2 strategy constants
CRYPTO_STOP_LOSS_PCT = 0.045

# Crypto trade cooldown service singleton
_order_cooldown_service: CryptoTradeCooldownService | None = None


def _get_crypto_trade_cooldown_service() -> CryptoTradeCooldownService:
    """Get or create the crypto trade cooldown service."""
    global _order_cooldown_service
    if _order_cooldown_service is None:
        _order_cooldown_service = CryptoTradeCooldownService()
    return _order_cooldown_service


def _order_session_factory() -> async_sessionmaker[AsyncSession]:
    return typing_cast(
        async_sessionmaker[AsyncSession], typing_cast(object, AsyncSessionLocal)
    )


async def _save_order_fill(
    symbol: str,
    instrument_type: str,
    side: str,
    price: float,
    quantity: float,
    total_amount: float,
    fee: float,
    currency: str,
    account: str,
    order_id: str | None,
) -> int | None:
    """Save executed order to review.trades for permanent history.

    Returns the trade ID if inserted, None if conflict (already exists).
    """
    try:
        async with _order_session_factory()() as db:
            stmt = (
                pg_insert(Trade)
                .values(
                    trade_date=now_kst(),
                    symbol=symbol,
                    instrument_type=instrument_type,
                    side=side,
                    price=price,
                    quantity=quantity,
                    total_amount=total_amount,
                    fee=fee,
                    currency=currency,
                    account=account,
                    order_id=order_id,
                )
                .on_conflict_do_nothing(
                    constraint="uq_review_trades_account_order",
                )
            )
            result = await db.execute(stmt)
            await db.commit()

            # result.inserted_primary_key returns a tuple of primary keys
            if result.inserted_primary_key and result.inserted_primary_key[0]:
                return typing_cast(int, result.inserted_primary_key[0])
            return None
    except Exception as exc:
        logger.warning("Failed to save order fill: %s", exc)
        return None


async def _link_journal_to_fill(symbol: str, trade_id: int) -> None:
    """Link a draft journal to a fill: draft -> active, set trade_id, recalculate hold_until."""
    try:
        async with _order_session_factory()() as db:
            stmt = (
                select(TradeJournal)
                .where(
                    TradeJournal.symbol == symbol,
                    TradeJournal.status == JournalStatus.draft,
                )
                .order_by(desc(TradeJournal.created_at))
                .limit(1)
            )
            result = await db.execute(stmt)
            journal = result.scalars().first()

            if journal is None:
                return

            journal.status = JournalStatus.active
            journal.trade_id = trade_id
            if journal.min_hold_days:
                from datetime import timedelta

                journal.hold_until = now_kst() + timedelta(days=journal.min_hold_days)

            await db.commit()
            logger.info(
                "Linked journal id=%s to trade id=%s for %s",
                journal.id,
                trade_id,
                symbol,
            )
    except Exception as exc:
        logger.warning("Failed to link journal to fill: %s", exc)


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
    symbol: str, market_type: str
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
        kis = KISClient()
        margin_data = await kis.inquire_integrated_margin()
        domestic_cash = extract_domestic_cash_summary_from_integrated_margin(
            margin_data
        )
        return float(domestic_cash.get("orderable") or 0)

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
    exchange_code = await get_us_exchange_by_symbol(symbol)

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


def _validate_buy_journal_requirements(
    *,
    side: str,
    dry_run: bool,
    thesis: str | None,
    strategy: str | None,
) -> None:
    """Validate that buy orders have required journal fields when not in dry-run mode."""
    if side != "buy" or dry_run:
        return
    if not (thesis or "").strip():
        raise ValueError("thesis is required for buy orders when dry_run=False")
    if not (strategy or "").strip():
        raise ValueError("strategy is required for buy orders when dry_run=False")


async def _create_trade_journal_for_buy(
    *,
    symbol: str,
    market_type: str,
    preview: dict[str, Any],
    thesis: str,
    strategy: str,
    target_price: float | None,
    stop_loss: float | None,
    min_hold_days: int | None,
    notes: str | None,
    indicators_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    """Create a draft trade journal entry for a buy order.

    Returns a dict with journal_created, journal_id, journal_status.
    Raises on DB errors to allow caller to handle.
    """
    from decimal import Decimal
    from app.models.trade_journal import InstrumentType, JournalStatus, TradeJournal

    hold_until = (
        now_kst() + datetime.timedelta(days=min_hold_days)
        if min_hold_days and min_hold_days > 0
        else None
    )
    account_name = "upbit" if market_type == "crypto" else "kis"

    journal = TradeJournal(
        symbol=symbol,
        instrument_type=InstrumentType(market_type),
        side="buy",
        entry_price=Decimal(str(_to_float(preview.get("price"), default=0.0))),
        quantity=Decimal(str(_to_float(preview.get("quantity"), default=0.0))),
        amount=Decimal(str(_to_float(preview.get("estimated_value"), default=0.0))),
        thesis=thesis.strip(),
        strategy=strategy.strip(),
        target_price=Decimal(str(target_price)) if target_price is not None else None,
        stop_loss=Decimal(str(stop_loss)) if stop_loss is not None else None,
        min_hold_days=min_hold_days,
        hold_until=hold_until,
        indicators_snapshot=indicators_snapshot,
        notes=notes,
        account=account_name,
        status=JournalStatus.draft,
    )

    async with _order_session_factory()() as db:
        db.add(journal)
        await db.commit()
        await db.refresh(journal)

    return {
        "journal_created": True,
        "journal_id": journal.id,
        "journal_status": "draft",
    }


async def _place_order_impl(
    symbol: str,
    side: Literal["buy", "sell"],
    market: str | None = None,
    order_type: Literal["limit", "market"] = "limit",
    quantity: float | None = None,
    price: float | None = None,
    amount: float | None = None,
    dry_run: bool = True,
    reason: str = "",
    thesis: str | None = None,
    strategy: str | None = None,
    target_price: float | None = None,
    stop_loss: float | None = None,
    min_hold_days: int | None = None,
    notes: str | None = None,
    indicators_snapshot: dict[str, Any] | None = None,
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

    market_type, normalized_symbol = _resolve_market_type(symbol, market)

    # Validate buy order journal requirements before any external API calls
    try:
        _validate_buy_journal_requirements(
            side=side_lower,
            dry_run=dry_run,
            thesis=thesis,
            strategy=strategy,
        )
    except ValueError as e:
        return {
            "success": False,
            "error": str(e),
            "source": "upbit" if market_type == "crypto" else "kis",
            "symbol": normalized_symbol,
            "instrument_type": market_type,
        }

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

    # Check stop-loss cooldown for crypto buys
    if side_lower == "buy" and market_type == "crypto":
        cooldown_service = _get_crypto_trade_cooldown_service()
        if await cooldown_service.is_in_cooldown(normalized_symbol):
            return _order_error(
                "Symbol is in stop-loss cooldown until re-entry window expires"
            )

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

        avg_price = 0.0
        if side_lower == "sell":
            holdings = await _get_holdings_for_order(normalized_symbol, market_type)
            if not holdings:
                return _order_error(f"No holdings found for {symbol}")

            available_quantity = _to_float(holdings.get("quantity"), default=0.0)
            locked_quantity = _to_float(holdings.get("locked"), default=0.0)

            if quantity is not None and quantity > available_quantity:
                return _order_error(
                    f"Requested sell quantity {quantity} exceeds orderable balance {available_quantity}. "
                    f"locked={locked_quantity} (in open orders, not sellable)."
                )

            order_quantity = available_quantity if quantity is None else quantity
            avg_price = _to_float(holdings.get("avg_price"), default=0.0)

            if order_type_lower == "limit" and price is not None:
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

        # Record stop-loss cooldown for crypto sells below threshold
        if (
            market_type == "crypto"
            and side_lower == "sell"
            and avg_price > 0
            and current_price <= avg_price * (1 - CRYPTO_STOP_LOSS_PCT)
        ):
            try:
                cooldown_service = _get_crypto_trade_cooldown_service()
                await cooldown_service.record_stop_loss(normalized_symbol)
            except Exception as cooldown_exc:
                logger.warning(
                    "Failed to record stop-loss cooldown: %s",
                    cooldown_exc,
                )

        # --- Recording to DB (Phase 1) ---
        fill_recorded = False
        try:
            # Normalize result for storage
            # Note: Upbit returns uuid, KIS returns odno
            order_id = execution_result.get("uuid") or execution_result.get("odno")

            # Use preview data for price/quantity as most APIs don't return fill details
            # immediately in the order response (they are asynchronous)
            # We record what we SENT as the fill for now.
            price_val = _to_float(dry_run_result.get("price"), default=0.0)
            qty_val = _to_float(dry_run_result.get("quantity"), default=0.0)
            amt_val = _to_float(dry_run_result.get("estimated_value"), default=0.0)
            fee_val = _to_float(dry_run_result.get("fee"), default=0.0)

            currency = "KRW" if market_type != "equity_us" else "USD"
            account_name = "upbit" if market_type == "crypto" else "kis"

            trade_id = await _save_order_fill(
                symbol=normalized_symbol,
                instrument_type=market_type,
                side=side_lower,
                price=price_val,
                quantity=qty_val,
                total_amount=amt_val,
                fee=fee_val,
                currency=currency,
                account=account_name,
                order_id=str(order_id) if order_id else None,
            )

            if trade_id:
                fill_recorded = True
                # Phase 2: Link journal to this trade
                await _link_journal_to_fill(normalized_symbol, trade_id)
        except Exception as db_exc:
            logger.warning("Failed to record fill to DB: %s", db_exc)

        return {
            "success": True,
            "dry_run": False,
            "preview": dry_run_result,
            "execution": execution_result,
            "fill_recorded": fill_recorded,
            "message": "Order placed and fill recorded successfully"
            if fill_recorded
            else "Order placed successfully",
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
    "_get_crypto_trade_cooldown_service",
    "CRYPTO_STOP_LOSS_PCT",
]
