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
        (True, False, {"success": False}, 1),
    ],
)
async def test_exit_codes(dry_run, terminal_only, result, expected):
    with patch.object(
        cli, "kis_live_reconcile_orders_impl", AsyncMock(return_value=result)
    ):
        assert await cli._run(dry_run=dry_run, terminal_only=terminal_only) == expected
