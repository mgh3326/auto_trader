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
async def test_reconcile_expired_marks_terminal_without_booking_fill_or_journal():
    """ROB-952: terminal US DAY expiry closes the ledger, never a trade."""
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
        symbol="GOOGL",
        exchange="NASD",
        market_symbol=None,
        side="buy",
        order_kind="limit",
        quantity=1.0,
        price=380.1,
        amount=380.1,
        currency="USD",
        order_no="0031116724",
        order_time="224201",
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
    expired = FillEvidence(
        FillVerdict.EXPIRED, Decimal("0"), None, None, "expired", "us_day_order"
    )

    class _Adapter:
        broker = "kis"
        fetch_evidence = AsyncMock(return_value=expired)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(ll, "_save_order_fill", new=AsyncMock()) as save_fill,
        patch.object(
            ll, "_create_trade_journal_for_buy", new=AsyncMock()
        ) as create_journal,
        patch.object(ll, "_close_journals_on_sell", new=AsyncMock()) as close_journals,
    ):
        preview = await ll._reconcile_one_live_row(row, dry_run=True)
        out = await ll._reconcile_one_live_row(row, dry_run=False)

    assert preview["verdict"] == "expired"
    assert preview["action"] == "would_mark_expired"
    assert out["verdict"] == "expired"
    assert out["action"] == "marked_expired"
    save_fill.assert_not_awaited()
    create_journal.assert_not_awaited()
    close_journals.assert_not_awaited()
    after = await ll._load_live_ledger_row(lid)
    assert after.status == "expired"
    assert after.filled_qty is None
    assert after.trade_id is None
    assert after.journal_id is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_evidence_fetch_failure_keeps_us_ledger_open() -> None:
    """ROB-952: unavailable broker evidence remains fail-closed, never expiry."""
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import live_order_ledger as ll

    lid = await ll._save_live_order_ledger(
        broker="kis",
        account_scope="kis_live",
        market="us",
        symbol="GOOGL",
        exchange="NASD",
        market_symbol=None,
        side="buy",
        order_kind="limit",
        quantity=1.0,
        price=380.1,
        amount=380.1,
        currency="USD",
        order_no="US-EVIDENCE-FAILURE",
        order_time="224201",
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

    class _Adapter:
        broker = "kis"
        fetch_evidence = AsyncMock(side_effect=RuntimeError("history unavailable"))

    with patch.object(ll, "get_evidence_adapter", return_value=_Adapter()):
        with pytest.raises(RuntimeError, match="history unavailable"):
            await ll._reconcile_one_live_row(row, dry_run=False)

    after = await ll._load_live_ledger_row(lid)
    assert after.status == "accepted"
    assert after.filled_qty is None


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
async def test_reconcile_us_sell_with_no_prior_journal_leaves_pnl_null():
    """ROB-955 regression guard — no false matching when no buy journal exists.

    Reproduces the exact prod incident (trades 274/275, XOM/AMZN 2026-07-17/18):
    the position was a pre-existing broker holding with no ``TradeJournal`` buy
    row ever recorded (data gap, not a matching-key bug — see ROB-955
    investigation).

    NOTE: the DB session/query here is mocked to unconditionally return an
    empty ``scalars().all()`` regardless of the WHERE predicate — this test
    exercises ``_reconcile_one_live_row``'s handling of an empty journal
    result (booking the fill/trade while leaving ``journals_closed=0`` and
    ``security_pnl_usd``/``journal_id`` null), NOT the SQL matching predicate
    itself (symbol/status/account_type/account). The matching predicate is
    covered by the real-DB tests below
    (``test_reconcile_us_sell_real_db_*``), which run against an actual
    ``TradeJournal`` row with no session-factory mock.
    """
    from decimal import Decimal
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.mcp_server.tooling import live_order_ledger as ll
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await ll._save_live_order_ledger(
        broker="kis",
        account_scope="kis_live",
        market="us",
        symbol="ROB955XOM",
        exchange="NYSE",
        market_symbol=None,
        side="sell",
        order_kind="limit",
        quantity=1.0,
        price=145.51,
        amount=145.51,
        currency="USD",
        order_no="ROB955-SELL-NOMATCH",
        order_time="2210",
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
        exit_reason="defensive_trim",
        indicators_snapshot=None,
    )
    row = await ll._load_live_ledger_row(lid)
    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("1"), Decimal("146.515"), None, "filled", ""
    )

    class _Adapter:
        broker = "kis"
        fetch_evidence = AsyncMock(return_value=filled)

    # Real _close_journals_on_sell queries this session factory; return an
    # empty active-journal set to mirror the prod data gap exactly.
    mock_session = AsyncMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_session.execute.return_value = mock_result
    session_cm = AsyncMock()
    session_cm.__aenter__.return_value = mock_session
    session_cm.__aexit__.return_value = None
    journal_factory = MagicMock(return_value=session_cm)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(ll, "capture_reconcile_spot_fx", new=AsyncMock(return_value=None)),
        patch.object(ll, "_save_order_fill", new=AsyncMock(return_value=274)),
        patch(
            "app.mcp_server.tooling.order_journal._order_session_factory",
            return_value=journal_factory,
        ),
    ):
        out = await ll._reconcile_one_live_row(row, dry_run=False)

    assert out["verdict"] == "filled"
    assert out["action"] == "booked"
    assert out["journals_closed"] == 0
    assert out["closed_journal_ids"] == []
    assert out.get("security_pnl_usd") is None

    after = await ll._load_live_ledger_row(lid)
    assert after.status == "filled"
    assert after.trade_id == 274
    assert after.journal_id is None
    assert after.security_pnl_usd is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_us_sell_with_matching_active_journal_computes_pnl():
    """ROB-955 regression guard — normal matching still computes realized PnL.

    Mirrors the same XOM lot basis from the prod incident's manual retro
    correction (KIS avg cost 136.28 -> sell 146.515 = +$10.235).

    NOTE: like the sibling test above, the DB session/query is mocked to
    unconditionally return a fixed single-journal list regardless of the
    WHERE predicate — this test exercises the FIFO lot consumption / PnL
    computation logic inside ``_close_journals_on_sell`` once a journal is
    in hand, NOT the SQL matching predicate that selects which journal(s)
    come back. The matching predicate is covered by the real-DB tests below
    (``test_reconcile_us_sell_real_db_*``).
    """
    from decimal import Decimal
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.mcp_server.tooling import live_order_ledger as ll
    from app.mcp_server.tooling.fx_pnl import FxRateCapture
    from app.models.trade_journal import TradeJournal
    from app.models.trading import InstrumentType
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    active_journal = TradeJournal(
        id=9001,
        symbol="ROB955XOM2",
        instrument_type=InstrumentType.equity_us,
        side="buy",
        entry_price=Decimal("136.28"),
        quantity=Decimal("1"),
        status="active",
        account="kis",
        account_type="live",
        thesis="t",
        buy_fx_rate=Decimal("1300.00"),
    )

    lid = await ll._save_live_order_ledger(
        broker="kis",
        account_scope="kis_live",
        market="us",
        symbol="ROB955XOM2",
        exchange="NYSE",
        market_symbol=None,
        side="sell",
        order_kind="limit",
        quantity=1.0,
        price=145.51,
        amount=145.51,
        currency="USD",
        order_no="ROB955-SELL-MATCH",
        order_time="2210",
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
        exit_reason="defensive_trim",
        indicators_snapshot=None,
    )
    row = await ll._load_live_ledger_row(lid)
    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("1"), Decimal("146.515"), None, "filled", ""
    )

    class _Adapter:
        broker = "kis"
        fetch_evidence = AsyncMock(return_value=filled)

    mock_session = AsyncMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [active_journal]
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_session.execute.return_value = mock_result
    session_cm = AsyncMock()
    session_cm.__aenter__.return_value = mock_session
    session_cm.__aexit__.return_value = None
    journal_factory = MagicMock(return_value=session_cm)

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
        patch.object(ll, "_save_order_fill", new=AsyncMock(return_value=275)),
        patch(
            "app.mcp_server.tooling.order_journal._order_session_factory",
            return_value=journal_factory,
        ),
    ):
        out = await ll._reconcile_one_live_row(row, dry_run=False)

    assert out["verdict"] == "filled"
    assert out["action"] == "booked"
    assert out["journals_closed"] == 1
    assert out["closed_journal_ids"] == [9001]
    assert out["security_pnl_usd"] == pytest.approx(10.235)
    assert active_journal.status == "closed"

    after = await ll._load_live_ledger_row(lid)
    assert after.status == "filled"
    assert after.trade_id == 275
    assert after.security_pnl_usd == Decimal("10.2350")


