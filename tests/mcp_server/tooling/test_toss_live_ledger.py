from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.review import TossLiveOrderLedger
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    await db_session.execute(delete(TossLiveOrderLedger))
    await db_session.commit()
    yield


@pytest.fixture(autouse=True)
def _patch_session_factory(db_session):
    from app.mcp_server.tooling import toss_live_ledger

    # Create a mock that when called twice returns db_session
    # async with _order_session_factory()() as db:
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = db_session
    mock_cm.__aexit__.return_value = None

    def factory_call():
        return mock_cm

    with patch.object(
        toss_live_ledger, "_order_session_factory", return_value=factory_call
    ):
        yield


async def _accepted(db_session, *, side: str = "buy", market: str = "us"):
    is_kr = market == "kr"
    suffix = side if market == "us" else f"{market}-{side}"
    return await TossLiveOrderLedgerService(db_session).record_send(
        operation_kind="place",
        market=market,
        symbol="034020" if is_kr else "AAPL",
        side=side,
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("3") if is_kr else Decimal("2"),
        price=Decimal("85000") if is_kr else Decimal("190"),
        order_amount=None,
        currency="KRW" if is_kr else "USD",
        client_order_id=f"cid-{suffix}",
        broker_order_id=f"ord-{suffix}",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
        thesis="t" if side == "buy" else None,
        strategy="s" if side == "buy" else None,
        exit_reason="trim" if side == "sell" else None,
    )


async def test_reconcile_filled_buy_books_once(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod, "_save_order_fill", new=AsyncMock(return_value=101)
        ) as m_fill,
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ) as m_journal,
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
    ):
        out1 = await mod._reconcile_one_toss_row(row, dry_run=False)
        row2 = await db_session.get(TossLiveOrderLedger, row.id)
        db_session.expunge(row2)
        out2 = await mod._reconcile_one_toss_row(row2, dry_run=False)

    assert out1["action"] == "booked"
    assert out2["action"] == "noop_already_booked"
    assert m_fill.await_count == 1
    assert m_fill.await_args.kwargs["fee"] == 0.06
    assert m_journal.await_count == 1


async def test_reconcile_filled_kr_buy_books_with_equity_kr_instrument_type(db_session):
    """ROB-631: KR equity fills must book with InstrumentType.equity_kr.

    The reconcile path previously hardcoded the invalid literal ``"equity"`` for
    KR rows, which is not an ``InstrumentType`` member, so the buy-journal create
    raised ``ValueError: 'equity' is not a valid InstrumentType`` and the row was
    parked as anomaly/requires_manual_review instead of being booked.
    """
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence
    from app.models.trading import InstrumentType

    row = await _accepted(db_session, side="buy", market="kr")
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("3"),
        avg_price=Decimal("85000"),
        commission=Decimal("100"),
        tax=Decimal("50"),
        fee_total=Decimal("150"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod, "_save_order_fill", new=AsyncMock(return_value=101)
        ) as m_fill,
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ) as m_journal,
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    # The instrument type fed to both the trade-fill insert and the buy journal
    # must be a valid InstrumentType member for KR equities, not the bare "equity".
    assert m_fill.await_args.kwargs["instrument_type"] == "equity_kr"
    assert m_journal.await_args.kwargs["market_type"] == "equity_kr"
    # Bind the contract to the real consequence: InstrumentType(...) must not raise.
    assert (
        InstrumentType(m_journal.await_args.kwargs["market_type"])
        is InstrumentType.equity_kr
    )


async def test_reconcile_filled_kr_sell_books_with_equity_kr_instrument_type(
    db_session,
):
    """ROB-631: KR sell fills also pass instrument type to _save_order_fill.

    On the sell path the bad "equity" literal was swallowed by _save_order_fill's
    try/except, silently dropping the trade row. The fill must carry "equity_kr".
    """
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session, side="sell", market="kr")
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("3"),
        avg_price=Decimal("90000"),
        commission=Decimal("100"),
        tax=Decimal("50"),
        fee_total=Decimal("150"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )
    close_result = {
        "journals_closed": 1,
        "journals_kept": 0,
        "closed_ids": [77],
        "total_pnl_pct": 5.0,
        "realized_pnl_basis": "journal_entry",
        "total_pnl_krw": 15000.0,
    }

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod, "_save_order_fill", new=AsyncMock(return_value=303)
        ) as m_fill,
        patch.object(
            mod, "_close_journals_on_sell", new=AsyncMock(return_value=close_result)
        ),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    assert m_fill.await_args.kwargs["instrument_type"] == "equity_kr"


