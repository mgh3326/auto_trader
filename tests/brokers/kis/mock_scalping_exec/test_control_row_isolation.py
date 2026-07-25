"""ROB-843 P2 — control/reservation rows must never be treated as trades.

Any legacy control row in review.kis_mock_order_ledger (reserved symbol /
scalping_role / reason) is excluded by a shared real-order predicate from the
retrospective scan and the roundtrip journal bridge, while genuine entry/exit
anomalies and reconciled trades stay included.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.timezone import now_kst
from app.mcp_server.tooling.kis_mock_ledger import (
    _order_session_factory,
    _save_kis_mock_order_ledger,
)
from app.models.review import KISMockOrderLedger
from app.models.trade_journal import TradeJournal
from app.services.trade_journal.mock_roundtrip_journal_bridge import (
    sync_mock_roundtrip_journals,
)
from app.services.trade_journal.trade_retrospective_service import (
    build_retrospective_pending,
)

_CONTROL_SYMBOL = "__ledger_tracking__"


async def _ins(**over) -> None:
    kw = {
        "symbol": "000000",
        "instrument_type": "equity_kr",
        "side": "buy",
        "order_type": "limit",
        "quantity": 1.0,
        "price": 70000.0,
        "amount": 70000.0,
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


@pytest_asyncio.fixture(autouse=True)
async def _clear_control_and_journals():
    async def _c():
        async with _order_session_factory()() as db:
            await db.execute(
                delete(KISMockOrderLedger).where(
                    KISMockOrderLedger.symbol.in_([_CONTROL_SYMBOL, "900701", "900702"])
                )
            )
            await db.execute(
                delete(TradeJournal).where(
                    TradeJournal.symbol.in_([_CONTROL_SYMBOL, "900701", "900702"])
                )
            )
            await db.commit()

    await _c()
    yield
    await _c()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_control_anomaly_row_not_in_retrospective_pending(db_session) -> None:
    day = now_kst().strftime("%Y-%m-%d")
    # legacy control row (anomaly) + a genuine entry anomaly for a real symbol
    await _ins(
        symbol=_CONTROL_SYMBOL,
        side="buy",
        scalping_role="tracking_degraded",
        reason="ledger_tracking_degraded",
        status="unknown",
        lifecycle_state="anomaly",
        correlation_id="ctl-1",
    )
    await _ins(
        symbol="900701",
        side="buy",
        scalping_role="entry",
        status="unknown",
        lifecycle_state="anomaly",
        correlation_id="real-1",
    )

    result = await build_retrospective_pending(
        db_session, kst_date_from=day, kst_date_to=day, account_mode="kis_mock"
    )
    symbols = {p["symbol"] for p in result["pending"]}
    assert _CONTROL_SYMBOL not in symbols  # control row excluded
    assert "900701" in symbols  # genuine anomaly still surfaced


@pytest.mark.integration
@pytest.mark.asyncio
async def test_control_reconciled_rows_make_no_journal(db_session) -> None:
    # A pair of control reconciled rows (would otherwise pair into a fake
    # __ledger_tracking__ journal) + a genuine reconciled roundtrip.
    for side, role in (("buy", "tracking_degraded"), ("sell", "tracking_degraded")):
        await _ins(
            symbol=_CONTROL_SYMBOL,
            side=side,
            scalping_role=role,
            reason="ledger_tracking_degraded",
            status="unknown",
            lifecycle_state="reconciled",
            correlation_id="ctl-journal",
        )
    for side, role in (("buy", "entry"), ("sell", "exit")):
        await _ins(
            symbol="900702",
            side=side,
            scalping_role=role,
            lifecycle_state="reconciled",
            correlation_id="real-journal",
        )

    await sync_mock_roundtrip_journals(db_session, force=True)

    journ_symbols = (
        (await db_session.execute(select(TradeJournal.symbol))).scalars().all()
    )
    assert _CONTROL_SYMBOL not in journ_symbols  # no fake control journal
    assert "900702" in journ_symbols  # genuine roundtrip journaled
