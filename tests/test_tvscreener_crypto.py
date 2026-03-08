from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.mcp_server.tooling.analysis_screen_core import (
    _enrich_crypto_indicators,
    _screen_crypto_via_tvscreener,
)
from app.services.tvscreener_service import (
    TvScreenerRateLimitError,
    TvScreenerTimeoutError,
)


class _Condition:
    def __init__(self, label: str) -> None:
        self.label = label

    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        return isinstance(other, _Condition) and self.label == other.label

    def __and__(self, other: object) -> object:
        raise AssertionError("crypto filters must not be combined with '&'")


class _Field:
    def __init__(self, label: str) -> None:
        self.label = label

    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        return cast(bool, cast(object, _Condition(f"{self.label}=={other}")))

    def isin(self, other: object) -> _Condition:
        values = list(cast(Any, other))
        return _Condition(f"{self.label} in {values}")


@pytest.fixture
def fake_tvscreener_module() -> SimpleNamespace:
    return SimpleNamespace(
        CryptoField=SimpleNamespace(
            NAME=_Field("name"),
            DESCRIPTION=_Field("description"),
            PRICE=_Field("price"),
            CHANGE_PERCENT=_Field("change_percent"),
            RELATIVE_STRENGTH_INDEX_14=_Field("rsi14"),
            AVERAGE_DIRECTIONAL_INDEX_14=_Field("adx14"),
            VOLUME_24H_IN_USD=_Field("volume24h"),
            VALUE_TRADED=_Field("value_traded"),
            MARKET_CAP=_Field("market_cap"),
            EXCHANGE=_Field("exchange"),
        )
    )


@pytest.fixture
def crypto_candidates() -> list[dict[str, object]]:
    return [
        {
            "market": "crypto",
            "original_market": "KRW-BTC",
            "symbol": "KRW-BTC",
            "name": "BTC",
            "rsi": None,
        },
        {
            "market": "crypto",
            "original_market": "KRW-ETH",
            "symbol": "KRW-ETH",
            "name": "ETH",
            "rsi": None,
        },
        {
            "market": "crypto",
            "original_market": "KRW-XRP",
            "symbol": "KRW-XRP",
            "name": "XRP",
            "rsi": None,
        },
    ]


@pytest.fixture
def normalized_crypto_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["UPBIT:BTCKRW", "UPBIT:ETHKRW", "UPBIT:XRPKRW"],
            "name": ["BTCKRW", "ETHKRW", "XRPKRW"],
            "description": ["Bitcoin", "Ethereum", "XRP"],
            "price": [150_000_000.0, 5_000_000.0, 3_000.0],
            "change_percent": [1.5, -0.2, 3.2],
            "relative_strength_index_14": [45.5, 32.1, 68.9],
            "average_directional_index_14": [25.3, 18.7, 42.1],
            "volume_24h_in_usd": [156_000_000.0, 95_000_000.0, 44_000_000.0],
            "value_traded": [900_000_000_000.0, 1_200_000_000_000.0, 700_000_000_000.0],
            "market_cap": [
                2_500_000_000_000_000.0,
                1_200_000_000_000_000.0,
                500_000_000_000_000.0,
            ],
            "exchange": ["UPBIT", "UPBIT", "UPBIT"],
        }
    )


