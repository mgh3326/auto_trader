"""Paper Trading Service — virtual account/order/position management."""

from __future__ import annotations

import logging
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


__all__ = [
    "FEE_RATES",
    "calculate_fee",
    "PaperTradingService",
]
