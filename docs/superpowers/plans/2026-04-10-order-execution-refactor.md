# Order Execution Module Refactor Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the 1,134-line `order_execution.py` into three focused modules so that no single function exceeds 50 lines, while keeping every existing import path working.

**Architecture:** Extract validation logic into `order_validation.py` and journal/fill DB logic into `order_journal.py`. The remaining `order_execution.py` becomes a thin orchestrator that calls validate -> execute -> record. All existing `__all__` exports and external imports are preserved via re-exports from `order_execution.py`.

**Tech Stack:** Python 3.13+, SQLAlchemy async, pytest-asyncio

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `app/mcp_server/tooling/order_validation.py` | Input validation, price fetch, holdings fetch, balance check, daily limit, amount-to-quantity conversion, sell-side validation, preview (buy/sell) | **Create** |
| `app/mcp_server/tooling/order_journal.py` | `_save_order_fill`, `_link_journal_to_fill`, `_create_trade_journal_for_buy`, `_close_journals_on_sell`, `_append_journal_warning`, `_validate_buy_journal_requirements` | **Create** |
| `app/mcp_server/tooling/order_execution.py` | Thin orchestrator: `_place_order_impl` (validate -> execute -> record), `_execute_order`, constants, `__all__` re-exports | **Modify** |
| `app/mcp_server/tooling/shared.py` | Shared utilities | **DO NOT TOUCH** |
| `tests/test_mcp_place_order.py` | Place order tests | **No change** (must pass as-is) |
| `tests/test_mcp_trade_journal.py` | Journal tests | **No change** (must pass as-is) |

### Import Compatibility Strategy

All existing consumers import via these patterns:
1. `from app.mcp_server.tooling.order_execution import _place_order_impl` (screener_service.py)
2. `from app.mcp_server.tooling.order_execution import _normalize_market_type_to_external` (orders_modify_cancel.py)
3. `from app.mcp_server.tooling import order_execution as _order_execution` then `_order_execution._preview_order` (orders_history.py)
4. `from app.mcp_server.tooling.order_execution import _close_journals_on_sell` (test_mcp_trade_journal.py)
5. `from app.mcp_server.tooling.order_execution import _create_trade_journal_for_buy` (test_mcp_trade_journal.py)
6. `from app.mcp_server.tooling.order_execution import _link_journal_to_fill` (test_mcp_trade_journal.py)

**Solution:** `order_execution.py` re-exports every moved function so all import paths continue to resolve. The `__all__` list stays identical.

---

### Task 1: Create `order_journal.py` - Journal & Fill DB Logic

**Files:**
- Create: `app/mcp_server/tooling/order_journal.py`
- Test: `tests/test_mcp_trade_journal.py` (existing, no changes)

This task moves these 6 functions out of `order_execution.py`:
- `_order_session_factory` (lines 67-70)
- `_save_order_fill` (lines 73-119)
- `_link_journal_to_fill` (lines 122-156)
- `_validate_buy_journal_requirements` (lines 533-546)
- `_create_trade_journal_for_buy` (lines 549-602)
- `_close_journals_on_sell` (lines 605-682)
- `_append_journal_warning` (lines 685-687)

- [ ] **Step 1: Create `order_journal.py` with all journal/fill functions**

```python
"""Trade journal and order fill database operations."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, cast as typing_cast

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.tooling.shared import logger
from app.mcp_server.tooling.shared import to_float as _to_float
from app.models.review import Trade
from app.models.trade_journal import JournalStatus, TradeJournal
from app.models.trading import InstrumentType


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


async def _close_journals_on_sell(
    *,
    symbol: str,
    sell_quantity: float,
    sell_price: float,
    exit_reason: str | None = None,
) -> dict[str, Any]:
    """Close active trade journals in FIFO order when a sell order succeeds.

    - quantity is None: close immediately (legacy/manual case)
    - quantity <= remaining_sell_qty: close and decrement remaining
    - quantity > remaining_sell_qty: stop FIFO (partial sell, leave active)

    Returns dict with journals_closed, journals_kept, closed_ids, total_pnl_pct.
    """
    sell_qty_dec = Decimal(str(sell_quantity))
    sell_price_dec = Decimal(str(sell_price))
    remaining_qty = sell_qty_dec
    resolved_reason = (exit_reason or "").strip() or "sold_via_place_order"

    async with _order_session_factory()() as db:
        stmt = (
            select(TradeJournal)
            .where(
                TradeJournal.symbol == symbol,
                TradeJournal.status == JournalStatus.active,
            )
            .order_by(TradeJournal.created_at.asc())
        )
        result = await db.execute(stmt)
        journals = list(result.scalars().all())

        closed_ids: list[int] = []
        weighted_pnl_sum = Decimal("0")
        weighted_qty_sum = Decimal("0")

        for journal in journals:
            journal_qty = journal.quantity

            if journal_qty is None:
                pass  # Legacy/manual case: close without consuming quantity
            elif remaining_qty > 0 and journal_qty <= remaining_qty:
                remaining_qty -= journal_qty
            elif remaining_qty > 0 and journal_qty > remaining_qty:
                break
            else:
                break

            journal.status = JournalStatus.closed
            journal.exit_price = sell_price_dec
            journal.exit_date = now_kst()
            journal.exit_reason = resolved_reason

            if journal.entry_price and journal.entry_price > 0:
                pnl_pct = (
                    (sell_price_dec - journal.entry_price) / journal.entry_price
                ) * Decimal("100")
                journal.pnl_pct = pnl_pct
                if journal_qty and journal_qty > 0:
                    weighted_pnl_sum += pnl_pct * journal_qty
                    weighted_qty_sum += journal_qty

            closed_ids.append(journal.id)

        await db.commit()

    total_pnl_pct = (
        float(weighted_pnl_sum / weighted_qty_sum) if weighted_qty_sum > 0 else 0.0
    )
    return {
        "journals_closed": len(closed_ids),
        "journals_kept": len(journals) - len(closed_ids),
        "closed_ids": closed_ids,
        "total_pnl_pct": total_pnl_pct,
    }


def _append_journal_warning(existing: str | None, new_message: str) -> str:
    """Append a new journal warning to an existing one."""
    return new_message if not existing else f"{existing}; {new_message}"
```

