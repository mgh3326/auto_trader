import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.symbol import to_db_symbol
from app.models.review import KISLiveOrderLedger
from app.services.trade_journal.aggregates import load_fills


def _digit_symbol() -> str:
    """Per-test unique 6-12 digit-ish symbol, xdist-safe.

    The ``db_session`` fixture commits to a SHARED database with no per-test
    rollback (see tests/services/test_decision_history.py). Real symbols like
    005930 leak across workers — and into the real-session decision_history
    injection exercised by test_analyze_stock_batch_quick_summary. Seed under a
    unique symbol and with ``flush()`` (visible to this session, never
    committed) so nothing escapes the test.
    """
    return ("9" + uuid.uuid4().hex[:9])[:10].upper()


@pytest.mark.asyncio
async def test_load_fills_reads_kis_filled_rows(db_session):
    sym = _digit_symbol()
    corr = "rob713-load-" + uuid.uuid4().hex[:8]
    item = uuid.uuid4()
    db_session.add(
        KISLiveOrderLedger(
            trade_date=datetime(2026, 6, 1, tzinfo=UTC),
            symbol=sym,
            instrument_type="equity_kr",
            side="buy",
            order_type="limit",
            account_mode="kis_live",
            broker="kis",
            status="filled",
            lifecycle_state="filled",
            quantity=Decimal("10"),
            filled_qty=Decimal("10"),
            avg_fill_price=Decimal("100"),
            correlation_id=corr,
            report_item_uuid=item,
        )
    )
    await db_session.flush()

    fills = await load_fills(db_session, market="kr")
    ours = [f for f in fills if f.correlation_id == corr]
    assert len(ours) == 1
    f = ours[0]
    assert f.symbol == to_db_symbol(sym)
    assert f.side == "buy"
    assert f.qty == 10
    assert f.market == "kr"
    assert f.price == 100.0
    assert f.item_uuid == str(item)


@pytest.mark.asyncio
async def test_load_fills_skips_unfilled_and_smoke(db_session):
    corr = "rob713-unfilled-" + uuid.uuid4().hex[:8]
    db_session.add(
        KISLiveOrderLedger(
            trade_date=datetime(2026, 6, 1, tzinfo=UTC),
            symbol=_digit_symbol(),
            instrument_type="equity_kr",
            side="buy",
            order_type="limit",
            account_mode="kis_live",
            broker="kis",
            status="accepted",
            lifecycle_state="accepted",
            quantity=Decimal("10"),
            filled_qty=Decimal("0"),
            correlation_id=corr,
        )
    )
    await db_session.flush()
    fills = await load_fills(db_session, market="kr")
    assert not any(f.correlation_id == corr for f in fills)
