from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.mcp_server.tooling.analysis_screen_core import (
    _screen_crypto_via_tvscreener,
)
from app.services.tvscreener_service import (
    TvScreenerError,
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
async def test_finalize_crypto_screen_forces_rsi_sort_to_asc() -> None:
    from app.mcp_server.tooling.analysis_screen_crypto import finalize_crypto_screen

    result = await finalize_crypto_screen(
        candidates=[{"symbol": "KRW-BTC", "rsi": 45.0, "rsi_bucket": 45}],
        filters_applied={
            "market": "crypto",
            "sort_by": "rsi",
            "sort_order": "desc",
        },
        market="crypto",
        limit=20,
        max_rsi=None,
        warnings=[],
        rsi_enrichment={
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "timeout": 0,
            "rate_limited": 0,
            "error_samples": [],
        },
        coingecko_payload={
            "data": {},
            "cached": True,
            "age_seconds": 0.0,
            "stale": False,
            "error": None,
        },
        total_markets=1,
        top_by_volume=1,
        filtered_by_warning=0,
        filtered_by_crash=0,
    )

    assert result["filters_applied"]["sort_order"] == "asc"
    assert result["warnings"] == [
        "crypto sort_by='rsi' always uses ascending order; requested desc was ignored."
    ]
    assert result["meta"]["filtered_by_warning"] == 0
    assert result["meta"]["filtered_by_crash"] == 0


def test_to_optional_float_treats_nan_strings_as_missing() -> None:
    from app.mcp_server.tooling.analysis_screen_crypto import _to_optional_float

    assert _to_optional_float("nan") is None
    assert _to_optional_float(float("nan")) is None


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
            "app.mcp_server.tooling.screening.crypto._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.TvScreenerService",
            return_value=service,
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.upbit_service.fetch_multiple_tickers",
            new=fetch_multiple_tickers,
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.get_upbit_market_display_names",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.get_upbit_warning_markets",
            new=AsyncMock(return_value=set()),
        ),
        patch.object(
            __import__(
                "app.mcp_server.tooling.screening.crypto",
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
    assert result["results"][0]["adx"] == pytest.approx(18.7)
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
            "app.mcp_server.tooling.screening.crypto._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.TvScreenerService",
            return_value=service,
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.upbit_service.fetch_multiple_tickers",
            new=fetch_multiple_tickers,
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.get_upbit_market_display_names",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.get_upbit_warning_markets",
            new=AsyncMock(return_value=set()),
        ),
        patch.object(
            __import__(
                "app.mcp_server.tooling.screening.crypto",
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
    assert first["volume_24h"] == pytest.approx(777.0)


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
            "app.mcp_server.tooling.screening.crypto._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.TvScreenerService",
            return_value=service,
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.upbit_service.fetch_multiple_tickers",
            new=fetch_multiple_tickers,
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.get_upbit_market_display_names",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.get_upbit_warning_markets",
            new=AsyncMock(return_value=set()),
        ),
        patch.object(
            __import__(
                "app.mcp_server.tooling.screening.crypto",
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
async def test_screen_crypto_via_tvscreener_coerces_rsi_desc_before_query(
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
            "app.mcp_server.tooling.screening.crypto._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.TvScreenerService",
            return_value=service,
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.upbit_service.fetch_multiple_tickers",
            new=fetch_multiple_tickers,
        ),
        patch.object(
            __import__(
                "app.mcp_server.tooling.screening.crypto",
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
            sort_by="rsi",
            sort_order="desc",
            limit=1,
        )

    kwargs = service.query_crypto_screener.await_args.kwargs
    assert (
        kwargs["sort_by"]
        == fake_tvscreener_module.CryptoField.RELATIVE_STRENGTH_INDEX_14
    )
    assert kwargs["ascending"] is True
    assert result["filters_applied"]["sort_order"] == "asc"
    assert (
        "crypto sort_by='rsi' always uses ascending order; requested desc was ignored."
        in result["warnings"]
    )


@pytest.mark.asyncio
async def test_finalize_crypto_screen_preserves_existing_market_cap_when_coingecko_is_null() -> (
    None
):
    from app.mcp_server.tooling.analysis_screen_crypto import finalize_crypto_screen

    result = await finalize_crypto_screen(
        candidates=[
            {
                "symbol": "KRW-BTC",
                "original_market": "KRW-BTC",
                "market_cap": 2_500_000_000_000_000.0,
                "market_cap_rank": None,
                "rsi": 45.0,
                "rsi_bucket": 45,
                "trade_amount_24h": 900_000_000_000.0,
            }
        ],
        filters_applied={
            "market": "crypto",
            "sort_by": "trade_amount",
            "sort_order": "desc",
        },
        market="crypto",
        limit=20,
        max_rsi=None,
        warnings=[],
        rsi_enrichment={
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "timeout": 0,
            "rate_limited": 0,
            "error_samples": [],
        },
        coingecko_payload={
            "data": {"BTC": {"market_cap": None, "market_cap_rank": 1}},
            "cached": True,
            "age_seconds": 0.0,
            "stale": False,
            "error": None,
        },
        total_markets=1,
        top_by_volume=1,
        filtered_by_warning=0,
        filtered_by_crash=0,
    )

    first = result["results"][0]
    assert first["market_cap"] == 2_500_000_000_000_000.0
    assert first["market_cap_rank"] == 1


@pytest.mark.asyncio
async def test_screen_crypto_fallback_removed_propagates_error():
    """tvscreener 실패 시 legacy fallback 없이 예외가 전파되어야 함."""
    from app.mcp_server.tooling.screening.crypto import _screen_crypto_with_fallback

    with patch(
        "app.mcp_server.tooling.screening.crypto._screen_crypto_via_tvscreener"
    ) as mock_tvscreener:
        mock_tvscreener.side_effect = TvScreenerError("screener query failed")

        with pytest.raises(TvScreenerError, match="screener query failed"):
            await _screen_crypto_with_fallback(
                market="crypto",
                asset_type=None,
                category=None,
                min_market_cap=None,
                max_per=None,
                min_dividend_yield=None,
                max_rsi=None,
                sort_by="trade_amount",
                sort_order="desc",
                limit=10,
            )
