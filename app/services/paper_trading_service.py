"""Paper Trading Service — virtual account/order/position management."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.paper_trading import PaperAccount, PaperDailySnapshot, PaperPosition, PaperTrade
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
    async def _fetch_current_price(self, symbol: str, instrument_type: str) -> Decimal:
        if instrument_type == "equity_kr":
            from app.mcp_server.tooling.market_data_quotes import _fetch_quote_equity_kr
            quote = await _fetch_quote_equity_kr(symbol)
            price = quote.get("price")
        elif instrument_type == "equity_us":
            from app.mcp_server.tooling.market_data_quotes import _fetch_quote_equity_us
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

        from app.mcp_server.tooling.shared import resolve_market_type
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
    async def _get_position(self, account_id: int, symbol: str) -> PaperPosition | None:
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
                position.avg_price = (
                    (new_invested / new_qty) if new_qty > 0 else Decimal("0")
                )
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

    async def get_position(self, account_id: int, symbol: str) -> dict[str, Any] | None:
        from app.mcp_server.tooling.shared import resolve_market_type
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

    async def get_portfolio_summary(self, account_id: int) -> dict[str, Any]:
        account = await self.get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")

        positions = await self.get_positions(account_id=account_id)

        total_invested = Decimal("0")
        total_evaluated = Decimal("0")
        total_pnl = Decimal("0")
        for p in positions:
            total_invested += Decimal(str(p.get("total_invested") or "0"))
            eval_amt = p.get("evaluation_amount")
            pnl = p.get("unrealized_pnl")
            if eval_amt is not None:
                total_evaluated += Decimal(str(eval_amt))
            if pnl is not None:
                total_pnl += Decimal(str(pnl))

        total_pnl_pct: Decimal | None = None
        if total_invested > 0:
            total_pnl_pct = (total_pnl / total_invested * Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

        return {
            "total_invested": total_invested,
            "total_evaluated": total_evaluated,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "cash_krw": account.cash_krw,
            "cash_usd": account.cash_usd,
            "positions_count": len(positions),
        }


    # ------------------------------------------------------------------ #
    # Daily snapshot
    # ------------------------------------------------------------------ #
    async def _evaluate_positions_value(self, account_id: int) -> Decimal:
        """Sum evaluation_amount across live positions (raw KRW+USD)."""
        positions = await self.get_positions(account_id=account_id)
        total = Decimal("0")
        for p in positions:
            eval_amt = p.get("evaluation_amount")
            if eval_amt is not None:
                total += Decimal(str(eval_amt))
        return _q_money(total)

    async def record_daily_snapshot(self, account_id: int) -> PaperDailySnapshot:
        account = await self.get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")

        today = now_kst().date()

        existing_today = (
            await self.db.execute(
                select(PaperDailySnapshot).where(
                    PaperDailySnapshot.account_id == account_id,
                    PaperDailySnapshot.snapshot_date == today,
                )
            )
        ).scalar_one_or_none()

        positions_value = await self._evaluate_positions_value(account_id)
        total_equity = _q_money(account.cash_krw + account.cash_usd + positions_value)

        prior = (
            await self.db.execute(
                select(PaperDailySnapshot)
                .where(
                    PaperDailySnapshot.account_id == account_id,
                    PaperDailySnapshot.snapshot_date < today,
                )
                .order_by(PaperDailySnapshot.snapshot_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        daily_return_pct: Decimal | None = None
        if prior is not None and prior.total_equity > 0:
            daily_return_pct = (
                (total_equity / prior.total_equity - Decimal("1")) * Decimal("100")
            ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

        if existing_today is None:
            snapshot = PaperDailySnapshot(
                account_id=account_id,
                snapshot_date=today,
                cash_krw=account.cash_krw,
                cash_usd=account.cash_usd,
                positions_value=positions_value,
                total_equity=total_equity,
                daily_return_pct=daily_return_pct,
            )
            self.db.add(snapshot)
        else:
            existing_today.cash_krw = account.cash_krw
            existing_today.cash_usd = account.cash_usd
            existing_today.positions_value = positions_value
            existing_today.total_equity = total_equity
            existing_today.daily_return_pct = daily_return_pct
            snapshot = existing_today

        await self.db.commit()
        return snapshot

    async def calculate_daily_returns(
        self,
        account_id: int,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict[str, Any]]:
        stmt = select(PaperDailySnapshot).where(
            PaperDailySnapshot.account_id == account_id
        )
        if start_date is not None:
            stmt = stmt.where(PaperDailySnapshot.snapshot_date >= start_date)
        if end_date is not None:
            stmt = stmt.where(PaperDailySnapshot.snapshot_date <= end_date)
        stmt = stmt.order_by(PaperDailySnapshot.snapshot_date.asc())

        result = await self.db.execute(stmt)
        snaps = list(result.scalars().all())
        return [
            {
                "date": s.snapshot_date.isoformat(),
                "total_equity": s.total_equity,
                "daily_return_pct": s.daily_return_pct,
            }
            for s in snaps
        ]

    # ------------------------------------------------------------------ #
    # Performance analytics helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_round_trips(trades: list[PaperTrade]) -> list[dict[str, Any]]:
        """Group raw trades into round trips per symbol until position is flat.
        Excludes open (unclosed) trips."""
        from collections import defaultdict

        grouped: dict[str, list[tuple[int, PaperTrade]]] = defaultdict(list)
        for idx, t in enumerate(trades):
            grouped[t.symbol].append((idx, t))

        round_trips: list[dict[str, Any]] = []
        for symbol, indexed in grouped.items():
            indexed.sort(key=lambda item: (item[1].executed_at, item[0]))
            position_qty = Decimal("0")
            buy_cost = Decimal("0")
            total_pnl = Decimal("0")
            entry_date: datetime | None = None
            entry_reason = ""
            last_exit_date: datetime | None = None
            last_sell_reason = ""

            for _, t in indexed:
                qty = Decimal(t.quantity)
                if t.side == "buy":
                    if position_qty <= 0:
                        entry_date = t.executed_at
                        entry_reason = t.reason or ""
                        buy_cost = Decimal("0")
                        total_pnl = Decimal("0")
                    position_qty += qty
                    buy_cost += qty * Decimal(t.price) + Decimal(t.fee)
                elif t.side == "sell" and position_qty > 0:
                    position_qty -= qty
                    total_pnl += Decimal(t.realized_pnl or 0)
                    last_exit_date = t.executed_at
                    last_sell_reason = t.reason or ""

                    if position_qty <= 0 and entry_date is not None and last_exit_date is not None:
                        holding_days = (last_exit_date.date() - entry_date.date()).days
                        return_pct = (
                            float(total_pnl / buy_cost * Decimal("100"))
                            if buy_cost > 0 else 0.0
                        )
                        round_trips.append({
                            "symbol": symbol,
                            "entry_date": entry_date.isoformat(),
                            "exit_date": last_exit_date.isoformat(),
                            "holding_days": max(holding_days, 0),
                            "pnl": float(total_pnl),
                            "return_pct": return_pct,
                            "entry_reason": entry_reason,
                            "exit_reason": last_sell_reason,
                        })
                        position_qty = Decimal("0")
                        buy_cost = Decimal("0")
                        total_pnl = Decimal("0")
                        entry_date = None
                        entry_reason = ""
                        last_exit_date = None
                        last_sell_reason = ""

        round_trips.sort(key=lambda trip: (trip["exit_date"], trip["symbol"]))
        return round_trips

    @staticmethod
    def _calc_max_drawdown_pct(equity_curve: list[Decimal]) -> float | None:
        if not equity_curve:
            return None
        peak = equity_curve[0]
        max_dd = Decimal("0")
        for v in equity_curve:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (peak - v) / peak
                if dd > max_dd:
                    max_dd = dd
        return float(max_dd * Decimal("100"))

    @staticmethod
    def _calc_sharpe_ratio(daily_returns_pct: list[Decimal]) -> float | None:
        """Annualised Sharpe ratio (252 trading days). Assumes 0% risk-free rate."""
        import math
        values = [float(r) for r in daily_returns_pct if r is not None]
        if len(values) < 2:
            return None
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        stdev = math.sqrt(variance)
        if stdev == 0:
            return None
        return (mean / stdev) * math.sqrt(252)


__all__ = [
    "FEE_RATES",
    "calculate_fee",
    "PaperTradingService",
]
