"""ROB-843 Blockers 1 & 4 — cooldown anchor + daily broker-order de-dup.

Cooldown must key off the position-closing SELL's ``reconciled_at`` (not
``trade_date``, not a BUY), and fail-close on a legacy close missing it. The
daily broker-order count must count each actually-submitted order once,
excluding synthetic scalping audit rows and non-submitted rows.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import delete, update

from app.core.timezone import now_kst
from app.mcp_server.tooling.kis_mock_ledger import (
    _order_session_factory,
    _save_kis_mock_order_ledger,
)
from app.models.review import KISMockOrderLedger
from app.services.brokers.kis.mock_scalping_exec.ledger_state import (
    _start_of_kst_day,
    count_daily_broker_orders,
    load_kis_mock_order_history,
)


async def _reset(symbol: str) -> None:
    """Clear a symbol's rows so tests are deterministic on the persistent
    shared test DB (committed rows survive across runs and days)."""
    async with _order_session_factory()() as db:
        await db.execute(
            delete(KISMockOrderLedger).where(KISMockOrderLedger.symbol == symbol)
        )
        await db.commit()


async def _ins(**over) -> None:
    kw = {
        "symbol": "000000",
        "instrument_type": "equity_kr",
        "side": "buy",
        "order_type": "limit",
        "quantity": 1.0,
        "price": 1000.0,
        "amount": 1000.0,
        "currency": "KRW",
        "order_no": None,
        "order_time": None,
        "krx_fwdg_ord_orgno": None,
        "status": "accepted",
        "response_code": None,
        "response_message": None,
        "raw_response": None,
        "reason": "t",
        "thesis": None,
        "strategy": None,
        "notes": None,
    }
    kw.update(over)
    await _save_kis_mock_order_ledger(**kw)


async def _backdate(symbol: str, *, trade_date=None, reconciled_at="__keep__") -> None:
    values: dict = {}
    if trade_date is not None:
        values["trade_date"] = trade_date
    if reconciled_at != "__keep__":
        values["reconciled_at"] = reconciled_at
    async with _order_session_factory()() as db:
        await db.execute(
            update(KISMockOrderLedger)
            .where(KISMockOrderLedger.symbol == symbol)
            .values(**values)
        )
        await db.commit()


# --- Blocker 1: cooldown anchor -----------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cooldown_uses_reconciled_at_not_trade_date(db_session) -> None:
    sym = "900101"
    await _reset(sym)
    await _ins(
        symbol=sym, side="sell", lifecycle_state="reconciled", order_no=f"c-{sym}"
    )
    # Backdate trade_date 30 days; reconciled_at stays ~now (auto-stamped).
    await _backdate(sym, trade_date=now_kst() - timedelta(days=30))
    hist = await load_kis_mock_order_history(symbol=sym)
    # If trade_date were the anchor this would be ~2.6M seconds; reconciled_at
    # (recent) keeps it small -> cooldown active off the real close time.
    assert hist.seconds_since_last_close_for_symbol is not None
    assert hist.seconds_since_last_close_for_symbol < 3600


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconciled_buy_is_not_a_close_anchor(db_session) -> None:
    sym = "900102"
    await _reset(sym)
    await _ins(
        symbol=sym, side="buy", lifecycle_state="reconciled", order_no=f"b-{sym}"
    )
    hist = await load_kis_mock_order_history(symbol=sym)
    assert hist.seconds_since_last_close_for_symbol is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_and_anomaly_sells_are_not_close_anchors(db_session) -> None:
    sym = "900103"
    await _reset(sym)
    await _ins(
        symbol=sym,
        side="sell",
        lifecycle_state="failed",
        status="rejected",
        order_no=f"f-{sym}",
    )
    await _ins(
        symbol=sym,
        side="sell",
        lifecycle_state="anomaly",
        status="unknown",
        order_no=f"a-{sym}",
    )
    hist = await load_kis_mock_order_history(symbol=sym)
    assert hist.seconds_since_last_close_for_symbol is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_latest_sell_reconciled_at_is_selected(db_session) -> None:
    sym = "900104"
    await _reset(sym)
    await _ins(
        symbol=sym, side="sell", lifecycle_state="reconciled", order_no=f"old-{sym}"
    )
    await _backdate(sym, reconciled_at=now_kst() - timedelta(hours=5))
    await _ins(
        symbol=sym, side="sell", lifecycle_state="reconciled", order_no=f"new-{sym}"
    )
    # newest row keeps auto reconciled_at ~ now; MAX(reconciled_at) -> small age
    hist = await load_kis_mock_order_history(symbol=sym)
    assert hist.seconds_since_last_close_for_symbol is not None
    assert hist.seconds_since_last_close_for_symbol < 3600


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_close_missing_reconciled_at_fail_closes(db_session) -> None:
    sym = "900105"
    await _reset(sym)
    await _ins(
        symbol=sym, side="sell", lifecycle_state="reconciled", order_no=f"c-{sym}"
    )
    await _backdate(sym, reconciled_at=None)  # simulate legacy null
    with pytest.raises(RuntimeError):
        await load_kis_mock_order_history(symbol=sym)


# --- Blocker 4: daily broker-order de-dup -------------------------------------


async def _count(symbol: str) -> int:
    since = _start_of_kst_day(now_kst())
    async with _order_session_factory()() as db:
        return await count_daily_broker_orders(db, since=since, symbol=symbol)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_native_plus_synthetic_counts_once(db_session) -> None:
    sym = "900201"
    await _reset(sym)
    await _ins(symbol=sym, order_no=f"N1-{sym}")  # native (scalping_role None)
    await _ins(symbol=sym, order_no=f"N1-{sym}-entry", scalping_role="entry")
    assert await _count(sym) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_distinct_broker_ids_count_two(db_session) -> None:
    sym = "900202"
    await _reset(sym)
    await _ins(symbol=sym, order_no=f"B1-{sym}")
    await _ins(symbol=sym, order_no=f"S1-{sym}")
    assert await _count(sym) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_trimmed_id_counts_once(db_session) -> None:
    sym = "900203"
    await _reset(sym)
    await _ins(symbol=sym, order_no=f"D1-{sym}")
    await _ins(symbol=sym, order_no=f" D1-{sym} ")  # whitespace variant, same id
    assert await _count(sym) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_preview_blocked_and_synthetic_count_zero(db_session) -> None:
    sym = "900204"
    await _reset(sym)
    # rejected native with no broker id (pre-submit failure / id-less)
    await _ins(symbol=sym, order_no=None, status="rejected", lifecycle_state="failed")
    # synthetic audit row (mirrors a submission; excluded)
    await _ins(symbol=sym, order_no=f"X1-{sym}-exit", scalping_role="exit")
    assert await _count(sym) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_native_plus_synthetic_fill_counts_once(db_session) -> None:
    """P1-2: a native row + a real synthetic fill (shared correlation_id+side)
    is one submission, not two."""
    sym = "900206"
    await _reset(sym)
    cid = f"cid-{sym}"
    await _ins(symbol=sym, side="buy", order_no=f"N-{sym}", correlation_id=cid)
    await _ins(
        symbol=sym,
        side="buy",
        order_no=f"N-{sym}-entry",
        scalping_role="entry",
        lifecycle_state="fill",
        correlation_id=cid,
    )
    assert await _count(sym) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_synthetic_only_fill_is_counted_as_fallback(db_session) -> None:
    """P1-2: native write lost — a synthetic fill is the only durable evidence,
    so it still counts the submission (no undercount)."""
    sym = "900207"
    await _reset(sym)
    cid = f"cid-{sym}"
    await _ins(
        symbol=sym,
        side="buy",
        order_no=f"S-{sym}-entry",
        scalping_role="entry",
        lifecycle_state="fill",
        correlation_id=cid,
    )
    assert await _count(sym) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_synthetic_anomaly_fallback_is_counted(db_session) -> None:
    """P1-2: an unfilled/anomaly synthetic (native lost) still counts once."""
    sym = "900208"
    await _reset(sym)
    cid = f"cid-{sym}"
    await _ins(
        symbol=sym,
        side="sell",
        order_no=f"A-{sym}",
        scalping_role="native_fallback",
        lifecycle_state="anomaly",
        status="unknown",
        correlation_id=cid,
    )
    assert await _count(sym) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fallback_and_later_synthetic_dedupe(db_session) -> None:
    """P1-2: a tracking-fallback row and the later synthetic fill share
    (correlation_id, side) and de-dup to a single count."""
    sym = "900209"
    await _reset(sym)
    cid = f"cid-{sym}"
    await _ins(
        symbol=sym,
        side="buy",
        order_no=None,
        scalping_role="native_fallback",
        lifecycle_state="anomaly",
        status="unknown",
        correlation_id=cid,
    )
    await _ins(
        symbol=sym,
        side="buy",
        order_no=f"L-{sym}-entry",
        scalping_role="entry",
        lifecycle_state="fill",
        correlation_id=cid,
    )
    assert await _count(sym) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_buy_and_sell_submissions_each_count(db_session) -> None:
    sym = "900205"
    await _reset(sym)
    await _ins(symbol=sym, side="buy", order_no=f"BUY1-{sym}")
    await _ins(symbol=sym, side="sell", order_no=f"SELL1-{sym}")
    assert await _count(sym) == 2
