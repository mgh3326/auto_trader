import pytest
from unittest.mock import AsyncMock

from app.services.brokers.kis.client import KISClient


@pytest.mark.asyncio
async def test_inquire_korea_orders_mock_fails_closed(monkeypatch):
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
        await client.inquire_korea_orders(is_mock=True)


@pytest.mark.asyncio
async def test_inquire_korea_orders_live_unchanged(monkeypatch):
    """Live path must still send TTTC8036R (no regression)."""
    client = KISClient(is_mock=False)
    monkeypatch.setattr(client, "_ensure_token", AsyncMock(return_value=None))

    captured: dict = {}

    async def fake_request(method, url, *, headers, params, **kwargs):
        captured["tr_id"] = headers.get("tr_id")
        return {"rt_cd": "0", "output": []}

    monkeypatch.setattr(client, "_request_with_rate_limit", fake_request)

    # Force account number presence without leaking real values.
    monkeypatch.setattr(
        type(client._settings),
        "kis_account_no",
        property(lambda self: "00000000-01"),
    )

    await client.inquire_korea_orders(is_mock=False)
    assert captured["tr_id"] == "TTTC8036R"
