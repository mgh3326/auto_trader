"""Order execution orchestrator.

Thin coordinator: validate -> execute -> record.
Business logic lives in order_validation and order_journal.
"""

from __future__ import annotations

import datetime
from typing import Any, Literal
from typing import cast as typing_cast

import app.services.brokers.upbit.client as upbit_service
from app.mcp_server.tick_size import adjust_tick_size_kr, get_tick_size_kr
from app.mcp_server.tooling.order_journal import (
    _append_journal_warning,
    _close_journals_on_sell,
    _create_trade_journal_for_buy,
    _link_journal_to_fill,
    _order_session_factory,
    _save_order_fill,
    _validate_buy_journal_requirements,
)
from app.mcp_server.tooling.order_validation import (
    _check_balance_and_warn,
    _check_daily_order_limit,
    _get_balance_for_order,
    _get_current_price_for_order,
    _get_holdings_for_order,
    _preview_order,
    _record_order_history,
    _resolve_buy_quantity,
    _validate_sell_side,
)
from app.mcp_server.tooling.shared import logger
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)
from app.mcp_server.tooling.shared import (
    to_float as _to_float,
)
from app.services.brokers.kis import KISClient
from app.services.crypto_trade_cooldown_service import CryptoTradeCooldownService
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


# ---------------------------------------------------------------------------
# Order execution (broker dispatch)
# ---------------------------------------------------------------------------


async def _execute_order(
    symbol: str,
    side: str,
    order_type: str,
    quantity: float | None,
    price: float | None,
    market_type: str,
) -> dict[str, Any]:
    if market_type == "crypto":
        return await _execute_crypto_order(symbol, side, order_type, quantity, price)
    if market_type == "equity_kr":
        return await _execute_kr_order(symbol, side, order_type, quantity, price)
    return await _execute_us_order(symbol, side, quantity, price)


async def _execute_crypto_order(
    symbol: str,
    side: str,
    order_type: str,
    quantity: float | None,
    price: float | None,
) -> dict[str, Any]:
    if side == "buy":
        if order_type == "market":
            price_str = f"{price:.0f}" if price else "0"
            return await upbit_service.place_market_buy_order(symbol, price_str)
        volume_str = f"{quantity:.8f}"
        adjusted_price = upbit_service.adjust_price_to_upbit_unit(price)
        return await upbit_service.place_buy_order(
            symbol, adjusted_price, volume_str, "limit"
        )

    holdings = await _get_holdings_for_order(symbol, "crypto")
    if not holdings:
        raise ValueError("No holdings found")

    volume = holdings["quantity"] if quantity is None else quantity
    volume_str = f"{volume:.8f}"
    if order_type == "market":
        return await upbit_service.place_market_sell_order(symbol, volume_str)

    adjusted_price = upbit_service.adjust_price_to_upbit_unit(price)
    return await upbit_service.place_sell_order(symbol, volume_str, f"{adjusted_price}")


async def _execute_kr_order(
    symbol: str,
    side: str,
    order_type: str,
    quantity: float | None,
    price: float | None,
) -> dict[str, Any]:
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


async def _execute_us_order(
    symbol: str,
    side: str,
    quantity: float | None,
    price: float | None,
) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# _place_order_impl sub-steps
# ---------------------------------------------------------------------------

_MAX_ORDERS_PER_DAY = 20


def _validate_inputs(
    symbol: str,
    side: str,
    order_type: str,
    price: float | None,
    amount: float | None,
    quantity: float | None,
) -> tuple[str, str, str]:
    """Validate and normalize basic inputs. Returns (symbol, side, order_type)."""
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
            "amount and quantity cannot both be specified. "
            "Use amount for notional-based buying or quantity for unit-based buying."
        )

    if amount is not None and side_lower != "buy":
        raise ValueError(
            "amount can only be used for buy orders. Use quantity for sell orders."
        )

    return symbol, side_lower, order_type_lower


async def _fetch_current_price(
    normalized_symbol: str,
    market_type: str,
    order_type: str,
    price: float | None,
) -> float:
    """Fetch current price, falling back to limit price when available."""
    try:
        current_price = await _get_current_price_for_order(
            normalized_symbol, market_type
        )
    except Exception:
        if order_type == "limit" and price is not None:
            current_price = float(price)
        else:
            raise

    if current_price is None:
        if order_type == "limit" and price is not None:
            current_price = float(price)
        else:
            raise ValueError(f"Failed to get current price for {normalized_symbol}")

    return current_price


