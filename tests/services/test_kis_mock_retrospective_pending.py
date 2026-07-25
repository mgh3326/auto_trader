"""ROB-730 — kis_mock source branch in build_retrospective_pending.

Mock terminal ledger events (fill/reconciled/failed/anomaly) must surface in the
retrospective due-list, tagged account_mode='kis_mock' and filterable separately
from live. cancel-family (cancelled/stale) is hidden unless include_cancelled.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.core.timezone import now_kst
from app.models.review import KISMockOrderLedger
from app.models.trading import InstrumentType
from app.services.trade_journal.trade_retrospective_service import (
    build_retrospective_pending,
)


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _pending_with_retry(db: Any, **kwargs: Any) -> Any:
    """Call build_retrospective_pending, retrying on deadlock.

    It scans the live order ledgers too, which on the shared xdist test DB can
    deadlock with the parallel live-ledger tests. A deadlock victim is a
    read-only query here, safe to rollback + retry.
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


async def _make_mock_order(
    db: Any,
    *,
    symbol: str,
    side: str,
    lifecycle_state: str,
    status: str = "accepted",
    correlation_id: str | None = None,
) -> KISMockOrderLedger:
    row = KISMockOrderLedger(
        trade_date=now_kst(),
        symbol=symbol,
        instrument_type=InstrumentType.equity_kr,
        side=side,
        order_type="limit",
        quantity=Decimal("10"),
        price=Decimal("70000"),
        amount=Decimal("700000"),
        fee=Decimal("0"),
        currency="KRW",
        order_no=_uniq("MOCK"),
        account_mode="kis_mock",
        broker="kis",
        status=status,
        lifecycle_state=lifecycle_state,
        correlation_id=correlation_id,
    )
    db.add(row)
    # COMMIT (not flush): the pending scan touches the live ledgers too; an
    # uncommitted write here would hold locks during that broad scan and
    # deadlock with parallel live-ledger tests (xdist + shared DB).
    await db.commit()
    return row


@pytest.mark.asyncio
async def test_mock_fill_surfaces_as_pending(db_session: Any) -> None:
    symbol = _uniq("005930")
    cid = _uniq("live:kis_mock:buy")
    await _make_mock_order(
        db_session,
        symbol=symbol,
        side="buy",
        lifecycle_state="fill",
        correlation_id=cid,
    )
    today = now_kst().strftime("%Y-%m-%d")
    result = await _pending_with_retry(
        db_session, kst_date_from=today, kst_date_to=today, include_cancelled=False
    )
    mock = [
        e
        for e in result["pending"]
        if e.get("account_mode") == "kis_mock" and e.get("symbol") == symbol
    ]
    assert mock, result["pending"]
    assert mock[0]["ledger"] == "kis_mock"
    assert mock[0]["status"] == "fill"
    assert mock[0]["market"] == "kr"
    assert mock[0]["suggested_correlation_id"] == cid


@pytest.mark.asyncio
async def test_mock_filter_isolates_from_live(db_session: Any) -> None:
    symbol = _uniq("005930")
    await _make_mock_order(
        db_session, symbol=symbol, side="buy", lifecycle_state="reconciled"
    )
    today = now_kst().strftime("%Y-%m-%d")
    # account_mode="kis_mock" -> mock present
    only_mock = await _pending_with_retry(
        db_session, kst_date_from=today, kst_date_to=today, account_mode="kis_mock"
    )
    assert any(e.get("symbol") == symbol for e in only_mock["pending"])
    assert all(e.get("account_mode") == "kis_mock" for e in only_mock["pending"])
    # account_mode="kis_live" -> mock absent (separately filterable)
    only_live = await _pending_with_retry(
        db_session, kst_date_from=today, kst_date_to=today, account_mode="kis_live"
    )
    assert not any(e.get("symbol") == symbol for e in only_live["pending"])


@pytest.mark.asyncio
async def test_mock_failed_and_anomaly_surface_by_default(db_session: Any) -> None:
    failed_sym = _uniq("FAILT")
    anomaly_sym = _uniq("ANOM")
    await _make_mock_order(
        db_session,
        symbol=failed_sym,
        side="buy",
        lifecycle_state="failed",
        status="rejected",
    )
    await _make_mock_order(
        db_session, symbol=anomaly_sym, side="buy", lifecycle_state="anomaly"
    )
    today = now_kst().strftime("%Y-%m-%d")
    result = await _pending_with_retry(
        db_session, kst_date_from=today, kst_date_to=today, account_mode="kis_mock"
    )
    syms = {e.get("symbol") for e in result["pending"]}
    assert failed_sym in syms
    assert anomaly_sym in syms


@pytest.mark.asyncio
async def test_mock_cancel_family_hidden_unless_included(db_session: Any) -> None:
    cancelled_sym = _uniq("CANC")
    stale_sym = _uniq("STAL")
    await _make_mock_order(
        db_session, symbol=cancelled_sym, side="buy", lifecycle_state="cancelled"
    )
    await _make_mock_order(
        db_session, symbol=stale_sym, side="buy", lifecycle_state="stale"
    )
    today = now_kst().strftime("%Y-%m-%d")
    # default: hidden
    default = await _pending_with_retry(
        db_session,
        kst_date_from=today,
        kst_date_to=today,
        account_mode="kis_mock",
        include_cancelled=False,
    )
    default_syms = {e.get("symbol") for e in default["pending"]}
    assert cancelled_sym not in default_syms
    assert stale_sym not in default_syms
    # include_cancelled=True: surfaced
    included = await _pending_with_retry(
        db_session,
        kst_date_from=today,
        kst_date_to=today,
        account_mode="kis_mock",
        include_cancelled=True,
    )
    included_syms = {e.get("symbol") for e in included["pending"]}
    assert cancelled_sym in included_syms
    assert stale_sym in included_syms
