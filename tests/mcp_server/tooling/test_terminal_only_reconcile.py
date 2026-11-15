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


def toss_row(kind="place"):
    return SimpleNamespace(
        id=2,
        broker_order_id="order",
        client_order_id="client",
        market="us",
        symbol="AAPL",
        operation_kind=kind,
        status="accepted",
        filled_qty=Decimal(0),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broker,case,books",
    [
        ("kis", "pending", False),
        ("kis", "missing", False),
        ("kis", "expired", False),
        ("kis", "cancelled", False),
        ("kis", "partial", True),
        ("kis", "filled", True),
        ("toss", "pending", False),
        ("toss", "expired", False),
        ("toss", "cancelled", False),
        ("toss", "rejected", False),
        ("toss", "replaced", False),
        ("toss", "cancel_request", False),
        ("toss", "partial_cancelled", True),
        ("toss", "filled", True),
    ],
)
async def test_terminal_scope_matches_full_except_fill_booking(broker, case, books):
    if broker == "kis":
        verdict = {
            "pending": FillVerdict.PENDING,
            "missing": FillVerdict.NONE,
            "expired": FillVerdict.EXPIRED,
            "cancelled": FillVerdict.CANCELLED,
            "partial": FillVerdict.PARTIAL,
            "filled": FillVerdict.FILLED,
        }[case]
        evidence = FillEvidence(
            verdict, Decimal(1) if books else Decimal(0), Decimal(10), None, "test", ""
        )
        adapter = SimpleNamespace(fetch_evidence=AsyncMock(return_value=evidence))
        with (
            patch.object(live, "get_evidence_adapter", return_value=adapter),
            patch.object(
                live, "_recorded_positive_fill_qty", new_callable=AsyncMock
            ) as recorded,
        ):
            recorded.return_value = Decimal(0)
            full = await live._reconcile_one_live_row(live_row(), dry_run=True)
            terminal = await live._reconcile_one_live_row(
                live_row(), dry_run=True, scope="terminal_only"
            )
    else:
        status = {
            "pending": "PENDING",
            "expired": "REJECTED",
            "cancelled": "CANCELED",
            "rejected": "REJECTED",
            "replaced": "REPLACED",
            "cancel_request": "REJECTED",
            "partial_cancelled": "CANCELED",
            "filled": "FILLED",
        }[case]
        order = SimpleNamespace(
            status=status,
            execution={"filledQuantity": "1", "averageFilledPrice": "10"}
            if books
            else {"filledQuantity": "0"},
            time_in_force="DAY",
            canceled_at="2026-11-12T15:00:00+09:00" if case == "expired" else None,
        )
        source = SimpleNamespace(
            evidence_for=AsyncMock(return_value=classify_toss_order_evidence(order))
        )
        row = toss_row("cancel" if case == "cancel_request" else "place")
        with patch.object(
            toss, "_recorded_positive_toss_fill_qty", new_callable=AsyncMock
        ) as recorded:
            recorded.return_value = Decimal(0)
            full = await toss._reconcile_one_toss_row(
                row, dry_run=True, evidence_source=source
            )
            terminal = await toss._reconcile_one_toss_row(
                row, dry_run=True, evidence_source=source, scope="terminal_only"
            )
    if books:
        assert full["action"] == "would_book"
        assert terminal == {
            **full,
            "action": "deferred_fill_booking",
            "requires_session_reconcile": True,
        }
    else:
        assert terminal == full


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["kis", "toss"])
@pytest.mark.parametrize("partial", [False, True])
async def test_terminal_scope_never_books_fill(broker, partial):
    if broker == "kis":
        evidence = FillEvidence(
            FillVerdict.PARTIAL if partial else FillVerdict.FILLED,
            Decimal(1),
            Decimal(10),
            None,
            "test",
            "",
        )
        adapter = SimpleNamespace(fetch_evidence=AsyncMock(return_value=evidence))
        with (
            patch.object(live, "get_evidence_adapter", return_value=adapter),
            patch.object(live, "_save_order_fill", new_callable=AsyncMock) as fill,
            patch.object(
                live, "_create_trade_journal_for_buy", new_callable=AsyncMock
            ) as journal,
            patch.object(
                live, "_close_journals_on_sell", new_callable=AsyncMock
            ) as pnl,
        ):
            result = await live._reconcile_one_live_row(
                live_row(), dry_run=False, scope="terminal_only"
            )
    else:
        order = SimpleNamespace(
            status="CANCELED" if partial else "FILLED",
            execution={"filledQuantity": "1", "averageFilledPrice": "10"},
        )
        source = SimpleNamespace(
            evidence_for=AsyncMock(return_value=classify_toss_order_evidence(order))
        )
        with (
            patch.object(toss, "_save_order_fill", new_callable=AsyncMock) as fill,
            patch.object(
                toss, "_create_trade_journal_for_buy", new_callable=AsyncMock
            ) as journal,
            patch.object(
                toss, "_close_journals_on_sell", new_callable=AsyncMock
            ) as pnl,
        ):
            result = await toss._reconcile_one_toss_row(
                toss_row(), dry_run=False, evidence_source=source, scope="terminal_only"
            )
    assert result["action"] == "deferred_fill_booking"
    fill.assert_not_awaited()
    journal.assert_not_awaited()
    pnl.assert_not_awaited()


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
                market="us", broker="kis", dry_run=False, scope="terminal_only"
            )
        )["success"]
        assert not (
            await toss.toss_reconcile_orders_impl(dry_run=False, scope="terminal_only")
        )["success"]
    ls.assert_not_awaited()
    ts.assert_not_awaited()


