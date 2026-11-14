from unittest.mock import AsyncMock, patch

import pytest

import scripts.kis_us_auto_reconcile as us
import scripts.toss_live_auto_reconcile as toss


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cli,kernel",
    [(us, "live_reconcile_orders_impl"), (toss, "toss_reconcile_orders_impl")],
)
@pytest.mark.parametrize(
    "apply,row,expected",
    [
        (False, {"action": "noop_pending"}, 0),
        (False, {"action": "noop_terminal"}, 0),
        (True, {"action": "marked_cancelled"}, 0),
        (False, {"action": "marked_expired"}, 3),
        (False, {"action": "would_mark_expired"}, 3),
        (True, {"action": "deferred_fill_booking"}, 3),
        (False, {"action": "noop_pending", "reason_code": "not_found"}, 3),
        (False, {"action": "noop_pending", "requires_manual_review": True}, 3),
        (True, {"action": "noop_no_evidence"}, 3),
        (True, {"action": "anomaly", "error": "failed"}, 3),
        (True, {"action": "noop_pending", "error": ""}, 3),
        (True, {"action": "unexpected"}, 3),
    ],
)
async def test_cli_closed_exit_vocabulary(cli, kernel, apply, row, expected, capsys):
    with patch.object(
        cli, kernel, AsyncMock(return_value={"success": True, "reconciled": [row]})
    ):
        assert await cli._run(apply=apply, terminal_only=True) == expected
    assert ("WARNING" in capsys.readouterr().out) is (expected == 3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cli,kernel",
    [(us, "live_reconcile_orders_impl"), (toss, "toss_reconcile_orders_impl")],
)
async def test_cli_incomplete_or_failed_scan_is_nonzero(cli, kernel):
    for result in (
        {"success": False},
        {"success": True, "candidate_scan": {"truncated": True}},
        {"success": True, "proposal_rung_sweep": {"error": "failed"}},
        {"success": True, "proposal_projection_repair": {"failed": 1}},
        {"success": True, "batch_build_error": {"type": "failed"}},
    ):
        with patch.object(cli, kernel, AsyncMock(return_value=result)):
            assert await cli._run(apply=False, terminal_only=False) == 3


@pytest.mark.parametrize(
    "cli,flag,kernel",
    [
        (us, "KIS_US_TERMINAL_AUTO_RECONCILE_ENABLED", "live_reconcile_orders_impl"),
        (toss, "TOSS_TERMINAL_AUTO_RECONCILE_ENABLED", "toss_reconcile_orders_impl"),
    ],
)
def test_disabled_cli_never_calls_kernel(cli, flag, kernel, monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli", "--apply-terminal-only"])
    monkeypatch.setattr(cli.settings, flag, False)
    with patch.object(cli, kernel, AsyncMock()) as call:
        assert cli.main() == 2
    call.assert_not_awaited()


@pytest.mark.parametrize(
    "cli,kernel",
    [(us, "live_reconcile_orders_impl"), (toss, "toss_reconcile_orders_impl")],
)
def test_cli_default_is_full_scope_dry_run(cli, kernel, monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli"])
    with patch.object(cli, kernel, AsyncMock(return_value={"success": True})) as call:
        assert cli.main() == 0
    assert call.await_args.kwargs["dry_run"] is True
    assert call.await_args.kwargs["scope"] == "full"
