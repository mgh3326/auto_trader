# tests/mcp_server/tooling/test_kis_live_ledger.py
import pytest
import pytest_asyncio

from app.models.review import KISLiveOrderLedger

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def clean_kis_live_ledger(db_session):
    from sqlalchemy import text

    from app.mcp_server.tooling.kis_live_ledger import _order_session_factory

    async with _order_session_factory()() as db:
        await db.execute(text("TRUNCATE TABLE review.kis_live_order_ledger CASCADE"))
        await db.commit()


@pytest.mark.unit
def test_kis_live_order_ledger_model_columns():

    assert KISLiveOrderLedger.__tablename__ == "kis_live_order_ledger"
    cols = {c.name for c in KISLiveOrderLedger.__table__.columns}
    # intent fields must persist so reconcile can build the journal later
    for required in (
        "order_no",
        "symbol",
        "instrument_type",
        "side",
        "order_type",
        "quantity",
        "price",
        "amount",
        "currency",
        "status",
        "lifecycle_state",
        "thesis",
        "strategy",
        "target_price",
        "stop_loss",
        "min_hold_days",
        "notes",
        "exit_reason",
        "reason",
        "filled_qty",
        "avg_fill_price",
        "trade_id",
        "journal_id",
    ):
        assert required in cols, required
    # order_no uniqueness so the same broker order can't double-book
    constraint_names = {c.name for c in KISLiveOrderLedger.__table__.constraints}
    assert "uq_kis_live_ledger_order_no" in constraint_names


