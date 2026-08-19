from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.market_close_digest.queries import merge_retro_pnl
from app.services.market_close_digest.service import run_market_close_digest
from app.services.market_close_digest.types import LedgerFill, RetroRow
from tests.services.market_close_digest.fakes import AC1_SESSION_DATE

pytestmark = pytest.mark.unit


def test_merge_retro_pnl_fills_gap_only() -> None:
    filled_at = datetime(2026, 8, 19, 20, 0, tzinfo=UTC)
    fill = LedgerFill(
        source="toss_live_order_ledger",
        broker="toss",
        symbol="AAPL",
        side="sell",
        qty=Decimal("1"),
        price=Decimal("220"),
        notional=Decimal("220"),
        pnl=None,
        pnl_pct=None,
        pnl_currency="USD",
        filled_at=filled_at,
        correlation_id="corr-a",
    )
    already = LedgerFill(
        source="toss_live_order_ledger",
        broker="toss",
        symbol="MSFT",
        side="sell",
        qty=Decimal("1"),
        price=Decimal("410"),
        notional=Decimal("410"),
        pnl=Decimal("-3.10"),
        pnl_pct=Decimal("-0.4"),
        pnl_currency="USD",
        filled_at=filled_at,
        correlation_id="corr-b",
    )
    retros = (
        RetroRow(
            symbol="AAPL",
            side="sell",
            realized_pnl=Decimal("12.30"),
            pnl_pct=Decimal("1.2"),
            pnl_currency="USD",
            correlation_id="corr-a",
            created_at=filled_at,
        ),
        RetroRow(
            symbol="MSFT",
            side="sell",
            realized_pnl=Decimal("99"),
            pnl_pct=Decimal("9"),
            pnl_currency="USD",
            correlation_id="corr-b",
            created_at=filled_at,
        ),
    )
    merged = merge_retro_pnl((fill, already), retros)
    assert merged[0].pnl == Decimal("12.30")
    assert merged[0].pnl_pct == Decimal("1.2")
    assert merged[1].pnl == Decimal("-3.10")


@pytest.mark.asyncio
async def test_sqlalchemy_digest_mutation_counter_is_zero(db_session) -> None:
    result = await run_market_close_digest(
        market="us",
        session_date=AC1_SESSION_DATE,
        session=db_session,
        send=False,
    )
    assert result.mutation_count == 0
    assert result.status in {"ok", "zero_fills"}
    assert result.sent is False
