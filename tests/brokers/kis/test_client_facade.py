"""KISClient facade contract tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.brokers.kis.client import KISClient


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_domestic_balance_snapshot_is_public_facade(mocker) -> None:
    """ROB-341 smoke adapters need a public balance-snapshot facade."""
    client = KISClient(is_mock=True)
    account_call = mocker.patch.object(
        client._account,
        "fetch_domestic_balance_snapshot",
        new=AsyncMock(return_value={"holdings": [], "cash": {}, "page_count": 1}),
    )

    result = await client.fetch_domestic_balance_snapshot(is_mock=True)

    assert result == {"holdings": [], "cash": {}, "page_count": 1}
    account_call.assert_awaited_once_with(
        is_mock=True,
        timeout=5.0,
        retry_request_errors=True,
        max_pages=10,
    )
