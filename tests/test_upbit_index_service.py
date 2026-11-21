"""ROB-381 PR2 — Upbit index/altseason service + MCP handlers.

Mocks httpx by routing on URL. Indices come from datalab-static fixtures (the
PR1 recon samples); 24h breadth is derived from a synthetic official-ticker
response. No network. No broker/order/account API.
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
from datetime import UTC, datetime

import httpx
import pytest
import yaml

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


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ticker_row(
    market: str,
    rate: float | None,
    *,
    freshness: str | int = "fresh",
    **extra: object,
) -> dict[str, object]:
    """Real-shape Upbit ``/v1/ticker`` row.

    ``freshness``: ``"fresh"`` (last trade 60s ago), ``"stale"`` (2h ago —
    beyond the 1800s freshness window), ``"none"`` (no ``trade_timestamp``
    key), or an int epoch-ms override.
    """
    row: dict[str, object] = {
        "market": market,
        "signed_change_rate": rate,
        **extra,
    }
    if freshness == "fresh":
        row["trade_timestamp"] = _now_ms() - 60_000
    elif freshness == "stale":
        row["trade_timestamp"] = _now_ms() - 7_200_000
    elif freshness != "none":
        row["trade_timestamp"] = freshness
    return row


def _breadth_universe(
    n_alts: int,
    *,
    btc_freshness: str | int = "fresh",
    btc_rate: float = 0.01,
    alt_rates: list[float] | None = None,
    stale_alts: frozenset[int] = frozenset(),
    undated_alts: frozenset[int] = frozenset(),
) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    """``(market/all rows, ticker rows)`` for KRW-BTC + ``n_alts`` KRW alts.

    Defaults give every alt a fresh timestamp and a rate beating BTC; callers
    narrow with ``alt_rates``/``stale_alts``/``undated_alts``.
    """
    rates = alt_rates if alt_rates is not None else [0.02] * n_alts
    assert len(rates) == n_alts
    markets = [{"market": "KRW-BTC"}]
    tickers = [_ticker_row("KRW-BTC", btc_rate, freshness=btc_freshness)]
    for i in range(n_alts):
        market = f"KRW-ALT{i:03d}"
        markets.append({"market": market})
        freshness = (
            "stale" if i in stale_alts else "none" if i in undated_alts else "fresh"
        )
        tickers.append(_ticker_row(market, rates[i], freshness=freshness))
    return markets, tickers


def _breadth_get_json(markets, tickers):
    async def get_json(url, params=None):
        if url == upbit_index.MARKET_ALL_URL:
            return markets
        return tickers

    return get_json


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
    markets, tickers = _breadth_universe(34, alt_rates=[0.02] * 17 + [0.005] * 17)
    mapping = {
        **_datalab_mapping(),
        "/market/all": markets,
        "/ticker": tickers,
    }
    monkeypatch.setattr(httpx.AsyncClient, "get", _route(mapping))

    payload = await upbit_index.fetch_upbit_altseason()

    assert payload is not None
    assert payload["ubai_ubmi_ratio"] == pytest.approx(7100.0 / 15600.0, rel=1e-6)
    breadth = payload["breadth"]
    assert breadth["window"] == "24h"
    # 34 fresh alts: 17 beat BTC (0.02 > 0.01), 17 lose (0.005 < 0.01).
    assert breadth["alts_total"] == 34
    assert breadth["alts_beating_btc"] == 17
    assert breadth["alts_beating_btc_pct"] == pytest.approx(0.5)
    assert breadth["alts_excluded_stale"] == 0
    assert breadth["latest_trade_at"] is not None


@pytest.mark.asyncio
async def test_breadth_date_uses_oldest_contributing_ticker(monkeypatch):
    """hk 6019 — a stale alt is excluded, so it cannot drag the observation
    hours into the past; ``latest_trade_at`` dates the oldest *contributor*."""
    markets, tickers = _breadth_universe(34, stale_alts={0})
    btc_ts_ms = _now_ms() - 600_000  # oldest contributing observation (10m)
    tickers[0]["trade_timestamp"] = btc_ts_ms

    monkeypatch.setattr(upbit_index, "_get_json", _breadth_get_json(markets, tickers))
    breadth = await upbit_index._fetch_krw_breadth_24h()
    assert breadth is not None
    assert breadth["alts_total"] == 33
    assert breadth["alts_excluded_stale"] == 1
    assert (
        breadth["latest_trade_at"]
        == datetime.fromtimestamp(btc_ts_ms / 1000, tz=UTC).isoformat()
    )


@pytest.mark.asyncio
async def test_breadth_excludes_stale_alts_from_numerator_and_denominator(
    monkeypatch,
):
    markets, tickers = _breadth_universe(34, stale_alts={0, 1, 2})
    # Exact rates over the 31 contributing alts (indices 3..33):
    # 3..16 beat BTC (14 alts at 0.02), 17..33 lose (17 alts at 0.005).
    for i in range(3, 34):
        tickers[i + 1]["signed_change_rate"] = 0.02 if i < 17 else 0.005

    monkeypatch.setattr(upbit_index, "_get_json", _breadth_get_json(markets, tickers))
    breadth = await upbit_index._fetch_krw_breadth_24h()

    assert breadth is not None
    assert breadth["alts_total"] == 31
    assert breadth["alts_excluded_stale"] == 3
    assert breadth["alts_beating_btc"] == 14
    assert breadth["alts_beating_btc_pct"] == pytest.approx(14 / 31, abs=1e-4)
    assert breadth["latest_trade_at"] is not None


@pytest.mark.asyncio
async def test_breadth_excludes_undated_alt_instead_of_undating_reading(monkeypatch):
    """hk 6019 — an alt with no trade_timestamp leaves the contributing set
    (counted as excluded); it does not poison ``latest_trade_at``."""
    markets, tickers = _breadth_universe(34, undated_alts={0})

    monkeypatch.setattr(upbit_index, "_get_json", _breadth_get_json(markets, tickers))
    breadth = await upbit_index._fetch_krw_breadth_24h()
    assert breadth is not None
    assert breadth["alts_total"] == 33
    assert breadth["alts_excluded_stale"] == 1
    assert breadth["latest_trade_at"] is not None


@pytest.mark.asyncio
async def test_breadth_stale_btc_makes_reading_unavailable(monkeypatch):
    markets, tickers = _breadth_universe(34, btc_freshness="stale")

    monkeypatch.setattr(upbit_index, "_get_json", _breadth_get_json(markets, tickers))
    assert await upbit_index._fetch_krw_breadth_24h() is None


@pytest.mark.asyncio
async def test_breadth_undated_btc_makes_reading_unavailable(monkeypatch):
    markets, tickers = _breadth_universe(34, btc_freshness="none")

    monkeypatch.setattr(upbit_index, "_get_json", _breadth_get_json(markets, tickers))
    assert await upbit_index._fetch_krw_breadth_24h() is None


@pytest.mark.asyncio
async def test_breadth_below_contributing_count_floor_is_unavailable(monkeypatch):
    """34 eligible alts, 5 stale → 29 contributing < the 30-market floor."""
    markets, tickers = _breadth_universe(34, stale_alts=frozenset(range(5)))

    monkeypatch.setattr(upbit_index, "_get_json", _breadth_get_json(markets, tickers))
    assert await upbit_index._fetch_krw_breadth_24h() is None


@pytest.mark.asyncio
async def test_breadth_below_contributing_share_floor_is_unavailable(monkeypatch):
    """70 requested alts, 36 stale → 34 contributing: above the count floor
    but below 50% of the requested KRW alt universe."""
    markets, tickers = _breadth_universe(70, stale_alts=frozenset(range(36)))

    monkeypatch.setattr(upbit_index, "_get_json", _breadth_get_json(markets, tickers))
    assert await upbit_index._fetch_krw_breadth_24h() is None


@pytest.mark.asyncio
async def test_breadth_partial_ticker_response_counts_against_share_floor(
    monkeypatch,
):
    """hk 6019 review — a market in market/all but absent from the ticker
    response cannot be proven fresh; it counts against the share floor
    instead of passing invisibly on the surviving sliver."""
    markets, tickers = _breadth_universe(70)
    # Ticker response carries KRW-BTC + only 30 of the 70 requested alts.
    del tickers[31:]

    monkeypatch.setattr(upbit_index, "_get_json", _breadth_get_json(markets, tickers))
    assert await upbit_index._fetch_krw_breadth_24h() is None


@pytest.mark.asyncio
async def test_breadth_floor_trip_is_logged(monkeypatch, caplog):
    """hk 6019 review — a floored reading is an expected steady state, so the
    producer logs the counts an operator needs to tell floor from outage."""
    markets, tickers = _breadth_universe(34, stale_alts=frozenset(range(5)))

    monkeypatch.setattr(upbit_index, "_get_json", _breadth_get_json(markets, tickers))
    with caplog.at_level(logging.INFO, logger="app.services.external.upbit_index"):
        assert await upbit_index._fetch_krw_breadth_24h() is None
    assert "alt breadth unavailable" in caplog.text
    assert "29 of 34" in caplog.text
    assert "5 excluded stale" in caplog.text


def test_ticker_stale_window_matches_recovery_gate_policy():
    """hk 6019 — the producer freshness bound IS the gate's stale window."""
    config = (
        pathlib.Path(__file__).resolve().parents[1] / "config" / "trading_policy.yaml"
    )
    policy = yaml.safe_load(config.read_text(encoding="utf-8"))
    conditions = policy["market_rules"]["crypto"]["recovery_gate"]["conditions"]
    breadth = next(c for c in conditions if c["id"] == "alt_breadth_24h")
    assert breadth["stale_after_seconds"] == upbit_index._TICKER_STALE_AFTER_SECONDS


