"""ROB-1487: batch paths resolve the US exchange map once, not per symbol.

``analyze_stock_batch`` used to issue two single-row ``us_symbol_universe``
SELECTs per US symbol — one of them on its own ``AsyncSessionLocal`` inside the
concurrency-5 gather, which overran ``DB_POOL_SIZE`` and made the pool dial
fresh connections. These tests pin the batched substitution and that the scope
does not leak outside the batch.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.mcp_server.tooling import analysis_screening, analysis_tool_handlers
from app.services import us_symbol_universe_service as universe


class _Rows:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, str]]:
        return list(self._rows)


class _RecordingSession:
    """Session factory stand-in that records every universe statement."""

    statements: list[Any] = []

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows

    async def execute(self, statement: Any) -> _Rows:
        type(self).statements.append(statement)
        wanted: set[str] = set()
        for params in statement.compile().params.values():
            wanted.update(str(value) for value in params)
        return _Rows([row for row in self._rows if row[0] in wanted])

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


async def _install_recording_universe(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[tuple[str, str]],
) -> None:
    _RecordingSession.statements = []
    monkeypatch.setattr(universe, "AsyncSessionLocal", lambda: _RecordingSession(rows))


async def _run_batch(
    symbols: list[str],
    market: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    seen: dict[str, str] | None = None,
) -> dict[str, Any]:
    async def fake_analyze(
        symbol: str,
        market_type: str | None = None,
        include_peers: bool = False,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        exchange = await universe.get_us_exchange_by_symbol(symbol)
        if seen is not None:
            seen[symbol] = exchange
        return {"symbol": symbol, "data_state": "fresh", "cache_hit": False}

    monkeypatch.setattr(analysis_screening, "_analyze_stock_impl", fake_analyze)
    return await analysis_tool_handlers._run_batch_analysis(
        symbols,
        market=market,
        include_peers=False,
        formatter=lambda sym, result: dict(result),
    )


@pytest.mark.asyncio
async def test_analyze_stock_batch_issues_one_universe_query_for_n_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _install_recording_universe(
        monkeypatch,
        [("AAPL", "NASD"), ("MSFT", "NASD"), ("NVDA", "NASD")],
    )
    seen: dict[str, str] = {}

    result = await _run_batch(["AAPL", "MSFT", "NVDA"], "us", monkeypatch, seen=seen)

    assert seen == {"AAPL": "NASD", "MSFT": "NASD", "NVDA": "NASD"}
    assert len(_RecordingSession.statements) == 1
    assert result["summary"]["successful"] == 3


@pytest.mark.asyncio
async def test_scope_is_reset_after_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    await _install_recording_universe(monkeypatch, [("AAPL", "NASD")])

    await _run_batch(["AAPL"], "us", monkeypatch)

    assert universe._us_exchange_cache.get() is None


@pytest.mark.asyncio
async def test_kr_only_batch_does_not_touch_the_us_universe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _install_recording_universe(monkeypatch, [])

    async def fake_analyze(
        symbol: str,
        market_type: str | None = None,
        include_peers: bool = False,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return {"symbol": symbol, "data_state": "fresh", "cache_hit": False}

    monkeypatch.setattr(analysis_screening, "_analyze_stock_impl", fake_analyze)

    await analysis_tool_handlers._run_batch_analysis(
        ["005930"], market="kr", include_peers=False, formatter=lambda s, r: dict(r)
    )

    assert _RecordingSession.statements == []


@pytest.mark.asyncio
async def test_unresolvable_symbol_stays_a_per_symbol_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prefetch must not turn a per-symbol error into a batch failure."""

    await _install_recording_universe(monkeypatch, [("AAPL", "NASD")])

    original_resolve = analysis_screening._resolve_market_type

    def resolve(symbol: str, market: str | None) -> tuple[str, str]:
        if symbol == "BAD":
            raise ValueError("invalid US equity symbol")
        return original_resolve(symbol, market)

    async def fake_analyze(
        symbol: str,
        market_type: str | None = None,
        include_peers: bool = False,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        if symbol == "BAD":
            raise ValueError("invalid US equity symbol")
        return {"symbol": symbol, "data_state": "fresh", "cache_hit": False}

    monkeypatch.setattr(analysis_screening, "_resolve_market_type", resolve)
    monkeypatch.setattr(analysis_screening, "_analyze_stock_impl", fake_analyze)

    result = await analysis_tool_handlers._run_batch_analysis(
        ["AAPL", "BAD"],
        market="us",
        include_peers=False,
        formatter=lambda s, r: dict(r),
    )

    assert result["summary"]["failed"] == 1
    assert result["summary"]["errors"] == ["BAD: invalid US equity symbol"]
    assert len(_RecordingSession.statements) == 1
