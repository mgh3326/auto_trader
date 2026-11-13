from unittest.mock import AsyncMock, patch

import pytest

import scripts.kis_us_auto_reconcile as us
import scripts.toss_live_auto_reconcile as toss


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cli,kernel",
    [(us, "live_reconcile_orders_impl"), (toss, "toss_reconcile_orders_impl")],
)
async def test_dry_run_with_closable_rows_warns_and_exits_nonzero(cli, kernel, capsys):
    with patch.object(
        cli,
        kernel,
        AsyncMock(
            return_value={
                "success": True,
                "reconciled": [{"action": "would_mark_expired"}],
            }
        ),
    ):
        assert await cli._run(apply=False, terminal_only=True) == 3
    assert "WARNING" in capsys.readouterr().out


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cli,kernel",
    [(us, "live_reconcile_orders_impl"), (toss, "toss_reconcile_orders_impl")],
)
async def test_full_apply_preserves_existing_kernel_path(cli, kernel):
    with patch.object(cli, kernel, AsyncMock(return_value={"success": True})) as call:
        assert await cli._run(apply=True, terminal_only=False) == 0
    kwargs = {"dry_run": False, "terminal_only": False}
    if cli is us:
        kwargs.update(market="us", broker="kis")
    call.assert_awaited_once_with(**kwargs)
