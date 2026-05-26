"""Daemon gating tests (ROB-321 PR4b)."""

from __future__ import annotations

import pytest

from app.core.config import settings
from scripts import kis_mock_scalping_daemon as daemon


@pytest.mark.unit
@pytest.mark.asyncio
async def test_daemon_disabled_is_noop(mocker) -> None:
    mocker.patch.object(settings, "kis_mock_scalping_ws_enabled", False)
    # If the gate fails this would build the WS client — assert it does NOT.
    ws = mocker.patch.object(daemon, "KISQuoteWebSocket")
    rc = await daemon.run_daemon(daemon._parse_args(["--symbols", "005930"]))
    assert rc == 0
    ws.assert_not_called()


@pytest.mark.unit
def test_daemon_defaults_to_mock_account_mode() -> None:
    args = daemon._parse_args([])
    assert args.account_mode == "kis_mock"