- [ ] **Step 2: Verify the new file passes lint**

Run: `uv run ruff check app/mcp_server/tooling/order_journal.py`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add app/mcp_server/tooling/order_journal.py
git commit -m "refactor(order-execution): extract journal/fill DB ops into order_journal.py"
```

---

### Task 2: Create `order_validation.py` - Validation & Preview Logic

**Files:**
- Create: `app/mcp_server/tooling/order_validation.py`

This task moves these functions out of `order_execution.py`:
- `_get_current_price_for_order` (lines 177-188)
- `_get_holdings_for_order` (lines 191-230)
- `_get_balance_for_order` (lines 233-254)
- `_check_daily_order_limit` (lines 257-280)
- `_record_order_history` (lines 283-322)
- `_preview_order` (lines 325-429) — split into `_preview_buy` and `_preview_sell` helpers

Also extracts from `_place_order_impl`:
- Amount-to-quantity conversion logic (lines 793-818) -> `_resolve_buy_quantity`
- Sell-side validation logic (lines 821-848) -> `_validate_sell_side`
- Balance precheck logic (lines 883-921) -> `_check_balance_and_warn`

- [ ] **Step 1: Create `order_validation.py`**

```python
"""Order validation, price lookup, and preview logic."""

from __future__ import annotations

import datetime
import json
from typing import Any

import app.services.brokers.upbit.client as upbit_service
from app.core.config import settings
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
                balance
                if balance >= min_market_buy_amount
                else min_market_buy_amount
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
) -> dict[str, Any]:
    """Build a dry-run preview dict for a sell order."""
    result: dict[str, Any] = {
        "symbol": symbol,
        "side": "sell",
        "order_type": order_type,
        "current_price": current_price,
    }

    holdings = await _get_holdings_for_order(symbol, market_type)
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
        result["price"] = execution_price

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
        raise ValueError(f"Failed to get current price for amount conversion")
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
) -> tuple[float, float, dict[str, Any] | None]:
    """Validate sell-side: check holdings, locked, price constraints.

    Returns (order_quantity, avg_price, error_dict_or_None).
    """
    holdings = await _get_holdings_for_order(normalized_symbol, market_type)
    if not holdings:
        return 0.0, 0.0, order_error_fn(f"No holdings found for {symbol}")

    available_quantity = _to_float(holdings.get("quantity"), default=0.0)
    locked_quantity = _to_float(holdings.get("locked"), default=0.0)

    if quantity is not None and quantity > available_quantity:
        return 0.0, 0.0, order_error_fn(
            f"Requested sell quantity {quantity} exceeds orderable balance {available_quantity}. "
            f"locked={locked_quantity} (in open orders, not sellable)."
        )

    order_quantity = available_quantity if quantity is None else quantity
    avg_price = _to_float(holdings.get("avg_price"), default=0.0)

    if order_type == "limit" and price is not None:
        min_sell_price = avg_price * 1.01
        if price < min_sell_price:
            return 0.0, 0.0, order_error_fn(
                f"Sell price {price} below minimum "
                f"(avg_buy_price * 1.01 = {min_sell_price:.0f})"
            )
        if price < current_price:
            return 0.0, 0.0, order_error_fn(
                f"Sell price {price} below current price {current_price}"
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
) -> tuple[str | None, dict[str, Any] | None]:
    """Pre-check cash balance for buy orders.

    Returns (warning_message_or_None, error_dict_or_None).
    If error_dict is not None, the caller should return it immediately.
    """
    try:
        balance = await _get_balance_for_order(market_type)
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
```

- [ ] **Step 2: Verify the new file passes lint**

Run: `uv run ruff check app/mcp_server/tooling/order_validation.py`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add app/mcp_server/tooling/order_validation.py
git commit -m "refactor(order-execution): extract validation/preview logic into order_validation.py"
```

