"""Paper Trading Service — virtual account/order/position management."""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.mcp_server.tooling.market_data_quotes import (
    _fetch_quote_equity_kr,
    _fetch_quote_equity_us,
)
from app.mcp_server.tooling.shared import resolve_market_type
from app.models.paper_trading import PaperAccount, PaperPosition, PaperTrade
from app.models.trading import InstrumentType
from app.services.brokers.upbit.client import fetch_multiple_current_prices

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fee schedule
# ---------------------------------------------------------------------------
FEE_RATES: dict[str, dict[str, float]] = {
    "equity_kr": {"buy": 0.00015, "sell": 0.00015, "tax_sell": 0.0018},
    "equity_us": {"buy": 0.0007, "sell": 0.0007, "min_fee_usd": 1.0},
    "crypto": {"buy": 0.0005, "sell": 0.0005},
}

# Quantize targets matching Numeric(20, 4) for money fields
_MONEY_Q = Decimal("0.0001")
# Numeric(20, 8) for crypto quantity; equity quantities are whole shares
_CRYPTO_QTY_Q = Decimal("0.00000001")


def _q_money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_Q, rounding=ROUND_HALF_UP)


def _q_crypto_qty(value: Decimal) -> Decimal:
    return value.quantize(_CRYPTO_QTY_Q, rounding=ROUND_HALF_UP)


