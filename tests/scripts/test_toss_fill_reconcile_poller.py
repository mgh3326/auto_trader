# tests/scripts/test_toss_fill_reconcile_poller.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import scripts.toss_fill_reconcile_poller as cli


@pytest.mark.asyncio
async def test_disabled_kill_switch_makes_zero_calls():
    with (
        patch.object(cli.settings, "TOSS_FILL_POLL_ENABLED", False),
        patch.object(cli, "TossReadClient") as client_cls,
        patch.object(cli, "TossFillPollerService") as poller_cls,
        patch.object(cli, "TossLiveOrderLedgerService") as ledger_cls,
        patch.object(cli, "toss_reconcile_orders_impl", AsyncMock()) as reconcile,
    ):
        result = await cli.run_poll(dry_run=True)

    assert result["status"] == "disabled"
    client_cls.from_settings.assert_not_called()
    poller_cls.assert_not_called()
    ledger_cls.assert_not_called()
    reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_market_gate_inactive_skips_with_zero_broker_calls():
    with (
        patch.object(cli.settings, "TOSS_FILL_POLL_ENABLED", True),
        patch.object(
            cli,
            "_toss_fill_poll_market_gate",
            return_value={"active": False, "reason": "closed"},
        ),
        patch.object(cli, "TossReadClient") as client_cls,
        patch.object(cli, "TossFillPollerService") as poller_cls,
        patch.object(cli, "TossLiveOrderLedgerService") as ledger_cls,
        patch.object(cli, "toss_reconcile_orders_impl", AsyncMock()) as reconcile,
    ):
        result = await cli.run_poll(dry_run=False)

    assert result["status"] == "skipped"
    assert result["gate"]["active"] is False
    client_cls.from_settings.assert_not_called()
    poller_cls.assert_not_called()
    ledger_cls.assert_not_called()
    reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_lists_open_rows_without_broker_call():
    fake_row = type(
        "Row",
        (),
        {
            "id": 1,
            "broker_order_id": "toss-order-1",
            "client_order_id": "client-1",
            "market": "kr",
            "symbol": "005930",
            "operation_kind": "place",
            "status": "accepted",
        },
    )()

    fake_service = AsyncMock()
    fake_service.list_open = AsyncMock(return_value=[fake_row])

    with (
        patch.object(cli.settings, "TOSS_FILL_POLL_ENABLED", True),
        patch.object(cli, "_toss_fill_poll_market_gate", return_value={"active": True}),
        patch.object(cli, "TossLiveOrderLedgerService", return_value=fake_service),
        patch.object(cli, "TossReadClient") as client_cls,
        patch.object(cli, "TossFillPollerService") as poller_cls,
        patch.object(cli, "toss_reconcile_orders_impl", AsyncMock()) as reconcile,
    ):
        result = await cli.run_poll(dry_run=True, market="kr")

    assert result["status"] == "preview"
    assert result["target_count"] == 1
    assert result["targets"][0]["broker_order_id"] == "toss-order-1"
    fake_service.list_open.assert_awaited_once_with(market="kr", limit=100)
    client_cls.from_settings.assert_not_called()
    poller_cls.assert_not_called()
    reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_rerun_does_not_double_book_already_reconciled_row():
    """Two consecutive --commit passes over the same broker-confirmed fill must
    only invoke the write path once; the second pass sees the kernel's own
    dedupe outcome (``noop_already_booked``) and reports zero booked symbols.
    """
    fake_client = AsyncMock()
    fake_client.aclose = AsyncMock()

    fake_discover_service = AsyncMock()
    fake_discover_service.discover_external_orders = AsyncMock(
        return_value={"success": True, "seeded": 0, "candidates": 1}
    )

    first_reconcile = {
        "success": True,
        "counts": {"filled": 1},
        "reconciled": [{"action": "booked", "symbol": "005930"}],
    }
    second_reconcile = {
        "success": True,
        "counts": {"filled": 1},
        "reconciled": [{"action": "noop_already_booked", "symbol": "005930"}],
    }

    with (
        patch.object(cli.settings, "TOSS_FILL_POLL_ENABLED", True),
        patch.object(cli.settings, "TOSS_FILL_POLL_LOOKBACK_DAYS", 7),
        patch.object(cli.settings, "TOSS_FILL_POLL_CLOSED_PAGE_CAP", 20),
        patch.object(cli.settings, "TOSS_FILL_POLL_RECONCILE_LIMIT", 100),
        patch.object(cli, "_toss_fill_poll_market_gate", return_value={"active": True}),
        patch.object(cli.TossReadClient, "from_settings", return_value=fake_client),
        patch.object(cli, "TossFillPollerService", return_value=fake_discover_service),
        patch.object(
            cli,
            "toss_reconcile_orders_impl",
            AsyncMock(side_effect=[first_reconcile, second_reconcile]),
        ) as reconcile,
        patch.object(cli, "_invalidate_sellable_cache", AsyncMock()) as invalidate,
    ):
        first = await cli.run_poll(dry_run=False)
        second = await cli.run_poll(dry_run=False)

    assert first["status"] == "ran"
    assert first["booked_symbols"] == ["005930"]
    assert second["status"] == "ran"
    assert second["booked_symbols"] == []
    assert reconcile.await_count == 2
    for call in reconcile.await_args_list:
        assert call.kwargs["dry_run"] is False
        assert call.kwargs["limit"] == 100
    invalidate.assert_any_await(["005930"])
    invalidate.assert_any_await([])
    assert fake_client.aclose.await_count == 2


@pytest.mark.asyncio
async def test_run_poll_error_is_not_swallowed():
    with (
        patch.object(cli.settings, "TOSS_FILL_POLL_ENABLED", True),
        patch.object(cli, "_toss_fill_poll_market_gate", return_value={"active": True}),
        patch.object(
            cli.TossReadClient,
            "from_settings",
            side_effect=RuntimeError("toss disabled"),
        ),
    ):
        with pytest.raises(RuntimeError, match="toss disabled"):
            await cli.run_poll(dry_run=False)


@pytest.mark.asyncio
async def test_main_reports_structured_error_and_exit_code(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["toss_fill_reconcile_poller.py", "--commit"])
    with patch.object(cli, "run_poll", AsyncMock(side_effect=RuntimeError("boom"))):
        rc = await cli._main()

    assert rc == 1
    out = capsys.readouterr().out
    assert '"status": "error"' in out
    assert "boom" in out


@pytest.mark.asyncio
async def test_main_dry_run_by_default(monkeypatch):
    monkeypatch.setattr("sys.argv", ["toss_fill_reconcile_poller.py"])
    with patch.object(
        cli, "run_poll", AsyncMock(return_value={"status": "preview"})
    ) as run_poll:
        rc = await cli._main()

    assert rc == 0
    run_poll.assert_awaited_once_with(dry_run=True, market=None)
