"""ROB-147 — view-model tests for build_screener_results."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.invest_screener import ScreenerResultsResponse
from app.services.invest_view_model import screener_service
from app.services.invest_view_model.screener_service import (
    build_screener_presets,
    build_screener_results,
)


def _stub_screening_rows() -> list[dict[str, Any]]:
    return [
        {
            "symbol": "005930",
            "name": "삼성전자",
            "market": "kr",
            "sector": "반도체",
            "market_cap_krw": 478_000_000_000_000,
            "close": 80_000,
            "change_rate": 1.23,
            "change_amount": 970,
            "consecutive_up_days": 5,
            "volume": 12_345_678,
            "per": 14.0,
            "pbr": 1.2,
            "dividend_yield": 1.8,
            "rsi": 55.0,
        },
        {
            "symbol": "035720",
            "name": "카카오",
            "market": "kr",
            "sector": "인터넷",
            "market_cap_krw": 20_000_000_000_000,
            "close": 45_000,
            "change_rate": -0.5,
            "change_amount": -200,
            "consecutive_up_days": 4,
            "volume": 3_000_000,
            "per": None,
            "pbr": None,
            "dividend_yield": None,
            "rsi": None,
        },
    ]


class _FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeExecuteResult:
    def __init__(
        self, *, scalar_rows: list[Any] | None = None, rows: list[Any] | None = None
    ) -> None:
        self._scalar_rows = scalar_rows or []
        self._rows = rows or []

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._scalar_rows)

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one_or_none(self) -> Any | None:
        return self._scalar_rows[0] if self._scalar_rows else None

    def one(self) -> Any:
        if self._rows:
            return self._rows[0]
        return type("EmptyRow", (), {})()


class _FakeSession:
    def __init__(self, results: list[_FakeExecuteResult]) -> None:
        self.results = list(results)
        self.calls = 0

    async def execute(self, stmt: Any) -> _FakeExecuteResult:  # noqa: ARG002
        self.calls += 1
        if not self.results:
            return _FakeExecuteResult()
        return self.results.pop(0)


class _FakeSnapshot:
    def __init__(self, **kwargs: Any) -> None:
        self.market = kwargs.get("market", "kr")
        self.symbol = kwargs["symbol"]
        self.snapshot_date = kwargs.get("snapshot_date", date(2026, 5, 11))
        self.latest_close = kwargs.get("latest_close", Decimal("80000"))
        self.prev_close = kwargs.get("prev_close", Decimal("79000"))
        self.change_amount = kwargs.get("change_amount", Decimal("1000"))
        self.change_rate = kwargs.get("change_rate", Decimal("1.2658"))
        self.consecutive_up_days = kwargs.get("consecutive_up_days", 6)
        self.week_change_rate = kwargs.get("week_change_rate", Decimal("3.5"))
        self.closes_window = kwargs.get(
            "closes_window", [76000, 77000, 78000, 79000, 80000]
        )
        self.daily_volume = kwargs.get("daily_volume", 1234567)
        self.computed_at = kwargs.get(
            "computed_at", datetime(2026, 5, 11, 0, 30, tzinfo=UTC)
        )


class _FakeCryptoSnapshot:
    def __init__(self, **kwargs: Any) -> None:
        self.symbol = kwargs.get("symbol", "KRW-BTC")
        self.snapshot_date = kwargs.get("snapshot_date", date(2026, 5, 13))
        self.name = kwargs.get("name", "Bitcoin")
        self.latest_close = kwargs.get("latest_close", Decimal("145000000"))
        self.change_amount = kwargs.get("change_amount", Decimal("1000000"))
        self.change_rate = kwargs.get("change_rate", Decimal("1.25"))
        self.trade_amount_24h = kwargs.get("trade_amount_24h", Decimal("123456789000"))
        self.volume_24h = kwargs.get("volume_24h", Decimal("9876.5"))
        self.volume_24h_usd = kwargs.get("volume_24h_usd", Decimal("90000000"))
        self.market_cap = kwargs.get("market_cap", Decimal("1800000000000"))
        self.rsi = kwargs.get("rsi", Decimal("58.0"))
        self.adx = kwargs.get("adx", Decimal("24.0"))
        self.computed_at = kwargs.get(
            "computed_at", datetime(2026, 5, 13, 2, 30, tzinfo=UTC)
        )


def _coverage_row(
    *,
    latest_count: int,
    stale: int = 0,
    last: datetime | None = datetime(2026, 5, 13, 2, 30, tzinfo=UTC),
) -> Any:
    return type(
        "CoverageRow",
        (),
        {"latest_count": latest_count, "stale": stale, "last": last},
    )()


def _name_row(symbol: str, name: str) -> Any:
    return type("NameRow", (), {"symbol": symbol, "name": name})()


class _FakeResolver:
    def __init__(self, watched: set[tuple[str, str]]) -> None:
        self._w = watched
        self.calls: list[tuple[str, str]] = []

    def relation(self, market: str, symbol: str) -> str:
        self.calls.append((market, symbol))
        return "watchlist" if (market, symbol) in self._w else "none"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_presets_returns_default_selected() -> None:
    resp = build_screener_presets()
    assert len(resp.presets) >= 6
    assert resp.selectedPresetId == "consecutive_gainers"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_consecutive_gainers_happy_path() -> None:
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={"results": _stub_screening_rows(), "warnings": []}
    )
    resolver = _FakeResolver(watched={("kr", "005930")})

    resp: ScreenerResultsResponse = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.presetId == "consecutive_gainers"
    assert resp.title == "연속 상승세"
    assert resp.metricLabel == "주가등락률"
    assert len(resp.results) == 2
    assert resp.results[0].rank == 1
    assert resp.results[0].symbol == "005930"
    assert resp.results[0].marketCapLabel == "478.0조원"
    assert resp.results[0].isWatched is True
    assert resp.results[0].changeDirection == "up"
    assert resp.results[1].symbol == "035720"
    assert resp.results[1].isWatched is False
    assert resp.results[1].changeDirection == "down"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_forwards_us_market_and_formats_us_labels() -> (
    None
):
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "symbol": "AAPL",
                    "name": "Apple Inc.",
                    "market": "us",
                    "sector": "Technology",
                    "market_cap_usd": 3_200_000_000_000,
                    "current_price": 210.4,
                    "change_rate": 1.5,
                    "change_amount": 3.1,
                    "volume": 50_000_000,
                    "per": 32.1,
                    "pbr": 48.0,
                    "dividend_yield": 0.5,
                },
                {
                    "symbol": "msft",
                    "name": "Microsoft Corp.",
                    "market": "us",
                    "sector": "Technology",
                    "market_cap": 2_900_000_000_000,
                    "current_price": 400.0,
                    "change_rate": -0.25,
                    "change_amount": -1.23,
                    "volume": 20_000_000,
                    "per": 30.0,
                },
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched={("us", "AAPL")})

    resp = await build_screener_results(
        # growth_expectation routes via the generic provider (not KR-only, not
        # snapshot-first); cheap_value is now KR-only/snapshot-only after ROB-422 PR2c-1.
        preset_id="growth_expectation",
        screening_service=fake_screening,
        resolver=resolver,
        market="us",
    )

    fake_screening.list_screening.assert_awaited_once()
    assert fake_screening.list_screening.await_args.kwargs["market"] == "us"
    assert resp.results[0].market == "us"
    assert resp.results[0].marketCapLabel == "$3.20T"
    assert resp.results[0].priceLabel == "$210.40"
    assert resp.results[0].changeAmountLabel == "+$3.10"
    assert resp.results[0].isWatched is True
    assert resp.results[1].symbol == "MSFT"
    assert resp.results[1].marketCapLabel == "$2.90T"
    assert resp.results[1].changeAmountLabel == "-$1.23"
    assert resolver.calls == [("us", "AAPL"), ("us", "MSFT")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_presets_returns_crypto_presets() -> None:
    resp = build_screener_presets("crypto")

    assert resp.selectedPresetId == "crypto_high_volume"
    assert [p.id for p in resp.presets] == [
        "crypto_high_volume",
        "crypto_oversold",
        "crypto_momentum",
    ]
    assert all(p.market == "crypto" for p in resp.presets)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_forwards_crypto_market_and_formats_crypto_labels() -> (
    None
):
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "symbol": "KRW-BTC",
                    "name": "Bitcoin",
                    "market": "crypto",
                    "category": "Crypto",
                    "market_cap_usd": 2_100_000_000_000,
                    "current_price": 150_000_000,
                    "change_rate": 2.5,
                    "change_amount": 1_250_000,
                    "trade_amount_24h": 120_000_000_000,
                }
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched={("crypto", "KRW-BTC")})

    resp = await build_screener_results(
        preset_id="crypto_high_volume",
        screening_service=fake_screening,
        resolver=resolver,
        market="crypto",
    )

    fake_screening.list_screening.assert_awaited_once()
    assert fake_screening.list_screening.await_args.kwargs == {
        "market": "crypto",
        "sort_by": "trade_amount",
        "sort_order": "desc",
        "limit": 20,
    }
    row = resp.results[0]
    assert resp.presetId == "crypto_high_volume"
    assert row.market == "crypto"
    assert row.symbol == "KRW-BTC"
    assert row.name == "Bitcoin"
    assert row.priceLabel == "150,000,000원"
    assert row.changeAmountLabel == "+1,250,000원"
    assert row.marketCapLabel == "$2.10T"
    assert row.volumeLabel == "120,000,000,000"
    assert row.metricValueLabel == "120,000,000,000"
    assert row.isWatched is True
    assert resolver.calls == [("crypto", "KRW-BTC")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_uses_fresh_crypto_snapshots_first() -> None:
    fake_screening = type(
        "ScreenerService",
        (_FakeProductionScreening,),
        {"__module__": "app.services.screener_service"},
    )()
    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[date(2026, 5, 13)]),
            _FakeExecuteResult(scalar_rows=[_FakeCryptoSnapshot()]),
            _FakeExecuteResult(scalar_rows=[date(2026, 5, 13)]),
            _FakeExecuteResult(rows=[_coverage_row(latest_count=30)]),
        ]
    )

    resp = await build_screener_results(
        preset_id="crypto_high_volume",
        screening_service=fake_screening,
        resolver=_FakeResolver(watched={("crypto", "KRW-BTC")}),
        market="crypto",
        session=session,
        now=lambda: datetime(2026, 5, 13, 3, 0, tzinfo=UTC),
    )

    fake_screening.list_screening.assert_not_called()
    assert session.calls == 4
    assert resp.freshness.cacheHit is True
    assert resp.freshness.dataState == "fresh"
    row = resp.results[0]
    assert row.symbol == "KRW-BTC"
    assert row.priceLabel == "145,000,000원"
    assert row.metricValueLabel == "123,456,789,000"
    assert row.isWatched is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crypto_snapshot_rows_include_source_risk_and_candidate_context() -> None:
    fake_screening = type(
        "ScreenerService",
        (_FakeProductionScreening,),
        {"__module__": "app.services.screener_service"},
    )()
    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[date(2026, 5, 13)]),
            _FakeExecuteResult(
                scalar_rows=[
                    _FakeCryptoSnapshot(
                        rsi=Decimal("31.5"),
                        trade_amount_24h=Decimal("234000000000"),
                    )
                ]
            ),
            _FakeExecuteResult(scalar_rows=[date(2026, 5, 13)]),
            _FakeExecuteResult(rows=[_coverage_row(latest_count=30)]),
        ]
    )

    resp = await build_screener_results(
        preset_id="crypto_high_volume",
        screening_service=fake_screening,
        resolver=_FakeResolver(watched=set()),
        market="crypto",
        session=session,
        now=lambda: datetime(2026, 5, 13, 3, 0, tzinfo=UTC),
    )

    assert {source.source for source in resp.sources} >= {
        "snapshot_cache",
        "tvscreener_upbit",
    }
    row = resp.results[0]
    assert {source.source for source in row.sourceContext} >= {
        "snapshot_cache",
        "tvscreener_upbit",
    }
    assert any(risk.kind == "low_rsi" for risk in row.riskContext)
    assert row.candidateContext is not None
    assert row.candidateContext.reasons


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crypto_fallback_filters_to_upbit_krw_and_labels_mcp_sources() -> None:
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "symbol": "KRW-BTC",
                    "market": "crypto",
                    "name": "Bitcoin",
                    "current_price": 150_000_000,
                    "change_rate": 2.5,
                    "trade_amount_24h": 120_000_000_000,
                    "source": "tvscreener",
                },
                {
                    "symbol": "BTC/USDT",
                    "market": "crypto",
                    "name": "Bitcoin/Tether",
                    "current_price": 100_000,
                    "change_rate": 1.0,
                    "trade_amount_24h": 1,
                },
                {
                    "symbol": "XRP/KRW",
                    "market": "crypto",
                    "name": "XRP",
                    "current_price": 3_200,
                    "change_rate": 3.0,
                    "trade_amount_24h": 40_000_000_000,
                    "source": "upbit_official",
                },
                {
                    "symbol": "UPBIT:ETHKRW",
                    "market": "crypto",
                    "name": "Ethereum",
                    "current_price": 4_800_000,
                    "change_rate": 0.5,
                    "trade_amount_24h": 50_000_000_000,
                    "market_cap_rank": 2,
                },
            ],
            "warnings": [
                "HTTPSConnectionPool(host='API.UPBIT.COM'): TOKEN=synthetic-credential",
            ],
            "meta": {"source": "tvscreener", "coingecko_cached": True},
        }
    )

    resp = await build_screener_results(
        preset_id="crypto_high_volume",
        screening_service=fake_screening,
        resolver=_FakeResolver(watched=set()),
        market="crypto",
    )

    assert [row.symbol for row in resp.results] == ["KRW-BTC", "KRW-XRP", "KRW-ETH"]
    assert all(row.market == "crypto" for row in resp.results)
    assert any("비KRW" in warning for warning in resp.warnings)
    warning_text = " ".join(resp.warnings)
    assert "api.upbit.com" not in warning_text.lower()
    assert "token=" not in warning_text.lower()
    assert {source.source for source in resp.sources} >= {
        "mcp_screen_stocks",
        "tvscreener_upbit",
        "coingecko_reference",
    }
    assert {source.source for source in resp.results[0].sourceContext} >= {
        "mcp_screen_stocks",
        "tvscreener_upbit",
    }
    assert any(
        source.source == "coingecko_reference" and source.state == "reference_only"
        for source in resp.results[2].sourceContext
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_does_not_fallback_when_fresh_crypto_snapshot_has_no_qualifiers() -> (
    None
):
    fake_screening = type(
        "ScreenerService",
        (_FakeProductionScreening,),
        {"__module__": "app.services.screener_service"},
    )()
    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[date(2026, 5, 13)]),
            _FakeExecuteResult(scalar_rows=[]),
            _FakeExecuteResult(scalar_rows=[date(2026, 5, 13)]),
            _FakeExecuteResult(rows=[_coverage_row(latest_count=30)]),
        ]
    )

    resp = await build_screener_results(
        preset_id="crypto_oversold",
        screening_service=fake_screening,
        resolver=_FakeResolver(watched=set()),
        market="crypto",
        session=session,
        now=lambda: datetime(2026, 5, 13, 3, 0, tzinfo=UTC),
    )

    fake_screening.list_screening.assert_not_called()
    assert resp.results == []
    assert any("암호화폐 스크리너 스냅샷" in w for w in resp.warnings)
    assert resp.freshness.dataState == "fresh"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_falls_back_when_crypto_snapshot_is_stale() -> (
    None
):
    fake_screening = type(
        "ScreenerService",
        (_FakeProductionScreening,),
        {"__module__": "app.services.screener_service"},
    )()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "symbol": "KRW-ETH",
                    "name": "Ethereum",
                    "market": "crypto",
                    "current_price": 4_800_000,
                    "change_rate": 0.5,
                    "trade_amount_24h": 50_000_000_000,
                }
            ],
            "warnings": [],
            "timestamp": datetime(2026, 5, 13, 3, 0, tzinfo=UTC).isoformat(),
            "cache_hit": False,
        }
    )
    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[date(2026, 5, 12)]),
            _FakeExecuteResult(scalar_rows=[]),
            _FakeExecuteResult(scalar_rows=[date(2026, 5, 12)]),
            _FakeExecuteResult(rows=[_coverage_row(latest_count=30)]),
        ]
    )

    resp = await build_screener_results(
        preset_id="crypto_high_volume",
        screening_service=fake_screening,
        resolver=_FakeResolver(watched=set()),
        market="crypto",
        session=session,
        now=lambda: datetime(2026, 5, 13, 3, 0, tzinfo=UTC),
    )

    fake_screening.list_screening.assert_awaited_once()
    assert resp.results[0].symbol == "KRW-ETH"
    assert resp.freshness.cacheHit is False


@pytest.mark.unit
def test_calculate_consecutive_up_days_counts_latest_streak() -> None:
    assert screener_service.calculate_consecutive_up_days([100, 101, 102, 103]) == 3
    assert (
        screener_service.calculate_consecutive_up_days([100, 101, 100, 102, 103]) == 2
    )
    assert screener_service.calculate_consecutive_up_days([100, 101, 101, 102]) == 1
    assert screener_service.calculate_consecutive_up_days([100]) is None


@pytest.mark.unit
def test_consecutive_gainers_metric_is_week_change_rate() -> None:
    label, warnings = screener_service._metric_value_label(
        "consecutive_gainers",
        {
            "symbol": "005930",
            "week_change_rate": 6.0,
            "consecutive_up_days": 5,
            "change_rate": 2.4,
        },
    )
    assert label == "+6.00%"
    assert warnings == []


@pytest.mark.unit
def test_consecutive_gainers_metric_warns_when_week_change_unavailable() -> None:
    label, warnings = screener_service._metric_value_label(
        "consecutive_gainers",
        {"symbol": "005930", "change_rate": 2.4},
    )

    assert label == "-"
    assert warnings == ["주가등락률 데이터 준비중"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_unknown_preset_returns_empty_with_warning() -> (
    None
):
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock()
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="does_not_exist",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.presetId == "does_not_exist"
    assert resp.results == []
    assert resp.warnings, "unknown preset should produce a warning"
    fake_screening.list_screening.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_unavailable_metric_uses_dash_and_warns() -> None:
    """oversold_recovery uses RSI; rows missing rsi must render '-' + warning."""
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "symbol": "035720",
                    "name": "카카오",
                    "market": "kr",
                    "sector": "인터넷",
                    "market_cap_krw": 20_000_000_000_000,
                    "close": 45_000,
                    "change_rate": -0.5,
                    "volume": 3_000_000,
                    "rsi": None,
                }
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="oversold_recovery",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.results[0].metricValueLabel == "-"
    assert any("RSI" in w for w in resp.results[0].warnings)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_screening_warnings_propagate() -> None:
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [],
            "warnings": ["KIS quote service degraded"],
        }
    )
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert "KIS quote service degraded" in resp.warnings
    assert resp.results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_uses_code_when_symbol_missing() -> None:
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "code": "005930",
                    "name": "삼성전자",
                    "market": "kr",
                    "sector": "반도체",
                    "market_cap_krw": 478_000_000_000_000,
                    "close": 80_000,
                    "change_rate": 1.23,
                    "change_amount": 970,
                    "volume": 12_345_678,
                }
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched={("kr", "005930")})

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.results[0].symbol == "005930"
    assert resp.results[0].name == "삼성전자"
    assert resp.results[0].marketCapLabel == "478.0조원"
    assert resp.results[0].isWatched is True
    assert resolver.calls == [("kr", "005930")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_normalizes_kr_exchange_prefixed_symbol() -> None:
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "symbol": "KRX:005930",
                    "market": "kr",
                    "market_cap_krw": 478_000_000_000_000,
                    "change_rate": 1.23,
                    "volume": 12_345_678,
                }
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.results[0].symbol == "005930"
    assert resp.results[0].name == "005930"
    assert resolver.calls == [("kr", "005930")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_uses_market_cap_fallback_when_krw_is_absurd() -> (
    None
):
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "symbol": "005930",
                    "market": "kr",
                    "market_cap_krw": 414_671_400_000_000_000,
                    "market_cap": 4_146_714,
                    "change_rate": 1.23,
                    "volume": 12_345_678,
                }
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.results[0].marketCapLabel == "414.7조원"
    assert "시가총액 단위 보정됨" in resp.results[0].warnings


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_uses_sub_1t_krw_market_cap_fallback() -> None:
    """Over-scaled KRW rows should not hide plausible sub-1조 KRW fallback values."""
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "symbol": "123456",
                    "market": "kr",
                    "market_cap_krw": 80_000_000_000_000_000_000,
                    "market_cap": 800_000_000_000,
                    "change_rate": 1.23,
                    "volume": 12_345_678,
                }
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.results[0].marketCapLabel == "8,000억원"
    assert "시가총액 단위 보정됨" in resp.results[0].warnings


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_dashes_absurd_market_cap_without_fallback() -> (
    None
):
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "symbol": "005930",
                    "market": "kr",
                    "market_cap_krw": 414_671_400_000_000_000,
                    "change_rate": 1.23,
                    "volume": 12_345_678,
                }
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.results[0].marketCapLabel == "-"
    assert "시가총액 데이터 확인 필요" in resp.results[0].warnings


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_keeps_results_contract_preferred_over_stocks() -> (
    None
):
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "code": "005930",
                    "market": "kr",
                    "market_cap_krw": 478_000_000_000_000,
                    "change_rate": 1.23,
                    "volume": 12_345_678,
                }
            ],
            "stocks": [
                {
                    "symbol": "000000",
                    "market": "kr",
                    "market_cap_krw": 1_000_000_000,
                    "change_rate": -1.0,
                    "volume": 1,
                }
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert len(resp.results) == 1
    assert resp.results[0].symbol == "005930"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_warns_when_symbol_missing() -> None:
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "name": "이름만 있는 행",
                    "market": "unsupported",
                    "market_cap_krw": "",
                    "market_cap": object(),
                    "change_rate": None,
                    "volume": None,
                }
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    row = resp.results[0]
    assert row.symbol == ""
    assert row.market == "kr"
    assert row.changePctLabel == "-"
    assert row.changeDirection == "flat"
    assert row.volumeLabel == "-"
    assert "종목코드 데이터 준비중" in row.warnings
    assert resolver.calls == [("kr", "")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_normalizes_us_symbol_and_market_cap() -> None:
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "symbol": "MSFT",
                    "name": "Microsoft",
                    "market": "us",
                    "market_cap": 900_000_000_000,
                    "change_rate": 0,
                    "volume": 10,
                },
                {
                    "symbol": "aapl",
                    "name": "Apple",
                    "market": "us",
                    "market_cap": 2_900_000_000_000,
                    "change_rate": 0,
                    "volume": 10,
                },
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched={("us", "AAPL")})

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    row = resp.results[1]
    assert resp.results[0].symbol == "MSFT"
    assert resp.results[0].marketCapLabel == "$900.0B"
    assert row.symbol == "AAPL"
    assert row.market == "us"
    assert row.marketCapLabel == "$2.90T"
    assert row.changePctLabel == "0.00%"
    assert row.changeDirection == "flat"
    assert row.isWatched is True
    assert resolver.calls == [("us", "MSFT"), ("us", "AAPL")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_coerces_string_market_cap_values() -> None:
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "symbol": "005930",
                    "market": "kr",
                    "market_cap_krw": "478,000,000,000,000",
                    "market_cap": "ignored-invalid",
                    "change_rate": 1.23,
                    "volume": 12_345_678,
                },
                {
                    "symbol": "000660",
                    "market": "kr",
                    "market_cap_krw": "too-large",
                    "market_cap": "4,146,714",
                    "change_rate": 1.0,
                    "volume": 10,
                },
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.results[0].marketCapLabel == "478.0조원"
    assert resp.results[0].warnings == ["주가등락률 데이터 준비중"]
    assert resp.results[1].marketCapLabel == "414.7조원"
    assert resp.results[1].warnings == ["주가등락률 데이터 준비중"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_warns_when_only_market_cap_fallback_is_absurd() -> (
    None
):
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "symbol": "999999",
                    "market": "kr",
                    "market_cap": 20_000_000_000_000_000,
                    "change_rate": 1.23,
                    "volume": 1,
                }
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.results[0].marketCapLabel == "-"
    assert "시가총액 데이터 확인 필요" in resp.results[0].warnings


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preset_id", "field", "value", "expected"),
    [
        # cheap_value/steady_dividend are now snapshot-only (ROB-422 PR2c-1) and no
        # longer route through the generic-provider stub this test drives. Their metric
        # formatting (per / dividend_yield) is covered by other presets/tests.
        ("oversold_recovery", "rsi", 29.94, "29.9"),
        ("kr_high_volume_surge", "volume", 1_234_567, "1,234,567"),
    ],
)
async def test_build_screener_results_formats_preset_metrics(
    preset_id: str, field: str, value: float, expected: str
) -> None:
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "symbol": "005930",
                    "market": "kr",
                    "market_cap_krw": 478_000_000_000_000,
                    "change_rate": 1.23,
                    "volume": value if field == "volume" else 12_345_678,
                    field: value,
                }
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id=preset_id,
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.results[0].metricValueLabel == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_handles_missing_metric_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(
        screener_service._METRIC_FIELD,
        "consecutive_gainers",
    )
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "symbol": "005930",
                    "market": "kr",
                    "market_cap_krw": 478_000_000_000_000,
                    "change_rate": 1.23,
                    "volume": 12_345_678,
                }
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.results[0].metricValueLabel == "-"
    assert resp.results[0].warnings == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_formats_unmapped_metric_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        screener_service._METRIC_FIELD,
        "consecutive_gainers",
        "custom_score",
    )
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "results": [
                {
                    "symbol": "005930",
                    "market": "kr",
                    "market_cap_krw": 478_000_000_000_000,
                    "change_rate": 1.23,
                    "volume": 12_345_678,
                    "custom_score": "A+",
                }
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.results[0].metricValueLabel == "A+"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_emits_freshness_live() -> None:
    fake = MagicMock()
    fake.list_screening = AsyncMock(
        return_value={
            "results": _stub_screening_rows(),
            "warnings": [],
            "timestamp": "2026-05-10T05:30:00+00:00",
            "cache_hit": False,
        }
    )
    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake,
        resolver=_FakeResolver(set()),
        now=lambda: datetime(2026, 5, 10, 5, 42, tzinfo=UTC),
    )
    assert resp.freshness.source == "live"
    assert resp.freshness.cacheHit is False
    assert resp.freshness.asOfLabel == "2026.05.10 14:30 기준"  # KST = UTC+9
    assert resp.freshness.relativeLabel == "12분 전 갱신"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_emits_freshness_cached() -> None:
    fake = MagicMock()
    fake.list_screening = AsyncMock(
        return_value={
            "results": _stub_screening_rows(),
            "warnings": [],
            "timestamp": "2026-05-10T05:30:00+00:00",
            "cache_hit": True,
        }
    )
    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake,
        resolver=_FakeResolver(set()),
        now=lambda: datetime(2026, 5, 10, 5, 31, tzinfo=UTC),
    )
    assert resp.freshness.source == "cached"
    assert resp.freshness.cacheHit is True
    assert resp.freshness.relativeLabel == "방금 갱신"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_emits_freshness_previous_session_when_market_closed() -> (
    None
):
    # Sat 11:00 UTC -> Sat 20:00 KST; KR market closed -> previous_session
    fake = MagicMock()
    fake.list_screening = AsyncMock(
        return_value={
            "results": _stub_screening_rows(),
            "warnings": [],
            "timestamp": "2026-05-08T06:30:00+00:00",  # Fri 15:30 KST close
            "cache_hit": True,
        }
    )
    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake,
        resolver=_FakeResolver(set()),
        market="kr",
        now=lambda: datetime(2026, 5, 10, 11, 0, tzinfo=UTC),  # Sat
    )
    assert resp.freshness.source == "previous_session"
    assert resp.freshness.relativeLabel == "전 거래일 기준"


class _FakeProductionScreening:
    __module__ = "app.services.screener_service"
    __name__ = "ScreenerService"

    def __init__(self) -> None:
        self.list_screening = AsyncMock(
            side_effect=AssertionError("external call should be skipped")
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_uses_snapshots_before_external_screening() -> (
    None
):
    fake_screening = type(
        "ScreenerService",
        (_FakeProductionScreening,),
        {"__module__": "app.services.screener_service"},
    )()
    resolver = _FakeResolver(watched={("kr", "005930")})
    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[date(2026, 5, 11)]),  # MAX(snapshot_date)
            _FakeExecuteResult(
                scalar_rows=[_FakeSnapshot(symbol="005930")]
            ),  # qualifying rows
            _FakeExecuteResult(rows=[_name_row("005930", "삼성전자")]),  # filter names
            _FakeExecuteResult(
                scalar_rows=[_FakeSnapshot(symbol="005930")]
            ),  # enrichment
            _FakeExecuteResult(rows=[_name_row("005930", "삼성전자")]),  # kr_names
        ]
    )

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
        session=session,
    )

    fake_screening.list_screening.assert_not_called()
    assert resp.results[0].symbol == "005930"
    assert resp.results[0].name == "삼성전자"
    assert resp.results[0].metricValueLabel == "+3.50%"
    assert resp.freshness.cacheHit is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_orders_snapshot_rows_by_week_change() -> None:
    fake_screening = type(
        "ScreenerService",
        (_FakeProductionScreening,),
        {"__module__": "app.services.screener_service"},
    )()
    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[date(2026, 5, 11)]),  # MAX(snapshot_date)
            _FakeExecuteResult(
                # SQL ORDER BY week_change_rate DESC produces this order from DB:
                scalar_rows=[
                    _FakeSnapshot(
                        symbol="222222",
                        consecutive_up_days=5,
                        week_change_rate=Decimal("8.0"),
                    ),
                    _FakeSnapshot(
                        symbol="333333",
                        consecutive_up_days=6,
                        week_change_rate=Decimal("3.0"),
                    ),
                    _FakeSnapshot(
                        symbol="111111",
                        consecutive_up_days=9,
                        week_change_rate=Decimal("1.0"),
                    ),
                ]
            ),  # qualifying rows in week_change_rate DESC order (SQL)
            _FakeExecuteResult(
                scalar_rows=[
                    _FakeSnapshot(
                        symbol="222222",
                        consecutive_up_days=5,
                        week_change_rate=Decimal("8.0"),
                    ),
                    _FakeSnapshot(
                        symbol="333333",
                        consecutive_up_days=6,
                        week_change_rate=Decimal("3.0"),
                    ),
                    _FakeSnapshot(
                        symbol="111111",
                        consecutive_up_days=9,
                        week_change_rate=Decimal("1.0"),
                    ),
                ]
            ),  # enrichment
            _FakeExecuteResult(),  # kr_names
        ]
    )

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=_FakeResolver(watched=set()),
        session=session,
    )

    assert [row.symbol for row in resp.results] == ["222222", "333333", "111111"]
    assert [row.metricValueLabel for row in resp.results] == [
        "+8.00%",
        "+3.00%",
        "+1.00%",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_redacts_external_connection_failures() -> None:
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        side_effect=ConnectionError(
            "HTTPSConnectionPool(host='api.finnhub.io', port=443): token=secret Max retries exceeded"
        )
    )
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.results == []
    assert resp.warnings == [
        "외부 시세/스크리너 데이터 소스 연결이 일시적으로 불안정해 캐시된 결과만 표시합니다."
    ]
    assert "api.finnhub" not in " ".join(resp.warnings)
    assert "secret" not in " ".join(resp.warnings)


# ---------------------------------------------------------------------------
# ROB-212: single-partition snapshot regression tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_load_consecutive_gainers_uses_latest_snapshot_partition_only() -> None:
    """Latest partition with no qualifiers must not expose older qualifying rows."""
    from app.services.invest_view_model.screener_service import (
        _load_consecutive_gainers_from_snapshots,
    )

    # MAX(snapshot_date) returns today (2026-05-13); the qualifying rows query
    # for that partition returns empty (the known stale-data scenario: 79 rows
    # exist in older partitions but the latest has 0 qualifiers).
    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[date(2026, 5, 13)]),  # MAX(snapshot_date)
            _FakeExecuteResult(scalar_rows=[]),  # latest partition: 0 qualifying rows
        ]
    )

    result = await _load_consecutive_gainers_from_snapshots(session, market="kr")

    assert result is not None, (
        "None would mean 'could not check'; a _SnapshotLoadResult means 'checked and empty'"
    )
    assert result.rows == [], "must return empty rows, not historical qualifiers"
    assert result.partition_date == date(2026, 5, 13), (
        "partition_date must be set even when rows is empty"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_load_consecutive_gainers_filters_kr_non_common_stock_names() -> None:
    """Snapshot-first KR screener should remove obvious ETF/preferred rows before slicing."""
    from app.services.invest_view_model.screener_service import (
        _load_consecutive_gainers_from_snapshots,
    )

    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[date(2026, 5, 13)]),
            _FakeExecuteResult(
                scalar_rows=[
                    _FakeSnapshot(symbol="069500", week_change_rate=Decimal("10.0")),
                    _FakeSnapshot(symbol="005930", week_change_rate=Decimal("8.0")),
                    _FakeSnapshot(symbol="005935", week_change_rate=Decimal("7.0")),
                    _FakeSnapshot(symbol="000660", week_change_rate=Decimal("6.0")),
                ]
            ),
            _FakeExecuteResult(
                rows=[
                    _name_row("069500", "KODEX 200"),
                    _name_row("005930", "삼성전자"),
                    _name_row("005935", "삼성전자우"),
                    _name_row("000660", "SK하이닉스"),
                ]
            ),
        ]
    )

    result = await _load_consecutive_gainers_from_snapshots(
        session, market="kr", limit=2
    )

    assert result is not None
    assert [row["symbol"] for row in result.rows] == ["005930", "000660"]
    assert [row["name"] for row in result.rows] == ["삼성전자", "SK하이닉스"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_load_consecutive_gainers_returns_none_when_no_snapshot_table_rows() -> (
    None
):
    """When the snapshot table has no rows for the market, returns None (fall through allowed)."""
    from app.services.invest_view_model.screener_service import (
        _load_consecutive_gainers_from_snapshots,
    )

    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[]),  # MAX(snapshot_date) → None
        ]
    )

    result = await _load_consecutive_gainers_from_snapshots(session, market="kr")

    assert result is None, (
        "None signals 'could not check' so external screening may proceed"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_screener_results_warns_and_does_not_fallback_when_latest_partition_empty() -> (
    None
):
    """Stale snapshot (latest partition 0 qualifiers) must warn and NOT surface external results."""
    fake_screening = type(
        "ScreenerService",
        (_FakeProductionScreening,),
        {"__module__": "app.services.screener_service"},
    )()
    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[date(2026, 5, 13)]),  # MAX(snapshot_date)
            _FakeExecuteResult(
                scalar_rows=[]
            ),  # no qualifying rows in latest partition
        ]
    )

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=_FakeResolver(watched=set()),
        session=session,
    )

    fake_screening.list_screening.assert_not_called()
    assert resp.results == []
    assert any("스크리너 스냅샷 업데이트" in w for w in resp.warnings)
    assert resp.freshness.dataState == "stale"


# ---------------------------------------------------------------------------
# ROB-277: investor-flow rows carry snapshot_date / collected_at / classified state
# ---------------------------------------------------------------------------


class _FakeInvestorFlowSnapshot:
    """Minimal fake matching InvestorFlowSnapshot ORM attributes used in
    _load_investor_flow_discovery_from_snapshots."""

    def __init__(self, **kwargs: Any) -> None:
        self.market = kwargs.get("market", "kr")
        self.symbol = kwargs["symbol"]
        self.snapshot_date = kwargs.get("snapshot_date", date(2026, 5, 15))
        self.collected_at = kwargs.get(
            "collected_at", datetime(2026, 5, 15, 6, 30, tzinfo=UTC)
        )
        self.foreign_net = kwargs.get("foreign_net", 500_000_000)
        self.institution_net = kwargs.get("institution_net", -100_000_000)
        self.individual_net = kwargs.get("individual_net", -400_000_000)
        self.foreign_consecutive_buy_days = kwargs.get(
            "foreign_consecutive_buy_days", 3
        )
        self.institution_consecutive_buy_days = kwargs.get(
            "institution_consecutive_buy_days", None
        )
        self.double_buy = kwargs.get("double_buy", False)
        self.foreign_net_buy_rank = kwargs.get("foreign_net_buy_rank", None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_investor_flow_rows_carry_snapshot_date_collected_at_and_classified_state() -> (
    None
):
    """ROB-277: snapshot_date and collected_at must pass through the row dict, and
    _screener_snapshot_state must be the classified result of
    classify_investor_flow_partition — NOT the hardcoded literal 'fresh'.

    We use a snapshot dated 2026-05-15 (Thursday), and "now" is set to
    2026-05-20 (Tuesday) — two trading days later — so the snapshot_date differs
    from today_trading_date("kr") → the state must be "stale".
    """
    from app.services.invest_view_model.screener_service import (
        _load_investor_flow_discovery_from_snapshots,
    )

    stale_snapshot_date = date(2026, 5, 15)
    collected = datetime(2026, 5, 15, 6, 30, tzinfo=UTC)

    session = _FakeSession(
        [
            # MAX(snapshot_date)
            _FakeExecuteResult(scalar_rows=[stale_snapshot_date]),
            # qualifying rows
            _FakeExecuteResult(
                scalar_rows=[
                    _FakeInvestorFlowSnapshot(
                        symbol="005930",
                        snapshot_date=stale_snapshot_date,
                        collected_at=collected,
                        foreign_consecutive_buy_days=3,
                    )
                ]
            ),
            # kr_symbol_universe name rows
            _FakeExecuteResult(rows=[_name_row("005930", "삼성전자")]),
        ]
    )

    load_result = await _load_investor_flow_discovery_from_snapshots(
        session, market="kr", limit=20
    )

    assert load_result is not None, "expected _SnapshotLoadResult, got None"
    assert len(load_result.rows) == 1

    row = load_result.rows[0]

    # snapshot_date passes through
    assert row["snapshot_date"] == stale_snapshot_date, (
        f"expected snapshot_date={stale_snapshot_date!r}, got {row.get('snapshot_date')!r}"
    )

    # collected_at passes through
    assert row["collected_at"] == collected, (
        f"expected collected_at={collected!r}, got {row.get('collected_at')!r}"
    )

    # state must NOT be the hardcoded literal "fresh"; for a snapshot 5 days old
    # it should be "stale"
    state = row["_screener_snapshot_state"]
    assert state != "fresh", (
        f"_screener_snapshot_state must be classified, not hardcoded 'fresh'; got {state!r}"
    )
    assert state == "stale", (
        f"expected 'stale' for snapshot_date={stale_snapshot_date} vs today 2026-05-20; got {state!r}"
    )


# ---------------------------------------------------------------------------
# ROB-277 Task 4: _build_freshness direct unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_freshness_snapshot_first_uses_partition_date_not_now() -> None:
    """ROB-277 D1: snapshot-first response surfaces partition date in asOfLabel,
    not now()."""
    import datetime as dt

    from app.services.invest_view_model.screener_service import _build_freshness

    def fake_now() -> dt.datetime:
        return dt.datetime(2026, 5, 20, 0, 10, tzinfo=dt.UTC)

    f = _build_freshness(
        raw_timestamp="2026-05-20T00:10:00+00:00",
        cache_hit=True,
        market="kr",
        now=fake_now,
        dataState="stale",
        primary_kind="screener_snapshot",
        primary_snapshot_date=dt.date(2026, 5, 13),
        primary_computed_at=None,
        primary_source="invest_screener_snapshots",
        dependency_specs=None,
    )
    assert f.primary is not None
    assert f.primary.kind == "screener_snapshot"
    assert f.primary.snapshotDate == "2026-05-13"
    assert "2026.05.13" in f.primary.asOfLabel
    assert f.primary.dataState == "stale"
    # data-as-of label is the snapshot date, NOT 2026.05.20
    assert "2026.05.13" in f.asOfLabel
    assert "2026.05.20" not in f.asOfLabel
    # served time is now
    assert f.servedAt is not None and f.servedAt.startswith("2026-05-20")
    # source enum unchanged (D2)
    assert f.source == "cached"
    # legacy alias holds (D1.c)
    assert f.dataState == f.overallState
    assert f.overallState == "stale"
    assert f.fetchedAt == f.servedAt  # ROB-277 D1: fetchedAt is a servedAt alias


@pytest.mark.unit
def test_build_freshness_live_path_uses_raw_timestamp() -> None:
    """When primary_kind defaults to 'live', the legacy raw_timestamp drives asOfLabel."""
    import datetime as dt

    from app.services.invest_view_model.screener_service import _build_freshness

    def fake_now() -> dt.datetime:
        return dt.datetime(2026, 5, 20, 0, 10, tzinfo=dt.UTC)

    f = _build_freshness(
        raw_timestamp="2026-05-20T00:08:00+00:00",
        cache_hit=False,
        market="kr",
        now=fake_now,
        dataState="fresh",
    )
    assert f.primary is not None
    assert f.primary.kind == "live"
    assert f.source == "live"
    assert f.dataState == f.overallState == "fresh"


@pytest.mark.unit
def test_build_freshness_dependency_specs_render_with_lag_label() -> None:
    """Dependency with older snapshot_date than primary gets a '{N}일 지연' lagLabel."""
    import datetime as dt

    from app.services.invest_view_model.screener_service import _build_freshness

    def fake_now() -> dt.datetime:
        return dt.datetime(2026, 5, 20, 0, 10, tzinfo=dt.UTC)

    f = _build_freshness(
        raw_timestamp=None,
        cache_hit=True,
        market="kr",
        now=fake_now,
        dataState="fresh",
        primary_kind="screener_snapshot",
        primary_snapshot_date=dt.date(2026, 5, 20),
        primary_computed_at=dt.datetime(2026, 5, 20, 0, 5, tzinfo=dt.UTC),
        primary_source="invest_screener_snapshots",
        dependency_specs=[
            {
                "kind": "investor_flow",
                "snapshot_date": dt.date(2026, 5, 18),
                "collected_at": None,
                "data_state": "stale",
                "source": "investor_flow_snapshots",
            }
        ],
    )
    assert len(f.dependencies) == 1
    dep = f.dependencies[0]
    assert dep.kind == "investor_flow"
    assert dep.snapshotDate == "2026-05-18"
    assert dep.lagLabel == "2일 지연"
    assert dep.dataState == "stale"
    # primary fresh + dep stale → overall stale (D1.c rule 2)
    assert f.overallState == "stale"
    assert f.dataState == "stale"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_consecutive_gainers_response_carries_screener_snapshot_primary() -> None:
    """ROB-277 Task 5: a snapshot-first consecutive_gainers response surfaces
    partition date in freshness.primary, top-level source stays 'cached' (D2),
    and freshness.dataState == overallState (D1.c alias)."""
    import datetime as dt

    fake_screening = type(
        "ScreenerService",
        (_FakeProductionScreening,),
        {"__module__": "app.services.screener_service"},
    )()
    snap_date = dt.date(2026, 5, 13)
    computed = dt.datetime(2026, 5, 13, 0, 30, tzinfo=dt.UTC)

    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[snap_date]),  # MAX(snapshot_date)
            _FakeExecuteResult(
                scalar_rows=[
                    _FakeSnapshot(
                        symbol="005930",
                        snapshot_date=snap_date,
                        computed_at=computed,
                        week_change_rate=Decimal("3.5"),
                    )
                ]
            ),  # qualifying rows
            _FakeExecuteResult(rows=[_name_row("005930", "삼성전자")]),  # filter names
            _FakeExecuteResult(
                scalar_rows=[
                    _FakeSnapshot(
                        symbol="005930",
                        snapshot_date=snap_date,
                        computed_at=computed,
                        week_change_rate=Decimal("3.5"),
                    )
                ]
            ),  # enrichment
            _FakeExecuteResult(rows=[_name_row("005930", "삼성전자")]),  # kr_names
        ]
    )

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=_FakeResolver(watched=set()),
        market="kr",
        session=session,
        now=lambda: dt.datetime(2026, 5, 20, 0, 10, tzinfo=dt.UTC),
    )

    fake_screening.list_screening.assert_not_called()
    f = resp.freshness

    # D1: primary is populated with screener_snapshot kind and partition date
    assert f.primary is not None, "freshness.primary must not be None for snapshot path"
    assert f.primary.kind == "screener_snapshot"
    assert f.primary.source == "invest_screener_snapshots"
    assert f.primary.snapshotDate == "2026-05-13"

    # D1: asOfLabel reflects the partition date, NOT now() (2026.05.20)
    assert "2026.05.13" in f.asOfLabel, (
        f"asOfLabel should reflect partition date 2026.05.13, got: {f.asOfLabel!r}"
    )
    assert "2026.05.20" not in f.asOfLabel, (
        f"asOfLabel must not reflect now(); got: {f.asOfLabel!r}"
    )

    # D2: source enum is "cached" (unchanged)
    assert f.source == "cached"

    # D1.c: dataState is alias for overallState
    assert f.dataState == f.overallState


@pytest.mark.unit
def test_build_freshness_no_change_to_existing_callsite_with_defaults() -> None:
    """Calling _build_freshness with only the pre-ROB-277 kwargs still produces a
    valid ScreenerFreshness (additive change preserves backward compat)."""
    import datetime as dt

    from app.services.invest_view_model.screener_service import _build_freshness

    def fake_now() -> dt.datetime:
        return dt.datetime(2026, 5, 20, 0, 10, tzinfo=dt.UTC)

    f = _build_freshness(
        raw_timestamp="2026-05-20T00:08:00+00:00",
        cache_hit=False,
        market="kr",
        now=fake_now,
        dataState="fresh",
    )
    # All legacy fields populated
    assert f.fetchedAt is not None
    assert f.asOfLabel.startswith("2026.05.")
    assert f.source in {"live", "cached", "previous_session"}
    assert f.cacheHit is False
    assert f.fetchedAt == f.servedAt  # ROB-277 D1: fetchedAt is a servedAt alias


# ROB-276: double_buy preset wiring (Task 3)
# ---------------------------------------------------------------------------


_DOUBLE_BUY_TEST_SYMBOLS = ["921100", "921200", "921300", "922100", "923000"]


@pytest.mark.unit
def test_double_buy_in_metric_field_map() -> None:
    """Task 1 placeholder must remain present so the snapshot row renders a metric."""
    from app.services.invest_view_model.screener_service import _METRIC_FIELD

    assert _METRIC_FIELD["double_buy"] == "change_rate"


@pytest.mark.asyncio
async def test_double_buy_preset_missing_snapshot_reports_missing_state() -> None:
    """When neither snapshot partition exists, dataState must report missing.

    The loader returns None when MAX(snapshot_date) is NULL, and the safety net
    in build_screener_results pins dataState to "missing" with a warning. A
    MagicMock session whose .execute returns scalar_one_or_none()==None covers
    this without touching the shared DB.
    """
    null_scalar = MagicMock()
    null_scalar.scalar_one_or_none.return_value = None
    session = MagicMock()
    session.execute = AsyncMock(return_value=null_scalar)

    fake_screening = type(
        "ScreenerService",
        (_FakeProductionScreening,),
        {"__module__": "app.services.screener_service"},
    )()

    resp = await build_screener_results(
        preset_id="double_buy",
        screening_service=fake_screening,
        resolver=_FakeResolver(watched=set()),
        session=session,
    )

    fake_screening.list_screening.assert_not_called()
    assert resp.results == []
    assert resp.freshness.dataState == "missing"
    assert any("스냅샷" in w for w in resp.warnings)


@pytest.mark.asyncio
async def test_double_buy_preset_returns_snapshot_filtered_rows(db_session) -> None:
    """Seed flow+price snapshots; build_screener_results must surface qualifiers.

    Uses the same persistent-DB pattern as
    tests/test_invest_view_model_double_buy_screener.py with a synthetic
    92xxxx symbol range so other suites are not affected.
    """
    import datetime as _dt
    import decimal as _dec

    import sqlalchemy as _sa

    from app.models.invest_screener_snapshot import InvestScreenerSnapshot
    from app.models.investor_flow_snapshot import InvestorFlowSnapshot
    from app.models.kr_symbol_universe import KRSymbolUniverse

    # Far-future date wins the latest-partition lookup against shared test data.
    today = _dt.date(2099, 12, 30)

    async def _purge() -> None:
        await db_session.execute(
            _sa.delete(InvestorFlowSnapshot).where(
                InvestorFlowSnapshot.symbol.in_(_DOUBLE_BUY_TEST_SYMBOLS)
            )
        )
        await db_session.execute(
            _sa.delete(InvestScreenerSnapshot).where(
                InvestScreenerSnapshot.symbol.in_(_DOUBLE_BUY_TEST_SYMBOLS)
            )
        )
        await db_session.execute(
            _sa.delete(KRSymbolUniverse).where(
                KRSymbolUniverse.symbol.in_(_DOUBLE_BUY_TEST_SYMBOLS)
            )
        )
        await db_session.commit()

    await _purge()
    try:
        db_session.add_all(
            [
                KRSymbolUniverse(
                    symbol="921100",
                    name="더블바이주A",
                    exchange="KOSPI",
                    is_active=True,
                ),
                KRSymbolUniverse(
                    symbol="921200",
                    name="더블바이주B",
                    exchange="KOSPI",
                    is_active=True,
                ),
                KRSymbolUniverse(
                    symbol="921300",
                    name="더블셀주",
                    exchange="KOSPI",
                    is_active=True,
                ),
            ]
        )
        db_session.add_all(
            [
                InvestorFlowSnapshot(
                    market="kr",
                    symbol="921100",
                    snapshot_date=today,
                    foreign_net=1_000_000,
                    institution_net=2_000_000,
                    double_buy=True,
                    double_sell=False,
                    source="naver_finance",
                ),
                InvestorFlowSnapshot(
                    market="kr",
                    symbol="921200",
                    snapshot_date=today,
                    foreign_net=500_000,
                    institution_net=600_000,
                    double_buy=True,
                    double_sell=False,
                    source="naver_finance",
                ),
                InvestorFlowSnapshot(
                    market="kr",
                    symbol="921300",
                    snapshot_date=today,
                    foreign_net=-1,
                    institution_net=-1,
                    double_buy=False,
                    double_sell=True,
                    source="naver_finance",
                ),
            ]
        )
        db_session.add_all(
            [
                InvestScreenerSnapshot(
                    market="kr",
                    symbol="921100",
                    snapshot_date=today,
                    latest_close=_dec.Decimal("12000"),
                    prev_close=_dec.Decimal("10000"),
                    change_rate=_dec.Decimal("20.0"),
                    daily_volume=100_000,
                    closes_window=[10000, 12000],
                    source="kis",
                ),
                InvestScreenerSnapshot(
                    market="kr",
                    symbol="921200",
                    snapshot_date=today,
                    latest_close=_dec.Decimal("11000"),
                    prev_close=_dec.Decimal("10000"),
                    change_rate=_dec.Decimal("10.0"),
                    daily_volume=80_000,
                    closes_window=[10000, 11000],
                    source="kis",
                ),
                InvestScreenerSnapshot(
                    market="kr",
                    symbol="921300",
                    snapshot_date=today,
                    latest_close=_dec.Decimal("900"),
                    prev_close=_dec.Decimal("1000"),
                    change_rate=_dec.Decimal("-10.0"),
                    daily_volume=50_000,
                    closes_window=[1000, 900],
                    source="kis",
                ),
            ]
        )
        await db_session.commit()

        fake_screening = type(
            "ScreenerService",
            (_FakeProductionScreening,),
            {"__module__": "app.services.screener_service"},
        )()

        resp = await build_screener_results(
            preset_id="double_buy",
            screening_service=fake_screening,
            resolver=_FakeResolver(watched=set()),
            session=db_session,
            now=lambda: datetime(2099, 12, 30, 6, 0, tzinfo=UTC),
        )

        fake_screening.list_screening.assert_not_called()
        symbols = [row.symbol for row in resp.results]
        # Both double_buy positives must appear; double_sell must not.
        assert "921100" in symbols
        assert "921200" in symbols
        assert "921300" not in symbols
        # SQL ORDER BY change_rate DESC — 921100 (20%) must outrank 921200 (10%).
        assert symbols.index("921100") < symbols.index("921200")
        # change_rate metric should render with a + sign for positive rows.
        target = next(r for r in resp.results if r.symbol == "921100")
        assert target.metricValueLabel == "+20.00%"
    finally:
        await _purge()


@pytest.mark.asyncio
async def test_double_buy_preset_stale_when_price_snapshot_older_than_flow(
    db_session,
) -> None:
    """When price snapshot lags the flow snapshot, dataState must downshift to stale.

    The loader tags the row's _screener_snapshot_state="stale" when
    price_snapshot_date != flow_snapshot_date; the view-model split-freshness
    helper turns that into a user-visible "1일 지연" warning and a stale state.
    """
    import datetime as _dt
    import decimal as _dec

    import sqlalchemy as _sa

    from app.models.invest_screener_snapshot import InvestScreenerSnapshot
    from app.models.investor_flow_snapshot import InvestorFlowSnapshot
    from app.models.kr_symbol_universe import KRSymbolUniverse

    flow_date = _dt.date(2099, 12, 29)
    price_date = _dt.date(2099, 12, 30)  # latest partition is the price side
    symbol = "922100"

    async def _purge() -> None:
        await db_session.execute(
            _sa.delete(InvestorFlowSnapshot).where(
                InvestorFlowSnapshot.symbol.in_(_DOUBLE_BUY_TEST_SYMBOLS)
            )
        )
        await db_session.execute(
            _sa.delete(InvestScreenerSnapshot).where(
                InvestScreenerSnapshot.symbol.in_(_DOUBLE_BUY_TEST_SYMBOLS)
            )
        )
        await db_session.execute(
            _sa.delete(KRSymbolUniverse).where(
                KRSymbolUniverse.symbol.in_(_DOUBLE_BUY_TEST_SYMBOLS)
            )
        )
        await db_session.commit()

    await _purge()
    try:
        db_session.add(
            KRSymbolUniverse(
                symbol=symbol,
                name="지연테스트주",
                exchange="KOSPI",
                is_active=True,
            )
        )
        db_session.add(
            InvestorFlowSnapshot(
                market="kr",
                symbol=symbol,
                snapshot_date=flow_date,
                foreign_net=1,
                institution_net=1,
                double_buy=True,
                double_sell=False,
                source="naver_finance",
            )
        )
        db_session.add(
            InvestScreenerSnapshot(
                market="kr",
                symbol=symbol,
                snapshot_date=price_date,
                latest_close=_dec.Decimal("10500"),
                prev_close=_dec.Decimal("10000"),
                change_rate=_dec.Decimal("5.0"),
                daily_volume=1,
                closes_window=[9000, 9500, 9800, 9900, 10500],
                source="kis",
            )
        )
        await db_session.commit()

        fake_screening = type(
            "ScreenerService",
            (_FakeProductionScreening,),
            {"__module__": "app.services.screener_service"},
        )()

        resp = await build_screener_results(
            preset_id="double_buy",
            screening_service=fake_screening,
            resolver=_FakeResolver(watched=set()),
            session=db_session,
            # `now` set to the price date so today_trading_date returns 12-30 KST.
            now=lambda: datetime(2099, 12, 30, 6, 0, tzinfo=UTC),
        )

        fake_screening.list_screening.assert_not_called()
        assert any(r.symbol == symbol for r in resp.results), (
            "the symbol must still appear; staleness lives on freshness/warning"
        )
        assert resp.freshness.dataState in {"stale", "fallback"}
        # Split warning: price snapshot stale message
        assert any("시세 스냅샷" in w and "1일 지연" in w for w in resp.warnings), (
            f"expected price-side stale warning in {resp.warnings}"
        )
    finally:
        await _purge()


@pytest.mark.asyncio
async def test_double_buy_preset_flow_stale_warning_when_all_flow_dates_in_past(
    db_session,
) -> None:
    """Flow-stale branch fires even when price/flow dates match but pre-date today.

    The loader tags _screener_snapshot_state="fresh" because price==flow, so the
    price-side warning must NOT appear. But because the flow snapshot date is
    strictly older than today's market date, the flow-side "1일 지연" warning
    must fire — proving the helper is keying off flow_snapshot_date and not
    accidentally reusing the price snapshot date.
    """
    import datetime as _dt
    import decimal as _dec

    import sqlalchemy as _sa

    from app.models.invest_screener_snapshot import InvestScreenerSnapshot
    from app.models.investor_flow_snapshot import InvestorFlowSnapshot
    from app.models.kr_symbol_universe import KRSymbolUniverse

    past_dt = _dt.date(2099, 12, 30)  # snapshot dates (both partitions)
    # today's market date will be 2099-12-31 (Thursday) — see now() below.
    symbol = "923000"

    async def _purge() -> None:
        await db_session.execute(
            _sa.delete(InvestorFlowSnapshot).where(
                InvestorFlowSnapshot.symbol.in_(_DOUBLE_BUY_TEST_SYMBOLS)
            )
        )
        await db_session.execute(
            _sa.delete(InvestScreenerSnapshot).where(
                InvestScreenerSnapshot.symbol.in_(_DOUBLE_BUY_TEST_SYMBOLS)
            )
        )
        await db_session.execute(
            _sa.delete(KRSymbolUniverse).where(
                KRSymbolUniverse.symbol.in_(_DOUBLE_BUY_TEST_SYMBOLS)
            )
        )
        await db_session.commit()

    await _purge()
    try:
        db_session.add(
            KRSymbolUniverse(
                symbol=symbol,
                name="플로우스테일테스트",
                exchange="KOSPI",
                is_active=True,
            )
        )
        db_session.add(
            InvestorFlowSnapshot(
                market="kr",
                symbol=symbol,
                snapshot_date=past_dt,
                foreign_net=1,
                institution_net=1,
                double_buy=True,
                double_sell=False,
                source="naver_finance",
            )
        )
        db_session.add(
            InvestScreenerSnapshot(
                market="kr",
                symbol=symbol,
                snapshot_date=past_dt,
                latest_close=_dec.Decimal("10000"),
                prev_close=_dec.Decimal("9000"),
                change_rate=_dec.Decimal("11.0"),
                daily_volume=1,
                closes_window=[9000, 9500, 9800, 9900, 10000],
                source="kis",
            )
        )
        await db_session.commit()

        fake_screening = type(
            "ScreenerService",
            (_FakeProductionScreening,),
            {"__module__": "app.services.screener_service"},
        )()

        # now() at 2099-12-31 06:00 UTC = 15:00 KST → today_trading_date("kr") = 2099-12-31
        # Flow/price snapshot dates are 2099-12-30 → strictly < market date.
        resp = await build_screener_results(
            preset_id="double_buy",
            screening_service=fake_screening,
            resolver=_FakeResolver(watched=set()),
            session=db_session,
            now=lambda: datetime(2099, 12, 31, 6, 0, tzinfo=UTC),
        )

        fake_screening.list_screening.assert_not_called()
        flow_warning = (
            "수급 스냅샷이 직전 영업일 기준이라 외인/기관 정보가 1일 지연되었습니다."
        )
        assert flow_warning in resp.warnings, (
            f"expected flow-side stale warning in {resp.warnings}"
        )
        # price==flow ⇒ loader tagged _screener_snapshot_state="fresh", so the
        # price-side warning must NOT appear.
        assert not any("시세 스냅샷" in w for w in resp.warnings), (
            f"price-stale warning must not appear when price==flow date: {resp.warnings}"
        )
        # state_override from helper bumps fresh → stale.
        assert resp.freshness.dataState in {"stale", "fallback"}
    finally:
        await _purge()


# ---------------------------------------------------------------------------
# ROB-277 follow-up FU6: integration tests for FU2 (dependency split) and
# FU3 (0-qualifier primary metadata threading)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_consecutive_gainers_fresh_primary_with_stale_investor_flow_dependency() -> (
    None
):
    """ROB-277 follow-up FU2/FU6: when primary partition is today's and an
    investor_flow snapshot is 2 trading days older, freshness.dependencies must
    surface that as a 'stale' investor_flow entry with the correct snapshotDate
    and lagLabel, and overallState must be 'stale' per D1.c rule 2."""
    import datetime as dt

    today_kr = dt.date(2026, 5, 20)  # Wednesday
    inv_partition = dt.date(2026, 5, 18)  # Monday — 2 trading days older

    primary_computed = dt.datetime(2026, 5, 20, 0, 5, tzinfo=dt.UTC)

    fake_screening = type(
        "ScreenerService",
        (_FakeProductionScreening,),
        {"__module__": "app.services.screener_service"},
    )()

    from app.schemas.investor_flow import InvestorFlowItem

    inv_item = InvestorFlowItem(
        symbol="005930",
        market="kr",
        dataState="stale",
        snapshotDate=inv_partition,
        collectedAt=dt.datetime(2026, 5, 18, 7, 30, tzinfo=dt.UTC),
        foreignNet=1_000_000_000,
        institutionNet=500_000_000,
        individualNet=-1_500_000_000,
        doubleBuy=False,
        doubleSell=False,
        foreignConsecutiveBuyDays=4,
        foreignConsecutiveSellDays=None,
        institutionConsecutiveBuyDays=None,
        institutionConsecutiveSellDays=None,
        individualConsecutiveBuyDays=None,
        individualConsecutiveSellDays=None,
    )

    from unittest.mock import patch

    # Query sequence for consecutive_gainers snapshot-first path with 1 qualifying row:
    # Q1: MAX(snapshot_date) in _load_consecutive_gainers_from_snapshots
    # Q2: qualifying snapshot rows
    # Q3: kr_symbol_universe names for filtering (candidate_snaps non-empty)
    # Q4: enrichment via _hydrate_from_snapshots → repo.get_fresh
    # Q5: kr_names bulk-lookup in build_screener_results
    # _hydrate_investor_flow_chips → patched, no DB query
    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[today_kr]),  # Q1: MAX(snapshot_date)
            _FakeExecuteResult(
                scalar_rows=[
                    _FakeSnapshot(
                        symbol="005930",
                        snapshot_date=today_kr,
                        computed_at=primary_computed,
                        week_change_rate=Decimal("3.5"),
                    )
                ]
            ),  # Q2: qualifying rows
            _FakeExecuteResult(
                rows=[_name_row("005930", "삼성전자")]
            ),  # Q3: filter names
            _FakeExecuteResult(
                scalar_rows=[
                    _FakeSnapshot(
                        symbol="005930",
                        snapshot_date=today_kr,
                        computed_at=primary_computed,
                        week_change_rate=Decimal("3.5"),
                    )
                ]
            ),  # Q4: enrichment
            _FakeExecuteResult(rows=[_name_row("005930", "삼성전자")]),  # Q5: kr_names
        ]
    )

    async def _fake_latest_items(*, db, symbols, market="kr"):
        return {"005930": inv_item}

    with patch(
        "app.services.invest_view_model.screener_service._latest_investor_flow_items",
        side_effect=_fake_latest_items,
    ):
        resp = await build_screener_results(
            preset_id="consecutive_gainers",
            screening_service=fake_screening,
            resolver=_FakeResolver(watched=set()),
            market="kr",
            session=session,
            now=lambda: dt.datetime(2026, 5, 20, 0, 10, tzinfo=dt.UTC),
        )

    f = resp.freshness
    # Primary points at today (fresh)
    assert f.primary is not None
    assert f.primary.snapshotDate == today_kr.isoformat()
    assert f.primary.dataState == "fresh"
    # Dependency surfaces the investor-flow partition (NOT the primary date)
    assert len(f.dependencies) == 1
    dep = f.dependencies[0]
    assert dep.kind == "investor_flow"
    assert dep.snapshotDate == inv_partition.isoformat()
    assert dep.dataState == "stale"
    # lagLabel reflects the 2-day gap (calendar diff between today and inv_partition)
    assert dep.lagLabel is not None and "일 지연" in dep.lagLabel
    # D1.c rule 2: primary fresh + dep stale → overall stale
    assert f.overallState == "stale"
    assert f.dataState == "stale"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_consecutive_gainers_zero_qualifiers_still_threads_partition_metadata() -> (
    None
):
    """ROB-277 follow-up FU3/FU6: when the latest partition has no qualifying
    rows (consecutive_up_days >= 5 + week_change_rate >= 0 filter excludes all),
    freshness.primary must still carry the partition's snapshotDate so the UI
    can render '데이터 기준 {date}' rather than collapsing to live/now()."""
    import datetime as dt

    partition_date = dt.date(2026, 5, 13)  # stale

    fake_screening = type(
        "ScreenerService",
        (_FakeProductionScreening,),
        {"__module__": "app.services.screener_service"},
    )()

    # Query sequence when latest partition has 0 qualifying rows:
    # Q1: MAX(snapshot_date) in _load_consecutive_gainers_from_snapshots → partition_date
    # Q2: qualifying rows (consecutive_up_days>=5, week_change_rate>=0) → empty
    # No name query (candidate_snaps is empty)
    # No enrichment (rows is empty)
    # No kr_names lookup (rows is empty)
    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[partition_date]),  # Q1: MAX(snapshot_date)
            _FakeExecuteResult(scalar_rows=[]),  # Q2: qualifying rows: NONE
        ]
    )

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=_FakeResolver(watched=set()),
        market="kr",
        session=session,
        now=lambda: dt.datetime(2026, 5, 20, 0, 10, tzinfo=dt.UTC),
    )

    fake_screening.list_screening.assert_not_called()
    assert resp.results == []
    # Snapshot was checked, partition metadata threaded through:
    f = resp.freshness
    assert f.primary is not None, "primary must not be None even with 0 qualifier rows"
    assert f.primary.kind == "screener_snapshot"
    assert f.primary.snapshotDate == partition_date.isoformat()
    assert f.primary.source == "invest_screener_snapshots"
    # asOfLabel reflects the partition, not now()
    assert "2026.05.13" in f.asOfLabel
    assert "2026.05.20" not in f.asOfLabel
    # source enum: cached (snapshot was checked)
    assert f.source == "cached"


def test_crypto_candidate_context_matches_builder_labels():
    from app.services.invest_view_model.screener_service import (
        _crypto_candidate_context,
    )

    row = {
        "symbol": "KRW-BTC",
        "source": "tvscreener_upbit",
        "change_rate": 4.2,
        "rsi": 40.0,
        "trade_amount_24h": 123456,
    }
    ctx = _crypto_candidate_context(row, "crypto_momentum")
    assert ctx is not None
    assert ctx.scoreLabel == "+4.20%"
    assert ctx.reasons == ["단기 상승 모멘텀 후보"]
    assert ctx.source == "tvscreener_upbit"
