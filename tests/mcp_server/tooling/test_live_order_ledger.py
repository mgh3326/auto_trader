import pytest
import pytest_asyncio
from sqlalchemy import delete


@pytest.mark.unit
def test_live_order_ledger_model_shape():
    from app.models.review import LiveOrderLedger

    assert LiveOrderLedger.__tablename__ == "live_order_ledger"
    cols = set(LiveOrderLedger.__table__.columns.keys())
    # 디스크리미네이터 + 시장 메타가 존재
    for c in (
        "broker",
        "account_scope",
        "market",
        "symbol",
        "exchange",
        "market_symbol",
        "order_no",
        "order_kind",
        "status",
        "filled_qty",
        "avg_fill_price",
        "buy_fx_rate",
        "sell_fx_rate",
        "fx_pnl_krw",
        "security_pnl_usd",
        "security_pnl_krw",
        "total_pnl_krw",
        "fx_rate_source",
        "fx_pnl_accuracy",
        "trade_id",
        "journal_id",
    ):
        assert c in cols, f"missing column {c}"
    assert LiveOrderLedger.__table__.schema == "review"


@pytest_asyncio.fixture(autouse=True)
async def _clean_live_ledger(db_session):
    # Depend on db_session so its create_all builds review.live_order_ledger
    # before we touch it (CI builds the test schema via create_all, not alembic).
    from app.mcp_server.tooling.live_order_ledger import _order_session_factory
    from app.models.review import LiveOrderLedger

    async with _order_session_factory()() as db:
        await db.execute(delete(LiveOrderLedger))
        await db.commit()
    yield


