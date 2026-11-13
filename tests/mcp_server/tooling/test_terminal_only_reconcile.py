from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_server.tooling import live_order_ledger as live
from app.mcp_server.tooling import toss_live_ledger as toss
from app.mcp_server.tooling.toss_live_evidence import classify_toss_order_evidence
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    FillEvidence,
    FillVerdict,
)


def live_row():
    return SimpleNamespace(
        id=1,
        order_no="order",
        broker="kis",
        market="us",
        symbol="AAPL",
        filled_qty=Decimal(0),
    )


def toss_row():
    return SimpleNamespace(
        id=2,
        broker_order_id="order",
        client_order_id="client",
        market="us",
        symbol="AAPL",
        operation_kind="place",
        status="accepted",
        filled_qty=Decimal(0),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict,action",
    [
        (FillVerdict.EXPIRED, "marked_expired"),
        (FillVerdict.CANCELLED, "marked_cancelled"),
        (FillVerdict.NONE, "noop_no_evidence"),
        (FillVerdict.FILLED, "deferred_fill_booking"),
        (FillVerdict.PARTIAL, "deferred_fill_booking"),
    ],
)
async def test_kis_us_terminal_only_never_books_fill(verdict, action):
    evidence = FillEvidence(
        verdict,
        Decimal(1)
        if verdict in (FillVerdict.FILLED, FillVerdict.PARTIAL)
        else Decimal(0),
        Decimal(10),
        None,
        "test",
        "",
    )
    adapter = SimpleNamespace(fetch_evidence=AsyncMock(return_value=evidence))
    with (
        patch.object(live, "get_evidence_adapter", return_value=adapter),
        patch.object(
            live, "_update_live_ledger_outcome", new_callable=AsyncMock
        ) as update,
        patch.object(live, "_converge_proposal_rung", new_callable=AsyncMock),
        patch.object(live, "_save_order_fill", new_callable=AsyncMock) as fill,
        patch.object(
            live, "_create_trade_journal_for_buy", new_callable=AsyncMock
        ) as journal,
        patch.object(live, "_close_journals_on_sell", new_callable=AsyncMock) as pnl,
    ):
        result = await live._reconcile_one_live_row(
            live_row(), dry_run=False, terminal_only=True
        )
    assert result["action"] == action
    assert update.await_count == (
        1 if verdict in (FillVerdict.EXPIRED, FillVerdict.CANCELLED) else 0
    )
    fill.assert_not_awaited()
    journal.assert_not_awaited()
    pnl.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,qty,tif,expected",
    [
        ("REJECTED", "0", "DAY", "marked_rejected"),
        ("CANCELED", "0", "DAY", "marked_cancelled"),
        ("REJECTED", "0", "DAY_EXPIRED", "marked_expired"),
        ("CANCELED", "1", "DAY", "deferred_fill_booking"),
        ("FILLED", "2", "DAY", "deferred_fill_booking"),
        ("CANCELED", None, "DAY", "noop_no_evidence"),
    ],
)
async def test_toss_terminal_only_never_books_fill(status, qty, tif, expected):
    order = SimpleNamespace(
        status=status,
        execution={}
        if qty is None
        else {"filledQuantity": qty, "averageFilledPrice": "10"},
        canceled_at="2026-11-12T15:00:00+09:00" if tif == "DAY_EXPIRED" else None,
        time_in_force="DAY",
    )
    if tif == "DAY_EXPIRED":
        order.canceled_at = "2026-11-12T15:00:00+09:00"
    evidence = classify_toss_order_evidence(order)
    source = SimpleNamespace(evidence_for=AsyncMock(return_value=evidence))
    service = MagicMock()
    service.update_reconcile_outcome = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=object())
    cm.__aexit__ = AsyncMock(return_value=None)
    with (
        patch.object(toss, "_order_session_factory", return_value=lambda: cm),
        patch.object(toss, "TossLiveOrderLedgerService", return_value=service),
        patch.object(
            toss, "_converge_toss_proposal_rung", new=AsyncMock(return_value=None)
        ) as projection,
        patch.object(toss, "_save_order_fill", new_callable=AsyncMock) as fill,
        patch.object(
            toss, "_create_trade_journal_for_buy", new_callable=AsyncMock
        ) as journal,
        patch.object(toss, "_close_journals_on_sell", new_callable=AsyncMock) as pnl,
    ):
        result = await toss._reconcile_one_toss_row(
            toss_row(), dry_run=False, terminal_only=True, evidence_source=source
        )
    assert result["action"] == expected
    assert service.update_reconcile_outcome.await_count == int(
        expected.startswith("marked_")
    )
    assert projection.await_count == int(expected.startswith("marked_"))
    if expected.startswith("marked_"):
        assert projection.await_args.kwargs["filled_qty"] is None
    fill.assert_not_awaited()
    journal.assert_not_awaited()
    pnl.assert_not_awaited()


