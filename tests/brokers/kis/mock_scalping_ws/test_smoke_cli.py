"""Quote WS smoke gating tests (ROB-321 PR2 Task 4)."""

from __future__ import annotations

import pytest

from app.core.config import settings
from scripts import kis_mock_scalping_ws_smoke as smoke


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smoke_disabled_is_noop(mocker) -> None:
    mocker.patch.object(settings, "kis_mock_scalping_ws_enabled", False)
    # If the gate fails, this construction would be attempted — assert it is NOT.
    construct = mocker.patch.object(smoke, "KISQuoteWebSocket")

    args = smoke._parse_args(["--symbols", "005930", "--max-events", "1"])
    rc = await smoke.run_smoke(args)

    assert rc == 0
    construct.assert_not_called()


@pytest.mark.unit
def test_smoke_defaults_to_mock_account_mode() -> None:
    args = smoke._parse_args([])
    assert args.account_mode == "kis_mock"
