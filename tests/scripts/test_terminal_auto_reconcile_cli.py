from unittest.mock import AsyncMock, patch

import pytest

import scripts.kis_us_auto_reconcile as us
import scripts.toss_live_auto_reconcile as toss

CLIS = [
    pytest.param(us, "live_reconcile_orders_impl", id="kis_us"),
    pytest.param(toss, "toss_reconcile_orders_impl", id="toss"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("cli,kernel", CLIS)
@pytest.mark.parametrize(
    "apply,row,expected",
    [
        (False, {"action": "noop_pending"}, 0),
        (False, {"action": "noop_terminal"}, 0),
        (True, {"action": "marked_cancelled"}, 0),
        (True, {"action": "booked"}, 0),
        (False, {"action": "booked"}, 3),
        (False, {"action": "marked_expired"}, 3),
        (False, {"action": "would_mark_expired"}, 3),
        (True, {"action": "deferred_fill_booking"}, 3),
        (False, {"action": "noop_pending", "reason_code": "not_found"}, 3),
        (False, {"action": "noop_pending", "requires_manual_review": True}, 3),
        (True, {"action": "noop_no_evidence"}, 3),
        (True, {"action": "unexpected"}, 3),
        (True, {"action": "anomaly", "error": "failed"}, 4),
        (True, {"action": "noop_pending", "error": ""}, 4),
    ],
)
async def test_cli_closed_exit_vocabulary(cli, kernel, apply, row, expected, capsys):
    with patch.object(
        cli, kernel, AsyncMock(return_value={"success": True, "reconciled": [row]})
    ):
        assert await cli._run(apply=apply, terminal_only=True) == expected
    last_line = capsys.readouterr().out.strip().splitlines()[-1]
    assert ("WARNING" in last_line) is (expected == 3)
    assert last_line.startswith("실패") is (expected == 4)


@pytest.mark.asyncio
@pytest.mark.parametrize("cli,kernel", CLIS)
async def test_full_apply_booked_row_is_clean(cli, kernel, capsys):
    result = {"success": True, "reconciled": [{"action": "booked"}]}
    with patch.object(cli, kernel, AsyncMock(return_value=result)) as call:
        assert await cli._run(apply=True, terminal_only=False) == 0
    assert call.await_args.kwargs["dry_run"] is False
    assert "WARNING" not in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("cli,kernel", CLIS)
@pytest.mark.parametrize(
    "result,conditions",
    [
        ({"success": False}, "success_false"),
        ({"success": False, "error": "boom"}, "success_false, kernel_error"),
        (
            {"success": True, "candidate_scan": {"truncated": True}},
            "candidate_scan_truncated",
        ),
        (
            {"success": True, "proposal_rung_sweep": {"error": "failed"}},
            "proposal_rung_sweep_error",
        ),
        (
            {"success": True, "proposal_projection_repair": {"failed": 1}},
            "projection_repair_failed",
        ),
        (
            {"success": True, "batch_build_error": {"type": "failed"}},
            "batch_build_error",
        ),
        (
            {"success": True, "reconciled": [{"action": "anomaly", "error": "x"}]},
            "row_error",
        ),
    ],
)
async def test_cli_incomplete_or_failed_run_exits_4_naming_condition(
    cli, kernel, result, conditions, capsys
):
    with patch.object(cli, kernel, AsyncMock(return_value=result)):
        assert await cli._run(apply=False, terminal_only=False) == 4
    last_line = capsys.readouterr().out.strip().splitlines()[-1]
    assert last_line == f"실패: 대사 실행이 완료되지 않았다 (조건: {conditions})"


@pytest.mark.asyncio
@pytest.mark.parametrize("cli,kernel", CLIS)
async def test_failure_takes_precedence_over_attention(cli, kernel, capsys):
    result = {
        "success": True,
        "candidate_scan": {"truncated": True},
        "reconciled": [{"action": "deferred_fill_booking"}],
    }
    with patch.object(cli, kernel, AsyncMock(return_value=result)):
        assert await cli._run(apply=True, terminal_only=True) == 4
    assert "WARNING" not in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("cli,kernel", CLIS)
async def test_failure_line_does_not_echo_raw_error_text(cli, kernel, capsys):
    raw = "raw-broker-text-should-not-appear"
    result = {"success": False, "error": raw}
    with patch.object(cli, kernel, AsyncMock(return_value=result)):
        assert await cli._run(apply=False, terminal_only=False) == 4
    last_line = capsys.readouterr().out.strip().splitlines()[-1]
    assert raw not in last_line


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
    "cli,flag,kernel",
    [
        (us, "LIVE_AUTO_RECONCILE_ENABLED", "live_reconcile_orders_impl"),
        (toss, "TOSS_LIVE_AUTO_RECONCILE_ENABLED", "toss_reconcile_orders_impl"),
    ],
)
def test_full_apply_gate_refused_exits_2(cli, flag, kernel, monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli", "--apply"])
    monkeypatch.setattr(cli.settings, flag, False)
    with patch.object(cli, kernel, AsyncMock()) as call:
        assert cli.main() == 2
    call.assert_not_awaited()


@pytest.mark.parametrize("cli,kernel", CLIS)
def test_cli_default_is_full_scope_dry_run(cli, kernel, monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli"])
    with patch.object(cli, kernel, AsyncMock(return_value={"success": True})) as call:
        assert cli.main() == 0
    assert call.await_args.kwargs["dry_run"] is True
    assert call.await_args.kwargs["scope"] == "full"
