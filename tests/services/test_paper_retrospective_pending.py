"""ROB-705 — Paper source branch in retrospective-pending + stop_loss suggestion."""

import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.core.timezone import now_kst
from app.models.paper_trading import PaperTrade
from app.models.trading import InstrumentType
from app.services.paper_trading_service import PaperTradingService
from app.services.trade_journal.trade_retrospective_service import (
    build_retrospective_pending,
)


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _pending_with_retry(db: Any, **kwargs: Any) -> Any:
    """Call build_retrospective_pending, retrying on deadlock.

    It scans the live order ledgers (kis_live_order_ledger etc.), which on the
    shared xdist test DB deadlocks with the parallel kis_live-ledger tests. A
    deadlock victim is a read-only query here, safe to rollback + retry.
    """
    from sqlalchemy.exc import DBAPIError

    last: Exception | None = None
    for _ in range(6):
        try:
            return await build_retrospective_pending(db, **kwargs)
        except DBAPIError as exc:
            if "deadlock" not in str(exc).lower():
                raise
            last = exc
            await db.rollback()
    assert last is not None
    raise last


async def _make_paper_trade(
    db: Any,
    *,
    account_id: int,
    symbol: str,
    side: str,
    price: Decimal,
    realized_pnl: Decimal | None = None,
    correlation_id: str | None = None,
) -> PaperTrade:
    qty = Decimal("0.01")
    trade = PaperTrade(
        account_id=account_id,
        symbol=symbol,
        instrument_type=InstrumentType.crypto,
        side=side,
        order_type="market",
        quantity=qty,
        price=price,
        total_amount=price * qty,
        fee=Decimal("0"),
        currency="KRW",
        realized_pnl=realized_pnl,
        correlation_id=correlation_id,
        executed_at=now_kst(),
    )
    db.add(trade)
    # COMMIT (not flush): build_retrospective_pending scans the live order
    # ledgers too. If this INSERT stays uncommitted, the test transaction holds
    # write locks during that broad multi-ledger scan and deadlocks with the
    # parallel kis_live-ledger tests (xdist + shared DB). Committing first means
    # the scan runs lock-free -> out of any deadlock cycle.
    await db.commit()
    return trade


@pytest.mark.asyncio
async def test_paper_fill_surfaces_as_pending(db_session: Any) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob705-pend"), initial_capital_krw=Decimal("100000000")
    )
    await _make_paper_trade(
        db_session,
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        price=Decimal("50000000"),
        correlation_id="paper:1:buyabc",
    )
    today = now_kst().strftime("%Y-%m-%d")
    result = await _pending_with_retry(
        db_session, kst_date_from=today, kst_date_to=today, include_cancelled=False
    )
    paper = [
        e
        for e in result["pending"]
        if e.get("account_mode") == "paper" and e.get("symbol") == "KRW-BTC"
    ]
    assert paper, result["pending"]
    assert paper[0]["suggested_correlation_id"] == "paper:1:buyabc"


@pytest.mark.asyncio
async def test_loss_sell_surfaces_with_stop_loss_suggestion(db_session: Any) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob705-stop"), initial_capital_krw=Decimal("100000000")
    )
    await _make_paper_trade(
        db_session,
        account_id=acct.id,
        symbol="KRW-ETH",
        side="sell",
        price=Decimal("45000000"),
        realized_pnl=Decimal("-50000"),
        correlation_id="paper:1:stopxyz",
    )
    today = now_kst().strftime("%Y-%m-%d")
    result = await _pending_with_retry(
        db_session, kst_date_from=today, kst_date_to=today, include_cancelled=False
    )
    sells = [
        e
        for e in result["pending"]
        if e.get("account_mode") == "paper"
        and e.get("symbol") == "KRW-ETH"
        and e.get("side") == "sell"
    ]
    assert sells, result["pending"]
    assert sells[0]["suggested_trigger_type"] == "stop_loss"


@pytest.mark.asyncio
async def test_profitable_sell_has_no_stop_loss_suggestion(db_session: Any) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob705-win"), initial_capital_krw=Decimal("100000000")
    )
    await _make_paper_trade(
        db_session,
        account_id=acct.id,
        symbol="KRW-SOL",
        side="sell",
        price=Decimal("55000000"),
        realized_pnl=Decimal("50000"),
        correlation_id="paper:1:win123",
    )
    today = now_kst().strftime("%Y-%m-%d")
    result = await _pending_with_retry(
        db_session, kst_date_from=today, kst_date_to=today, include_cancelled=False
    )
    sol = [
        e
        for e in result["pending"]
        if e.get("account_mode") == "paper" and e.get("symbol") == "KRW-SOL"
    ]
    assert sol, result["pending"]
    assert sol[0].get("suggested_trigger_type") is None
