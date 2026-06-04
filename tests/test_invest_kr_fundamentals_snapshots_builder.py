from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.invest_kr_fundamentals_snapshots.builder import (
    KrFundamentalsProviderRow,
    build_kr_fundamentals_snapshot_payloads,
    build_kr_fundamentals_snapshots,
    provider_row_from_mapping,
)
from app.services.invest_kr_fundamentals_snapshots.repository import (
    KrFundamentalsSnapshotUpsert,
)


def _sample_row(**overrides: object) -> KrFundamentalsProviderRow:
    base: dict[str, object] = {
        "symbol": "005930",
        "name": "삼성전자",
        "price": Decimal("356250"),
        "change_rate": Decimal("-1.1789"),
        "volume": Decimal("15118684"),
        "market_cap": Decimal("1482709604635.74"),
        "per": Decimal("28.5551"),
        "pbr": Decimal("4.957"),
        "dividend_yield": Decimal("0.4646"),
        "roe_ttm": Decimal("19.1610"),
        "payout_ratio_ttm": Decimal("13.2668"),
        "gross_margin_ttm": Decimal("47.0017"),
        "revenue_yoy": Decimal("10.8801"),
        "eps_yoy": Decimal("32.7484"),
        "eps_qoq": Decimal("144.9448"),
        "net_income_yoy": Decimal("31.6453"),
        "net_income_cagr_5y": Decimal("11.1492"),
        "continuous_dividend_payout": Decimal("38"),
        "continuous_dividend_growth": Decimal("2"),
        "week_high_52": Decimal("370000"),
        "rsi14": Decimal("75.5192"),
        "sector": "Electronic Technology",
        "industry": "Telecommunications Equipment",
        "raw_payload": {"symbol_raw": "KRX:005930"},
    }
    base.update(overrides)
    return KrFundamentalsProviderRow(**base)  # type: ignore[arg-type]


class _Provider:
    def __init__(self, rows: list[KrFundamentalsProviderRow]) -> None:
        self._rows = rows

    async def fetch_rows(
        self, *, limit: int | None = None
    ) -> list[KrFundamentalsProviderRow]:
        return self._rows[:limit] if limit is not None else self._rows


class _Repo:
    def __init__(self) -> None:
        self.payloads: list[KrFundamentalsSnapshotUpsert] = []

    async def upsert(self, payload: KrFundamentalsSnapshotUpsert) -> None:
        self.payloads.append(payload)


def test_build_payloads_extracts_symbol_and_sets_source() -> None:
    payloads = build_kr_fundamentals_snapshot_payloads(
        [_sample_row(symbol="KRX:005930")],
        snapshot_date=dt.date(2026, 6, 4),
    )
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload.symbol == "005930"
    assert payload.snapshot_date == dt.date(2026, 6, 4)
    assert payload.source == "tvscreener_kr"
    assert payload.name == "삼성전자"
    assert payload.roe_ttm == Decimal("19.1610")
    assert payload.continuous_dividend_payout == Decimal("38")
    assert payload.week_high_52 == Decimal("370000")
    assert payload.industry == "Telecommunications Equipment"


def test_build_payloads_uppercases_and_strips_krx_prefix() -> None:
    payloads = build_kr_fundamentals_snapshot_payloads(
        [_sample_row(symbol="krx:000660")],
        snapshot_date=dt.date(2026, 6, 4),
    )
    assert payloads[0].symbol == "000660"


def test_build_payloads_drops_rows_without_price() -> None:
    payloads = build_kr_fundamentals_snapshot_payloads(
        [_sample_row(symbol="005930", price=None)],
        snapshot_date=dt.date(2026, 6, 4),
    )
    assert payloads == []


def test_build_payloads_drops_rows_without_symbol() -> None:
    payloads = build_kr_fundamentals_snapshot_payloads(
        [_sample_row(symbol="")],
        snapshot_date=dt.date(2026, 6, 4),
    )
    assert payloads == []


def test_provider_row_from_mapping_maps_normalized_keys() -> None:
    row = provider_row_from_mapping(
        {
            "symbol": "KRX:005930",
            "name": "005930",
            "description": "삼성전자",
            "price": 356250.0,
            "change_percent": -1.1789,
            "volume": 15118684.0,
            "market_capitalization": 1482709604635.74,
            "price_to_earnings_ratio_ttm": 28.5551,
            "price_to_book_mrq": 4.957,
            "dividend_yield_forward": 0.4646,
            "return_on_equity_ttm": 19.1610,
            "dividend_payout_ratio_ttm": 13.2668,
            "gross_margin_ttm": 47.0017,
            "revenue_annual_yoy_growth": 10.8801,
            "eps_diluted_annual_yoy_growth": 32.7484,
            "eps_diluted_quarterly_qoq_growth": 144.9448,
            "net_income_annual_yoy_growth": 31.6453,
            "net_income_cagr_5y": 11.1492,
            "continuous_dividend_payout": 38,
            "continuous_dividend_growth": 2,
            "52_week_high": 370000.0,
            "relative_strength_index_14": 75.5192,
            "sector": "Electronic Technology",
            "industry": "Telecommunications Equipment",
        }
    )
    assert row is not None
    assert row.symbol == "005930"
    # description preferred over the ticker-shaped name column
    assert row.name == "삼성전자"
    assert row.price == Decimal("356250.0")
    assert row.change_rate == Decimal("-1.1789")
    assert row.market_cap == Decimal("1482709604635.74")
    assert row.per == Decimal("28.5551")
    assert row.pbr == Decimal("4.957")
    assert row.dividend_yield == Decimal("0.4646")
    assert row.roe_ttm == Decimal("19.161")
    assert row.payout_ratio_ttm == Decimal("13.2668")
    assert row.gross_margin_ttm == Decimal("47.0017")
    assert row.revenue_yoy == Decimal("10.8801")
    assert row.eps_yoy == Decimal("32.7484")
    assert row.eps_qoq == Decimal("144.9448")
    assert row.net_income_yoy == Decimal("31.6453")
    assert row.net_income_cagr_5y == Decimal("11.1492")
    assert row.continuous_dividend_payout == Decimal("38")
    assert row.continuous_dividend_growth == Decimal("2")
    assert row.week_high_52 == Decimal("370000.0")
    assert row.rsi14 == Decimal("75.5192")
    assert row.sector == "Electronic Technology"
    assert row.industry == "Telecommunications Equipment"