async def test_reconcile_cancelled_partial_books_delta_and_terminal(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="partial",
        local_status="cancelled",
        broker_status="CANCELED",
        filled_qty=Decimal("0.5"),
        avg_price=Decimal("190.5"),
        commission=Decimal("0.02"),
        tax=Decimal("0"),
        fee_total=Decimal("0.02"),
        settlement_date=None,
        raw_order={"status": "CANCELED"},
        reason="partial cancelled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=303)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 404}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "cancelled"
    assert refreshed.filled_qty == Decimal("0.5")


async def test_reconcile_pending_is_noop(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="pending",
        local_status="pending",
        broker_status="PENDING",
        filled_qty=Decimal("0"),
        avg_price=None,
        commission=None,
        tax=None,
        fee_total=Decimal("0"),
        settlement_date=None,
        raw_order={"status": "PENDING"},
        reason="pending",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "noop_pending"
    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "accepted"


async def test_reconcile_impl_lists_only_toss_rows(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod

    await _accepted(db_session)

    with patch.object(
        mod,
        "_reconcile_one_toss_row",
        new=AsyncMock(return_value={"verdict": "pending", "action": "noop_pending"}),
    ):
        out = await mod.toss_reconcile_orders_impl(dry_run=True)

    assert out["success"] is True
    assert out["dry_run"] is True
    assert out["counts"] == {"pending": 1}


async def test_rejected_replacement_reopens_original_order(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    original = await _accepted(db_session)
    replacement = await TossLiveOrderLedgerService(db_session).record_send(
        operation_kind="modify",
        market="us",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("2"),
        price=Decimal("191"),
        order_amount=None,
        currency="USD",
        client_order_id="cid-rejected-replacement",
        broker_order_id="ord-rejected-replacement",
        original_order_id=original.broker_order_id,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
    )
    original.replaced_by_order_id = replacement.broker_order_id
    await db_session.commit()
    db_session.expunge(replacement)

    evidence = TossFillEvidence(
        verdict="pending",
        local_status="replace_rejected",
        broker_status="REPLACE_REJECTED",
        filled_qty=Decimal("0"),
        avg_price=None,
        commission=None,
        tax=None,
        fee_total=Decimal("0"),
        settlement_date=None,
        raw_order={"status": "REPLACE_REJECTED"},
        reason="replace rejected",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()):
        out = await mod._reconcile_one_toss_row(replacement, dry_run=False)

    assert out["action"] == "noop_pending"
    refreshed_original = await db_session.get(TossLiveOrderLedger, original.id)
    refreshed_replacement = await db_session.get(TossLiveOrderLedger, replacement.id)
    assert refreshed_original.status == "accepted"
    assert refreshed_original.replaced_by_order_id is None
    assert refreshed_replacement.status == "replace_rejected"


async def test_reconcile_impl_reports_manual_review_on_error_without_mutating_dry_run(
    db_session,
):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.services.brokers.toss.errors import TossApiResponseError, TossErrorEnvelope

    row = await _accepted(db_session)
    err = TossApiResponseError(
        TossErrorEnvelope(
            request_id="ray-dry",
            code="non-json-response",
            message="<html>Forbidden dry-run</html>",
            data=None,
        ),
        status_code=403,
    )

    with patch.object(mod, "_reconcile_one_toss_row", new=AsyncMock(side_effect=err)):
        out = await mod.toss_reconcile_orders_impl(dry_run=True)

    assert out["counts"] == {"anomaly": 1}
    assert out["reconciled"][0]["requires_manual_review"] is True
    assert out["reconciled"][0]["manual_review_reason"].startswith(
        "reconcile failed; operator must verify Toss order detail"
    )
    assert out["reconciled"][0]["error_details"] == {
        "type": "TossApiResponseError",
        "status_code": 403,
        "code": "non-json-response",
        "request_id": "ray-dry",
        "message": "<html>Forbidden dry-run</html>",
        "data": None,
    }

    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "accepted"
    assert refreshed.requires_manual_review is False
    assert refreshed.last_reconcile_error is None


async def test_reconcile_impl_marks_manual_review_on_error_when_not_dry_run(
    db_session,
):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.services.brokers.toss.errors import TossApiResponseError, TossErrorEnvelope

    row = await _accepted(db_session)
    err = TossApiResponseError(
        TossErrorEnvelope(
            request_id="ray-apply",
            code="non-json-response",
            message="<html>Forbidden apply</html>",
            data=None,
        ),
        status_code=403,
    )

    with patch.object(mod, "_reconcile_one_toss_row", new=AsyncMock(side_effect=err)):
        out = await mod.toss_reconcile_orders_impl(dry_run=False)

    assert out["counts"] == {"anomaly": 1}
    assert out["reconciled"][0]["action"] == "requires_manual_review"
    assert out["reconciled"][0]["requires_manual_review"] is True

    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "anomaly"
    assert refreshed.requires_manual_review is True
    assert refreshed.manual_review_reason.startswith(
        "reconcile failed; operator must verify Toss order detail"
    )
    assert refreshed.last_reconcile_error == {
        "type": "TossApiResponseError",
        "status_code": 403,
        "code": "non-json-response",
        "request_id": "ray-apply",
        "message": "<html>Forbidden apply</html>",
        "data": None,
    }


async def test_toss_us_buy_reconcile_captures_buy_fx_rate(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.fx_pnl import FxRateCapture
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session, side="buy")
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("100"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod,
            "capture_reconcile_spot_fx",
            new=AsyncMock(
                return_value=FxRateCapture(
                    rate=Decimal("1389.33"),
                    fx_rate_source="reconcile_spot",
                    fx_pnl_accuracy="approximate",
                )
            ),
        ),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ) as m_journal,
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["buy_fx_rate"] == pytest.approx(1389.33)
    assert out["fx_rate_source"] == "reconcile_spot"
    assert m_journal.await_args.kwargs["buy_fx_rate"] == 1389.33


async def test_toss_us_sell_reconcile_surfaces_fx_pnl(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.fx_pnl import FxRateCapture
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session, side="sell")
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("130"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )
    close_result = {
        "journals_closed": 1,
        "journals_kept": 0,
        "closed_ids": [77],
        "total_pnl_pct": 30.0,
        "realized_pnl_basis": "journal_entry",
        "buy_fx_rate": 1389.33,
        "sell_fx_rate": 1503.19,
        "fx_pnl_krw": 22772.0,
        "security_pnl_usd": 60.0,
        "security_pnl_krw": 90191.4,
        "total_pnl_krw": 112963.4,
        "fx_rate_source": "reconcile_spot",
        "fx_pnl_accuracy": "approximate",
        "fx_unavailable_journal_ids": [],
    }

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod,
            "capture_reconcile_spot_fx",
            new=AsyncMock(
                return_value=FxRateCapture(
                    rate=Decimal("1503.19"),
                    fx_rate_source="reconcile_spot",
                    fx_pnl_accuracy="approximate",
                )
            ),
        ),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod, "_close_journals_on_sell", new=AsyncMock(return_value=close_result)
        ),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["fx_pnl_krw"] == 22772.0
    assert out["total_pnl_krw"] == 112963.4
    assert out["fx_rate_source"] == "reconcile_spot"
    assert out["fx_pnl_accuracy"] == "approximate"


async def test_toss_us_sell_reconcile_persists_zero_fx_values(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.fx_pnl import FxRateCapture
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session, side="sell")
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("100"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )
    close_result = {
        "journals_closed": 1,
        "journals_kept": 0,
        "closed_ids": [77],
        "total_pnl_pct": 0.0,
        "realized_pnl_basis": "journal_entry",
        "buy_fx_rate": 1500.0,
        "sell_fx_rate": 1500.0,
        "fx_pnl_krw": 0.0,
        "security_pnl_usd": 0.0,
        "security_pnl_krw": 0.0,
        "total_pnl_krw": 0.0,
        "fx_rate_source": "reconcile_spot",
        "fx_pnl_accuracy": "approximate",
        "fx_unavailable_journal_ids": [],
    }

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod,
            "capture_reconcile_spot_fx",
            new=AsyncMock(
                return_value=FxRateCapture(
                    rate=Decimal("1500"),
                    fx_rate_source="reconcile_spot",
                    fx_pnl_accuracy="approximate",
                )
            ),
        ),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod, "_close_journals_on_sell", new=AsyncMock(return_value=close_result)
        ),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["fx_pnl_krw"] == 0.0
    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.fx_pnl_krw == Decimal("0.0000")
    assert refreshed.security_pnl_usd == Decimal("0.0000")
    assert refreshed.total_pnl_krw == Decimal("0.0000")