---

### Task 3: Rewrite `order_execution.py` as Thin Orchestrator

**Files:**
- Modify: `app/mcp_server/tooling/order_execution.py` (full rewrite)

This task replaces the entire content of `order_execution.py` with:
1. Re-exports from `order_validation.py` and `order_journal.py`
2. A slim `_execute_order` function (unchanged, ~100 lines — it dispatches to 3 different brokers so splitting further would hurt readability)
3. A refactored `_place_order_impl` broken into: validate -> execute -> record phases

- [ ] **Step 1: Rewrite `order_execution.py`**

```python
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
    return await upbit_service.place_sell_order(
        symbol, volume_str, f"{adjusted_price}"
    )


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
    symbol: str, side: str, order_type: str, price: float | None,
    amount: float | None, quantity: float | None,
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
    normalized_symbol: str, market_type: str, order_type: str, price: float | None,
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
        dry_run_result["price"] = (
            current_price if order_type == "market" else price
        )
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
            journal_warning = f"trade journal creation failed after order execution: {journal_exc}"
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
            journal_warning = "trade journal created but fill was not recorded; journal remains draft"
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
        symbol, side, order_type, price, amount, quantity,
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
            normalized_symbol, market_type, order_type_lower, price,
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
    "_save_order_fill",
    "_validate_buy_journal_requirements",
    "_append_journal_warning",
    "_get_crypto_trade_cooldown_service",
    "CRYPTO_STOP_LOSS_PCT",
]
```

- [ ] **Step 2: Verify lint passes**

Run: `uv run ruff check app/mcp_server/tooling/order_execution.py app/mcp_server/tooling/order_validation.py app/mcp_server/tooling/order_journal.py`
Expected: No errors

- [ ] **Step 3: Verify format passes**

Run: `uv run ruff format --check app/mcp_server/tooling/order_execution.py app/mcp_server/tooling/order_validation.py app/mcp_server/tooling/order_journal.py`
Expected: No reformatting needed (or run `make format` first)

- [ ] **Step 4: Commit**

```bash
git add app/mcp_server/tooling/order_execution.py
git commit -m "refactor(order-execution): rewrite as thin orchestrator with validate->execute->record"
```

---

### Task 4: Run Full Test Suite and Fix Issues

**Files:**
- Potentially fix: `app/mcp_server/tooling/order_execution.py`, `order_validation.py`, `order_journal.py`
- Test: `tests/test_mcp_place_order.py`, `tests/test_mcp_trade_journal.py`, `tests/test_mcp_order_tools.py`

- [ ] **Step 1: Run the critical test file**

Run: `uv run pytest tests/test_mcp_place_order.py -v --tb=short`
Expected: All 35 tests pass

- [ ] **Step 2: Run journal tests**

Run: `uv run pytest tests/test_mcp_trade_journal.py -v --tb=short`
Expected: All tests pass

- [ ] **Step 3: Run order tools tests**

Run: `uv run pytest tests/test_mcp_order_tools.py -v --tb=short`
Expected: All tests pass

- [ ] **Step 4: Run full lint**

Run: `make lint`
Expected: No errors

- [ ] **Step 5: Run full unit test suite**

Run: `make test-unit`
Expected: All tests pass

- [ ] **Step 6: Fix any failures**

If any test fails:
1. Read the traceback carefully
2. Check if it's an import path issue (most likely cause) — add missing re-export to `order_execution.py`
3. Check if a function signature changed — restore original signature
4. Check if the `globals().get("_preview_order", _preview_order)` pattern in `_build_preview` still works — tests monkeypatch `order_execution._preview_order`
5. Fix and re-run

- [ ] **Step 7: Commit fixes if any**

```bash
git add -A
git commit -m "fix: resolve test failures from order_execution refactor"
```

---

### Task 5: Verify Shared.py Untouched and Final Validation

- [ ] **Step 1: Verify shared.py has no changes**

Run: `git diff app/mcp_server/tooling/shared.py`
Expected: Empty output (no changes)

- [ ] **Step 2: Verify external imports still work**

Run: `python -c "from app.mcp_server.tooling.order_execution import _place_order_impl, _normalize_market_type_to_external, _close_journals_on_sell, _create_trade_journal_for_buy, _link_journal_to_fill; print('All imports OK')"`
Expected: `All imports OK`

- [ ] **Step 3: Run full lint + unit tests one final time**

Run: `make lint && make test-unit`
Expected: All green

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "refactor(order-execution): split into order_validation.py + order_journal.py + slim orchestrator

- order_validation.py: price fetch, holdings, balance check, preview (buy/sell helpers), amount→quantity, sell-side validation
- order_journal.py: save_order_fill, link_journal_to_fill, create_trade_journal_for_buy, close_journals_on_sell (FIFO)
- order_execution.py: thin orchestrator with validate→execute→record phases
- All existing import paths preserved via re-exports
- shared.py untouched"
```
