"""ROB-334 — read-only fill-evidence smoke CLI tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from scripts import kis_mock_fill_evidence_smoke as smoke


@pytest.mark.unit
def test_parse_args_defaults() -> None:
    args = smoke._parse_args([])
    assert args.order_no is None
    assert args.symbol is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disabled_is_noop(mocker) -> None:
    mocker.patch.object(smoke.settings, "kis_mock_scalping_ws_enabled", False)
    rc = await smoke.run_smoke(smoke._parse_args([]))
    assert rc == 4  # disabled / not configured -> env/config no-op


@pytest.mark.unit
@pytest.mark.asyncio
async def test_classifies_when_order_no_given(mocker) -> None:
    mocker.patch.object(smoke.settings, "kis_mock_scalping_ws_enabled", True)
    mocker.patch.object(smoke.settings, "kis_mock_app_key", "fake")
    mocker.patch.object(smoke.settings, "kis_mock_app_secret", "fake")
    mocker.patch.object(smoke.settings, "kis_mock_account_no", "fake")
    fake_client = mocker.MagicMock()
    fake_client.domestic_orders.inquire_daily_order_domestic = AsyncMock(
        return_value=[{"odno": "123456", "ord_qty": "1", "tot_ccld_qty": "1",
                       "avg_prvs": "70000"}]
    )
    mocker.patch.object(smoke, "_create_kis_client", return_value=fake_client)
    rc = await smoke.run_smoke(smoke._parse_args(["--order-no", "123456"]))
    assert rc == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inquiry_error_returns_2(mocker) -> None:
    mocker.patch.object(smoke.settings, "kis_mock_scalping_ws_enabled", True)
    mocker.patch.object(smoke.settings, "kis_mock_app_key", "fake")
    mocker.patch.object(smoke.settings, "kis_mock_app_secret", "fake")
    mocker.patch.object(smoke.settings, "kis_mock_account_no", "fake")
    fake_client = mocker.MagicMock()
    fake_client.domestic_orders.inquire_daily_order_domestic = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    mocker.patch.object(smoke, "_create_kis_client", return_value=fake_client)
    rc = await smoke.run_smoke(smoke._parse_args(["--order-no", "123456"]))
    assert rc == 2

