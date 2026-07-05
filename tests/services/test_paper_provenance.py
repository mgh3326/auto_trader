"""ROB-705 — Place-time provenance integration tests.

Verifies ``place_limit_order`` stamps the deterministic ``correlation_id``
spine on the ``PaperPendingOrder``, links a draft ``TradeJournal``, and (when
probability/target/review-date are supplied) creates a ``price_target``
Forecast carrying the same correlation id.
"""

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.models.paper_trading import PaperPendingOrder
from app.services.paper_limit_order_service import PaperLimitOrderService
from app.services.paper_trading_service import PaperTradingService


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_place_stamps_correlation_and_journal(db_session: Any) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob705-prov"), initial_capital_krw=Decimal("1000000")
    )
    svc = PaperLimitOrderService(db_session)
    out = await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        limit_price=Decimal("90000000"),
        amount=Decimal("100000"),
        thesis="support bounce",
        strategy="support_ladder",
        target_price=Decimal("100000000"),
        stop_loss=Decimal("85000000"),
        probability=0.6,
        review_date="2026-07-15",
    )
    assert out["success"], out
    row = (
        await db_session.execute(
            select(PaperPendingOrder).where(PaperPendingOrder.id == out["order_id"])
        )
    ).scalar_one()
    assert row.correlation_id and row.correlation_id.startswith(f"paper:{acct.id}:")
    assert row.journal_id is not None  # draft journal linked
    assert row.forecast_id is not None  # forecast linked
