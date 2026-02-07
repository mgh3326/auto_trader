import pytest

from app.services import upbit


@pytest.mark.asyncio
async def test_fetch_multiple_tickers_keeps_comma_unescaped(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_request_json(url: str, params=None):
        captured["url"] = url
        captured["params"] = params
        return []

    monkeypatch.setattr(upbit, "_request_json", fake_request_json)

    await upbit.fetch_multiple_tickers(["KRW-BTC", "KRW-ETH", "KRW-XRP"])

    assert captured["params"] is None
    assert "markets=KRW-BTC,KRW-ETH,KRW-XRP" in str(captured["url"])
    assert "%2C" not in str(captured["url"])
