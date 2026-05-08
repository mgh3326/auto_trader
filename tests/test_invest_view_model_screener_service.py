"""ROB-147 — view-model tests for build_screener_results."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.invest_screener import ScreenerResultsResponse
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