@pytest.mark.asyncio
async def test_toss_terminal_projection_failure_keeps_ledger_open():
    order = SimpleNamespace(status="CANCELED", execution={"filledQuantity": "0"})
    source = SimpleNamespace(
        evidence_for=AsyncMock(return_value=classify_toss_order_evidence(order))
    )
    with (
        patch.object(
            toss,
            "_converge_toss_proposal_rung",
            new=AsyncMock(return_value={"converged": False}),
        ),
        patch.object(toss, "_order_session_factory") as session,
    ):
        result = await toss._reconcile_one_toss_row(
            toss_row(), dry_run=False, terminal_only=True, evidence_source=source
        )
    assert result["action"] == "projection_failed"
    session.assert_not_called()


@pytest.mark.asyncio
async def test_toss_terminal_projection_can_be_disabled():
    order = SimpleNamespace(status="CANCELED", execution={"filledQuantity": "0"})
    source = SimpleNamespace(
        evidence_for=AsyncMock(return_value=classify_toss_order_evidence(order))
    )
    service = MagicMock()
    service.update_reconcile_outcome = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=object())
    cm.__aexit__ = AsyncMock(return_value=None)
    with (
        patch.object(
            toss, "_converge_toss_proposal_rung", new_callable=AsyncMock
        ) as projection,
        patch.object(toss, "_order_session_factory", return_value=lambda: cm),
        patch.object(toss, "TossLiveOrderLedgerService", return_value=service),
    ):
        result = await toss._reconcile_one_toss_row(
            toss_row(),
            dry_run=False,
            terminal_only=True,
            evidence_source=source,
            project_proposal_rungs=False,
        )
    assert result["action"] == "marked_cancelled"
    projection.assert_not_awaited()
    service.update_reconcile_outcome.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_modes_refuse_before_broker_or_database(monkeypatch):
    monkeypatch.setattr(live.settings, "KIS_US_TERMINAL_AUTO_RECONCILE_ENABLED", False)
    monkeypatch.setattr(toss.settings, "TOSS_TERMINAL_AUTO_RECONCILE_ENABLED", False)
    with (
        patch.object(live, "run_resting_rung_sweep", new_callable=AsyncMock) as ls,
        patch.object(toss, "run_resting_rung_sweep", new_callable=AsyncMock) as ts,
    ):
        assert not (
            await live.live_reconcile_orders_impl(
                market="us", broker="kis", dry_run=False, terminal_only=True
            )
        )["success"]
        assert not (
            await toss.toss_reconcile_orders_impl(dry_run=False, terminal_only=True)
        )["success"]
    ls.assert_not_awaited()
    ts.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broker_order,verdict",
    [
        (
            {
                "odno": "order",
                "pdno": "AAPL",
                "ft_ord_qty": "2",
                "ft_ccld_qty": "0",
                "nccs_qty": "0",
            },
            FillVerdict.EXPIRED,
        ),
        (
            {
                "odno": "order",
                "pdno": "AAPL",
                "ft_ord_qty": "2",
                "ft_ccld_qty": "0",
                "nccs_qty": "0",
                "cncl_yn": "Y",
            },
            FillVerdict.CANCELLED,
        ),
        (
            {
                "odno": "order",
                "pdno": "AAPL",
                "ft_ord_qty": "2",
                "ft_ccld_qty": "1",
                "ft_ccld_unpr3": "10",
                "nccs_qty": "0",
                "cncl_yn": "Y",
            },
            FillVerdict.PARTIAL,
        ),
        (
            {
                "odno": "order",
                "pdno": "AAPL",
                "ft_ord_qty": "2",
                "ft_ccld_qty": "2",
                "ft_ccld_unpr3": "10",
                "nccs_qty": "0",
            },
            FillVerdict.FILLED,
        ),
        (
            {"odno": "order", "pdno": "AAPL", "ft_ord_qty": "2", "nccs_qty": "0"},
            FillVerdict.NONE,
        ),
    ],
)
async def test_kis_us_adapter_requires_complete_broker_quantities(
    broker_order, verdict
):
    from app.mcp_server.tooling import live_order_evidence as evidence

    with (
        patch.object(evidence, "_create_live_kis_client", return_value=object()),
        patch.object(
            evidence,
            "_build_us_exchange_candidates",
            new=AsyncMock(return_value=["NASD"]),
        ),
        patch.object(
            evidence,
            "_find_us_order_in_recent_history",
            new=AsyncMock(return_value=(broker_order, "NASD")),
        ),
    ):
        result = await evidence.UsOverseasEvidenceAdapter().fetch_evidence(
            live_row(), terminal_only=True
        )
    assert result.verdict == verdict


@pytest.mark.asyncio
async def test_terminal_mode_rejects_other_markets_before_broker():
    with patch.object(live, "run_resting_rung_sweep", new_callable=AsyncMock) as sweep:
        result = await live.live_reconcile_orders_impl(
            market="crypto", broker="upbit", dry_run=True, terminal_only=True
        )
    assert result["success"] is False
    sweep.assert_not_awaited()


def test_terminal_flags_default_false():
    from app.core.config import Settings

    assert (
        Settings.model_fields["KIS_US_TERMINAL_AUTO_RECONCILE_ENABLED"].default is False
    )
    assert (
        Settings.model_fields["TOSS_TERMINAL_AUTO_RECONCILE_ENABLED"].default is False
    )