async def test_reconcile_booked_fill_notifies_when_enabled(db_session):
    from types import SimpleNamespace

    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    notifier = SimpleNamespace(notify_fill=AsyncMock(return_value=True))
    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", True),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod, "capture_reconcile_spot_fx", new=AsyncMock(return_value=None)
        ),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
        patch.object(mod, "get_trade_notifier", return_value=notifier),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    assert out["fill_notified"] is True
    notifier.notify_fill.assert_awaited_once()
    order = notifier.notify_fill.await_args.args[0]
    assert order.account == "toss"
    assert order.market_type == "us"
    assert order.currency == "USD"
    assert order.filled_qty == 2
    assert notifier.notify_fill.await_args.kwargs["enrichment"] is None
    assert notifier.notify_fill.await_args.kwargs["detail_url"].endswith(
        "/invest/stocks/us/AAPL"
    )


async def test_reconcile_booked_fill_skips_notify_when_gate_disabled(db_session):
    from types import SimpleNamespace

    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    notifier = SimpleNamespace(notify_fill=AsyncMock(return_value=True))
    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod, "capture_reconcile_spot_fx", new=AsyncMock(return_value=None)
        ),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
        patch.object(mod, "get_trade_notifier", return_value=notifier),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    assert out["fill_notified"] is False
    notifier.notify_fill.assert_not_awaited()


