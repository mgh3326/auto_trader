from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.mcp_server.tooling import foreigners_liquidity as fl
from app.services.invest_kr_fundamentals_snapshots.repository import (
    InvestKrFundamentalsSnapshotsRepository,
)


# --------------------------------------------------------------------------
# Pure backfill
# --------------------------------------------------------------------------
class TestApplyMarketCapBackfill:
    def test_backfilled_from_snapshot(self):
        rows = [{"symbol": "005930", "price": 80000.0, "market_cap": None}]
        fl.apply_market_cap_backfill(
            rows,
            snapshot_caps={"005930": Decimal("400000000000000")},
            shares_map={},
        )
        assert rows[0]["market_cap"] == 4e14
        assert rows[0]["market_cap_source"] == "fundamentals_snapshot"

    def test_fallback_shares_times_price(self):
        rows = [{"symbol": "005930", "price": 80000.0, "market_cap": None}]
        fl.apply_market_cap_backfill(
            rows,
            snapshot_caps={},  # no snapshot
            shares_map={"005930": Decimal("6000000000")},
        )
        assert rows[0]["market_cap"] == pytest.approx(6_000_000_000 * 80000.0)
        assert rows[0]["market_cap_source"] == "shares_outstanding_x_price"

    def test_honest_null_when_both_missing(self):
        rows = [{"symbol": "999999", "price": 1000.0, "market_cap": None}]
        fl.apply_market_cap_backfill(rows, snapshot_caps={}, shares_map={})
        assert rows[0]["market_cap"] is None
        assert rows[0]["market_cap_source"] is None

    def test_no_fabrication_when_price_missing(self):
        rows = [{"symbol": "005930", "price": None, "market_cap": None}]
        fl.apply_market_cap_backfill(
            rows, snapshot_caps={}, shares_map={"005930": Decimal("6000000000")}
        )
        assert rows[0]["market_cap"] is None
        assert rows[0]["market_cap_source"] is None

    def test_keeps_existing_kis_payload_market_cap(self):
        rows = [{"symbol": "005930", "price": 80000.0, "market_cap": 1.23e14}]
        fl.apply_market_cap_backfill(
            rows,
            snapshot_caps={"005930": Decimal("9e14")},
            shares_map={},
        )
        assert rows[0]["market_cap"] == 1.23e14  # unchanged
        assert rows[0]["market_cap_source"] == "kis_payload"


# --------------------------------------------------------------------------
# Pure filter
# --------------------------------------------------------------------------
class TestFilterIlliquidForeigners:
    def _rows(self):
        return [
            {"symbol": "005930", "foreign_net_amount": 4e11, "market_cap": 1e14},
            {"symbol": "JUNK1", "foreign_net_amount": 5_000_000.0, "market_cap": None},
        ]

    def test_filter_excludes_low_foreign_amount(self):
        kept, excluded = fl.filter_illiquid_foreigners(self._rows())
        assert excluded == 1
        assert [r["symbol"] for r in kept] == ["005930"]

    def test_include_illiquid_keeps_all(self):
        rows = self._rows()
        kept, excluded = fl.filter_illiquid_foreigners(rows, include_illiquid=True)
        assert excluded == 0
        assert len(kept) == 2

    def test_market_cap_floor_excludes_when_known_and_tiny(self):
        rows = [
            {"symbol": "MICRO", "foreign_net_amount": 5e11, "market_cap": 1e9},
        ]
        kept, excluded = fl.filter_illiquid_foreigners(
            rows, min_market_cap_krw=3e10
        )
        assert excluded == 1
        assert kept == []

    def test_market_cap_null_does_not_trigger_floor(self):
        rows = [
            {"symbol": "OK", "foreign_net_amount": 5e11, "market_cap": None},
        ]
        kept, excluded = fl.filter_illiquid_foreigners(rows)
        assert excluded == 0
        assert [r["symbol"] for r in kept] == ["OK"]

    def test_fallback_reads_trade_amount_key_pre_b1(self):
        # Before B1 the mapper emits trade_amount, not foreign_net_amount.
        rows = [{"symbol": "005930", "trade_amount": 4e11, "market_cap": None}]
        kept, excluded = fl.filter_illiquid_foreigners(rows)
        assert excluded == 0
        assert len(kept) == 1

    def test_negative_net_sell_amount_uses_magnitude(self):
        rows = [{"symbol": "005930", "foreign_net_amount": -4e11, "market_cap": None}]
        kept, _ = fl.filter_illiquid_foreigners(rows)
        assert [r["symbol"] for r in kept] == ["005930"]


# --------------------------------------------------------------------------
# Batched repository reader (mocked session)
# --------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.executed = 0

    async def execute(self, _stmt):
        self.executed += 1
        return self._results.pop(0)


@pytest.mark.asyncio
class TestMarketCapBySymbols:
    async def test_returns_non_null_caps_for_latest_partition(self):
        import datetime as dt

        session = _FakeSession(
            [
                _FakeResult(scalar=dt.date(2026, 6, 29)),
                _FakeResult(
                    rows=[
                        SimpleNamespace(symbol="005930", market_cap=Decimal("4e14")),
                        SimpleNamespace(symbol="000660", market_cap=None),
                    ]
                ),
            ]
        )
        repo = InvestKrFundamentalsSnapshotsRepository(cast(Any, session))
        out = await repo.market_cap_by_symbols(["005930", "000660"])
        assert out == {"005930": Decimal("4e14")}  # null filtered out

    async def test_empty_symbols_short_circuits(self):
        session = _FakeSession([])
        repo = InvestKrFundamentalsSnapshotsRepository(cast(Any, session))
        assert await repo.market_cap_by_symbols([]) == {}
        assert session.executed == 0

    async def test_no_partition_returns_empty(self):
        session = _FakeSession([_FakeResult(scalar=None)])
        repo = InvestKrFundamentalsSnapshotsRepository(cast(Any, session))
        assert await repo.market_cap_by_symbols(["005930"]) == {}
