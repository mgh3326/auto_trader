"""Paper Trading Service — virtual account/order/position management."""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.mcp_server.tooling.shared import resolve_market_type
from app.models.paper_trading import PaperAccount, PaperPosition, PaperTrade
from app.models.trading import InstrumentType

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


__all__ = [
    "FEE_RATES",
    "calculate_fee",
    "PaperTradingService",
]
