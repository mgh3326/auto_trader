"""ROB-1309 regression tests — DB-only contract for the PRE-EXISTING
`screen_stocks_snapshot` tool.

Sentry evidence: `screen_stocks_snapshot` was making ~214 external HTTP calls
per invocation (p50 38.11s / p95 54.73s latency, 8 timeouts over a 120s
budget) via unconditional per-row sector lazy-fill + analyst-consensus +
live-price enrichment. These tests pin the fix's contract:

1. `screen_stocks_snapshot_impl` makes ZERO external-HTTP-capable provider
   calls (sector fetch, analyst-consensus opinion provider, live price
   fetch, consensus-count resolver) for KR/US/crypto — proven by spies that
   raise `AssertionError` if invoked, not by merely asserting on the output.
2. DB snapshot rows still come back correctly, paginated, with the same
   discovery-filter/holdings/warnings metadata as before — only the
   enrichment fields are gone.
3. min_analyst_count/min_analyst_buy_count are rejected fail-closed rather
   than silently ignored or triggering a network call.

DELIBERATE SCOPE NOTE (module-import hygiene): every name this file imports
at module scope (`screener_snapshot_tool`, `analyst_consensus_cache`,
`screener_analysis_enrichment`, `fundamentals._valuation`,
`naver_finance.investor`) already exists on `main` — this file exercises the
PRE-EXISTING `screen_stocks_snapshot_impl` unmodified and lets the spies
themselves raise `AssertionError` from inside real production code when they
fire. That is deliberate: before the ROB-1309 fix, running this file against
unmodified `screener_snapshot_tool.py` produces genuine assertion-based RED
failures (a real spy firing during the tool's default enrichment branch), NOT
a collection-time `ImportError` from referencing a symbol that doesn't exist
yet. The counterpart tests that require the NEW `screen_stocks_enrich` tool
and the new negative-cache module live in `tests/test_screener_enrich_tool.py`
instead, where an import-time failure legitimately reflects "this new tool
doesn't exist yet" rather than masking whether the OLD tool actually leaks
HTTP calls.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.mcp_server.tooling import screener_snapshot_tool as snapshot_tool


class _FakeCM:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _resp(results: list[dict[str, Any]], preset_id: str = "consecutive_gainers"):
    class _R:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {"presetId": preset_id, "results": results, "warnings": []}

    return _R()


def _no_provider_calls_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any of these being invoked means the DB-only tool leaked a network call."""

    async def _boom_sector_kr(code: str):  # noqa: ARG001
        raise AssertionError("screen_stocks_snapshot must not fetch KR sector data")

    async def _boom_sector_us(symbol: str):  # noqa: ARG001
        raise AssertionError("screen_stocks_snapshot must not fetch US sector data")

    async def _boom_opinions(**kwargs: Any):
        raise AssertionError(
            f"screen_stocks_snapshot must not fetch analyst opinions: {kwargs}"
        )

    async def _boom_counts(**kwargs: Any):
        raise AssertionError(
            f"screen_stocks_snapshot must not resolve analyst counts: {kwargs}"
        )

    async def _boom_price(symbol: str):  # noqa: ARG001
        raise AssertionError("screen_stocks_snapshot must not fetch a live price")

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_analysis_enrichment._fetch_kr_sector",
        _boom_sector_kr,
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_analysis_enrichment._fetch_us_sector",
        _boom_sector_us,
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.fundamentals._valuation.handle_get_investment_opinions",
        _boom_opinions,
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.analyst_consensus_cache.resolve_consensus_counts",
        _boom_counts,
    )
    monkeypatch.setattr(
        "app.services.naver_finance.investor._fetch_current_price",
        _boom_price,
    )