@pytest.mark.unit
def test_derive_live_send_status():
    from app.mcp_server.tooling.kis_live_ledger import _derive_live_send_status

    # rt_cd == "0" -> accepted regardless of odno presence
    assert _derive_live_send_status(rt_cd="0", order_no="0006366300") == "accepted"
    # non-zero rt_cd -> rejected (broker evidence of failure, never fake success)
    assert _derive_live_send_status(rt_cd="40", order_no=None) == "rejected"
    # missing rt_cd but odno present -> accepted
    assert _derive_live_send_status(rt_cd=None, order_no="0006366300") == "accepted"
    # missing rt_cd and no odno -> unknown
    assert _derive_live_send_status(rt_cd=None, order_no=None) == "unknown"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_save_kis_live_order_ledger_inserts_row(db_session):
    from sqlalchemy import select

    from app.mcp_server.tooling.kis_live_ledger import (
        _order_session_factory,
        _save_kis_live_order_ledger,
    )
    from app.models.review import KISLiveOrderLedger

    ledger_id = await _save_kis_live_order_ledger(
        symbol="035420",
        instrument_type="equity_kr",
        side="sell",
        order_type="limit",
        quantity=10.0,
        price=250000.0,
        amount=2500000.0,
        currency="KRW",
        order_no="TEST-0006366300",
        order_time="0925",
        krx_fwdg_ord_orgno="00950",
        status="accepted",
        response_code="0",
        response_message="정상처리",
        raw_response={"rt_cd": "0"},
        reason="rob395 test",
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason="take_profit",
        indicators_snapshot=None,
    )
    assert ledger_id is not None

    async with _order_session_factory()() as db:
        row = (
            await db.execute(
                select(KISLiveOrderLedger).where(
                    KISLiveOrderLedger.order_no == "TEST-0006366300"
                )
            )
        ).scalar_one()
    assert row.status == "accepted"
    assert row.lifecycle_state == "accepted"
    assert row.trade_id is None and row.journal_id is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_kis_live_order_does_not_book_fill(db_session):
    from app.mcp_server.tooling.kis_live_ledger import _record_kis_live_order

    out = await _record_kis_live_order(
        normalized_symbol="035420",
        market_type="equity_kr",
        side="sell",
        order_type="limit",
        dry_run_result={"price": 250000, "quantity": 10, "estimated_value": 2500000},
        execution_result={"rt_cd": "0", "odno": "TEST-REC-1", "ord_tmd": "0925"},
        reason="r",
        exit_reason="take_profit",
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
    )
    assert out["broker_status"] == "accepted"
    assert out["fill_recorded"] is False
    assert out["journal_created"] is False
    # MUST NOT pre-book realized_pnl / journals_closed
    assert "realized_pnl" not in out
    assert "journals_closed" not in out
    assert out["order_id"] == "TEST-REC-1"
    assert out["ledger_id"] is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_repairs_terminal_filled_proposal_projection(db_session):
    """ROB-900: a booked KIS fill must repair its linked resting rung.

    This intentionally seeds a terminal ledger row before reconcile.  The open
    row scan cannot see it, so only the terminal projection-repair path may
    converge the proposal.
    """
    from datetime import UTC, datetime
    from decimal import Decimal
    from uuid import uuid4

    from app.mcp_server.tooling import kis_live_ledger as kl
    from app.services.order_proposals import OrderProposalsService
    from app.services.order_proposals.service import RungInput

    suffix = uuid4().hex
    order_no = f"KIS-ROB900-{suffix}"
    correlation_id = f"live:kis_live:rob900-{suffix}"
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="214150",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="rob900-test",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("50000"), None)],
    )
    for state in ("revalidating", "approved", "submitting"):
        await service.transition_rung(group.proposal_id, 0, new_state=state)
    await service.record_resting(
        group.proposal_id,
        0,
        broker_order_id=order_no,
        correlation_id=correlation_id,
        idempotency_key=f"idem-{suffix}",
        approval_hash_digest=f"digest-{suffix}",
        now=datetime.now(UTC),
    )
    await db_session.commit()

    ledger_id = await kl._save_kis_live_order_ledger(
        symbol="214150",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=50000.0,
        amount=50000.0,
        currency="KRW",
        order_no=order_no,
        order_time="090000",
        krx_fwdg_ord_orgno=None,
        status="filled",
        response_code="0",
        response_message=None,
        raw_response={},
        reason=None,
        thesis="test",
        strategy="test",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        correlation_id=correlation_id,
    )
    assert ledger_id is not None

    result = await kl.kis_live_reconcile_orders_impl(dry_run=False)
    _, rungs = await OrderProposalsService(db_session).get_proposal(group.proposal_id)

    assert result["proposal_projection_repair"] == {
        "candidates": 1,
        "converged": 1,
        "failed": 0,
        "anomalies": {},
        "scan": {
            "scanned": 1,
            "exhausted": True,
            "scan_cap": 1000,
            "cap_reached": False,
            "scan_order": "ledger id ASC keyset pages",
        },
    }
    assert rungs[0].state == "filled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_terminal_repair_skips_terminal_and_resting_key_conflict(db_session):
    """ROB-900 P0: terminal correlation evidence blocks a conflicting KIS fill."""
    from datetime import UTC, datetime
    from decimal import Decimal
    from uuid import uuid4

    from app.mcp_server.tooling import kis_live_ledger as kl
    from app.services.order_proposals import OrderProposalsService
    from app.services.order_proposals.service import RungInput

    suffix = uuid4().hex
    order_no = f"KIS-ROB900-CONFLICT-{suffix}"
    resting_correlation = f"live:kis_live:resting-{suffix}"
    terminal_correlation = f"live:kis_live:terminal-{suffix}"
    service = OrderProposalsService(db_session)

    async def create_rung(*, broker_order_id: str, correlation_id: str):
        group = await service.create_proposal(
            symbol="214150",
            market="equity_kr",
            account_mode="kis_live",
            side="buy",
            order_type="limit",
            proposer="rob900-conflict-test",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("50000"), None)],
        )
        for state in ("revalidating", "approved", "submitting"):
            await service.transition_rung(group.proposal_id, 0, new_state=state)
        await service.record_resting(
            group.proposal_id,
            0,
            broker_order_id=broker_order_id,
            correlation_id=correlation_id,
            idempotency_key=f"idem-{broker_order_id}",
            approval_hash_digest=f"digest-{broker_order_id}",
            now=datetime.now(UTC),
        )
        return group.proposal_id

    resting_id = await create_rung(
        broker_order_id=order_no, correlation_id=resting_correlation
    )
    terminal_id = await create_rung(
        broker_order_id=f"terminal-{order_no}", correlation_id=terminal_correlation
    )
    await service.transition_rung(terminal_id, 0, new_state="filled")
    await db_session.commit()
    await kl._save_kis_live_order_ledger(
        symbol="214150",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=50000.0,
        amount=50000.0,
        currency="KRW",
        order_no=order_no,
        order_time="090000",
        krx_fwdg_ord_orgno=None,
        status="filled",
        response_code="0",
        response_message=None,
        raw_response={},
        reason=None,
        thesis="test",
        strategy="test",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        correlation_id=terminal_correlation,
    )

    result = await kl.kis_live_reconcile_orders_impl(dry_run=False)
    _, resting = await OrderProposalsService(db_session).get_proposal(resting_id)
    _, terminal = await OrderProposalsService(db_session).get_proposal(terminal_id)
    assert result["proposal_projection_repair"] == {
        "candidates": 0,
        "converged": 0,
        "failed": 0,
        "anomalies": {"proposal_evidence_conflict": 1},
        "scan": {
            "scanned": 1,
            "exhausted": True,
            "scan_cap": 1000,
            "cap_reached": False,
            "scan_order": "ledger id ASC keyset pages",
        },
    }
    assert terminal[0].state == "filled"
    assert resting[0].state == "resting"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_new_filled_row_converges_proposal_in_same_pass(db_session):
    """ROB-900 P1: KIS fill booking immediately projects its resting rung."""
    from datetime import UTC, datetime
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from app.mcp_server.tooling import kis_live_ledger as kl
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )
    from app.services.order_proposals import OrderProposalsService
    from app.services.order_proposals.service import RungInput

    suffix = uuid4().hex
    order_no = f"KIS-ROB900-IMMEDIATE-{suffix}"
    correlation_id = f"live:kis_live:immediate-{suffix}"
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="214150",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="rob900-immediate-test",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("50000"), None)],
    )
    for state in ("revalidating", "approved", "submitting"):
        await service.transition_rung(group.proposal_id, 0, new_state=state)
    await service.record_resting(
        group.proposal_id,
        0,
        broker_order_id=order_no,
        correlation_id=correlation_id,
        idempotency_key=f"idem-{suffix}",
        approval_hash_digest=f"digest-{suffix}",
        now=datetime.now(UTC),
    )
    await db_session.commit()
    await kl._save_kis_live_order_ledger(
        symbol="214150",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=50000.0,
        amount=50000.0,
        currency="KRW",
        order_no=order_no,
        order_time="090000",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response={},
        reason=None,
        thesis="test",
        strategy="test",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        correlation_id=correlation_id,
    )
    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("1"), Decimal("50000"), None, "filled", ""
    )
    with (
        patch.object(kl, "_fetch_live_daily_rows", new=AsyncMock(return_value=[])),
        patch.object(kl, "classify_fill_evidence", return_value=filled),
        patch.object(kl, "_save_order_fill", new=AsyncMock(return_value=1)),
        patch.object(
            kl,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_id": 2}),
        ),
        patch.object(kl, "_link_journal_to_fill", new=AsyncMock(return_value=None)),
    ):
        result = await kl.kis_live_reconcile_orders_impl(dry_run=False)
    _, rungs = await OrderProposalsService(db_session).get_proposal(group.proposal_id)
    assert result["reconciled"][0]["action"] == "booked_filled"
    assert rungs[0].state == "filled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_live_daily_rows_for_order():
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import kis_live_ledger as kl

    fake_rows = [
        {"odno": "0006366300", "ccld_qty": "10", "ord_qty": "10", "ccld_unpr": "250000"}
    ]
    fake_client = AsyncMock()
    fake_client.inquire_daily_order_domestic = AsyncMock(return_value=fake_rows)

    with patch.object(kl, "_create_live_kis_client", return_value=fake_client):
        rows = await kl._fetch_live_daily_rows(symbol="035420", order_no="0006366300")
    assert rows == fake_rows
    fake_client.inquire_daily_order_domestic.assert_awaited_once()
    # must be a live (is_mock=False) call
    _, kwargs = fake_client.inquire_daily_order_domestic.await_args
    assert kwargs.get("is_mock") is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_live_daily_rows_uses_exact_order_date_window():
    import datetime
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import kis_live_ledger as kl

    fake_client = AsyncMock()
    fake_client.inquire_daily_order_domestic = AsyncMock(return_value=[])

    with (
        patch.object(kl, "_create_live_kis_client", return_value=fake_client),
        patch.object(kl, "_today_yyyymmdd", return_value="20260610"),
    ):
        rows = await kl._fetch_live_daily_rows(
            symbol="035420",
            order_no="0006366300",
            order_trade_date=datetime.datetime(2026, 6, 9, 9, 30, tzinfo=datetime.UTC),
        )

    assert rows == []
    _, kwargs = fake_client.inquire_daily_order_domestic.await_args
    assert kwargs["start_date"] == "20260609"
    assert kwargs["end_date"] == "20260609"
    assert kwargs["order_number"] == "0006366300"
    assert kwargs["is_mock"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_ledger_outcome(db_session):
    from decimal import Decimal

    from sqlalchemy import select

    from app.mcp_server.tooling.kis_live_ledger import (
        _order_session_factory,
        _save_kis_live_order_ledger,
        _update_ledger_outcome,
    )
    from app.models.review import KISLiveOrderLedger

    lid = await _save_kis_live_order_ledger(
        symbol="000660",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=1000.0,
        amount=1000.0,
        currency="KRW",
        order_no="TEST-UPD-1",
        order_time="0930",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response=None,
        reason=None,
        thesis="t",
        strategy="s",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
    )
    await _update_ledger_outcome(
        ledger_id=lid,
        status="filled",
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("1000"),
        trade_id=42,
        journal_id=7,
    )
    async with _order_session_factory()() as db:
        row = (
            await db.execute(
                select(KISLiveOrderLedger).where(KISLiveOrderLedger.id == lid)
            )
        ).scalar_one()
    assert row.status == "filled"
    assert row.lifecycle_state == "filled"
    assert row.trade_id == 42 and row.journal_id == 7
    assert row.reconciled_at is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_ledger_outcome_preserves_existing_fill_fields_when_omitted(
    db_session,
):
    from decimal import Decimal

    from sqlalchemy import select

    from app.mcp_server.tooling.kis_live_ledger import (
        _order_session_factory,
        _save_kis_live_order_ledger,
        _update_ledger_outcome,
    )

    lid = await _save_kis_live_order_ledger(
        symbol="000660",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=2.0,
        price=1000.0,
        amount=2000.0,
        currency="KRW",
        order_no="TEST-UPD-PRESERVE",
        order_time="0930",
        krx_fwdg_ord_orgno=None,
        status="partial",
        response_code="0",
        response_message=None,
        raw_response=None,
        reason=None,
        thesis="t",
        strategy="s",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
    )
    await _update_ledger_outcome(
        ledger_id=lid,
        status="partial",
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("1000"),
        trade_id=42,
        journal_id=7,
    )

    await _update_ledger_outcome(ledger_id=lid, status="partial")

    async with _order_session_factory()() as db:
        row = (
            await db.execute(
                select(KISLiveOrderLedger).where(KISLiveOrderLedger.id == lid)
            )
        ).scalar_one()
    assert row.status == "partial"
    assert row.filled_qty == Decimal("1.00000000")
    assert row.avg_fill_price == Decimal("1000.0000")
    assert row.trade_id == 42
    assert row.journal_id == 7


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_filled_buy_books_fill_and_journal(db_session):
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import kis_live_ledger as kl
    from app.mcp_server.tooling.kis_live_ledger import _save_kis_live_order_ledger
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await _save_kis_live_order_ledger(
        symbol="000660",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=1000.0,
        amount=1000.0,
        currency="KRW",
        order_no="TEST-RC-BUY",
        order_time="0930",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response=None,
        reason=None,
        thesis="t",
        strategy="s",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
    )
    row = await kl._load_ledger_row(lid)

    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("1"), Decimal("1005"), None, "filled", ""
    )
    with (
        patch.object(
            kl,
            "_fetch_live_daily_rows",
            new=AsyncMock(return_value=[{"odno": "TEST-RC-BUY"}]),
        ),
        patch.object(kl, "classify_fill_evidence", return_value=filled),
        patch.object(kl, "_save_order_fill", new=AsyncMock(return_value=111)) as m_fill,
        patch.object(
            kl,
            "_create_trade_journal_for_buy",
            new=AsyncMock(
                return_value={
                    "journal_created": True,
                    "journal_id": 9,
                    "journal_status": "draft",
                }
            ),
        ) as m_buy,
        patch.object(
            kl, "_link_journal_to_fill", new=AsyncMock(return_value=None)
        ) as m_link,
    ):
        result = await kl._reconcile_one_ledger_row(row, dry_run=False)

    assert result["verdict"] == "filled"
    # fill booked with BROKER-confirmed qty/price, not the 1000 preview price
    _, fkw = m_fill.await_args
    assert float(fkw["price"]) == 1005.0
    assert float(fkw["quantity"]) == 1.0
    m_buy.assert_awaited_once()
    m_link.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_filled_sell_surfaces_journal_entry_basis(db_session):
    """ROB-544: sell reconcile surfaces realized_pnl_basis=='journal_entry'.

    The realized_pnl_pct is the FIFO lot/journal-entry basis (NOT the
    account-average pchs_avg_pric basis shown in place_order preview).
    """
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import kis_live_ledger as kl
    from app.mcp_server.tooling.kis_live_ledger import _save_kis_live_order_ledger
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await _save_kis_live_order_ledger(
        symbol="000660",
        instrument_type="equity_kr",
        side="sell",
        order_type="limit",
        quantity=1.0,
        price=1000.0,
        amount=1000.0,
        currency="KRW",
        order_no="TEST-RC-SELL",
        order_time="0930",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response=None,
        reason=None,
        thesis="t",
        strategy="s",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
    )
    row = await kl._load_ledger_row(lid)

    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("1"), Decimal("974"), None, "filled", ""
    )
    # journal-entry (FIFO lot) basis: -2.61% loss, NOT an account-average.
    close_result = {
        "journals_closed": 1,
        "journals_kept": 0,
        "closed_ids": [77],
        "total_pnl_pct": -2.61,
        "realized_pnl_basis": "journal_entry",
    }
    with (
        patch.object(
            kl,
            "_fetch_live_daily_rows",
            new=AsyncMock(return_value=[{"odno": "TEST-RC-SELL"}]),
        ),
        patch.object(kl, "classify_fill_evidence", return_value=filled),
        patch.object(kl, "_save_order_fill", new=AsyncMock(return_value=222)),
        patch.object(
            kl,
            "_close_journals_on_sell",
            new=AsyncMock(return_value=close_result),
        ),
        patch.object(kl, "_link_journal_to_fill", new=AsyncMock(return_value=None)),
    ):
        result = await kl._reconcile_one_ledger_row(row, dry_run=False)

    assert result["verdict"] == "filled"
    assert result["realized_pnl_pct"] == pytest.approx(-2.61)
    assert result["realized_pnl_basis"] == "journal_entry"
    # explicit alias mirrors the same FIFO lot basis value
    assert result["journal_pnl_pct"] == pytest.approx(-2.61)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_pending_is_noop(db_session):
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import kis_live_ledger as kl
    from app.mcp_server.tooling.kis_live_ledger import _save_kis_live_order_ledger
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await _save_kis_live_order_ledger(
        symbol="000660",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=1000.0,
        amount=1000.0,
        currency="KRW",
        order_no="TEST-RC-PEND",
        order_time="0930",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response=None,
        reason=None,
        thesis="t",
        strategy="s",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
    )
    row = await kl._load_ledger_row(lid)
    pending = FillEvidence(FillVerdict.PENDING, Decimal("0"), None, None, "pending", "")
    with (
        patch.object(kl, "_fetch_live_daily_rows", new=AsyncMock(return_value=[])),
        patch.object(kl, "classify_fill_evidence", return_value=pending),
        patch.object(kl, "_save_order_fill", new=AsyncMock()) as m_fill,
        patch.object(kl, "_close_journals_on_sell", new=AsyncMock()) as m_sell,
    ):
        result = await kl._reconcile_one_ledger_row(row, dry_run=False)
    assert result["verdict"] == "pending"
    m_fill.assert_not_awaited()
    m_sell.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_no_evidence_is_fail_closed_not_cancelled(db_session):
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import kis_live_ledger as kl
    from app.mcp_server.tooling.kis_live_ledger import _save_kis_live_order_ledger
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await _save_kis_live_order_ledger(
        symbol="000660",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=1000.0,
        amount=1000.0,
        currency="KRW",
        order_no="TEST-RC-NONE",
        order_time="0930",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response=None,
        reason=None,
        thesis="t",
        strategy="s",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
    )
    row = await kl._load_ledger_row(lid)
    no_evidence = FillEvidence(FillVerdict.NONE, None, None, None, "none", "")
    with (
        patch.object(kl, "_fetch_live_daily_rows", new=AsyncMock(return_value=[])),
        patch.object(kl, "classify_fill_evidence", return_value=no_evidence),
        patch.object(kl, "_update_ledger_outcome", new=AsyncMock()) as m_update,
    ):
        result = await kl._reconcile_one_ledger_row(row, dry_run=False)

    assert result["verdict"] == "none"
    assert result["action"] == "noop_no_evidence"
    assert result["requires_manual_review"] is True
    m_update.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_orders_impl_aggregates_counts():
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import kis_live_ledger as kl

    fake_rows = [object(), object()]
    with (
        patch.object(
            kl, "_list_open_ledger_rows", new=AsyncMock(return_value=fake_rows)
        ),
        patch.object(
            kl,
            "_reconcile_one_ledger_row",
            new=AsyncMock(
                side_effect=[
                    {"verdict": "filled", "order_id": "A"},
                    {"verdict": "pending", "order_id": "B"},
                ]
            ),
        ),
    ):
        out = await kl.kis_live_reconcile_orders_impl(dry_run=False)
    assert out["success"] is True
    assert out["counts"]["filled"] == 1
    assert out["counts"]["pending"] == 1
    assert len(out["reconciled"]) == 2
    assert out["dry_run"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mark_ledger_cancelled_only_touches_open_rows(db_session):
    from sqlalchemy import select

    from app.mcp_server.tooling.kis_live_ledger import (
        _mark_ledger_cancelled,
        _order_session_factory,
        _save_kis_live_order_ledger,
    )

    lid = await _save_kis_live_order_ledger(
        symbol="012450",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=1160000.0,
        amount=1160000.0,
        currency="KRW",
        order_no="TEST-CXL-1",
        order_time="0930",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response=None,
        reason=None,
        thesis="t",
        strategy="s",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
    )
    updated = await _mark_ledger_cancelled("TEST-CXL-1")
    assert updated == 1
    async with _order_session_factory()() as db:
        row = (
            await db.execute(
                select(KISLiveOrderLedger).where(KISLiveOrderLedger.id == lid)
            )
        ).scalar_one()
    assert row.status == "cancelled"
    assert row.lifecycle_state == "cancelled"

    # idempotent: a terminal row is not reopened/re-touched
    assert await _mark_ledger_cancelled("TEST-CXL-1") == 0
    # unknown order_no is a no-op
    assert await _mark_ledger_cancelled("NOPE") == 0
    assert await _mark_ledger_cancelled(None) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repoint_ledger_after_modify(db_session):
    from sqlalchemy import select

    from app.mcp_server.tooling.kis_live_ledger import (
        _order_session_factory,
        _repoint_ledger_after_modify,
        _save_kis_live_order_ledger,
    )

    await _save_kis_live_order_ledger(
        symbol="015760",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=38500.0,
        amount=38500.0,
        currency="KRW",
        order_no="TEST-MOD-OLD",
        order_time="0930",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response=None,
        reason=None,
        thesis="t",
        strategy="s",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
    )
    updated = await _repoint_ledger_after_modify(
        old_order_no="TEST-MOD-OLD",
        new_order_no="TEST-MOD-NEW",
        new_price=39000.0,
        new_quantity=2.0,
    )
    assert updated == 1
    async with _order_session_factory()() as db:
        row = (
            await db.execute(
                select(KISLiveOrderLedger).where(
                    KISLiveOrderLedger.order_no == "TEST-MOD-NEW"
                )
            )
        ).scalar_one()
    assert float(row.price) == 39000.0
    assert float(row.quantity) == 2.0
    assert row.status == "accepted"  # still open, now tracked under the new odno

    # missing ids are a no-op
    assert await _repoint_ledger_after_modify(old_order_no=None, new_order_no="X") == 0
    assert (
        await _repoint_ledger_after_modify(
            old_order_no="TEST-MOD-NEW", new_order_no=None
        )
        == 0
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_partial_rerun_is_delta_idempotent(db_session):
    """ROB-487 follow-up: re-reconciling an already-booked partial row must not
    re-create journals, double-close sells, or re-insert the same fill."""
    import uuid
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import kis_live_ledger as kl
    from app.mcp_server.tooling.kis_live_ledger import (
        _save_kis_live_order_ledger,
        _update_ledger_outcome,
    )
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    idem_order_no = f"TEST-RC-IDEM-{uuid.uuid4().hex[:8]}"
    lid = await _save_kis_live_order_ledger(
        symbol="000660",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=3.0,
        price=1000.0,
        amount=3000.0,
        currency="KRW",
        order_no=idem_order_no,
        order_time="0930",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response=None,
        reason=None,
        thesis="t",
        strategy="s",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
    )
    # First reconcile already booked a partial fill of 2 with journal 7.
    await _update_ledger_outcome(
        ledger_id=lid,
        status="partial",
        filled_qty=Decimal("2"),
        avg_fill_price=Decimal("1005"),
        trade_id=42,
        journal_id=7,
    )
    row = await kl._load_ledger_row(lid)

    partial_same = FillEvidence(
        FillVerdict.PARTIAL, Decimal("2"), Decimal("1005"), None, "partial", ""
    )
    with (
        patch.object(
            kl,
            "_fetch_live_daily_rows",
            new=AsyncMock(return_value=[{"odno": idem_order_no}]),
        ),
        patch.object(kl, "classify_fill_evidence", return_value=partial_same),
        patch.object(kl, "_save_order_fill", new=AsyncMock()) as m_fill,
        patch.object(kl, "_create_trade_journal_for_buy", new=AsyncMock()) as m_buy,
    ):
        result = await kl._reconcile_one_ledger_row(row, dry_run=False)

    assert result["action"] == "noop_already_booked"
    assert result["delta_qty"] == 0.0
    m_fill.assert_not_awaited()
    m_buy.assert_not_awaited()

    # Cumulative fill grows 2 -> 3: book only the delta, reuse the journal.
    row = await kl._load_ledger_row(lid)
    partial_grown = FillEvidence(
        FillVerdict.FILLED, Decimal("3"), Decimal("1006"), None, "filled", ""
    )
    with (
        patch.object(
            kl,
            "_fetch_live_daily_rows",
            new=AsyncMock(return_value=[{"odno": idem_order_no}]),
        ),
        patch.object(kl, "classify_fill_evidence", return_value=partial_grown),
        patch.object(
            kl, "_save_order_fill", new=AsyncMock(return_value=None)
        ) as m_fill,
        patch.object(kl, "_create_trade_journal_for_buy", new=AsyncMock()) as m_buy,
    ):
        result = await kl._reconcile_one_ledger_row(row, dry_run=False)

    assert result["action"] == "booked_filled"
    assert result["delta_qty"] == 1.0
    _, fkw = m_fill.await_args
    assert float(fkw["quantity"]) == 1.0
    m_buy.assert_not_awaited()  # journal_id already set — no orphan draft

    row = await kl._load_ledger_row(lid)
    assert row.status == "filled"
    assert float(row.filled_qty) == 3.0
    assert row.journal_id == 7


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_buy_journal_backfills_correlation_id(db_session):
    """ROB-714: reconcile-time buy journal must carry the ledger row's
    correlation_id so the forecast/journal/retrospective spine stays connected
    through reconcile. Drives the REAL _reconcile_one_ledger_row (not a spy)."""
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import kis_live_ledger as kl
    from app.mcp_server.tooling.kis_live_ledger import _save_kis_live_order_ledger
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await _save_kis_live_order_ledger(
        symbol="000660",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=1000.0,
        amount=1000.0,
        currency="KRW",
        order_no="TEST-RC-CORR-KR",
        order_time="0930",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response=None,
        reason=None,
        thesis="t",
        strategy="s",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        correlation_id="live:kis_live:reconcileKR",
    )
    row = await kl._load_ledger_row(lid)
    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("1"), Decimal("1005"), None, "filled", ""
    )
    with (
        patch.object(
            kl,
            "_fetch_live_daily_rows",
            new=AsyncMock(return_value=[{"odno": "TEST-RC-CORR-KR"}]),
        ),
        patch.object(kl, "classify_fill_evidence", return_value=filled),
        patch.object(kl, "_save_order_fill", new=AsyncMock(return_value=111)),
        patch.object(
            kl,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_id": 9}),
        ) as m_buy,
        patch.object(kl, "_link_journal_to_fill", new=AsyncMock(return_value=None)),
    ):
        await kl._reconcile_one_ledger_row(row, dry_run=False)

    m_buy.assert_awaited_once()
    assert m_buy.await_args.kwargs["correlation_id"] == "live:kis_live:reconcileKR"


# ---------------------------------------------------------------------------
# ROB-719 — gap C/D regression pins.
# ---------------------------------------------------------------------------


async def _rob719_proposal_rung(
    db_session,
    *,
    suffix: str,
    symbol: str,
    correlation_id: str,
    broker_order_id: str,
    quantity: str = "1",
):
    """Resting kis_live proposal rung wired to ``correlation_id``/``order_no``."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.services.order_proposals import OrderProposalsService
    from app.services.order_proposals.service import RungInput

    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol=symbol,
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="rob719-test",
        rungs=[RungInput(0, "buy", Decimal(quantity), Decimal("50000"), None)],
    )
    for state in ("revalidating", "approved", "submitting"):
        await service.transition_rung(group.proposal_id, 0, new_state=state)
    await service.record_resting(
        group.proposal_id,
        0,
        broker_order_id=broker_order_id,
        correlation_id=correlation_id,
        idempotency_key=f"idem-{suffix}",
        approval_hash_digest=f"digest-{suffix}",
        now=datetime.now(UTC),
    )
    await db_session.commit()
    return service, group.proposal_id


def _rob719_broker_row(order_no: str, symbol: str, **overrides):
    row = {
        "odno": order_no,
        "orgn_odno": "0000000000",
        "pdno": symbol,
        "sll_buy_dvsn_cd_name": "매수",
        "ord_qty": "1",
        "ord_unpr": "50000",
        "tot_ccld_qty": "0",
        "rjct_qty": "0",
        "rmn_qty": "1",
        "cncl_yn": "N",
        "excg_id_dvsn_cd": "SOR",
        "ord_tmd": "090000",
    }
    row.update(overrides)
    return row


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expire_evidence_converges_proposal_in_same_pass(db_session):
    """ROB-719 gap C: an expired ledger row converges its rung in one pass.

    Before this fix the expire/cancel branch returned after the ledger update
    and the rung only converged on a second non-dry pass (terminal repair
    pre-pass) or the resting sweep.
    """
    import datetime as _dt
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from app.mcp_server.tooling import kis_live_ledger as kl

    suffix = uuid4().hex
    order_no = f"KIS-ROB719-EXP-{suffix[:12]}"
    correlation_id = f"live:kis_live:rob719-exp-{suffix[:12]}"
    service, proposal_id = await _rob719_proposal_rung(
        db_session,
        suffix=suffix,
        symbol="214150",
        correlation_id=correlation_id,
        broker_order_id=order_no,
    )
    await kl._save_kis_live_order_ledger(
        symbol="214150",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=50000.0,
        amount=50000.0,
        currency="KRW",
        order_no=order_no,
        order_time="090000",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response={},
        reason=None,
        thesis="test",
        strategy="test",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        correlation_id=correlation_id,
    )

    # Full reject after the order date's NXT close is expiry evidence.
    KST = _dt.timezone(_dt.timedelta(hours=9))
    rows = [_rob719_broker_row(order_no, "214150", rjct_qty="1", rmn_qty="0")]
    now = _dt.datetime.now(_dt.UTC).astimezone(KST).replace(hour=20, minute=5)
    with (
        patch.object(kl, "_fetch_live_daily_rows", AsyncMock(return_value=rows)),
        patch.object(kl, "now_kst", return_value=now),
    ):
        result = await kl.kis_live_reconcile_orders_impl(dry_run=False)

    _, rungs = await service.get_proposal(proposal_id)
    entry = next(
        (r for r in result["reconciled"] if r.get("order_id") == order_no), None
    )
    assert entry is not None
    assert entry["verdict"] == "expired"
    assert entry["action"] == "marked_expired"
    # Same-pass convergence, not the next pass or the sweep.
    assert entry.get("proposal_rung") == {
        "converged": True,
        "proposal_rung_state": "expired",
    }
    assert rungs[0].state == "expired"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_row_with_booked_partial_preserves_qty_on_rung(
    db_session,
):
    """ROB-719 gap C: a terminal cancel keeps an already-booked partial fill.

    Mirrors the Toss #691 contract: when a terminal ledger row carries a
    previously booked partial quantity, the cancelled rung must keep it — the
    terminal projection closes with ``filled_qty=None`` so the pre-projected
    partial survives instead of being zeroed.  Exercised through the terminal
    repair pre-pass: a ``partial`` ledger row marked cancelled outside
    reconcile (e.g. ``_mark_ledger_cancelled``) leaves booked qty on the row.
    """
    from decimal import Decimal
    from uuid import uuid4

    from app.mcp_server.tooling import kis_live_ledger as kl
    from app.services.order_proposals import OrderProposalsService

    suffix = uuid4().hex
    order_no = f"KIS-ROB719-CXL-{suffix[:12]}"
    correlation_id = f"live:kis_live:rob719-cxl-{suffix[:12]}"
    service, proposal_id = await _rob719_proposal_rung(
        db_session,
        suffix=suffix,
        symbol="214150",
        correlation_id=correlation_id,
        broker_order_id=order_no,
        quantity="3",
    )
    ledger_id = await kl._save_kis_live_order_ledger(
        symbol="214150",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=3.0,
        price=50000.0,
        amount=150000.0,
        currency="KRW",
        order_no=order_no,
        order_time="090000",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response={},
        reason=None,
        thesis="test",
        strategy="test",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        correlation_id=correlation_id,
    )
    # A previously reconciled partial fill booked 2 of 3 onto the ledger row,
    # then the order was marked cancelled without clearing the booked qty.
    await kl._update_ledger_outcome(
        ledger_id=ledger_id, status="partial", filled_qty=Decimal("2")
    )
    await kl._update_ledger_outcome(ledger_id=ledger_id, status="cancelled")

    result = await kl.kis_live_reconcile_orders_impl(dry_run=False)

    repair = result["proposal_projection_repair"]
    assert repair["candidates"] == 1
    assert repair["converged"] == 1
    _, rungs = await OrderProposalsService(db_session).get_proposal(proposal_id)
    assert rungs[0].state == "cancelled"
    # The pre-terminal partial projection must not be zeroed by the close.
    assert rungs[0].filled_qty == Decimal("2")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_beyond_reach_rows_do_not_starve_open_scan(db_session):
    """ROB-719 gap D: rows past the TTTC8001R window fill only leftover slots.

    Two permanently unresolvable rows (order dates 100 days old, beyond the
    90-day lookback) plus one reachable row and ``limit=1``: the reachable row
    must be scanned first — under the old ``created_at ASC`` order the oldest
    row would occupy the only slot on every pass.
    """
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from app.mcp_server.tooling import kis_live_ledger as kl
    from app.models.review import KISLiveOrderLedger

    now = datetime.now(UTC)
    stale_ids = []
    async with kl._order_session_factory()() as db:
        for _ in range(2):
            row = KISLiveOrderLedger(
                trade_date=now - timedelta(days=100),
                symbol="214150",
                instrument_type="equity_kr",
                side="buy",
                order_type="limit",
                order_no=f"STALE-{uuid4().hex[:12]}",
                account_mode="kis_live",
                broker="kis",
                status="accepted",
                lifecycle_state="accepted",
                created_at=now - timedelta(days=100),
            )
            db.add(row)
            await db.flush()
            stale_ids.append(row.id)
        fresh = KISLiveOrderLedger(
            trade_date=now,
            symbol="214150",
            instrument_type="equity_kr",
            side="buy",
            order_type="limit",
            order_no=f"FRESH-{uuid4().hex[:12]}",
            account_mode="kis_live",
            broker="kis",
            status="accepted",
            lifecycle_state="accepted",
            created_at=now,
        )
        db.add(fresh)
        await db.flush()
        fresh_id = fresh.id
        await db.commit()

    with patch.object(kl, "_fetch_live_daily_rows", AsyncMock(return_value=[])):
        result = await kl.kis_live_reconcile_orders_impl(dry_run=True, limit=1)

    scanned_ids = {entry["ledger_id"] for entry in result["reconciled"]}
    assert scanned_ids == {fresh_id}
    assert stale_ids[0] < fresh_id  # stale rows are genuinely older
    coverage = result["candidate_scan"]
    assert coverage["scanned"] == 1
    assert coverage["open_total"] == 3
    assert coverage["probeable_open"] == 1
    assert coverage["unreached_beyond_reach"] == 2
    assert coverage["unreached_probeable"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_terminal_repair_prefilter_skips_ineligible_join_rows(db_session):
    """ROB-719 gap D: the repair pre-pass filters rung state before LIMIT.

    The stuck row is terminal-but-ineligible (its only evidence rung is
    already terminal, so ``_terminal_projection_match`` would reject it
    anyway).  With ``limit=1`` the old ``ORDER BY id LIMIT 1`` scan returned
    only that row and the real candidate behind it was never reached; the SQL
    pre-filter removes it from the page entirely.
    """
    from uuid import uuid4

    from app.mcp_server.tooling import kis_live_ledger as kl
    from app.services.order_proposals import OrderProposalsService

    suffix = uuid4().hex
    order_no_stuck = f"KIS-ROB719-STUCK-{suffix[:12]}"
    order_no_real = f"KIS-ROB719-REAL-{suffix[:12]}"
    correlation_real = f"live:kis_live:rob719-real-{suffix[:12]}"

    # Stuck candidate: terminal ledger row whose evidence rung is already
    # terminal — permanently ineligible.
    service, stuck_pid = await _rob719_proposal_rung(
        db_session,
        suffix=f"stuck-{suffix}",
        symbol="214150",
        correlation_id=f"live:kis_live:rob719-stuck-{suffix[:12]}",
        broker_order_id=order_no_stuck,
    )
    await service.transition_rung(stuck_pid, 0, new_state="filled")
    await db_session.commit()
    await kl._save_kis_live_order_ledger(
        symbol="214150",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=50000.0,
        amount=50000.0,
        currency="KRW",
        order_no=order_no_stuck,
        order_time="090000",
        krx_fwdg_ord_orgno=None,
        status="expired",
        response_code="0",
        response_message=None,
        raw_response={},
        reason=None,
        thesis="test",
        strategy="test",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
    )

    # Real candidate inserted AFTER the stuck row (higher ledger id).
    _, real_pid = await _rob719_proposal_rung(
        db_session,
        suffix=f"real-{suffix}",
        symbol="214150",
        correlation_id=correlation_real,
        broker_order_id=order_no_real,
    )
    await kl._save_kis_live_order_ledger(
        symbol="214150",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=50000.0,
        amount=50000.0,
        currency="KRW",
        order_no=order_no_real,
        order_time="090000",
        krx_fwdg_ord_orgno=None,
        status="filled",
        response_code="0",
        response_message=None,
        raw_response={},
        reason=None,
        thesis="test",
        strategy="test",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        correlation_id=correlation_real,
    )

    result = await kl.kis_live_reconcile_orders_impl(dry_run=False, limit=1)

    repair = result["proposal_projection_repair"]
    assert repair["candidates"] == 1
    assert repair["converged"] == 1
    assert repair["failed"] == 0
    # The ineligible joined row never made it into a page.
    assert repair["scan"]["scanned"] == 1
    _, rungs = await OrderProposalsService(db_session).get_proposal(real_pid)
    assert rungs[0].state == "filled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_partial_projection_never_lands_on_unrelated_rung_sharing_order_no(
    db_session,
):
    """ROB-719 gap C: the partial pre-projection must stay on the validated rung.

    KIS order numbers are only unique inside their own ledger, so two resting
    kis_live rungs in different symbol scopes can share one broker order
    number.  ``find_unambiguous_evidence_rung_id`` resolves the ledger row's
    rung through the symbol scope; an unscoped evidence lookup would pick the
    lower-id rung instead.  The partial qty must land on rung B only — rung A
    must remain untouched.
    """
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from app.mcp_server.tooling import kis_live_ledger as kl
    from app.services.order_proposals import OrderProposalsService

    suffix = uuid4().hex
    order_no = f"KIS-ROB719-DUP-{suffix[:12]}"
    corr_a = f"live:kis_live:rob719-dupa-{suffix[:12]}"
    corr_b = f"live:kis_live:rob719-dupb-{suffix[:12]}"
    # Unrelated rung: same broker order number, different symbol, lower id.
    _, pid_a = await _rob719_proposal_rung(
        db_session,
        suffix=f"dupa-{suffix}",
        symbol="005930",
        correlation_id=corr_a,
        broker_order_id=order_no,
    )
    _, pid_b = await _rob719_proposal_rung(
        db_session,
        suffix=f"dupb-{suffix}",
        symbol="214150",
        correlation_id=corr_b,
        broker_order_id=order_no,
        quantity="3",
    )
    ledger_id = await kl._save_kis_live_order_ledger(
        symbol="214150",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=3.0,
        price=50000.0,
        amount=150000.0,
        currency="KRW",
        order_no=order_no,
        order_time="090000",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response={},
        reason=None,
        thesis="test",
        strategy="test",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        correlation_id=corr_b,
    )
    await kl._update_ledger_outcome(
        ledger_id=ledger_id, status="partial", filled_qty=Decimal("2")
    )
    await kl._update_ledger_outcome(ledger_id=ledger_id, status="cancelled")

    # The ROB-1284 resting-rung sweep matches ledger rows to rungs by order_no
    # alone (a pre-existing cross-match, out of scope) — patch it out so this
    # test isolates the repair pre-pass projection.
    with patch.object(
        kl, "run_resting_rung_sweep", AsyncMock(return_value={"swept": 0})
    ):
        result = await kl.kis_live_reconcile_orders_impl(dry_run=False)

    assert result["proposal_projection_repair"]["converged"] == 1
    _, rungs_a = await OrderProposalsService(db_session).get_proposal(pid_a)
    _, rungs_b = await OrderProposalsService(db_session).get_proposal(pid_b)
    # The unrelated rung sharing the order number gets nothing.
    assert rungs_a[0].state == "resting"
    assert rungs_a[0].filled_qty is None
    # The ledger row's own rung keeps the booked partial on terminal close.
    assert rungs_b[0].state == "cancelled"
    assert rungs_b[0].filled_qty == Decimal("2")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2])
async def test_terminal_repair_pages_past_ineligible_prefix(db_session, limit):
    """ROB-719 gap D: the repair pre-pass keyset-pages past ineligible rows.

    Three terminal rows each join TWO accepting rungs with disjoint evidence
    keys (``proposal_evidence_conflict`` — scanned but never projectable),
    followed by one real candidate.  At ``limit=1`` a one-page scan starves
    the candidate; at ``limit=2`` each page is a join-duplicate pair that
    ``unique()`` collapses to a short page, which must not be mistaken for
    exhaustion.
    """
    from uuid import uuid4

    from app.mcp_server.tooling import kis_live_ledger as kl
    from app.services.order_proposals import OrderProposalsService

    suffix = uuid4().hex
    for i in range(3):
        corr_x = f"live:kis_live:rob719-cx{i}-{suffix[:8]}"
        corr_y = f"live:kis_live:rob719-cy{i}-{suffix[:8]}"
        odno_x = f"KIS-ROB719-CX{i}-{suffix[:8]}"
        odno_y = f"KIS-ROB719-CY{i}-{suffix[:8]}"
        await _rob719_proposal_rung(
            db_session,
            suffix=f"cx{i}-{suffix}",
            symbol="214150",
            correlation_id=corr_x,
            broker_order_id=odno_x,
        )
        await _rob719_proposal_rung(
            db_session,
            suffix=f"cy{i}-{suffix}",
            symbol="214150",
            correlation_id=corr_y,
            broker_order_id=odno_y,
        )
        # Terminal ledger row whose correlation_id resolves to rung X and
        # order_no to rung Y — disjoint key sets => evidence conflict.
        await kl._save_kis_live_order_ledger(
            symbol="214150",
            instrument_type="equity_kr",
            side="buy",
            order_type="limit",
            quantity=1.0,
            price=50000.0,
            amount=50000.0,
            currency="KRW",
            order_no=odno_y,
            order_time="090000",
            krx_fwdg_ord_orgno=None,
            status="cancelled",
            response_code="0",
            response_message=None,
            raw_response={},
            reason=None,
            thesis="test",
            strategy="test",
            target_price=None,
            stop_loss=None,
            min_hold_days=None,
            notes=None,
            exit_reason=None,
            indicators_snapshot=None,
            correlation_id=corr_x,
        )
    real_corr = f"live:kis_live:rob719-pg-{suffix[:12]}"
    real_odno = f"KIS-ROB719-PG-{suffix[:12]}"
    _, real_pid = await _rob719_proposal_rung(
        db_session,
        suffix=f"pg-{suffix}",
        symbol="214150",
        correlation_id=real_corr,
        broker_order_id=real_odno,
    )
    await kl._save_kis_live_order_ledger(
        symbol="214150",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=50000.0,
        amount=50000.0,
        currency="KRW",
        order_no=real_odno,
        order_time="090000",
        krx_fwdg_ord_orgno=None,
        status="filled",
        response_code="0",
        response_message=None,
        raw_response={},
        reason=None,
        thesis="test",
        strategy="test",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        correlation_id=real_corr,
    )

    result = await kl.kis_live_reconcile_orders_impl(dry_run=False, limit=limit)

    repair = result["proposal_projection_repair"]
    assert repair["candidates"] == 1
    assert repair["converged"] == 1
    assert repair["failed"] == 0
    assert repair["anomalies"] == {"proposal_evidence_conflict": 3}
    assert repair["scan"]["scanned"] == 4
    # limit=1 exits as soon as the candidate fills the quota; limit=2 keeps
    # paging until the scan actually drains (empty page => exhausted).
    assert repair["scan"]["exhausted"] is (limit == 2)
    assert repair["scan"]["cap_reached"] is False
    _, rungs = await OrderProposalsService(db_session).get_proposal(real_pid)
    assert rungs[0].state == "filled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_beyond_reach_rows_are_deprioritized_not_dropped(db_session):
    """ROB-719 gap D: beyond-reach rows still scan inside leftover slots.

    Ordering deprioritizes them; it never filters them out.  A 100-day-old
    row and a recent row with no ``order_no`` are both beyond evidence reach;
    with room in the limit they must still be looked up alongside the fresh
    reachable row.
    """
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from app.mcp_server.tooling import kis_live_ledger as kl
    from app.models.review import KISLiveOrderLedger

    now = datetime.now(UTC)
    async with kl._order_session_factory()() as db:
        stale = KISLiveOrderLedger(
            trade_date=now - timedelta(days=100),
            symbol="214150",
            instrument_type="equity_kr",
            side="buy",
            order_type="limit",
            order_no=f"STALE-{uuid4().hex[:12]}",
            account_mode="kis_live",
            broker="kis",
            status="accepted",
            lifecycle_state="accepted",
            created_at=now - timedelta(days=100),
        )
        no_key = KISLiveOrderLedger(
            trade_date=now,
            symbol="214150",
            instrument_type="equity_kr",
            side="buy",
            order_type="limit",
            order_no=None,
            account_mode="kis_live",
            broker="kis",
            status="accepted",
            lifecycle_state="accepted",
            created_at=now,
        )
        fresh = KISLiveOrderLedger(
            trade_date=now,
            symbol="214150",
            instrument_type="equity_kr",
            side="buy",
            order_type="limit",
            order_no=f"FRESH-{uuid4().hex[:12]}",
            account_mode="kis_live",
            broker="kis",
            status="accepted",
            lifecycle_state="accepted",
            created_at=now,
        )
        db.add_all([stale, no_key, fresh])
        await db.flush()
        beyond_ids = {stale.id, no_key.id}
        fresh_id = fresh.id
        await db.commit()

    with patch.object(kl, "_fetch_live_daily_rows", AsyncMock(return_value=[])):
        result = await kl.kis_live_reconcile_orders_impl(dry_run=True, limit=3)

    scanned_ids = {entry["ledger_id"] for entry in result["reconciled"]}
    # All three rows fit in the limit — deprioritized rows are still scanned.
    assert scanned_ids == beyond_ids | {fresh_id}
    coverage = result["candidate_scan"]
    assert coverage["scanned"] == 3
    assert coverage["open_total"] == 3
    assert coverage["probeable_open"] == 1
    assert coverage["beyond_evidence_reach"] == 2
    assert coverage["unscanned"] == 0
    assert coverage["truncated"] is False


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rung_partial_qty", "ledger_partial_qty", "expected_qty"),
    [("1", "2", "2"), ("3", "2", "3")],
    ids=["refreshes-larger-ledger-qty", "stale-ledger-qty-does-not-regress"],
)
async def test_terminal_repair_updates_booked_partial_on_already_partial_rung(
    db_session,
    rung_partial_qty,
    ledger_partial_qty,
    expected_qty,
):
    """ROB-719 gap C: pre-projection on an already-partially_filled rung.

    When the rung already carries a booked partial (partially_filled), the
    terminal projection must skip the illegal partially_filled ->
    partially_filled self-transition and the close must carry the larger
    booked qty: a fresher ledger partial refreshes the rung, a stale or
    equal one never regresses it.
    """
    from datetime import UTC, datetime
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from app.mcp_server.tooling import kis_live_ledger as kl
    from app.services.order_proposals import OrderProposalsService

    suffix = uuid4().hex
    order_no = f"KIS-ROB719-PRE-{suffix[:12]}"
    correlation_id = f"live:kis_live:rob719-pre-{suffix[:12]}"
    service, proposal_id = await _rob719_proposal_rung(
        db_session,
        suffix=suffix,
        symbol="214150",
        correlation_id=correlation_id,
        broker_order_id=order_no,
        quantity="3",
    )
    # A prior fill pass already booked 1 of 3 onto the rung.
    _, rungs = await service.get_proposal(proposal_id)
    await service.record_fill_evidence_for_rung(
        rung_id=rungs[0].id,
        correlation_id=correlation_id,
        broker_order_id=order_no,
        idempotency_key=f"idem-{suffix}",
        filled_qty=Decimal(rung_partial_qty),
        terminal_state="partially_filled",
        now=datetime.now(UTC),
        account_mode="kis_live",
        symbol="214150",
        market="equity_kr",
    )
    # Commit releases the rung lock before reconcile opens its own session.
    await db_session.commit()
    ledger_id = await kl._save_kis_live_order_ledger(
        symbol="214150",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=3.0,
        price=50000.0,
        amount=150000.0,
        currency="KRW",
        order_no=order_no,
        order_time="090000",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response={},
        reason=None,
        thesis="test",
        strategy="test",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        correlation_id=correlation_id,
    )
    # The ledger booked its own observed partial before the cancel.
    await kl._update_ledger_outcome(
        ledger_id=ledger_id, status="partial", filled_qty=Decimal(ledger_partial_qty)
    )
    await kl._update_ledger_outcome(ledger_id=ledger_id, status="cancelled")

    with patch.object(
        kl, "run_resting_rung_sweep", AsyncMock(return_value={"swept": 0})
    ):
        result = await kl.kis_live_reconcile_orders_impl(dry_run=False)

    repair = result["proposal_projection_repair"]
    assert repair["candidates"] == 1
    assert repair["converged"] == 1
    assert repair["failed"] == 0
    _, rungs = await OrderProposalsService(db_session).get_proposal(proposal_id)
    assert rungs[0].state == "cancelled"
    assert rungs[0].filled_qty == Decimal(expected_qty)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_terminal_repair_reports_cap_when_prefix_exceeds_scan_cap(
    db_session,
):
    """ROB-719 gap D: a cap-bound pass says so instead of silently truncating.

    More unprojectable rows (accepting-rung evidence conflicts) than the
    scan cap: the candidate behind the prefix is not reached this pass and
    the report must mark cap_reached with a note.
    """
    from uuid import uuid4

    from app.mcp_server.tooling import kis_live_ledger as kl

    suffix = uuid4().hex
    for i in range(12):
        corr_x = f"live:kis_live:rob719-capx{i}-{suffix[:8]}"
        corr_y = f"live:kis_live:rob719-capy{i}-{suffix[:8]}"
        await _rob719_proposal_rung(
            db_session,
            suffix=f"capx{i}-{suffix}",
            symbol="214150",
            correlation_id=corr_x,
            broker_order_id=f"KIS-ROB719-CAPX{i}-{suffix[:8]}",
        )
        await _rob719_proposal_rung(
            db_session,
            suffix=f"capy{i}-{suffix}",
            symbol="214150",
            correlation_id=corr_y,
            broker_order_id=f"KIS-ROB719-CAPY{i}-{suffix[:8]}",
        )
        await kl._save_kis_live_order_ledger(
            symbol="214150",
            instrument_type="equity_kr",
            side="buy",
            order_type="limit",
            quantity=1.0,
            price=50000.0,
            amount=50000.0,
            currency="KRW",
            order_no=f"KIS-ROB719-CAPY{i}-{suffix[:8]}",
            order_time="090000",
            krx_fwdg_ord_orgno=None,
            status="cancelled",
            response_code="0",
            response_message=None,
            raw_response={},
            reason=None,
            thesis="test",
            strategy="test",
            target_price=None,
            stop_loss=None,
            min_hold_days=None,
            notes=None,
            exit_reason=None,
            indicators_snapshot=None,
            correlation_id=corr_x,
        )
    real_corr = f"live:kis_live:rob719-capend-{suffix[:8]}"
    real_odno = f"KIS-ROB719-CAPEND-{suffix[:8]}"
    await _rob719_proposal_rung(
        db_session,
        suffix=f"capend-{suffix}",
        symbol="214150",
        correlation_id=real_corr,
        broker_order_id=real_odno,
    )
    await kl._save_kis_live_order_ledger(
        symbol="214150",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=50000.0,
        amount=50000.0,
        currency="KRW",
        order_no=real_odno,
        order_time="090000",
        krx_fwdg_ord_orgno=None,
        status="filled",
        response_code="0",
        response_message=None,
        raw_response={},
        reason=None,
        thesis="test",
        strategy="test",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        correlation_id=real_corr,
    )

    # limit=1 => scan_cap=10; the prefix of 12 conflicts exceeds it, so the
    # real candidate (highest ledger id) is not reached this pass.
    result = await kl.kis_live_reconcile_orders_impl(dry_run=False, limit=1)

    repair = result["proposal_projection_repair"]
    assert repair["candidates"] == 0
    assert repair["scan"]["scanned"] == 10
    assert repair["scan"]["cap_reached"] is True
    assert "scan_cap reached" in repair["scan"]["note"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_repair_reports_skipped_scan(db_session):
    """ROB-719 gap D: the dry-run repair payload carries a scan stub."""
    from app.mcp_server.tooling import kis_live_ledger as kl

    result = await kl.kis_live_reconcile_orders_impl(dry_run=True)
    assert result["proposal_projection_repair"] == {
        "candidates": 0,
        "converged": 0,
        "failed": 0,
        "anomalies": {},
        "scan": {"skipped": "dry_run"},
    }