async def _seed_active_journal(
    *, symbol: str, account: str = "kis", account_type: str = "live"
):
    """Insert a real, committed active ``TradeJournal`` row via the shared
    ``AsyncSessionLocal`` sessionmaker (the same factory ``order_journal``'s
    ``_order_session_factory()`` returns), so a subsequent unmocked
    ``_close_journals_on_sell`` query can find (or, deliberately, fail to
    find) it through the real WHERE predicate."""
    from decimal import Decimal

    from app.mcp_server.tooling.live_order_ledger import _order_session_factory
    from app.models.trade_journal import JournalStatus, TradeJournal
    from app.models.trading import InstrumentType

    journal = TradeJournal(
        symbol=symbol,
        instrument_type=InstrumentType.equity_us,
        side="buy",
        entry_price=Decimal("136.28"),
        quantity=Decimal("1"),
        status=JournalStatus.active,
        account=account,
        account_type=account_type,
        thesis="t",
        buy_fx_rate=Decimal("1300.00"),
    )
    async with _order_session_factory()() as db:
        db.add(journal)
        await db.commit()
        await db.refresh(journal)
        return journal.id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_us_sell_real_db_match_computes_pnl():
    """ROB-955 F1 remediation — exercises the REAL ``TradeJournal`` matching
    query (symbol/status/account_type/account WHERE predicate) end-to-end,
    with NO ``order_journal._order_session_factory`` mock anywhere in this
    test. A real active journal is inserted via a separate session, then
    ``_reconcile_one_live_row`` runs the genuine SQL query to find it.

    Paired with ``test_reconcile_us_sell_real_db_account_type_mismatch_*``
    below: together they prove the predicate is actually evaluated — if any
    matching key regresses (e.g. account_type default drifts, or the account
    filter is dropped), this test or its sibling goes red.
    """
    import uuid
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import live_order_ledger as ll
    from app.mcp_server.tooling.fx_pnl import FxRateCapture
    from app.models.trade_journal import JournalStatus, TradeJournal
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    symbol = f"ROB955REALDB{uuid.uuid4().hex[:8].upper()}"
    journal_id = await _seed_active_journal(symbol=symbol)

    lid = await ll._save_live_order_ledger(
        broker="kis",
        account_scope="kis_live",
        market="us",
        symbol=symbol,
        exchange="NYSE",
        market_symbol=None,
        side="sell",
        order_kind="limit",
        quantity=1.0,
        price=145.51,
        amount=145.51,
        currency="USD",
        order_no=f"ROB955-REALDB-MATCH-{symbol}",
        order_time="2210",
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
        exit_reason="defensive_trim",
        indicators_snapshot=None,
    )
    row = await ll._load_live_ledger_row(lid)
    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("1"), Decimal("146.515"), None, "filled", ""
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
        patch.object(ll, "_save_order_fill", new=AsyncMock(return_value=501)),
    ):
        out = await ll._reconcile_one_live_row(row, dry_run=False)

    assert out["verdict"] == "filled"
    assert out["journals_closed"] == 1
    assert out["closed_journal_ids"] == [journal_id]
    assert out["security_pnl_usd"] == pytest.approx(10.235)

    from app.mcp_server.tooling.live_order_ledger import _order_session_factory

    async with _order_session_factory()() as db:
        refreshed = await db.get(TradeJournal, journal_id)
        assert refreshed.status == JournalStatus.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_us_sell_real_db_account_type_mismatch_leaves_pnl_null():
    """ROB-955 F1 remediation — sibling of the match test above, proving the
    real query's ``account_type`` predicate is actually load-bearing.

    Seeds a real, otherwise-identical active journal but with
    ``account_type="paper"`` (the sell reconcile always queries
    ``account_type="live"``). If the matching predicate ever regressed to
    ignore ``account_type`` (or any other key), this journal would wrongly
    match and this test would go red — unlike the over-mocked guards this
    replaces, which returned a fixed result regardless of the WHERE clause.
    """
    import uuid
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import live_order_ledger as ll
    from app.models.trade_journal import JournalStatus, TradeJournal
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    symbol = f"ROB955REALDB{uuid.uuid4().hex[:8].upper()}"
    journal_id = await _seed_active_journal(symbol=symbol, account_type="paper")

    lid = await ll._save_live_order_ledger(
        broker="kis",
        account_scope="kis_live",
        market="us",
        symbol=symbol,
        exchange="NYSE",
        market_symbol=None,
        side="sell",
        order_kind="limit",
        quantity=1.0,
        price=145.51,
        amount=145.51,
        currency="USD",
        order_no=f"ROB955-REALDB-MISMATCH-{symbol}",
        order_time="2210",
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
        exit_reason="defensive_trim",
        indicators_snapshot=None,
    )
    row = await ll._load_live_ledger_row(lid)
    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("1"), Decimal("146.515"), None, "filled", ""
    )

    class _Adapter:
        broker = "kis"
        fetch_evidence = AsyncMock(return_value=filled)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(
            ll, "capture_reconcile_spot_fx", new=AsyncMock(return_value=None)
        ),
        patch.object(ll, "_save_order_fill", new=AsyncMock(return_value=502)),
    ):
        out = await ll._reconcile_one_live_row(row, dry_run=False)

    assert out["verdict"] == "filled"
    assert out["journals_closed"] == 0
    assert out["closed_journal_ids"] == []
    assert out.get("security_pnl_usd") is None

    from app.mcp_server.tooling.live_order_ledger import _order_session_factory

    async with _order_session_factory()() as db:
        refreshed = await db.get(TradeJournal, journal_id)
        # untouched: the mismatched-account_type journal must never be
        # consumed by a "live" sell reconcile.
        assert refreshed.status == JournalStatus.active


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


