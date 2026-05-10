"""ROB-147 — view-model tests for build_screener_results."""

from __future__ import annotations

from datetime import UTC, datetime
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
    assert resp.metricLabel == "연속상승"
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
        preset_id="cheap_value",
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
def test_calculate_consecutive_up_days_counts_latest_streak() -> None:
    assert screener_service.calculate_consecutive_up_days([100, 101, 102, 103]) == 3
    assert (
        screener_service.calculate_consecutive_up_days([100, 101, 100, 102, 103]) == 2
    )
    assert screener_service.calculate_consecutive_up_days([100, 101, 101, 102]) == 1
    assert screener_service.calculate_consecutive_up_days([100]) is None


@pytest.mark.unit
def test_consecutive_up_metric_prefers_consecutive_days() -> None:
    row_warnings: list[str] = []
    assert (
        screener_service._metric_value_label(
            "consecutive_gainers",
            {"symbol": "005930", "consecutive_up_days": 6, "change_rate": 2.4},
        )[0]
        == "6일"
    )
    assert row_warnings == []


@pytest.mark.unit
def test_consecutive_up_metric_warns_when_history_unavailable() -> None:
    label, warnings = screener_service._metric_value_label(
        "consecutive_gainers",
        {"symbol": "005930", "change_rate": 2.4},
    )

    assert label == "-"
    assert warnings == ["연속상승 데이터 준비중"]


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
    assert resp.results[0].warnings == ["연속상승 데이터 준비중"]
    assert resp.results[1].marketCapLabel == "414.7조원"
    assert resp.results[1].warnings == ["연속상승 데이터 준비중"]


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
        ("cheap_value", "per", 8.25, "8.2"),
        ("steady_dividend", "dividend_yield", 3.456, "3.46%"),
        ("oversold_recovery", "rsi", 29.94, "29.9"),
        ("high_volume_momentum", "volume", 1_234_567, "1,234,567"),
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