def calculate_fee(
    instrument_type: str,
    side: str,
    gross_amount: Decimal,
) -> Decimal:
    """Calculate commission + tax for a simulated fill.

    Parameters
    ----------
    instrument_type : "equity_kr" | "equity_us" | "crypto"
    side : "buy" | "sell"
    gross_amount : quantity * price (in the instrument's currency)
    """
    rates = FEE_RATES.get(instrument_type)
    if rates is None:
        raise ValueError(f"Unsupported instrument_type: {instrument_type}")

    gross = Decimal(gross_amount)

    if instrument_type == "equity_kr":
        commission = gross * Decimal(str(rates["buy" if side == "buy" else "sell"]))
        if side == "sell":
            commission += gross * Decimal(str(rates["tax_sell"]))
        return _q_money(commission)

    if instrument_type == "equity_us":
        commission = gross * Decimal(str(rates["buy" if side == "buy" else "sell"]))
        min_fee = Decimal(str(rates["min_fee_usd"]))
        return _q_money(max(commission, min_fee))

    # crypto
    commission = gross * Decimal(str(rates["buy" if side == "buy" else "sell"]))
    return _q_money(commission)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class PaperTradingService:
    """모의투자 계좌/주문/포지션 관리 서비스."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------ #
    # Account management
    # ------------------------------------------------------------------ #
    async def create_account(
        self,
        *,
        name: str,
        initial_capital_krw: Decimal,
        initial_capital_usd: Decimal = Decimal("0"),
        description: str | None = None,
        strategy_name: str | None = None,
    ) -> PaperAccount:
        initial_total = Decimal(initial_capital_krw) + Decimal(initial_capital_usd)
        account = PaperAccount(
            name=name,
            initial_capital=initial_total,
            cash_krw=Decimal(initial_capital_krw),
            cash_usd=Decimal(initial_capital_usd),
            description=description,
            strategy_name=strategy_name,
            is_active=True,
        )
        self.db.add(account)
        await self.db.commit()
        await self.db.refresh(account)
        return account

    async def get_account(self, account_id: int) -> PaperAccount | None:
        result = await self.db.execute(
            select(PaperAccount).where(PaperAccount.id == account_id)
        )
        return result.scalar_one_or_none()

    async def get_account_by_name(self, name: str) -> PaperAccount | None:
        result = await self.db.execute(
            select(PaperAccount).where(PaperAccount.name == name)
        )
        return result.scalar_one_or_none()

    async def list_accounts(self, is_active: bool | None = True) -> list[PaperAccount]:
        stmt = select(PaperAccount)
        if is_active is not None:
            stmt = stmt.where(PaperAccount.is_active == is_active)
        stmt = stmt.order_by(PaperAccount.created_at.desc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def reset_account(self, account_id: int) -> PaperAccount:
        from sqlalchemy import delete as sa_delete

        account = await self.get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")

        # Wipe positions for this account.
        await self.db.execute(
            sa_delete(PaperPosition).where(PaperPosition.account_id == account_id)
        )

        # Restore cash balances. The model only stores a combined
        # ``initial_capital``, so reset puts everything back into KRW and zeroes
        # USD.
        account.cash_krw = account.initial_capital
        account.cash_usd = Decimal("0")

        await self.db.commit()
        await self.db.refresh(account)
        return account

    async def delete_account(self, account_id: int) -> bool:
        account = await self.get_account(account_id)
        if account is None:
            return False
        await self.db.delete(account)
        await self.db.commit()
        return True

    # ------------------------------------------------------------------ #
    # Price fetch
    # ------------------------------------------------------------------ #
    async def _fetch_current_price(
        self, symbol: str, instrument_type: str
    ) -> Decimal:
        if instrument_type == "equity_kr":
            quote = await _fetch_quote_equity_kr(symbol)
            price = quote.get("price")
        elif instrument_type == "equity_us":
            quote = await _fetch_quote_equity_us(symbol)
            price = quote.get("price")
        elif instrument_type == "crypto":
            prices = await fetch_multiple_current_prices([symbol])
            price = prices.get(symbol)
            if price is None:
                raise ValueError(f"No price for {symbol}")
        else:
            raise ValueError(f"Unsupported instrument_type: {instrument_type}")

        if price is None:
            raise ValueError(f"Failed to fetch price for {symbol}")
        return Decimal(str(price))

    # ------------------------------------------------------------------ #
    # Order preview (shared with execute_order)
    # ------------------------------------------------------------------ #
    async def preview_order(
        self,
        *,
        account_id: int,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal | float | int | None = None,
        price: Decimal | float | int | None = None,
        amount: Decimal | float | int | None = None,
    ) -> dict[str, Any]:
        side = side.lower()
        order_type = order_type.lower()
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if order_type not in ("limit", "market"):
            raise ValueError("order_type must be 'limit' or 'market'")

        account = await self.get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")
        if not account.is_active:
            raise ValueError(f"Account {account_id} is inactive")

        instrument_type, resolved_symbol = resolve_market_type(symbol, None)
        currency = "USD" if instrument_type == "equity_us" else "KRW"

        # Resolve fill price
        if order_type == "market":
            fill_price = await self._fetch_current_price(
                resolved_symbol, instrument_type
            )
        else:
            if price is None:
                raise ValueError("price is required for limit orders")
            fill_price = Decimal(str(price))

        if fill_price <= 0:
            raise ValueError(f"Invalid fill price: {fill_price}")

        # Resolve quantity
        if quantity is None and amount is None:
            raise ValueError("quantity or amount must be provided")

        if quantity is not None:
            qty = Decimal(str(quantity))
        else:
            qty = Decimal(str(amount)) / fill_price

        if instrument_type == "crypto":
            qty = _q_crypto_qty(qty)
        else:
            # integer shares for equities
            qty = Decimal(int(qty))

        if qty <= 0:
            raise ValueError(f"Computed quantity is not positive: {qty}")

        gross = _q_money(qty * fill_price)
        fee = calculate_fee(instrument_type, side, gross)
        total_cost = _q_money(gross + fee) if side == "buy" else _q_money(gross - fee)

        return {
            "success": True,
            "dry_run": True,
            "account_id": account_id,
            "preview": {
                "symbol": resolved_symbol,
                "instrument_type": instrument_type,
                "side": side,
                "order_type": order_type,
                "quantity": qty,
                "price": fill_price,
                "gross": gross,
                "fee": fee,
                "total_cost": total_cost,
                "currency": currency,
            },
        }

    # ------------------------------------------------------------------ #
    # Internal position lookup
    # ------------------------------------------------------------------ #
    async def _get_position(
        self, account_id: int, symbol: str
    ) -> PaperPosition | None:
        result = await self.db.execute(
            select(PaperPosition).where(
                PaperPosition.account_id == account_id,
                PaperPosition.symbol == symbol,
            )
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------ #
    # Order execution
    # ------------------------------------------------------------------ #
    async def execute_order(
        self,
        *,
        account_id: int,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal | float | int | None = None,
        price: Decimal | float | int | None = None,
        amount: Decimal | float | int | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        preview = await self.preview_order(
            account_id=account_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            amount=amount,
        )
        p = preview["preview"]
        account = await self.get_account(account_id)  # refresh (same row)
        assert account is not None  # preview_order already validated

        resolved_symbol = p["symbol"]
        instrument_type = p["instrument_type"]
        qty = p["quantity"]
        fill_price = p["price"]
        gross = p["gross"]
        fee = p["fee"]
        total_cost = p["total_cost"]
        currency = p["currency"]

        realized_pnl: Decimal | None = None

        if side.lower() == "buy":
            # Balance check
            if currency == "USD":
                if account.cash_usd < total_cost:
                    raise ValueError(
                        f"Insufficient USD balance: have {account.cash_usd}, "
                        f"need {total_cost}"
                    )
                account.cash_usd = _q_money(account.cash_usd - total_cost)
            else:
                if account.cash_krw < total_cost:
                    raise ValueError(
                        f"Insufficient KRW balance: have {account.cash_krw}, "
                        f"need {total_cost}"
                    )
                account.cash_krw = _q_money(account.cash_krw - total_cost)

            # Upsert position with weighted-average cost (fee excluded from avg)
            position = await self._get_position(account_id, resolved_symbol)
            if position is None:
                position = PaperPosition(
                    account_id=account_id,
                    symbol=resolved_symbol,
                    instrument_type=InstrumentType(instrument_type),
                    quantity=qty,
                    avg_price=fill_price,
                    total_invested=gross,
                )
                self.db.add(position)
            else:
                new_qty = position.quantity + qty
                new_invested = position.total_invested + gross
                position.avg_price = (new_invested / new_qty) if new_qty > 0 else Decimal("0")
                position.quantity = new_qty
                position.total_invested = _q_money(new_invested)
        else:  # sell
            position = await self._get_position(account_id, resolved_symbol)
            if position is None:
                raise ValueError(
                    f"No position to sell for account {account_id} symbol {resolved_symbol}"
                )
            if position.quantity < qty:
                raise ValueError(
                    f"Insufficient quantity: have {position.quantity}, sell {qty}"
                )

            avg_price = position.avg_price
            proceeds_net = total_cost  # gross - fee (from preview)
            realized_pnl = _q_money(((fill_price - avg_price) * qty) - fee)

            # Credit cash
            if currency == "USD":
                account.cash_usd = _q_money(account.cash_usd + proceeds_net)
            else:
                account.cash_krw = _q_money(account.cash_krw + proceeds_net)

            # Update or delete position
            new_qty = position.quantity - qty
            if new_qty == 0:
                await self.db.delete(position)
            else:
                # Keep avg_price fixed on sell; reduce total_invested proportionally
                position.quantity = new_qty
                position.total_invested = _q_money(avg_price * new_qty)

        trade = PaperTrade(
            account_id=account_id,
            symbol=resolved_symbol,
            instrument_type=InstrumentType(instrument_type),
            side=side.lower(),
            order_type=order_type.lower(),
            quantity=qty,
            price=fill_price,
            total_amount=gross,
            fee=fee,
            currency=currency,
            reason=reason or None,
            realized_pnl=realized_pnl,
            executed_at=now_kst(),
        )
        self.db.add(trade)

        await self.db.commit()

        return {
            "success": True,
            "dry_run": False,
            "account_id": account_id,
            "preview": preview["preview"],
            "execution": {
                "symbol": resolved_symbol,
                "instrument_type": instrument_type,
                "side": side.lower(),
                "order_type": order_type.lower(),
                "quantity": qty,
                "price": fill_price,
                "gross": gross,
                "fee": fee,
                "total_cost": total_cost,
                "currency": currency,
                "realized_pnl": realized_pnl,
                "executed_at": trade.executed_at,
            },
        }

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    async def get_positions(
        self,
        account_id: int,
        market: str | None = None,
    ) -> list[dict[str, Any]]:
        stmt = select(PaperPosition).where(PaperPosition.account_id == account_id)
        if market is not None:
            stmt = stmt.where(PaperPosition.instrument_type == InstrumentType(market))
        stmt = stmt.order_by(PaperPosition.symbol)
        result = await self.db.execute(stmt)
        rows = list(result.scalars().all())

        output: list[dict[str, Any]] = []
        for pos in rows:
            item: dict[str, Any] = {
                "symbol": pos.symbol,
                "instrument_type": pos.instrument_type.value,
                "quantity": pos.quantity,
                "avg_price": pos.avg_price,
                "total_invested": pos.total_invested,
                "current_price": None,
                "evaluation_amount": None,
                "unrealized_pnl": None,
                "pnl_pct": None,
            }
            try:
                current_price = await self._fetch_current_price(
                    pos.symbol, pos.instrument_type.value
                )
                item["current_price"] = current_price
                evaluation = _q_money(current_price * pos.quantity)
                item["evaluation_amount"] = evaluation
                pnl = _q_money(evaluation - pos.total_invested)
                item["unrealized_pnl"] = pnl
                if pos.avg_price > 0:
                    pnl_pct = (
                        (current_price - pos.avg_price) / pos.avg_price * Decimal("100")
                    )
                    item["pnl_pct"] = pnl_pct.quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
            except Exception as exc:
                item["price_error"] = str(exc)
            output.append(item)
        return output

    async def get_position(
        self, account_id: int, symbol: str
    ) -> dict[str, Any] | None:
        resolved_symbol = resolve_market_type(symbol, None)[1]
        pos = await self._get_position(account_id, resolved_symbol)
        if pos is None:
            return None
        positions = await self.get_positions(account_id=account_id)
        for item in positions:
            if item["symbol"] == resolved_symbol:
                return item
        return None

    async def get_cash_balance(self, account_id: int) -> dict[str, Decimal]:
        account = await self.get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")
        return {"krw": account.cash_krw, "usd": account.cash_usd}

    async def get_trade_history(
        self,
        account_id: int,
        symbol: str | None = None,
        side: str | None = None,
        days: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        stmt = select(PaperTrade).where(PaperTrade.account_id == account_id)
        if symbol is not None:
            stmt = stmt.where(PaperTrade.symbol == symbol)
        if side is not None:
            stmt = stmt.where(PaperTrade.side == side.lower())
        if days is not None:
            cutoff = now_kst() - timedelta(days=days)
            stmt = stmt.where(PaperTrade.executed_at >= cutoff)
        stmt = stmt.order_by(PaperTrade.executed_at.desc()).limit(limit)

        result = await self.db.execute(stmt)
        rows = list(result.scalars().all())
        return [
            {
                "id": t.id,
                "symbol": t.symbol,
                "instrument_type": t.instrument_type.value,
                "side": t.side,
                "order_type": t.order_type,
                "quantity": t.quantity,
                "price": t.price,
                "total_amount": t.total_amount,
                "fee": t.fee,
                "currency": t.currency,
                "reason": t.reason,
                "realized_pnl": t.realized_pnl,
                "executed_at": t.executed_at,
            }
            for t in rows
        ]


__all__ = [
    "FEE_RATES",
    "calculate_fee",
    "PaperTradingService",
]