@pytest.mark.asyncio
async def test_enrich_crypto_indicators_uses_upbit_bulk_query(
    crypto_candidates: list[dict[str, object]],
    normalized_crypto_df: pd.DataFrame,
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_crypto_screener.return_value = normalized_crypto_df

    with (
        patch(
            "app.mcp_server.tooling.analysis_screen_core._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService",
            return_value=service,
        ),
    ):
        diagnostics = await _enrich_crypto_indicators(crypto_candidates)

    service.query_crypto_screener.assert_awaited_once()
    kwargs = service.query_crypto_screener.await_args.kwargs
    assert kwargs["limit"] == 300
    assert kwargs["columns"] == [
        fake_tvscreener_module.CryptoField.NAME,
        fake_tvscreener_module.CryptoField.RELATIVE_STRENGTH_INDEX_14,
        fake_tvscreener_module.CryptoField.AVERAGE_DIRECTIONAL_INDEX_14,
        fake_tvscreener_module.CryptoField.VOLUME_24H_IN_USD,
    ]
    assert kwargs["where_clause"] == [
        fake_tvscreener_module.CryptoField.EXCHANGE == "UPBIT",
        fake_tvscreener_module.CryptoField.NAME.isin(["BTCKRW", "ETHKRW", "XRPKRW"]),
    ]
    assert crypto_candidates[0]["rsi"] == 45.5
    assert crypto_candidates[0]["adx"] == 25.3
    assert crypto_candidates[0]["volume_24h"] == 156_000_000.0
    assert crypto_candidates[1]["rsi"] == 32.1
    assert diagnostics == {
        "attempted": 3,
        "succeeded": 3,
        "failed": 0,
        "rate_limited": 0,
        "timeout": 0,
        "error_samples": [],
    }


@pytest.mark.asyncio
async def test_enrich_crypto_indicators_applies_partial_matches_only(
    crypto_candidates: list[dict[str, object]],
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_crypto_screener.return_value = pd.DataFrame(
        {
            "symbol": ["UPBIT:BTCKRW", "UPBIT:ETHKRW", "BINANCE:XRPUSDT"],
            "relative_strength_index_14": [40.0, 35.0, 20.0],
            "average_directional_index_14": [20.0, 18.0, 10.0],
            "volume_24h_in_usd": [1.0, 2.0, 3.0],
        }
    )

    with (
        patch(
            "app.mcp_server.tooling.analysis_screen_core._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService",
            return_value=service,
        ),
    ):
        diagnostics = await _enrich_crypto_indicators(crypto_candidates)

    assert crypto_candidates[0]["rsi"] == 40.0
    assert crypto_candidates[1]["rsi"] == 35.0
    assert crypto_candidates[2]["rsi"] is None
    assert diagnostics["succeeded"] == 2
    assert diagnostics["failed"] == 1


@pytest.mark.asyncio
async def test_enrich_crypto_indicators_handles_empty_results(
    crypto_candidates: list[dict[str, object]],
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_crypto_screener.return_value = pd.DataFrame()

    with (
        patch(
            "app.mcp_server.tooling.analysis_screen_core._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService",
            return_value=service,
        ),
    ):
        diagnostics = await _enrich_crypto_indicators(crypto_candidates)

    assert all(candidate["rsi"] is None for candidate in crypto_candidates)
    assert diagnostics["failed"] == 3
    assert diagnostics["succeeded"] == 0


@pytest.mark.asyncio
async def test_enrich_crypto_indicators_handles_rate_limit(
    crypto_candidates: list[dict[str, object]],
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_crypto_screener.side_effect = TvScreenerRateLimitError("rate limit")

    with (
        patch(
            "app.mcp_server.tooling.analysis_screen_core._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService",
            return_value=service,
        ),
    ):
        diagnostics = await _enrich_crypto_indicators(crypto_candidates)

    assert diagnostics["rate_limited"] == 3
    assert diagnostics["failed"] == 0


@pytest.mark.asyncio
async def test_enrich_crypto_indicators_handles_timeout(
    crypto_candidates: list[dict[str, object]],
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_crypto_screener.side_effect = TvScreenerTimeoutError("timeout")

    with (
        patch(
            "app.mcp_server.tooling.analysis_screen_core._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService",
            return_value=service,
        ),
    ):
        diagnostics = await _enrich_crypto_indicators(crypto_candidates)

    assert diagnostics["timeout"] == 3
    assert diagnostics["failed"] == 0


@pytest.mark.asyncio
async def test_screen_crypto_via_tvscreener_uses_upbit_value_traded_contract(
    normalized_crypto_df: pd.DataFrame,
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_crypto_screener.return_value = normalized_crypto_df
    fetch_multiple_tickers = AsyncMock(
        return_value=[
            {"market": "KRW-BTC", "acc_trade_volume_24h": 15_600.0},
            {"market": "KRW-ETH", "acc_trade_volume_24h": 9_500.0},
            {"market": "KRW-XRP", "acc_trade_volume_24h": 4_400.0},
        ]
    )
    get_market_caps = AsyncMock(
        return_value={
            "data": {},
            "cached": True,
            "age_seconds": 0.0,
            "stale": False,
            "error": None,
        }
    )

    with (
        patch(
            "app.mcp_server.tooling.analysis_screen_core._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService",
            return_value=service,
        ),
        patch(
            "app.mcp_server.tooling.analysis_screen_core.upbit_service.fetch_multiple_tickers",
            new=fetch_multiple_tickers,
        ),
        patch.object(
            __import__(
                "app.mcp_server.tooling.analysis_screen_core",
                fromlist=["_CRYPTO_MARKET_CAP_CACHE"],
            )._CRYPTO_MARKET_CAP_CACHE,
            "get",
            new=get_market_caps,
        ),
    ):
        result = await _screen_crypto_via_tvscreener(
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="trade_amount",
            sort_order="desc",
            limit=2,
        )

    kwargs = service.query_crypto_screener.await_args.kwargs
    assert kwargs["columns"] == [
        fake_tvscreener_module.CryptoField.NAME,
        fake_tvscreener_module.CryptoField.DESCRIPTION,
        fake_tvscreener_module.CryptoField.PRICE,
        fake_tvscreener_module.CryptoField.CHANGE_PERCENT,
        fake_tvscreener_module.CryptoField.VALUE_TRADED,
        fake_tvscreener_module.CryptoField.MARKET_CAP,
        fake_tvscreener_module.CryptoField.RELATIVE_STRENGTH_INDEX_14,
        fake_tvscreener_module.CryptoField.AVERAGE_DIRECTIONAL_INDEX_14,
        fake_tvscreener_module.CryptoField.VOLUME_24H_IN_USD,
    ]
    assert kwargs["where_clause"] == [
        fake_tvscreener_module.CryptoField.EXCHANGE == "UPBIT",
    ]
    assert result["meta"]["source"] == "tvscreener"
    assert [item["symbol"] for item in result["results"]] == ["KRW-ETH", "KRW-BTC"]
    assert result["results"][0]["trade_amount_24h"] == 1_200_000_000_000.0
    assert result["results"][0]["volume_24h"] == 9_500.0
    assert result["results"][0]["adx"] == 18.7
    assert result["results"][0]["market_cap"] == 1_200_000_000_000_000.0


@pytest.mark.asyncio
async def test_screen_crypto_via_tvscreener_prefers_value_traded_over_usd_volume(
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_crypto_screener.return_value = pd.DataFrame(
        {
            "symbol": ["UPBIT:BTCKRW"],
            "name": ["BTCKRW"],
            "description": ["Bitcoin"],
            "price": [150_000_000.0],
            "change_percent": [1.5],
            "relative_strength_index_14": [45.5],
            "average_directional_index_14": [25.3],
            "value_traded": [900_000_000_000.0],
            "market_cap": [2_500_000_000_000_000.0],
            "volume_24h_in_usd": [1.0],
            "exchange": ["UPBIT"],
        }
    )
    fetch_multiple_tickers = AsyncMock(
        return_value=[{"market": "KRW-BTC", "acc_trade_volume_24h": 777.0}]
    )
    get_market_caps = AsyncMock(
        return_value={
            "data": {},
            "cached": True,
            "age_seconds": 0.0,
            "stale": False,
            "error": None,
        }
    )

    with (
        patch(
            "app.mcp_server.tooling.analysis_screen_core._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService",
            return_value=service,
        ),
        patch(
            "app.mcp_server.tooling.analysis_screen_core.upbit_service.fetch_multiple_tickers",
            new=fetch_multiple_tickers,
        ),
        patch.object(
            __import__(
                "app.mcp_server.tooling.analysis_screen_core",
                fromlist=["_CRYPTO_MARKET_CAP_CACHE"],
            )._CRYPTO_MARKET_CAP_CACHE,
            "get",
            new=get_market_caps,
        ),
    ):
        result = await _screen_crypto_via_tvscreener(
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="trade_amount",
            sort_order="desc",
            limit=1,
        )

    first = result["results"][0]
    assert first["symbol"] == "KRW-BTC"
    assert first["trade_amount_24h"] == 900_000_000_000.0
    assert first["volume_24h"] == 777.0


@pytest.mark.asyncio
async def test_screen_crypto_via_tvscreener_sorts_by_market_cap_field(
    normalized_crypto_df: pd.DataFrame,
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_crypto_screener.return_value = normalized_crypto_df
    fetch_multiple_tickers = AsyncMock(
        return_value=[
            {"market": "KRW-BTC", "acc_trade_volume_24h": 1.0},
            {"market": "KRW-ETH", "acc_trade_volume_24h": 1.0},
            {"market": "KRW-XRP", "acc_trade_volume_24h": 1.0},
        ]
    )
    get_market_caps = AsyncMock(
        return_value={
            "data": {},
            "cached": True,
            "age_seconds": 0.0,
            "stale": False,
            "error": None,
        }
    )

    with (
        patch(
            "app.mcp_server.tooling.analysis_screen_core._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService",
            return_value=service,
        ),
        patch(
            "app.mcp_server.tooling.analysis_screen_core.upbit_service.fetch_multiple_tickers",
            new=fetch_multiple_tickers,
        ),
        patch.object(
            __import__(
                "app.mcp_server.tooling.analysis_screen_core",
                fromlist=["_CRYPTO_MARKET_CAP_CACHE"],
            )._CRYPTO_MARKET_CAP_CACHE,
            "get",
            new=get_market_caps,
        ),
    ):
        result = await _screen_crypto_via_tvscreener(
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="market_cap",
            sort_order="desc",
            limit=3,
        )

    assert service.query_crypto_screener.await_args.kwargs["sort_by"] == (
        fake_tvscreener_module.CryptoField.MARKET_CAP
    )
    assert [item["symbol"] for item in result["results"]] == [
        "KRW-BTC",
        "KRW-ETH",
        "KRW-XRP",
    ]


@pytest.mark.asyncio
async def test_enrich_crypto_indicators_manual_fallback_uses_upbit_keys(
    crypto_candidates: list[dict[str, object]],
) -> None:
    with (
        patch(
            "app.mcp_server.tooling.analysis_screen_core._import_tvscreener",
            side_effect=ImportError,
        ),
        patch(
            "app.mcp_server.tooling.analysis_screen_core.compute_crypto_realtime_rsi_map",
            new=AsyncMock(
                return_value={"KRW-BTC": 41.2, "KRW-ETH": 37.4, "KRW-XRP": 55.1}
            ),
        ),
    ):
        diagnostics = await _enrich_crypto_indicators(crypto_candidates)

    assert [candidate["rsi"] for candidate in crypto_candidates] == [41.2, 37.4, 55.1]
    assert diagnostics["succeeded"] == 3


class TestCryptoScreeningIntegration:
    @pytest.mark.integration
    @pytest.mark.live
    @pytest.mark.asyncio
    async def test_enrich_real_symbols(self) -> None:
        pytest.importorskip("tvscreener")

        candidates = [
            {
                "market": "crypto",
                "original_market": "KRW-BTC",
                "symbol": "KRW-BTC",
                "name": "BTC",
                "rsi": None,
            },
            {
                "market": "crypto",
                "original_market": "KRW-ETH",
                "symbol": "KRW-ETH",
                "name": "ETH",
                "rsi": None,
            },
        ]

        diagnostics = await _enrich_crypto_indicators(candidates)

        assert diagnostics["attempted"] == 2
        assert diagnostics["succeeded"] >= 1
        assert any(candidate.get("rsi") is not None for candidate in candidates)
        for candidate in candidates:
            rsi = candidate.get("rsi")
            if rsi is not None:
                assert 0.0 <= float(rsi) <= 100.0