@pytest.mark.unit
@pytest.mark.asyncio
async def test_save_live_order_ledger_accepted_only():
    from app.mcp_server.tooling import live_order_ledger as ll

    lid = await ll._save_live_order_ledger(
        broker="kis",
        account_scope="kis_live",
        market="us",
        symbol="AAPL",
        exchange="NASD",
        market_symbol=None,
        side="buy",
        order_kind="limit",
        quantity=2.0,
        price=190.0,
        amount=380.0,
        currency="USD",
        order_no="US-ACC-1",
        order_time="0930",
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response={"odno": "US-ACC-1"},
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
    row = await ll._load_live_ledger_row(lid)
    assert row is not None
    assert row.status == "accepted"
    assert row.trade_id is None and row.journal_id is None  # no booking at send
    assert row.filled_qty is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_filled_buy_books_once_and_idempotent():
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import live_order_ledger as ll
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await ll._save_live_order_ledger(
        broker="kis",
        account_scope="kis_live",
        market="us",
        symbol="AAPL",
        exchange="NASD",
        market_symbol=None,
        side="buy",
        order_kind="limit",
        quantity=3.0,
        price=190.0,
        amount=570.0,
        currency="USD",
        order_no="US-RC-1",
        order_time="0930",
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
    row = await ll._load_live_ledger_row(lid)
    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("3"), Decimal("191.5"), None, "filled", ""
    )

    class _Adapter:
        broker = "kis"
        fetch_evidence = AsyncMock(return_value=filled)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(ll, "_save_order_fill", new=AsyncMock(return_value=111)) as m_fill,
        patch.object(
            ll,
            "_create_trade_journal_for_buy",
            new=AsyncMock(
                return_value={
                    "journal_created": True,
                    "journal_id": 9,
                    "journal_status": "draft",
                }
            ),
        ) as m_buy,
        patch.object(ll, "_link_journal_to_fill", new=AsyncMock(return_value=None)),
    ):
        out1 = await ll._reconcile_one_live_row(row, dry_run=False)
        # 재실행: 이미 booked → 델타 0 → 추가 booking 없음
        row2 = await ll._load_live_ledger_row(lid)
        out2 = await ll._reconcile_one_live_row(row2, dry_run=False)

    assert out1["verdict"] == "filled"
    assert out2["action"] == "noop_already_booked"
    # broker 확정 qty/price로 1회만 fill booking
    _, fkw = m_fill.await_args
    assert float(fkw["quantity"]) == 3.0
    assert float(fkw["price"]) == 191.5
    assert m_fill.await_count == 1  # 멱등: 두번째 reconcile은 booking 안 함
    assert m_buy.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_cancelled_no_journal():
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import live_order_ledger as ll
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await ll._save_live_order_ledger(
        broker="kis",
        account_scope="kis_live",
        market="us",
        symbol="AAPL",
        exchange="NASD",
        market_symbol=None,
        side="buy",
        order_kind="limit",
        quantity=3.0,
        price=190.0,
        amount=570.0,
        currency="USD",
        order_no="US-RC-2",
        order_time="0930",
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response=None,
        reason=None,
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
    )
    row = await ll._load_live_ledger_row(lid)
    none_ev = FillEvidence(FillVerdict.NONE, Decimal("0"), None, None, "cancelled", "")

    class _Adapter:
        broker = "kis"
        fetch_evidence = AsyncMock(return_value=none_ev)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(ll, "_save_order_fill", new=AsyncMock()) as m_fill,
        patch.object(ll, "_create_trade_journal_for_buy", new=AsyncMock()) as m_buy,
    ):
        out = await ll._reconcile_one_live_row(row, dry_run=False)

    assert out["verdict"] == "none"
    m_fill.assert_not_awaited()
    m_buy.assert_not_awaited()
    after = await ll._load_live_ledger_row(lid)
    assert after.status == "cancelled"
    assert after.journal_id is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_live_reconcile_impl_dry_run_empty():
    from app.mcp_server.tooling import live_order_ledger as ll

    # Scope to a guaranteed-nonexistent order_id so this is deterministic on the
    # shared test DB (other xdist workers may have open rows) and never reaches a
    # broker evidence adapter / real network call.
    out = await ll.live_reconcile_orders_impl(
        order_id="__no_such_order__", dry_run=True, limit=10
    )
    assert out["success"] is True
    assert out["dry_run"] is True
    assert out["counts"] == {}
    assert out["reconciled"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_live_order_inline_confirm_books_on_done():
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import live_order_ledger as ll
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("0.01"), Decimal("50000000"), None, "filled", ""
    )

    class _Adapter:
        broker = "upbit"
        fetch_evidence = AsyncMock(return_value=filled)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(ll, "_save_order_fill", new=AsyncMock(return_value=222)),
        patch.object(
            ll,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 12}),
        ),
        patch.object(ll, "_link_journal_to_fill", new=AsyncMock()),
    ):
        out = await ll._record_live_order(
            broker="upbit",
            account_scope="upbit_live",
            market="crypto",
            normalized_symbol="BTC",
            exchange=None,
            market_symbol="KRW-BTC",
            side="buy",
            order_kind="market",
            currency="KRW",
            order_no="U-INLINE-1",
            order_time=None,
            rt_cd="0",
            response_message=None,
            dry_run_result={
                "price": 0.0,
                "quantity": 0.01,
                "estimated_value": 500000.0,
            },
            execution_result={"uuid": "U-INLINE-1"},
            reason=None,
            exit_reason=None,
            thesis="t",
            strategy="s",
            target_price=None,
            stop_loss=None,
            min_hold_days=None,
            notes=None,
            indicators_snapshot=None,
            inline_confirm=True,
        )
    assert out["fill_recorded"] is True
    assert out["inline_reconcile"]["action"] == "booked"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_sell_reattaches_defensive_trim_note():
    """ROB-164/ROB-407: a defensive-trim sell stores approval audit at send; the
    evidence-gated reconcile close must reconstruct the DefensiveTrimContext from
    the ledger row and pass it to _close_journals_on_sell."""
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import live_order_ledger as ll
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await ll._save_live_order_ledger(
        broker="upbit",
        account_scope="upbit_live",
        market="crypto",
        symbol="KRW-BTC",
        exchange=None,
        market_symbol="KRW-BTC",
        side="sell",
        order_kind="limit",
        quantity=1.0,
        price=1005.0,
        amount=1005.0,
        currency="KRW",
        order_no="DT-SELL-1",
        order_time=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response=None,
        reason="defensive trim",
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        dt_approval_issue_id="ROB-164",
        dt_requester_agent_id="trader-agent",
        dt_caller_source="http_header",
    )
    row = await ll._load_live_ledger_row(lid)
    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("1"), Decimal("1005"), None, "filled", ""
    )

    class _Adapter:
        broker = "upbit"
        fetch_evidence = AsyncMock(return_value=filled)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(ll, "_save_order_fill", new=AsyncMock(return_value=321)),
        patch.object(
            ll,
            "_close_journals_on_sell",
            new=AsyncMock(
                return_value={
                    "journals_closed": 1,
                    "closed_ids": [42],
                    "total_pnl_pct": 0.5,
                    "buy_fx_rate": None,
                    "sell_fx_rate": None,
                    "fx_pnl_krw": None,
                    "security_pnl_usd": None,
                    "security_pnl_krw": None,
                    "total_pnl_krw": None,
                    "fx_rate_source": "unavailable",
                    "fx_pnl_accuracy": "unavailable",
                }
            ),
        ) as m_close,
    ):
        out = await ll._reconcile_one_live_row(row, dry_run=False)
    assert out["verdict"] == "filled"
    m_close.assert_awaited_once()
    ctx = m_close.await_args.kwargs["defensive_trim_ctx"]
    assert ctx is not None
    assert ctx.approval_issue_id == "ROB-164"
    assert ctx.requester_agent_id == "trader-agent"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_filled_sell_surfaces_journal_entry_basis():
    """ROB-544: the US/crypto sell reconcile surfaces the journal-entry (FIFO
    lot) close result, NOT an account-average.

    realized_pnl_basis=='journal_entry', and both realized_pnl_pct and the
    explicit journal_pnl_pct alias equal the close result's per-lot
    total_pnl_pct (mirrors test_reconcile_filled_sell_surfaces_journal_entry_basis
    in test_kis_live_ledger.py)."""
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import live_order_ledger as ll
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await ll._save_live_order_ledger(
        broker="upbit",
        account_scope="upbit_live",
        market="crypto",
        symbol="KRW-BTC",
        exchange=None,
        market_symbol="KRW-BTC",
        side="sell",
        order_kind="limit",
        quantity=1.0,
        price=974.0,
        amount=974.0,
        currency="KRW",
        order_no="JE-SELL-1",
        order_time=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response=None,
        reason=None,
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
    )
    row = await ll._load_live_ledger_row(lid)
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

    class _Adapter:
        broker = "upbit"
        fetch_evidence = AsyncMock(return_value=filled)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(ll, "_save_order_fill", new=AsyncMock(return_value=222)),
        patch.object(
            ll,
            "_close_journals_on_sell",
            new=AsyncMock(return_value=close_result),
        ),
    ):
        out = await ll._reconcile_one_live_row(row, dry_run=False)

    assert out["verdict"] == "filled"
    assert out["action"] == "booked"
    assert out["journals_closed"] == 1
    assert out["closed_journal_ids"] == [77]
    assert out["realized_pnl_basis"] == "journal_entry"
    # both the canonical key and the explicit alias mirror the same FIFO lot
    # basis value — NOT an account-average.
    assert out["realized_pnl_pct"] == pytest.approx(-2.61)
    assert out["journal_pnl_pct"] == pytest.approx(-2.61)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kis_us_buy_reconcile_captures_buy_fx_rate():
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import live_order_ledger as ll
    from app.mcp_server.tooling.fx_pnl import FxRateCapture
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await ll._save_live_order_ledger(
        broker="kis",
        account_scope="kis_live",
        market="us",
        symbol="AAPL",
        exchange="NASD",
        market_symbol=None,
        side="buy",
        order_kind="limit",
        quantity=3.0,
        price=190.0,
        amount=570.0,
        currency="USD",
        order_no="KIS-US-FX-1",
        order_time="0930",
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
    row = await ll._load_live_ledger_row(lid)
    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("3"), Decimal("191.5"), None, "filled", ""
    )

    class _Adapter:
        broker = "kis"
        fetch_evidence = AsyncMock(return_value=filled)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(
            ll,
            "capture_reconcile_spot_fx",
            new=AsyncMock(
                return_value=FxRateCapture(
                    rate=Decimal("1389.33"),
                    fx_rate_source="reconcile_spot",
                    fx_pnl_accuracy="approximate",
                )
            ),
        ),
        patch.object(ll, "_save_order_fill", new=AsyncMock(return_value=111)),
        patch.object(
            ll,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 9}),
        ) as m_buy,
        patch.object(ll, "_link_journal_to_fill", new=AsyncMock(return_value=None)),
    ):
        out = await ll._reconcile_one_live_row(row, dry_run=False)

    assert out["buy_fx_rate"] == pytest.approx(1389.33)
    assert out["fx_rate_source"] == "reconcile_spot"
    assert m_buy.await_args.kwargs["buy_fx_rate"] == 1389.33

    after = await ll._load_live_ledger_row(lid)
    assert after.buy_fx_rate == Decimal("1389.33")
    assert after.fx_rate_source == "reconcile_spot"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_buy_journal_backfills_correlation_id():
    """ROB-714: reconcile-time buy journal must carry the ledger row's
    correlation_id. Drives the REAL _reconcile_one_live_row (US path)."""
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import live_order_ledger as ll
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await ll._save_live_order_ledger(
        broker="kis",
        account_scope="kis_live",
        market="us",
        symbol="AAPL",
        exchange="NASD",
        market_symbol=None,
        side="buy",
        order_kind="limit",
        quantity=3.0,
        price=190.0,
        amount=570.0,
        currency="USD",
        order_no="US-RC-CORR",
        order_time="0930",
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
        correlation_id="live:kis_live:reconcileUS",
    )
    row = await ll._load_live_ledger_row(lid)
    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("3"), Decimal("191.5"), None, "filled", ""
    )

    class _Adapter:
        broker = "kis"
        fetch_evidence = AsyncMock(return_value=filled)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(ll, "_save_order_fill", new=AsyncMock(return_value=111)),
        patch.object(
            ll,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_id": 9}),
        ) as m_buy,
        patch.object(ll, "_link_journal_to_fill", new=AsyncMock(return_value=None)),
    ):
        await ll._reconcile_one_live_row(row, dry_run=False)

    m_buy.assert_awaited_once()
    assert m_buy.await_args.kwargs["correlation_id"] == "live:kis_live:reconcileUS"
