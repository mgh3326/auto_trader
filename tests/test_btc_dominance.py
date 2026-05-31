"""ROB-377 PR1: fetch_btc_dominance additive fields (total mcap + ETH dominance)."""

from __future__ import annotations

import httpx
import pytest

from app.services.external import btc_dominance


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


def _patch_global(monkeypatch, payload):
    async def fake_get(self_cli, url, *args, **kwargs):
        return _FakeResponse(payload)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


@pytest.mark.asyncio
async def test_fetch_btc_dominance_includes_total_mcap_and_eth(monkeypatch):
    btc_dominance._clear_btc_dominance_cache()
    _patch_global(
        monkeypatch,
        {
            "data": {
                "market_cap_percentage": {"btc": 52.351, "eth": 17.21},
                "market_cap_change_percentage_24h_usd": 1.853,
                "total_market_cap": {"usd": 2_310_000_000_000.0},
            }
        },
    )

    result = await btc_dominance.fetch_btc_dominance()

    assert result is not None
    # existing keys unchanged
    assert result["btc_dominance"] == pytest.approx(52.35)
    assert result["total_market_cap_change_24h"] == pytest.approx(1.85)
    # new additive keys
    assert result["total_market_cap_usd"] == pytest.approx(2_310_000_000_000.0)
    assert result["eth_dominance"] == pytest.approx(17.21)


@pytest.mark.asyncio
async def test_fetch_btc_dominance_new_fields_none_when_absent(monkeypatch):
    btc_dominance._clear_btc_dominance_cache()
    _patch_global(
        monkeypatch,
        {
            "data": {
                "market_cap_percentage": {"btc": 50.0},
                "market_cap_change_percentage_24h_usd": -0.5,
            }
        },
    )

    result = await btc_dominance.fetch_btc_dominance()

    assert result is not None
    assert result["btc_dominance"] == pytest.approx(50.0)
    assert result["total_market_cap_usd"] is None
    assert result["eth_dominance"] is None
