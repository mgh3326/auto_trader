# tests/scripts/test_kis_live_auto_reconcile_cli.py
from unittest.mock import AsyncMock, patch

import pytest

import scripts.kis_live_auto_reconcile as cli


@pytest.mark.asyncio
async def test_cli_default_dry_run_true():
    with patch.object(
        cli,
        "kis_live_reconcile_orders_impl",
        AsyncMock(return_value={"success": True, "counts": {}}),
    ) as k:
        rc = await cli._run(dry_run=True)
    k.assert_awaited_once_with(dry_run=True)
    assert rc == 0


@pytest.mark.asyncio
async def test_cli_apply_passes_dry_run_false():
    with patch.object(
        cli,
        "kis_live_reconcile_orders_impl",
        AsyncMock(return_value={"success": True, "counts": {}}),
    ) as k:
        rc = await cli._run(dry_run=False)
    k.assert_awaited_once_with(dry_run=False)
    assert rc == 0


@pytest.mark.parametrize(
    "args,enabled,safety,expected",
    [
        (["--apply-terminal-only"], False, False, 2),
        (["--apply"], True, False, 2),
        (["--apply", "--apply-terminal-only"], True, True, 2),
    ],
)
def test_cli_gates_and_exclusive_args(monkeypatch, args, enabled, safety, expected):
    monkeypatch.setattr("sys.argv", ["cli", *args])
    monkeypatch.setattr(cli.settings, "KIS_LIVE_AUTO_RECONCILE_ENABLED", enabled)
    monkeypatch.setattr(
        cli.settings, "KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED", safety
    )
    with patch.object(cli, "kis_live_reconcile_orders_impl", AsyncMock()) as kernel:
        if len(args) == 2:
            with pytest.raises(SystemExit) as exc:
                cli.main()
            assert exc.value.code == expected
        else:
            assert cli.main() == expected
        kernel.assert_not_awaited()


def test_terminal_only_gate_allows_without_safety_review(monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli", "--apply-terminal-only"])
    monkeypatch.setattr(cli.settings, "KIS_LIVE_AUTO_RECONCILE_ENABLED", True)
    monkeypatch.setattr(
        cli.settings, "KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED", False
    )
    with patch.object(
        cli, "kis_live_reconcile_orders_impl", AsyncMock(return_value={"success": True})
    ) as kernel:
        assert cli.main() == 0
    kernel.assert_awaited_once_with(dry_run=False, scope="terminal_only")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dry_run,terminal_only,result,expected",
    [
        (
            True,
            False,
            {"success": True, "reconciled": [{"action": "would_mark_expired"}]},
            3,
        ),
        (
            False,
            True,
            {"success": True, "reconciled": [{"action": "deferred_fill_booking"}]},
            3,
        ),
        (
            False,
            True,
            {"success": True, "reconciled": [{"action": "marked_expired"}]},
            0,
        ),
        (
            False,
            False,
            {"success": True, "reconciled": [{"action": "deferred_fill_booking"}]},
            0,
        ),
        (True, False, {"success": False}, 4),
        (
            False,
            True,
            {
                "success": True,
                "reconciled": [{"action": "noop_pending", "reason_code": "not_found"}],
            },
            3,
        ),
        (
            False,
            True,
            {
                "success": True,
                "reconciled": [
                    {"action": "noop_no_evidence", "requires_manual_review": True}
                ],
            },
            3,
        ),
        (
            False,
            False,
            {"success": True, "reconciled": [{"action": "booked_filled"}]},
            0,
        ),
        (
            False,
            False,
            {"success": True, "reconciled": [{"action": "noop_already_booked"}]},
            0,
        ),
        (
            False,
            False,
            {"success": True, "candidate_scan": {"truncated": True}},
            4,
        ),
        (
            False,
            False,
            {"success": True, "proposal_rung_sweep": {"error": "failed"}},
            4,
        ),
        (
            False,
            False,
            {"success": True, "proposal_projection_repair": {"failed": 1}},
            4,
        ),
        (
            False,
            False,
            {"success": True, "reconciled": [{"action": "anomaly", "error": "x"}]},
            4,
        ),
        (
            False,
            True,
            {
                "success": True,
                "candidate_scan": {"truncated": True},
                "reconciled": [{"action": "deferred_fill_booking"}],
            },
            4,
        ),
    ],
)
async def test_exit_codes(dry_run, terminal_only, result, expected):
    with patch.object(
        cli, "kis_live_reconcile_orders_impl", AsyncMock(return_value=result)
    ):
        assert await cli._run(dry_run=dry_run, terminal_only=terminal_only) == expected


@pytest.mark.asyncio
async def test_failed_run_prints_one_naming_line_without_raw_error(capsys):
    raw = "raw-broker-text-should-not-appear"
    with patch.object(
        cli,
        "kis_live_reconcile_orders_impl",
        AsyncMock(return_value={"success": False, "error": raw}),
    ):
        assert await cli._run(dry_run=False, terminal_only=False) == 4
    last_line = capsys.readouterr().out.strip().splitlines()[-1]
    assert (
        last_line
        == "실패: 대사 실행이 완료되지 않았다 (조건: success_false, kernel_error)"
    )
    assert raw not in last_line