async def _build_preview(
    *,
    normalized_symbol: str,
    side: str,
    order_type: str,
    order_quantity: float | None,
    price: float | None,
    current_price: float,
    market_type: str,
) -> dict[str, Any]:
    """Run preview and enrich result with defaults."""
    preview_fn = globals().get("_preview_order", _preview_order)
    dry_run_result = await preview_fn(
        symbol=normalized_symbol,
        side=side,
        order_type=order_type,
        quantity=order_quantity,
        price=price,
        current_price=current_price,
        market_type=market_type,
    )
    if not isinstance(dry_run_result, dict):
        raise ValueError("Order preview returned invalid result")
    if dry_run_result.get("error"):
        raise ValueError(str(dry_run_result["error"]))

    if (
        side == "sell"
        and order_quantity is not None
        and dry_run_result.get("quantity") is None
    ):
        dry_run_result["quantity"] = order_quantity

    dry_run_result.setdefault("symbol", normalized_symbol)
    dry_run_result.setdefault("side", side)
    dry_run_result.setdefault("order_type", order_type)
    if dry_run_result.get("price") is None:
        dry_run_result["price"] = current_price if order_type == "market" else price
    return dry_run_result


async def _record_fill_and_journals(
    *,
    side: str,
    normalized_symbol: str,
    market_type: str,
    execution_result: dict[str, Any],
    dry_run_result: dict[str, Any],
    order_quantity: float | None,
    current_price: float,
    avg_price: float,
    reason: str,
    exit_reason: str | None,
    thesis: str | None,
    strategy: str | None,
    target_price: float | None,
    stop_loss: float | None,
    min_hold_days: int | None,
    notes: str | None,
    indicators_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    """Save fill to DB, manage journals (create for buy, close for sell)."""
    journal_created = False
    journal_id: int | None = None
    journal_status: str | None = None
    journal_warning: str | None = None

    if side == "buy":
        try:
            journal_result = await _create_trade_journal_for_buy(
                symbol=normalized_symbol,
                market_type=market_type,
                preview=dry_run_result,
                thesis=typing_cast(str, thesis),
                strategy=typing_cast(str, strategy),
                target_price=target_price,
                stop_loss=stop_loss,
                min_hold_days=min_hold_days,
                notes=notes,
                indicators_snapshot=indicators_snapshot,
            )
            journal_created = journal_result["journal_created"]
            journal_id = journal_result["journal_id"]
            journal_status = journal_result["journal_status"]
        except Exception as journal_exc:
            journal_warning = (
                f"trade journal creation failed after order execution: {journal_exc}"
            )
            logger.warning(journal_warning)

    # Record stop-loss cooldown for crypto sells below threshold
    if (
        market_type == "crypto"
        and side == "sell"
        and avg_price > 0
        and current_price <= avg_price * (1 - CRYPTO_STOP_LOSS_PCT)
    ):
        try:
            cooldown_service = _get_crypto_trade_cooldown_service()
            await cooldown_service.record_stop_loss(normalized_symbol)
        except Exception as cooldown_exc:
            logger.warning("Failed to record stop-loss cooldown: %s", cooldown_exc)

    # Save fill to DB
    fill_recorded = False
    order_id = execution_result.get("uuid") or execution_result.get("odno")
    price_val = _to_float(dry_run_result.get("price"), default=0.0)
    qty_val = _to_float(dry_run_result.get("quantity"), default=0.0)
    amt_val = _to_float(dry_run_result.get("estimated_value"), default=0.0)
    fee_val = _to_float(dry_run_result.get("fee"), default=0.0)
    currency = "KRW" if market_type != "equity_us" else "USD"
    account_name = "upbit" if market_type == "crypto" else "kis"

    try:
        trade_id = await _save_order_fill(
            symbol=normalized_symbol,
            instrument_type=market_type,
            side=side,
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
            await _link_journal_to_fill(normalized_symbol, trade_id)
            if journal_created:
                journal_status = "active"
        elif journal_created:
            journal_warning = (
                "trade journal created but fill was not recorded; journal remains draft"
            )
    except Exception as db_exc:
        logger.warning("Failed to record fill to DB: %s", db_exc)

    # Close journals for sell orders
    journal_close_result: dict[str, Any] | None = None
    if side == "sell":
        try:
            preview_qty = _to_float(dry_run_result.get("quantity"), default=0.0)
            preview_price = _to_float(dry_run_result.get("price"), default=0.0)
            resolved_sell_qty = (
                preview_qty
                if preview_qty > 0
                else _to_float(order_quantity, default=0.0)
            )
            resolved_sell_price = (
                preview_price
                if preview_price > 0
                else _to_float(current_price, default=0.0)
            )
            journal_close_result = await _close_journals_on_sell(
                symbol=normalized_symbol,
                sell_quantity=resolved_sell_qty,
                sell_price=resolved_sell_price,
                exit_reason=exit_reason or reason,
            )
        except Exception as journal_exc:
            journal_warning = _append_journal_warning(
                journal_warning, f"journal close failed after sell: {journal_exc}"
            )
            logger.warning("Failed to close journals on sell: %s", journal_exc)

    result: dict[str, Any] = {
        "fill_recorded": fill_recorded,
        "journal_created": journal_created,
        "journal_id": journal_id,
        "journal_status": journal_status,
    }
    if journal_close_result:
        result["journals_closed"] = journal_close_result["journals_closed"]
        result["journals_kept"] = journal_close_result["journals_kept"]
        result["closed_journal_ids"] = journal_close_result["closed_ids"]
    if journal_warning:
        result["journal_warning"] = journal_warning
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


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
    exit_reason: str | None = None,
    thesis: str | None = None,
    strategy: str | None = None,
    target_price: float | None = None,
    stop_loss: float | None = None,
    min_hold_days: int | None = None,
    notes: str | None = None,
    indicators_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    symbol, side_lower, order_type_lower = _validate_inputs(
        symbol,
        side,
        order_type,
        price,
        amount,
        quantity,
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
        current_price = await _fetch_current_price(
            normalized_symbol,
            market_type,
            order_type_lower,
            price,
        )

        # Resolve amount -> quantity for buy orders
        order_quantity, price = _resolve_buy_quantity(
            amount=amount,
            quantity=quantity,
            order_type=order_type_lower,
            market_type=market_type,
            price=price,
            current_price=current_price,
        )

        if order_type_lower == "limit" and order_quantity is None:
            raise ValueError("quantity is required for limit orders")

        # Validate sell-side: holdings, locked, price constraints
        avg_price = 0.0
        if side_lower == "sell":
            order_quantity, avg_price, sell_error = await _validate_sell_side(
                symbol=symbol,
                normalized_symbol=normalized_symbol,
                market_type=market_type,
                quantity=quantity,
                order_type=order_type_lower,
                price=price,
                current_price=current_price,
                order_error_fn=_order_error,
            )
            if sell_error is not None:
                return sell_error

        # Build preview
        try:
            dry_run_result = await _build_preview(
                normalized_symbol=normalized_symbol,
                side=side_lower,
                order_type=order_type_lower,
                order_quantity=order_quantity,
                price=price,
                current_price=current_price,
                market_type=market_type,
            )
        except ValueError as preview_exc:
            return _order_error(str(preview_exc))

        order_amount = _to_float(dry_run_result.get("estimated_value"), default=0.0)

        # Balance pre-check for buy orders
        balance_warning: str | None = None
        if side_lower == "buy":
            balance_warning, balance_error = await _check_balance_and_warn(
                market_type=market_type,
                normalized_symbol=normalized_symbol,
                side=side_lower,
                order_amount=order_amount,
                dry_run=dry_run,
                order_error_fn=_order_error,
            )
            if balance_error is not None:
                return balance_error

        # Dry-run exit
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

        # Real execution
        if not await _check_daily_order_limit(_MAX_ORDERS_PER_DAY):
            return _order_error(f"Daily order limit ({_MAX_ORDERS_PER_DAY}) exceeded")

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

        # Record phase: fills + journals
        record_result = await _record_fill_and_journals(
            side=side_lower,
            normalized_symbol=normalized_symbol,
            market_type=market_type,
            execution_result=execution_result,
            dry_run_result=dry_run_result,
            order_quantity=order_quantity,
            current_price=current_price,
            avg_price=avg_price,
            reason=reason,
            exit_reason=exit_reason,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            indicators_snapshot=indicators_snapshot,
        )

        response: dict[str, Any] = {
            "success": True,
            "dry_run": False,
            "preview": dry_run_result,
            "execution": execution_result,
            **record_result,
            "message": "Order placed and fill recorded successfully"
            if record_result["fill_recorded"]
            else "Order placed successfully",
        }
        return response
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
    "_close_journals_on_sell",
    "_create_trade_journal_for_buy",
    "_link_journal_to_fill",
    "_order_session_factory",
    "_save_order_fill",
    "_validate_buy_journal_requirements",
    "_append_journal_warning",
    "_get_crypto_trade_cooldown_service",
    "CRYPTO_STOP_LOSS_PCT",
]