# --- ROB-816 PR-3c: proposal-rung convergence from the reconcile kernel -------


async def _seed_resting_proposal(*, order_no: str, correlation_id: str):
    """Create a committed order_proposal whose single rung is `resting`, wired to
    the given broker order_no / correlation_id so reconcile evidence can find it.
    """
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.mcp_server.tooling.live_order_ledger import _order_session_factory
    from app.services.order_proposals import OrderProposalsService
    from app.services.order_proposals.service import RungInput

    now = datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
    async with _order_session_factory()() as db:
        svc = OrderProposalsService(db)
        group = await svc.create_proposal(
            symbol="KRW-BTC",
            market="crypto",
            account_mode="upbit",
            side="buy",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "buy", Decimal("0.001"), Decimal("50000000"), None)],
            now=now,
        )
        pid = group.proposal_id
        for state in ("revalidating", "approved", "submitting"):
            await svc.transition_rung(pid, 0, new_state=state)
        await svc.record_resting(
            pid,
            0,
            broker_order_id=order_no,
            correlation_id=correlation_id,
            idempotency_key=f"idem-{order_no}",
            approval_hash_digest=f"digest-{order_no}",
            now=now,
        )
        await db.commit()
        return pid


async def _read_rung_state(pid):
    from app.mcp_server.tooling.live_order_ledger import _order_session_factory
    from app.services.order_proposals import OrderProposalsService

    async with _order_session_factory()() as db:
        svc = OrderProposalsService(db)
        _, rungs = await svc.get_proposal(pid)
        return rungs[0].state


