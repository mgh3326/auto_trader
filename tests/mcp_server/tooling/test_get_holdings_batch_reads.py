"""hk 6376: get_holdings batched reads — constant statements + output identity.

- Candle + protected-position statements stay constant when the symbol count
  grows (one batched SELECT per market / per account+market, not per symbol),
  including declared-P and crypto alias/drift paths.
- Batched output is byte-identical to the per-symbol path on the same fake data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
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


def _upbit_position(symbol: str) -> dict[str, Any]:
    return {
        "account": "upbit",
        "account_name": "Upbit",
        "broker": "upbit",
        "source": "upbit_api",
        "instrument_type": "crypto",
        "market": "crypto",
        "symbol": symbol,
        "name": symbol,
        "quantity": 1.0,
        "sellable_quantity": 1.0,
        "broker_sellable_quantity": 1.0,
        "sellable_observed": True,
        "avg_buy_price": 100000000.0,
        "current_price": 100000000.0,
        "evaluation_amount": 100000000.0,
        "profit_loss": 0.0,
        "profit_rate": 0.0,
    }


class _FakeMappings:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeScalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)


class _FakeResult:
    def __init__(
        self,
        mappings_rows: list[Any] | None = None,
        scalars_rows: list[Any] | None = None,
    ) -> None:
        self._mappings = mappings_rows or []
        self._scalars = scalars_rows or []

    def mappings(self) -> _FakeMappings:
        return _FakeMappings(self._mappings)

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._scalars)

    def all(self) -> list[Any]:
        return []

    def scalar_one_or_none(self) -> Any:
        return self._scalars[0] if self._scalars else None

    def scalar_one(self) -> int:
        return 0


def _fake_protected_row(
    account_scope: str, market: str, symbol: str
) -> SimpleNamespace:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return SimpleNamespace(
        id=1,
        account_scope=account_scope,
        market=market,
        symbol=symbol,
        protected_quantity=Decimal("5"),
        revision=1,
        last_confirmed_broker_held=Decimal("10"),
        last_confirmed_at=now,
        updated_by_user_id=1,
        updated_at=now,
    )


class _CountingSession:
    statements: list[str] = []
    # Test-configured symbols for head-query fakes (session execute cannot see
    # the IN-list values reliably, so the test tells the fake what to return).
    protection_keys: list[tuple[str, str, str]] = []

    def __init__(self) -> None:
        pass

    async def __aenter__(self) -> _CountingSession:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def execute(
        self, stmt: Any, params: Any = None, *args: Any, **kwargs: Any
    ) -> _FakeResult:
        text = str(stmt)
        _CountingSession.statements.append(text[:120])
        if "kr_candles_1d" in text:
            symbols: list[str] = []
            if isinstance(params, dict) and "symbols" in params:
                symbols = list(params["symbols"])
            elif isinstance(params, dict) and "symbol" in params:
                symbols = [params["symbol"]]
            rows: list[dict[str, Any]] = []
            now = datetime.now(UTC)
            for sym in symbols:
                for _ in range(2):
                    rows.append(
                        {
                            "symbol": sym,
                            "time": now,
                            "open": 50000.0,
                            "high": 51000.0,
                            "low": 49000.0,
                            "close": 51000.0,
                            "adj_close": None,
                            "volume": 1000.0,
                            "value": 51000000.0,
                            "source": "kis",
                            "partition": "KRX",
                        }
                    )
            return _FakeResult(mappings_rows=rows)
        if "protected_positions" in text and "split_part" in text:
            return _FakeResult(scalars_rows=[])
        if "protected_positions" in text:
            rows = [
                _fake_protected_row(scope, market, sym)
                for (scope, market, sym) in _CountingSession.protection_keys
            ]
            return _FakeResult(scalars_rows=rows)
        if "execution_ledger" in text:
            return _FakeResult(mappings_rows=[])
        return _FakeResult()

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _install_counting_db(
    monkeypatch: pytest.MonkeyPatch,
    protection_keys: list[tuple[str, str, str]] | None = None,
) -> None:
    from app.core import db as _db

    _CountingSession.statements = []
    _CountingSession.protection_keys = list(protection_keys or [])

    def _factory() -> _CountingSession:
        return _CountingSession()

    monkeypatch.setattr(_db, "AsyncSessionLocal", _factory)


@pytest.mark.asyncio
async def test_statements_constant_with_declared_p_and_crypto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import portfolio_holdings
    from app.services import protected_quantity_service as _pqs
    from app.services.daily_candles import read_service as _rs

    monkeypatch.setattr(_rs, "kr_daily_bar_may_be_forming", lambda now=None: False)
    monkeypatch.setattr(_rs, "cache_is_fresh_equity", lambda rows, cal, now=None: True)

    async def _fake_quote(symbol: str) -> dict[str, Any]:
        return {"price": 51000.0}

    monkeypatch.setattr(portfolio_holdings, "_fetch_quote_equity_kr", _fake_quote)
    monkeypatch.setattr(
        portfolio_holdings,
        "_fetch_quote_equity_us",
        AsyncMock(side_effect=RuntimeError("no us")),
    )

    for n in (5, 30):
        kr_symbols = [f"{100000 + i}" for i in range(n)]
        crypto_symbols = [f"KRW-T{i:03d}" for i in range(n)]
        positions = [_kr_live_position(s) for s in kr_symbols] + [
            _upbit_position(s) for s in crypto_symbols
        ]
        keys = [("kis_live", "kr", s) for s in kr_symbols] + [
            ("upbit_live", "crypto", s) for s in crypto_symbols
        ]
        _install_counting_db(monkeypatch, protection_keys=keys)
        _rs.clear_kr_batch_cache()
        _pqs.clear_protection_batch_caches()
        _CountingSession.statements = []
        await portfolio_holdings._fetch_price_map_for_positions(
            [dict(p) for p in positions if p["instrument_type"] == "equity_kr"]
        )
        candle_statements = len(_CountingSession.statements)
        _rs.clear_kr_batch_cache()

        _install_counting_db(monkeypatch, protection_keys=keys)
        _pqs.clear_protection_batch_caches()
        _CountingSession.statements = []
        await portfolio_holdings._apply_protection_to_live_positions(
            [dict(p) for p in positions], is_mock=False
        )
        protection_statements = len(_CountingSession.statements)
        _pqs.clear_protection_batch_caches()

        if n == 5:
            first_candle, first_protection = candle_statements, protection_statements
        else:
            assert candle_statements == first_candle == 1
            # heads 1 per group (kr + crypto) + alias 1 + drift 1 per group with P.
            # 2 groups -> 2 + 1 + 2 = 5, constant as the symbol count grows.
            assert protection_statements == first_protection == 5


@pytest.mark.asyncio
async def test_batched_equals_per_symbol_with_cache_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import portfolio_holdings
    from app.services import protected_quantity_service as _pqs
    from app.services.daily_candles import read_service as _rs

    monkeypatch.setattr(_rs, "kr_daily_bar_may_be_forming", lambda now=None: False)
    monkeypatch.setattr(_rs, "cache_is_fresh_equity", lambda rows, cal, now=None: True)

    async def _fake_quote(symbol: str) -> dict[str, Any]:
        return {"price": 51000.0}

    monkeypatch.setattr(portfolio_holdings, "_fetch_quote_equity_kr", _fake_quote)
    monkeypatch.setattr(
        portfolio_holdings,
        "_fetch_quote_equity_us",
        AsyncMock(side_effect=RuntimeError("no us")),
    )

    symbols = [f"{200000 + i}" for i in range(5)]
    keys = [("kis_live", "kr", s) for s in symbols]
    positions = [_kr_live_position(s) for s in symbols]

    # Batched path (prefetch on).
    _install_counting_db(monkeypatch, protection_keys=keys)
    _rs.clear_kr_batch_cache()
    _pqs.clear_protection_batch_caches()
    batched_prices = await portfolio_holdings._fetch_price_map_for_positions(
        [dict(p) for p in positions]
    )
    batched_protected = await portfolio_holdings._apply_protection_to_live_positions(
        [dict(p) for p in positions], is_mock=False
    )
    _rs.clear_kr_batch_cache()
    _pqs.clear_protection_batch_caches()

    # Per-symbol path (prefetch off, same fake rows via the same session fake).
    async def _no_prefetch(_symbols: list[str]) -> None:
        return None

    monkeypatch.setattr(portfolio_holdings, "_prefetch_kr_candle_frames", _no_prefetch)
    monkeypatch.setattr(_pqs, "prefetch_protection_heads", AsyncMock(return_value=None))
    _install_counting_db(monkeypatch, protection_keys=keys)
    _rs.clear_kr_batch_cache()
    _pqs.clear_protection_batch_caches()
    legacy_prices = await portfolio_holdings._fetch_price_map_for_positions(
        [dict(p) for p in positions]
    )
    legacy_protected = await portfolio_holdings._apply_protection_to_live_positions(
        [dict(p) for p in positions], is_mock=False
    )
    _rs.clear_kr_batch_cache()
    _pqs.clear_protection_batch_caches()

    assert batched_prices == legacy_prices
    assert batched_prices[0][("equity_kr", symbols[0])] == 51000.0
    assert batched_protected == legacy_protected


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
