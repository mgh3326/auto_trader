"""ROB-1309: screen_stocks_enrich MCP tool (live-enrichment counterpart to
screen_stocks_snapshot).

These tests were moved out of test_screener_snapshot_tool.py — they exercise
analyst-count filtering + enrich_snapshot_page wiring, which now lives
exclusively behind screen_stocks_enrich_impl. See
tests/test_screener_snapshot_db_only_regression.py for the DB-only
"screen_stocks_snapshot never calls providers" proof (which deliberately
does NOT import this module — see that file's docstring for why).

The tests in this file necessarily import `screener_enrich_tool` and
`enrichment_negative_cache`, both new in ROB-1309: on `main` (pre-fix) this
whole file fails to collect (ImportError), which is the expected/correct RED
for "this new tool doesn't exist yet" — not a claim about the OLD tool's
HTTP behavior (that claim is proven independently and without ImportError in
test_screener_snapshot_db_only_regression.py).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.mcp_server.tooling import screener_enrich_tool as enrich_tool
from app.mcp_server.tooling import screener_snapshot_tool as tool


class _FakeCM:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def _fake_external_boundaries_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty_positions(
        market_filter: str | None, *, is_mock: bool = False
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        del market_filter, is_mock
        return [], []

    monkeypatch.setattr(
        "app.mcp_server.tooling.portfolio_holdings._collect_kis_positions",
        _empty_positions,
    )

    async def _no_redis():
        return None

    monkeypatch.setattr("app.core.analyze_cache._get_redis_client", _no_redis)


def _patch_build_with_n_results(monkeypatch, n: int) -> None:
    class _BigResp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [{"symbol": f"S{i}"} for i in range(n)],
                "warnings": [],
            }

    async def _fake_build(**_kwargs: Any) -> _BigResp:
        return _BigResp()

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrich_enriches_only_returned_page(monkeypatch) -> None:
    _patch_build_with_n_results(monkeypatch, 5)
    captured: dict[str, Any] = {}

    async def _fake_enrich_page(
        *,
        rows: list[dict[str, Any]],
        market: str,
        session_factory,
        opinion_provider=None,
        fetch_kr_sector=None,
        fetch_us_sector=None,
    ) -> dict[str, Any]:
        del opinion_provider, fetch_kr_sector, fetch_us_sector
        captured["symbols"] = [row["symbol"] for row in rows]
        captured["market"] = market
        captured["session_factory"] = session_factory
        enriched = []
        for row in rows:
            enriched.append(
                {
                    **row,
                    "analystLabel": "매수 1 / 보유 0 / 매도 0 · 목표 +10.0%",
                    "analysisContext": {
                        "consensus": {
                            "source": "naver",
                            "buyCount": 1,
                            "holdCount": 0,
                            "sellCount": 0,
                            "strongBuyCount": 0,
                            "totalCount": 1,
                            "avgTargetPrice": 110.0,
                            "medianTargetPrice": 110.0,
                            "minTargetPrice": 110.0,
                            "maxTargetPrice": 110.0,
                            "upsidePct": 10.0,
                            "currentPrice": 100.0,
                        },
                        "rsi14": 58.0,
                        "dataState": "fresh",
                        "warnings": [],
                    },
                }
            )
        return {
            "results": enriched,
            "summary": {
                "attempted": len(rows),
                "consensusSucceeded": len(rows),
                "rsiSucceeded": len(rows),
                "sectorResolved": 0,
                "warnings": [],
            },
        }

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_analysis_enrichment.enrich_snapshot_page",
        _fake_enrich_page,
    )

    out = await enrich_tool.screen_stocks_enrich_impl(
        preset="consecutive_gainers", market="kr", limit=2, offset=1
    )

    assert captured["symbols"] == ["S1", "S2"]
    assert captured["market"] == "kr"
    assert len(out["results"]) == 2
    assert out["results"][0]["analysisContext"]["rsi14"] == pytest.approx(58.0)
    assert out["analysisEnrichment"]["attempted"] == 2
    assert out["enrichment"]["applied"] is True
    assert "meta" in out


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrich_analyst_filter_rejects_large_unpaged_enrichment(
    monkeypatch,
) -> None:
    class _Resp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [
                    {"symbol": f"S{i}", "marketCapValue": 1.0} for i in range(201)
                ],
                "warnings": [],
            }

    async def _fake_build(**_kwargs: Any) -> _Resp:
        return _Resp()

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build,
    )

    out = await enrich_tool.screen_stocks_enrich_impl(
        preset="consecutive_gainers",
        market="kr",
        min_analyst_count=1,
    )

    assert "error" in out
    assert "analyst enrichment row cap" in out["error"]
    assert out["results"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrich_min_analyst_filters_via_counts_and_enriches_only_page(
    monkeypatch,
) -> None:
    _patch_build_with_n_results(monkeypatch, 5)  # symbols S0..S4

    async def _fake_counts(*, symbols, market, redis_client=None, memo=None, **kw):
        # S0,S1,S2 qualify (>=3), S3,S4 do not
        return {
            s: {"totalCount": (3 if i < 3 else 1), "buyCount": (2 if i < 3 else 0)}
            for i, s in enumerate(symbols)
        }

    monkeypatch.setattr(
        "app.services.invest_view_model.analyst_consensus_cache.resolve_consensus_counts",
        _fake_counts,
    )

    enriched_symbols: list[list[str]] = []

    async def _fake_enrich_page(
        *,
        rows: list[dict[str, Any]],
        market: str,
        session_factory,
        opinion_provider=None,
        fetch_kr_sector=None,
        fetch_us_sector=None,
    ) -> dict[str, Any]:
        del (
            market,
            session_factory,
            opinion_provider,
            fetch_kr_sector,
            fetch_us_sector,
        )
        enriched_symbols.append([r["symbol"] for r in rows])
        return {
            "results": [
                {**r, "analystLabel": "x", "analysisContext": {}} for r in rows
            ],
            "summary": {
                "attempted": len(rows),
                "consensusSucceeded": 0,
                "rsiSucceeded": 0,
                "sectorResolved": 0,
                "warnings": [],
            },
        }

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_analysis_enrichment.enrich_snapshot_page",
        _fake_enrich_page,
    )

    out = await enrich_tool.screen_stocks_enrich_impl(
        preset="consecutive_gainers",
        market="kr",
        min_analyst_count=3,
        limit=2,
        offset=0,
    )
    # 3 qualified, page of 2
    assert out["pagination"]["total_available"] == 3
    assert len(out["results"]) == 2
    # enrichment saw ONLY the 2 returned page rows, not all matched/qualified rows
    assert enriched_symbols == [["S0", "S1"]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrich_min_analyst_buy_and_total_count_filters(monkeypatch) -> None:
    """ROB-1309: the analyst-count-filter half of the old combined market-cap+
    analyst test — now exercised against screen_stocks_enrich_impl."""

    class _Resp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [
                    {"symbol": "S1", "marketCapValue": 500_000_000_000.0},
                    {"symbol": "S2", "marketCapValue": 200_000_000_000.0},
                ],
                "warnings": [],
            }

    async def _fake_build(**_kwargs: Any) -> _Resp:
        return _Resp()

    async def _fake_enrich_page(
        *,
        rows: list[dict[str, Any]],
        market: str,
        session_factory,
        opinion_provider=None,
        fetch_kr_sector=None,
        fetch_us_sector=None,
    ) -> dict[str, Any]:
        del (
            market,
            session_factory,
            opinion_provider,
            fetch_kr_sector,
            fetch_us_sector,
        )
        return {
            "results": rows,
            "summary": {
                "attempted": len(rows),
                "consensusSucceeded": 0,
                "rsiSucceeded": 0,
                "sectorResolved": 0,
                "warnings": [],
            },
        }

    async def _fake_counts(*, symbols, market, redis_client=None, memo=None, **kw):
        return {
            "S1": {"totalCount": 2, "buyCount": 2},
            "S2": {"totalCount": 1, "buyCount": 0},
        }

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build,
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_analysis_enrichment.enrich_snapshot_page",
        _fake_enrich_page,
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.analyst_consensus_cache.resolve_consensus_counts",
        _fake_counts,
    )

    # Min analyst buy 1 -> S2 gone
    out = await enrich_tool.screen_stocks_enrich_impl(
        preset="consecutive_gainers", market="kr", min_analyst_buy_count=1
    )
    assert [r["symbol"] for r in out["results"]] == ["S1"]

    # Total analyst coverage can pass even when buyCount is 0.
    out = await enrich_tool.screen_stocks_enrich_impl(
        preset="consecutive_gainers", market="kr", min_analyst_count=1
    )
    assert [r["symbol"] for r in out["results"]] == ["S1", "S2"]


def _fake_kis_positions(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty(market_filter, *, is_mock=False):  # noqa: ARG001
        return [], []

    monkeypatch.setattr(
        "app.mcp_server.tooling.portfolio_holdings._collect_kis_positions", _empty
    )


def _fake_build_single(monkeypatch: pytest.MonkeyPatch, row: dict[str, Any]) -> None:
    class _R:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {"presetId": "consecutive_gainers", "results": [row], "warnings": []}

    async def _build(**_kwargs: Any) -> _R:
        return _R()

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _build,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_screen_stocks_enrich_is_the_path_that_reaches_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contrast case (moved from test_screener_snapshot_db_only_regression.py):
    the SAME provider hooks that must never fire for screen_stocks_snapshot DO
    fire (through screen_stocks_enrich's negative-cache wrappers) for
    screen_stocks_enrich — proving the split moved the calls rather than
    deleting the feature. Patches enrich_snapshot_page itself (its DB-lookup
    internals aren't the object under test here) and asserts the
    fetch_kr_sector/opinion_provider callables it's handed are live and reach
    the real provider hooks."""
    _fake_kis_positions(monkeypatch)
    _fake_build_single(monkeypatch, {"symbol": "005930", "market": "kr"})

    calls: dict[str, int] = {"sector": 0, "opinions": 0}

    async def _sector(code: str):  # noqa: ARG001
        calls["sector"] += 1
        return None, None

    async def _opinions(**kwargs: Any):
        calls["opinions"] += 1
        return {"error": "analyst_consensus_unavailable"}

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_analysis_enrichment._fetch_kr_sector",
        _sector,
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.fundamentals._valuation.handle_get_investment_opinions",
        _opinions,
    )

    async def _fake_enrich_page(
        *,
        rows,
        market,
        session_factory,
        opinion_provider,
        fetch_kr_sector,
        fetch_us_sector,
    ):
        del session_factory, fetch_us_sector
        for row in rows:
            await fetch_kr_sector(row["symbol"])
            await opinion_provider(symbol=row["symbol"], market=market)
        return {
            "results": [{**r, "analysisContext": {}} for r in rows],
            "summary": {
                "attempted": len(rows),
                "consensusSucceeded": 0,
                "rsiSucceeded": 0,
                "sectorResolved": 0,
                "warnings": [],
            },
        }

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_analysis_enrichment.enrich_snapshot_page",
        _fake_enrich_page,
    )

    out = await enrich_tool.screen_stocks_enrich_impl(
        preset="consecutive_gainers", market="kr"
    )

    assert calls["sector"] == 1
    assert calls["opinions"] == 1
    assert out["enrichment"]["applied"] is True
    assert "analysisContext" in out["results"][0]
    assert "enrichment_excluded" in out["meta"]
    assert "chronic_failure_candidates" in out["meta"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_negative_cache_hit_skips_provider_retry_within_ttl() -> None:
    """A failed sector fetch is recorded; a second lookup within TTL must
    skip the network call entirely (the wrapper never calls real_fetcher).
    Moved from test_screener_snapshot_db_only_regression.py — see this
    module's docstring for why."""
    from app.services.invest_view_model import enrichment_negative_cache as negcache

    calls = {"n": 0}

    async def _flaky(symbol: str):  # noqa: ARG001
        calls["n"] += 1
        raise RuntimeError("404 not found")

    class _FakeRedis:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}

        async def get(self, key: str):
            return self.store.get(key)

        async def set(self, key: str, value: str, ex: int | None = None):  # noqa: ARG002
            self.store[key] = value

        async def delete(self, key: str):
            self.store.pop(key, None)

    redis_client = _FakeRedis()

    async def _try_fetch() -> tuple[str | None, str | None] | None:
        entry = await negcache.should_skip(
            redis_client, kind="kr_sector", market="kr", symbol="005930"
        )
        if entry is not None:
            return None
        try:
            return await _flaky("005930")
        except Exception as exc:  # noqa: BLE001
            await negcache.record_failure(
                redis_client, kind="kr_sector", market="kr", symbol="005930", exc=exc
            )
            return None

    # First call: cache miss -> real fetch attempted -> fails -> recorded.
    await _try_fetch()
    assert calls["n"] == 1

    # Second call within TTL: cache hit -> real fetch skipped entirely.
    await _try_fetch()
    assert calls["n"] == 1

    entry = await negcache.get_entry(
        redis_client, kind="kr_sector", market="kr", symbol="005930"
    )
    assert entry is not None
    assert entry.consecutive_failures == 1
    assert entry.error_class == "not_found"
