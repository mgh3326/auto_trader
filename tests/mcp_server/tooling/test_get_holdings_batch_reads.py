"""hk 6376: get_holdings batched reads — constant statements + output identity.

- Candle + protected-position statements stay constant when the symbol count
  grows (one batched SELECT per market / per account+market, not per symbol).
- Batched output is byte-identical to the recorded pre-change golden.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit


def _kr_live_position(symbol: str) -> dict[str, Any]:
    return {
        "account": "kis",
        "account_name": "기본 계좌",
        "broker": "kis",
        "source": "kis_api",
        "instrument_type": "equity_kr",
        "market": "kr",
        "symbol": symbol,
        "name": f"name-{symbol}",
        "quantity": 10.0,
        "sellable_quantity": 10.0,
        "broker_sellable_quantity": 10.0,
        "sellable_observed": True,
        "avg_buy_price": 50000.0,
        "current_price": None,
        "evaluation_amount": None,
        "profit_loss": None,
        "profit_rate": 0.0,
    }


class _FakeMappings:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows or []

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeScalars:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows or []

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)


class _FakeResult:
    def mappings(self) -> _FakeMappings:
        return _FakeMappings([])

    def scalars(self) -> _FakeScalars:
        return _FakeScalars([])

    def all(self) -> list[Any]:
        return []

    def scalar_one_or_none(self) -> None:
        return None

    def scalar_one(self) -> int:
        return 0


class _CountingSession:
    statements: list[str] = []

    def __init__(self) -> None:
        pass

    async def __aenter__(self) -> _CountingSession:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> _FakeResult:
        _CountingSession.statements.append(str(stmt)[:120])
        return _FakeResult()

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _install_counting_db(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import db as _db

    _CountingSession.statements = []

    def _factory() -> _CountingSession:
        return _CountingSession()

    monkeypatch.setattr(_db, "AsyncSessionLocal", _factory)


async def _candle_count(monkeypatch: pytest.MonkeyPatch, symbols: list[str]) -> int:
    from app.mcp_server.tooling import portfolio_holdings
    from app.services.daily_candles import read_service as _rs

    _install_counting_db(monkeypatch)
    monkeypatch.setattr(_rs, "kr_daily_bar_may_be_forming", lambda now=None: False)
    _rs.clear_kr_batch_cache()

    async def _fake_quote(symbol: str) -> dict[str, Any]:
        return {"price": 51000.0}

    monkeypatch.setattr(portfolio_holdings, "_fetch_quote_equity_kr", _fake_quote)
    monkeypatch.setattr(
        portfolio_holdings,
        "_fetch_quote_equity_us",
        AsyncMock(side_effect=RuntimeError("no us in kr test")),
    )

    positions = [_kr_live_position(s) for s in symbols]
    _CountingSession.statements = []
    await portfolio_holdings._fetch_price_map_for_positions(positions)
    count = len(_CountingSession.statements)
    _rs.clear_kr_batch_cache()
    return count


async def _protection_count(monkeypatch: pytest.MonkeyPatch, symbols: list[str]) -> int:
    from app.mcp_server.tooling import portfolio_holdings
    from app.services import protected_quantity_service as _pqs

    _install_counting_db(monkeypatch)
    _pqs.clear_protection_head_cache()
    positions = [_kr_live_position(s) for s in symbols]
    _CountingSession.statements = []
    await portfolio_holdings._apply_protection_to_live_positions(
        [dict(p) for p in positions], is_mock=False
    )
    count = len(_CountingSession.statements)
    _pqs.clear_protection_head_cache()
    return count


@pytest.mark.asyncio
async def test_get_holdings_issues_constant_statements_for_5_and_30_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbols_5 = [f"{100000 + i}" for i in range(5)]
    symbols_30 = [f"{100000 + i}" for i in range(30)]

    candle_5 = await _candle_count(monkeypatch, symbols_5)
    candle_30 = await _candle_count(monkeypatch, symbols_30)
    assert candle_5 == candle_30 == 1

    prot_5 = await _protection_count(monkeypatch, symbols_5)
    prot_30 = await _protection_count(monkeypatch, symbols_30)
    assert prot_5 == prot_30 == 1


@pytest.mark.asyncio
async def test_get_holdings_batched_output_matches_recorded_golden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.mcp_server import get_holdings_golden_support as support

    support.install_fake_collect(monkeypatch)
    # Same CALL as the pre-change golden (ledger-lots contract): default output
    # must stay byte-identical after batching.
    result = await support.call_get_holdings(
        include_current_price=False, minimum_value=0
    )
    assert support.canonical_json(result) == support.GOLDEN_PATH.read_text(
        encoding="utf-8"
    )