def test_provider_row_from_mapping_missing_keys_become_none() -> None:
    row = provider_row_from_mapping(
        {
            "symbol": "KRX:005930",
            "price": 1000.0,
        }
    )
    assert row is not None
    assert row.symbol == "005930"
    assert row.price == Decimal("1000.0")
    assert row.name is None
    assert row.roe_ttm is None
    assert row.continuous_dividend_payout is None
    assert row.week_high_52 is None
    assert row.industry is None


def test_provider_row_from_mapping_rejects_non_krx() -> None:
    assert provider_row_from_mapping({"symbol": "NASDAQ:AAPL", "price": 100.0}) is None


def test_provider_row_from_mapping_rejects_missing_price() -> None:
    assert provider_row_from_mapping({"symbol": "KRX:005930"}) is None


@pytest.mark.asyncio
async def test_build_snapshots_dry_run_does_not_upsert() -> None:
    repo = _Repo()
    result = await build_kr_fundamentals_snapshots(
        provider=_Provider([_sample_row()]),
        repository=repo,
        snapshot_date=dt.date(2026, 6, 4),
        commit=False,
        limit=10,
    )
    assert result["snapshot_date"] == "2026-06-04"
    assert result["fetched"] == 1
    assert result["would_upsert"] == 1
    assert result["upserted"] == 0
    assert result["committed"] is False
    assert repo.payloads == []
    assert len(result["samples"]) == 1


@pytest.mark.asyncio
async def test_build_snapshots_commit_upserts() -> None:
    repo = _Repo()
    result = await build_kr_fundamentals_snapshots(
        provider=_Provider(
            [_sample_row(symbol="005930"), _sample_row(symbol="000660")]
        ),
        repository=repo,
        snapshot_date=dt.date(2026, 6, 4),
        commit=True,
        limit=10,
        universe_count=2,  # 2/2 = 100% >= 80% floor → allowed
    )
    assert result["fetched"] == 2
    assert result["would_upsert"] == 2
    assert result["upserted"] == 2
    assert result["committed"] is True
    assert result["commit_allowed"] is True
    assert result["block_reason"] is None
    assert {p.symbol for p in repo.payloads} == {"005930", "000660"}


# ---------------------------------------------------------------------------
# ROB-429 A2 — production commit guard (assert_min_coverage, floor 0.80)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_snapshots_dry_run_carries_coverage_metadata() -> None:
    repo = _Repo()
    result = await build_kr_fundamentals_snapshots(
        provider=_Provider([_sample_row(symbol="005930")]),
        repository=repo,
        snapshot_date=dt.date(2026, 6, 4),
        commit=False,
        limit=10,
        universe_count=100,
    )
    # dry-run still reports the full coverage metadata.
    assert result["active_universe_count"] == 100
    assert result["coverage_ratio"] == 0.01  # 1 / 100
    assert result["commit_allowed"] is False  # 1 < ceil(0.80 * 100) = 80
    assert result["block_reason"] is not None
    assert result["upserted"] == 0
    assert repo.payloads == []


@pytest.mark.asyncio
async def test_build_snapshots_commit_blocked_below_floor() -> None:
    repo = _Repo()
    result = await build_kr_fundamentals_snapshots(
        provider=_Provider([_sample_row(symbol="005930")]),
        repository=repo,
        snapshot_date=dt.date(2026, 6, 4),
        commit=True,
        limit=10,
        universe_count=100,  # floor = 80; would_upsert = 1 < 80 → blocked
    )
    assert result["commit_allowed"] is False
    assert result["committed"] is False
    assert result["upserted"] == 0
    assert repo.payloads == []  # nothing upserted on a blocked commit
    assert "80%" in result["block_reason"] or "floor" in result["block_reason"]


@pytest.mark.asyncio
async def test_build_snapshots_commit_allow_partial_overrides_floor() -> None:
    repo = _Repo()
    result = await build_kr_fundamentals_snapshots(
        provider=_Provider([_sample_row(symbol="005930")]),
        repository=repo,
        snapshot_date=dt.date(2026, 6, 4),
        commit=True,
        limit=10,
        universe_count=100,
        allow_partial=True,  # operator override → upsert despite thin coverage
    )
    assert result["committed"] is True
    assert result["commit_allowed"] is True
    assert result["upserted"] == 1
    assert result["block_reason"] is None
    assert {p.symbol for p in repo.payloads} == {"005930"}


@pytest.mark.asyncio
async def test_build_snapshots_guard_fail_open_when_universe_zero() -> None:
    # universe_count <= 0 disables the gate (fail-open, consistent with PR2a/2b).
    repo = _Repo()
    result = await build_kr_fundamentals_snapshots(
        provider=_Provider([_sample_row(symbol="005930")]),
        repository=repo,
        snapshot_date=dt.date(2026, 6, 4),
        commit=True,
        limit=10,
        universe_count=0,
    )
    assert result["committed"] is True
    assert result["commit_allowed"] is True
    assert result["coverage_ratio"] == 0.0
    assert result["upserted"] == 1