async def test_reconcile_booked_fill_skips_notify_below_threshold(db_session):
    from types import SimpleNamespace

    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    notifier = SimpleNamespace(notify_fill=AsyncMock(return_value=True))
    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", True),
        patch.object(mod, "is_fill_notifiable", return_value=False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod, "capture_reconcile_spot_fx", new=AsyncMock(return_value=None)
        ),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
        patch.object(mod, "get_trade_notifier", return_value=notifier),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    assert out["fill_notified"] is False
    notifier.notify_fill.assert_not_awaited()


async def test_reconcile_booked_fill_notification_failure_is_fail_open(db_session):
    from types import SimpleNamespace

    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    notifier = SimpleNamespace(
        notify_fill=AsyncMock(side_effect=RuntimeError("discord down"))
    )
    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", True),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod, "capture_reconcile_spot_fx", new=AsyncMock(return_value=None)
        ),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
        patch.object(mod, "get_trade_notifier", return_value=notifier),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    assert out["fill_notified"] is False
    notifier.notify_fill.assert_awaited_once()


async def test_reconcile_buy_journal_backfills_correlation_id(db_session):
    """ROB-714: reconcile-time buy journal must carry the ledger row's
    correlation_id. Drives the REAL _reconcile_one_toss_row (KR path)."""
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await TossLiveOrderLedgerService(db_session).record_send(
        operation_kind="place",
        market="kr",
        symbol="034020",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("3"),
        price=Decimal("85000"),
        order_amount=None,
        currency="KRW",
        client_order_id="cid-corr-kr",
        broker_order_id="ord-corr-kr",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
        thesis="t",
        strategy="s",
        correlation_id="live:toss_live:reconcileKR",
    )
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("3"),
        avg_price=Decimal("85100"),
        commission=Decimal("0"),
        tax=Decimal("0"),
        fee_total=Decimal("0"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ) as m_journal,
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
    ):
        await mod._reconcile_one_toss_row(row, dry_run=False)

    m_journal.assert_awaited_once()
    assert m_journal.await_args.kwargs["correlation_id"] == "live:toss_live:reconcileKR"