async def _save_crypto_ledger(*, order_no: str, correlation_id: str, status="accepted"):
    from app.mcp_server.tooling import live_order_ledger as ll

    return await ll._save_live_order_ledger(
        broker="upbit",
        account_scope="upbit_live",
        market="crypto",
        symbol="KRW-BTC",
        exchange=None,
        market_symbol="KRW-BTC",
        side="buy",
        order_kind="limit",
        quantity=0.001,
        price=50000000.0,
        amount=50000.0,
        currency="KRW",
        order_no=order_no,
        order_time=None,
        status=status,
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
        correlation_id=correlation_id,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_cancel_converges_proposal_rung(db_session):
    """ROB-816 PR-3c regression (canary proposal fa0dab30): a resting proposal
    rung whose broker order was cancelled converges to `cancelled` after
    reconcile picks up the broker's NONE (cancelled) evidence."""
    import uuid
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import live_order_ledger as ll
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    order_no = f"U-CXL-{uuid.uuid4()}"
    corr = f"corr-{order_no}"
    pid = await _seed_resting_proposal(order_no=order_no, correlation_id=corr)
    lid = await _save_crypto_ledger(order_no=order_no, correlation_id=corr)
    row = await ll._load_live_ledger_row(lid)
    none_ev = FillEvidence(FillVerdict.NONE, Decimal("0"), None, None, "cancelled", "")

    class _Adapter:
        broker = "upbit"
        fetch_evidence = AsyncMock(return_value=none_ev)

    with patch.object(ll, "get_evidence_adapter", return_value=_Adapter()):
        out = await ll._reconcile_one_live_row(row, dry_run=False)

    assert out["verdict"] == "none"
    assert await _read_rung_state(pid) == "cancelled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_fill_converges_proposal_rung_and_is_idempotent(db_session):
    """Broker fill evidence converges a resting rung to `filled`; a second
    reconcile pass over the already-booked ledger row is a no-op on the (now
    terminal) rung and never raises."""
    import uuid
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import live_order_ledger as ll
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    order_no = f"U-FILL-{uuid.uuid4()}"
    corr = f"corr-{order_no}"
    pid = await _seed_resting_proposal(order_no=order_no, correlation_id=corr)
    lid = await _save_crypto_ledger(order_no=order_no, correlation_id=corr)
    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("0.001"), Decimal("50000000"), None, "filled", ""
    )

    class _Adapter:
        broker = "upbit"
        fetch_evidence = AsyncMock(return_value=filled)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(ll, "_save_order_fill", new=AsyncMock(return_value=555)),
        patch.object(
            ll,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_id": 77}),
        ),
        patch.object(ll, "_link_journal_to_fill", new=AsyncMock(return_value=None)),
    ):
        out1 = await ll._reconcile_one_live_row(
            row=await ll._load_live_ledger_row(lid), dry_run=False
        )
        assert out1["action"] == "booked"
        assert await _read_rung_state(pid) == "filled"

        # Second pass: ledger row is now `filled`, delta<=0 → convergence must
        # short-circuit on the terminal rung rather than raise.
        out2 = await ll._reconcile_one_live_row(
            row=await ll._load_live_ledger_row(lid), dry_run=False
        )
        assert out2["action"] == "noop_already_booked"
        assert await _read_rung_state(pid) == "filled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_dry_run_does_not_touch_proposal_rung(db_session):
    """A dry-run reconcile is read-only — it never mutates proposal rung state."""
    import uuid
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import live_order_ledger as ll
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    order_no = f"U-DRY-{uuid.uuid4()}"
    corr = f"corr-{order_no}"
    pid = await _seed_resting_proposal(order_no=order_no, correlation_id=corr)
    lid = await _save_crypto_ledger(order_no=order_no, correlation_id=corr)
    row = await ll._load_live_ledger_row(lid)
    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("0.001"), Decimal("50000000"), None, "filled", ""
    )

    class _Adapter:
        broker = "upbit"
        fetch_evidence = AsyncMock(return_value=filled)

    with patch.object(ll, "get_evidence_adapter", return_value=_Adapter()):
        out = await ll._reconcile_one_live_row(row, dry_run=True)

    assert out["action"] == "would_book"
    assert await _read_rung_state(pid) == "resting"
