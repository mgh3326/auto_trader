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
