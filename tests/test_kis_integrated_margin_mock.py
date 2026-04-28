from unittest.mock import AsyncMock

import pytest

from app.services.brokers.kis.client import KISClient


@pytest.mark.asyncio
async def test_integrated_margin_mock_fails_closed(monkeypatch):
    client = KISClient(is_mock=True)

    # Patch token + transport so we never hit the network even if the
    # fail-closed branch regresses.
    monkeypatch.setattr(client, "_ensure_token", AsyncMock(return_value=None))
    monkeypatch.setattr(
        client,
        "_request_with_rate_limit",
        AsyncMock(side_effect=AssertionError("must not call KIS in mock")),
    )

    with pytest.raises(RuntimeError, match="mock"):
        await client.inquire_integrated_margin(is_mock=True)