@pytest.mark.asyncio
async def test_altseason_constituents_list_btc_outperformers(monkeypatch):
    upbit_index._clear_caches()
    n_fillers = 31
    markets = [{"market": "KRW-BTC"}, {"market": "KRW-ETH"}, {"market": "KRW-XRP"}]
    markets += [{"market": f"KRW-ALT{i:03d}"} for i in range(n_fillers)]
    tickers = [
        _ticker_row(
            "KRW-BTC",
            0.01,
            trade_price=100_000_000,
            acc_trade_volume_24h=100.0,
            acc_trade_price_24h=10_000_000_000.0,
        ),
        _ticker_row(
            "KRW-ETH",
            0.05,
            trade_price=5_000_000,
            acc_trade_volume_24h=200.0,
            acc_trade_price_24h=20_000_000_000.0,
        ),
        _ticker_row(
            "KRW-XRP",
            -0.02,
            trade_price=900,
            acc_trade_volume_24h=300.0,
            acc_trade_price_24h=30_000_000_000.0,
        ),
    ] + [_ticker_row(f"KRW-ALT{i:03d}", 0.005) for i in range(n_fillers)]
    mapping = {
        **_datalab_mapping(),
        "/market/all": markets,
        "/ticker": tickers,
    }
    monkeypatch.setattr(httpx.AsyncClient, "get", _route(mapping))

    payload = await upbit_index.fetch_upbit_altseason(
        include_constituents=True,
        constituents_limit=10,
    )

    breadth = payload["breadth"]
    assert breadth["alts_total"] == 33
    assert breadth["alts_beating_btc"] == 1
    assert breadth["constituents_count"] == 1
    assert breadth["constituents"][0]["symbol"] == "KRW-ETH"


@pytest.mark.asyncio
async def test_altseason_constituents_includes_relative_strength_zero(monkeypatch):
    """ROB-589: coins matching BTC rate (RS=0) must be included, same as top_stocks."""
    upbit_index._clear_caches()
    n_fillers = 31
    markets = [{"market": "KRW-BTC"}, {"market": "KRW-ETH"}, {"market": "KRW-XRP"}]
    markets += [{"market": f"KRW-ALT{i:03d}"} for i in range(n_fillers)]
    tickers = [
        _ticker_row("KRW-BTC", 0.01),
        _ticker_row("KRW-ETH", 0.01),  # RS = 0
        _ticker_row("KRW-XRP", 0.00),  # RS < 0
    ] + [_ticker_row(f"KRW-ALT{i:03d}", 0.005) for i in range(n_fillers)]
    mapping = {
        **_datalab_mapping(),
        "/market/all": markets,
        "/ticker": tickers,
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
    markets, tickers = _breadth_universe(34)
    monkeypatch.setattr(
        httpx.AsyncClient,
        "get",
        _route(
            {**_datalab_mapping(), "/market/all": markets, "/ticker": tickers},
            fail_substrings=("/market/all", "/ticker"),
        ),
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