@pytest.mark.asyncio
async def test_us_terminal_scope_does_not_reach_other_markets():
    with patch.object(live, "run_resting_rung_sweep", new_callable=AsyncMock) as sweep:
        result = await live.live_reconcile_orders_impl(
            market="crypto", broker="upbit", dry_run=True, scope="terminal_only"
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


@pytest.mark.asyncio
async def test_both_scopes_share_prepasses_and_scan_limit(monkeypatch):
    monkeypatch.setattr(live.settings, "KIS_US_TERMINAL_AUTO_RECONCILE_ENABLED", True)
    monkeypatch.setattr(toss.settings, "TOSS_TERMINAL_AUTO_RECONCILE_ENABLED", True)
    with (
        patch.object(
            live, "run_resting_rung_sweep", new=AsyncMock(return_value={})
        ) as sweep,
        patch.object(
            live, "_count_open_live_ledger_rows", new=AsyncMock(return_value=0)
        ),
        patch.object(
            live, "_count_beyond_reach_live_ledger_rows", new=AsyncMock(return_value=0)
        ),
        patch.object(
            live, "_list_open_live_ledger_rows", new=AsyncMock(return_value=[])
        ) as listing,
    ):
        await live.live_reconcile_orders_impl(
            market="us", broker="kis", dry_run=False, limit=7
        )
        await live.live_reconcile_orders_impl(
            market="us", broker="kis", dry_run=False, limit=7, scope="terminal_only"
        )
    assert sweep.await_count == 2
    assert sweep.await_args_list[0] == sweep.await_args_list[1]
    assert listing.await_args_list[0] == listing.await_args_list[1]

    db = MagicMock()
    count = MagicMock()
    count.scalar_one.return_value = 0
    db.execute = AsyncMock(return_value=count)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=None)
    service = SimpleNamespace(
        reopen_anomalies_for_reconcile=AsyncMock(side_effect=lambda **kw: {"rows": []}),
        list_open=AsyncMock(return_value=[]),
    )
    with (
        patch.object(toss, "_order_session_factory", return_value=lambda: cm),
        patch.object(toss, "TossLiveOrderLedgerService", return_value=service),
        patch.object(
            toss,
            "_repair_terminal_toss_proposal_projections",
            new=AsyncMock(return_value={}),
        ) as repair,
        patch.object(
            toss, "run_resting_rung_sweep", new=AsyncMock(return_value={})
        ) as sweep,
    ):
        await toss.toss_reconcile_orders_impl(dry_run=False, limit=7)
        await toss.toss_reconcile_orders_impl(
            dry_run=False, limit=7, scope="terminal_only"
        )
    assert repair.await_count == sweep.await_count == 2
    assert repair.await_args_list[0] == repair.await_args_list[1]
    assert sweep.await_args_list[0] == sweep.await_args_list[1]
    assert (
        service.reopen_anomalies_for_reconcile.await_args_list[0]
        == service.reopen_anomalies_for_reconcile.await_args_list[1]
    )
    assert service.list_open.await_args_list[0] == service.list_open.await_args_list[1]


@pytest.mark.asyncio
async def test_replaced_order_projects_same_rung_in_both_scopes():
    order = SimpleNamespace(status="REPLACED", execution={"filledQuantity": "0"})
    source = SimpleNamespace(
        evidence_for=AsyncMock(return_value=classify_toss_order_evidence(order))
    )
    service = SimpleNamespace(update_reconcile_outcome=AsyncMock())
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=object())
    cm.__aexit__ = AsyncMock(return_value=None)
    projection = AsyncMock(
        return_value={"converged": True, "proposal_rung_state": "expired"}
    )
    row = toss_row()
    with (
        patch.object(toss, "_order_session_factory", return_value=lambda: cm),
        patch.object(toss, "TossLiveOrderLedgerService", return_value=service),
        patch.object(toss, "_converge_toss_proposal_rung", new=projection),
        patch.object(
            toss, "_recorded_positive_toss_fill_qty", new_callable=AsyncMock
        ) as recorded,
    ):
        recorded.return_value = Decimal(0)
        full = await toss._reconcile_one_toss_row(
            row, dry_run=False, evidence_source=source
        )
        terminal = await toss._reconcile_one_toss_row(
            row, dry_run=False, evidence_source=source, scope="terminal_only"
        )
    assert full == terminal
    assert projection.await_args_list[0] == projection.await_args_list[1]
    assert (
        service.update_reconcile_outcome.await_args_list[0]
        == service.update_reconcile_outcome.await_args_list[1]
    )