def _fake_kis_positions(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty(market_filter, *, is_mock=False):  # noqa: ARG001
        return [], []

    monkeypatch.setattr(
        "app.mcp_server.tooling.portfolio_holdings._collect_kis_positions", _empty
    )


def _fake_build(monkeypatch: pytest.MonkeyPatch, results: list[dict[str, Any]]) -> None:
    async def _build(**_kwargs: Any):
        return _resp(results)

    monkeypatch.setattr(snapshot_tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _build,
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["kr", "us", "crypto"])
async def test_screen_stocks_snapshot_makes_zero_enrichment_http_calls(
    monkeypatch: pytest.MonkeyPatch, market: str
) -> None:
    _no_provider_calls_guard(monkeypatch)
    _fake_kis_positions(monkeypatch)
    _fake_build(
        monkeypatch,
        [{"symbol": "005930", "market": market, "marketCapValue": 1.0}],
    )

    out = await snapshot_tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers", market=market
    )

    assert "error" not in out or out.get("error") is None
    assert out["results"] == [
        {
            "symbol": "005930",
            "market": market,
            "marketCapValue": 1.0,
            "matchedPresets": ["consecutive_gainers"],
        }
    ]
    # ROB-1309: explicit non-silent contract marker — no enrichment happened.
    assert out["enrichment"]["applied"] is False
    assert out["enrichment"]["tool"] == "screen_stocks_enrich"
    # No enrichment fields leaked onto the row.
    assert "analysisContext" not in out["results"][0]
    assert "analystLabel" not in out["results"][0]
    assert "analysisEnrichment" not in out


@pytest.mark.unit
@pytest.mark.asyncio
async def test_screen_stocks_snapshot_db_fields_and_pagination_survive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB snapshot rows, holdings metadata, and pagination are unchanged by
    the enrichment split — only the enrichment fields are gone."""
    _no_provider_calls_guard(monkeypatch)
    _fake_kis_positions(monkeypatch)
    rows = [{"symbol": f"S{i}", "market": "kr"} for i in range(5)]
    _fake_build(monkeypatch, rows)

    out = await snapshot_tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers", market="kr", limit=2, offset=1
    )

    assert [r["symbol"] for r in out["results"]] == ["S1", "S2"]
    assert out["pagination"] == {
        "total_available": 5,
        "returned_count": 2,
        "offset": 1,
        "limit": 2,
        "has_more": True,
        "next_offset": 3,
    }
    assert out["holdings"]["source"] == "kis_live"
    assert out["discoveryFilters"]["min_analyst_count"] is None
    assert out["discoveryFilters"]["min_analyst_buy_count"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_screen_stocks_snapshot_rejects_analyst_filters_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """min_analyst_* must not be silently ignored or trigger a network call —
    it's a fail-closed redirect to screen_stocks_enrich."""
    _no_provider_calls_guard(monkeypatch)
    _fake_kis_positions(monkeypatch)
    _fake_build(monkeypatch, [{"symbol": "005930", "market": "kr"}])

    out = await snapshot_tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers", market="kr", min_analyst_count=1
    )

    assert out["results"] == []
    assert "error" in out
    assert out["redirectTool"] == "screen_stocks_enrich"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_analyst_consensus_cache_hit_skips_provider() -> None:
    """A Redis cache hit for KR analyst consensus must not call the provider."""
    from app.services.invest_view_model import analyst_consensus_cache

    class _FakeRedis:
        def __init__(self, payload: str) -> None:
            self._payload = payload

        async def get(self, key: str):  # noqa: ARG002
            return self._payload

    import json

    cached_payload = json.dumps({"total_count": 5, "buy_count": 3})
    redis_client = _FakeRedis(cached_payload)

    calls = {"n": 0}

    async def _boom_fetcher(**kwargs: Any):
        calls["n"] += 1
        raise AssertionError(f"provider must not be called on cache hit: {kwargs}")

    result = await analyst_consensus_cache.resolve_consensus(
        symbol="005930",
        market="kr",
        redis_client=redis_client,
        opinion_fetcher=_boom_fetcher,
    )

    assert calls["n"] == 0
    assert result is not None
    assert result["total_count"] == 5
