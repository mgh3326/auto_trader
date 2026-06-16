"""ROB-381 PR2 — Upbit index/altseason service + MCP handlers.

Mocks httpx by routing on URL. Indices come from datalab-static fixtures (the
PR1 recon samples); 24h breadth is derived from a synthetic official-ticker
response. No network. No broker/order/account API.
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from app.mcp_server.tooling.fundamentals._upbit_index import (
    handle_get_upbit_altseason,
    handle_get_upbit_index,
)
from app.services.external import upbit_index

_FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "upbit_index"


def _fx(name: str):
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


def _route(mapping: dict[str, object], *, fail_substrings: tuple[str, ...] = ()):
    """Build a fake httpx.AsyncClient.get that routes by URL substring."""

    async def fake_get(self_cli, url, *args, **kwargs):
        for sub in fail_substrings:
            if sub in url:
                raise httpx.ConnectError(f"boom: {sub}")
        for sub, payload in mapping.items():
            if sub in url:
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected URL: {url}")

    return fake_get


# Index value overlays so UBAI/UBMI ratio is deterministic.
_RECENT = [
    {"code": "IDX.UPBIT.UBAI", "tradePrice": 7100.0, "signedChangeRate": -0.002},
    {"code": "IDX.UPBIT.UBMI", "tradePrice": 15600.0, "signedChangeRate": -0.001},
]
_SUMMARY = [
    {
        "code": "IDX.UPBIT.UBAI",
        "stats": {"weeklyYield": 0.07, "beta": 1.2, "sharpeRatio": -0.03},
    }
]
_MARKET_ALL = [
    {"market": "KRW-BTC"},
    {"market": "KRW-ETH"},
    {"market": "KRW-XRP"},
    {"market": "BTC-ETH"},  # non-KRW, must be ignored
]
_TICKER = [
    {"market": "KRW-BTC", "signed_change_rate": 0.01},
    {"market": "KRW-ETH", "signed_change_rate": 0.05},  # beats BTC
    {"market": "KRW-XRP", "signed_change_rate": -0.02},  # loses to BTC
]


def _datalab_mapping():
    return {
        "/index/master": _fx("index_master.json"),
        "/index/recent": _RECENT,
        "/index/summary": _SUMMARY,
    }


@pytest.mark.asyncio
async def test_fetch_indices_merges_master_recent_summary(monkeypatch):
    upbit_index._clear_caches()
    monkeypatch.setattr(httpx.AsyncClient, "get", _route(_datalab_mapping()))

    payload = await upbit_index.fetch_upbit_indices()

    assert payload is not None
    assert payload["source"] == "upbit_datalab"
    ubai = payload["indices"]["IDX.UPBIT.UBAI"]
    assert ubai["symbol"] == "UBAI"
    assert ubai["category_type"] == "market"
    assert ubai["value"] == 7100.0  # recent overlay
    assert ubai["weeklyYield"] == 0.07  # summary overlay
    assert ubai["beta"] == 1.2


@pytest.mark.asyncio
async def test_fetch_indices_failopen_when_master_unavailable(monkeypatch):
    upbit_index._clear_caches()
    monkeypatch.setattr(
        httpx.AsyncClient,
        "get",
        _route(_datalab_mapping(), fail_substrings=("/index/master",)),
    )

    assert await upbit_index.fetch_upbit_indices() is None


@pytest.mark.asyncio
async def test_fetch_indices_recent_overlay_is_best_effort(monkeypatch):
    """If recent/summary fail, the catalog still returns (value=None)."""
    upbit_index._clear_caches()
    monkeypatch.setattr(
        httpx.AsyncClient,
        "get",
        _route(
            _datalab_mapping(),
            fail_substrings=("/index/recent", "/index/summary"),
        ),
    )

    payload = await upbit_index.fetch_upbit_indices()
    assert payload is not None
    assert payload["indices"]["IDX.UPBIT.UBAI"]["value"] is None


@pytest.mark.asyncio
async def test_altseason_ratio_and_breadth(monkeypatch):
    upbit_index._clear_caches()
    mapping = {
        **_datalab_mapping(),
        "/market/all": _MARKET_ALL,
        "/ticker": _TICKER,
    }
    monkeypatch.setattr(httpx.AsyncClient, "get", _route(mapping))

    payload = await upbit_index.fetch_upbit_altseason()

    assert payload is not None
    assert payload["ubai_ubmi_ratio"] == pytest.approx(7100.0 / 15600.0, rel=1e-6)
    breadth = payload["breadth"]
    assert breadth["window"] == "24h"
    assert breadth["alts_total"] == 2  # ETH + XRP (BTC excluded, BTC-ETH ignored)
    assert breadth["alts_beating_btc"] == 1  # ETH only
    assert breadth["alts_beating_btc_pct"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_altseason_constituents_list_btc_outperformers(monkeypatch):
    upbit_index._clear_caches()
    mapping = {
        **_datalab_mapping(),
        "/market/all": _MARKET_ALL,
        "/ticker": [
            {
                "market": "KRW-BTC",
                "trade_price": 100_000_000,
                "signed_change_rate": 0.01,
                "acc_trade_volume_24h": 100.0,
                "acc_trade_price_24h": 10_000_000_000.0,
            },
            {
                "market": "KRW-ETH",
                "trade_price": 5_000_000,
                "signed_change_rate": 0.05,
                "acc_trade_volume_24h": 200.0,
                "acc_trade_price_24h": 20_000_000_000.0,
            },
            {
                "market": "KRW-XRP",
                "trade_price": 900,
                "signed_change_rate": -0.02,
                "acc_trade_volume_24h": 300.0,
                "acc_trade_price_24h": 30_000_000_000.0,
            },
        ],
    }
    monkeypatch.setattr(httpx.AsyncClient, "get", _route(mapping))

    payload = await upbit_index.fetch_upbit_altseason(
        include_constituents=True,
        constituents_limit=10,
    )

    breadth = payload["breadth"]
    assert breadth["alts_total"] == 2
    assert breadth["alts_beating_btc"] == 1
    assert breadth["constituents_count"] == 1
    assert breadth["constituents"][0]["symbol"] == "KRW-ETH"


@pytest.mark.asyncio
async def test_altseason_constituents_includes_relative_strength_zero(monkeypatch):
    """ROB-589: coins matching BTC rate (RS=0) must be included, same as top_stocks."""
    upbit_index._clear_caches()
    mapping = {
        **_datalab_mapping(),
        "/market/all": _MARKET_ALL,
        "/ticker": [
            {"market": "KRW-BTC", "signed_change_rate": 0.01},
            {"market": "KRW-ETH", "signed_change_rate": 0.01},  # RS = 0
            {"market": "KRW-XRP", "signed_change_rate": 0.00},  # RS < 0
        ],
    }
    monkeypatch.setattr(httpx.AsyncClient, "get", _route(mapping))

    payload = await upbit_index.fetch_upbit_altseason(include_constituents=True)

    breadth = payload["breadth"]
    # alts_beating_btc follows the > btc_rate logic for the percentage (unchanged)
    assert breadth["alts_beating_btc"] == 0
    # but constituents now includes the RS=0 row
    assert breadth["constituents_count"] == 1
    assert breadth["constituents"][0]["symbol"] == "KRW-ETH"
    assert breadth["constituents"][0]["relative_strength_vs_btc_24h"] == 0.0


@pytest.mark.asyncio
async def test_altseason_partial_when_breadth_unavailable(monkeypatch):
    """Ratio survives even if the official ticker plane is down."""
    upbit_index._clear_caches()
    monkeypatch.setattr(
        httpx.AsyncClient,
        "get",
        _route(_datalab_mapping(), fail_substrings=("/market/all", "/ticker")),
    )

    payload = await upbit_index.fetch_upbit_altseason()
    assert payload is not None
    assert payload["ubai_ubmi_ratio"] is not None
    assert payload["breadth"] is None


@pytest.mark.asyncio
async def test_handle_get_upbit_index_category_filter(monkeypatch):
    upbit_index._clear_caches()
    monkeypatch.setattr(httpx.AsyncClient, "get", _route(_datalab_mapping()))

    result = await handle_get_upbit_index(category="market")
    assert result["category"] == "market"
    assert all(row["category_type"] == "market" for row in result["indices"].values())
    assert "IDX.UPBIT.UBAI" in result["indices"]


@pytest.mark.asyncio
async def test_handle_get_upbit_index_rejects_bad_category():
    with pytest.raises(ValueError):
        await handle_get_upbit_index(category="bogus")


@pytest.mark.asyncio
async def test_handle_get_upbit_index_failopen_returns_error_payload(monkeypatch):
    upbit_index._clear_caches()
    monkeypatch.setattr(
        httpx.AsyncClient,
        "get",
        _route(_datalab_mapping(), fail_substrings=("/index/master",)),
    )

    result = await handle_get_upbit_index()
    assert "error" in result
    assert result["source"] == "upbit_datalab"


@pytest.mark.asyncio
async def test_handle_get_upbit_altseason_failopen(monkeypatch):
    upbit_index._clear_caches()
    monkeypatch.setattr(
        httpx.AsyncClient,
        "get",
        _route(
            _datalab_mapping(),
            fail_substrings=("/index/", "/market/all", "/ticker"),
        ),
    )

    result = await handle_get_upbit_altseason()
    assert "error" in result


@pytest.mark.asyncio
async def test_handle_get_upbit_altseason_passes_constituent_options(monkeypatch):
    async def fake_fetch(*, include_constituents: bool, constituents_limit: int):
        return {
            "source": "upbit_datalab+upbit_open_api",
            "provenance": "test",
            "as_of": "2026-06-15T00:00:00+09:00",
            "ubai_ubmi_ratio": 0.5,
            "breadth": {
                "window": "24h",
                "constituents": [],
                "constituents_count": 0,
            },
            "options": {
                "include_constituents": include_constituents,
                "constituents_limit": constituents_limit,
            },
        }

    monkeypatch.setattr(upbit_index, "fetch_upbit_altseason", fake_fetch)

    result = await handle_get_upbit_altseason(
        include_constituents=True,
        constituents_limit=500,
    )

    assert result["options"] == {
        "include_constituents": True,
        "constituents_limit": 200,
    }
