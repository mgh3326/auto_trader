"""ROB-830 — get_holdings residual N+1 regressions.

The holdings fan-out historically resolved one ``crypto_instruments`` row per
crypto position and made one ``inquire-daily-itemchartprice`` HTTP call per
KR equity position. These tests lock the request-scoped batch resolver and
the DB-first KR enrichment so the N+1s cannot regress silently.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.mcp_server.tooling import portfolio_holdings


def _crypto_position(symbol: str, profit_rate: float) -> dict[str, object]:
    price = 100_000.0
    return {
        "account": "upbit",
        "account_name": "Upbit Main",
        "broker": "upbit",
        "source": "upbit_api",
        "instrument_type": "crypto",
        "market": "crypto",
        "symbol": symbol,
        "name": symbol,
        "quantity": 1.0,
        "avg_buy_price": price / (1.0 + profit_rate / 100.0),
        "current_price": price,
        "evaluation_amount": price,
        "profit_loss": price - price / (1.0 + profit_rate / 100.0),
        "profit_rate": profit_rate,
    }


@pytest.mark.asyncio
async def test_get_holdings_resolves_crypto_instruments_once_and_keeps_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    positions = [
        _crypto_position("KRW-BTC", -6.0),
        _crypto_position("KRW-ETH", 10.0),
    ]
    collect = AsyncMock(return_value=(positions, [], "crypto", "upbit"))
    resolve = AsyncMock(return_value={"KRW-BTC": 101, "KRW-ETH": 202})

    async def compute(position, *, instrument_id):
        assert instrument_id in {101, 202}
        return (50.0 if position["symbol"] == "KRW-ETH" else 35.0, None)

    monkeypatch.setattr(portfolio_holdings, "_collect_portfolio_positions", collect)
    monkeypatch.setattr(
        portfolio_holdings, "_resolve_crypto_instrument_ids_for_holdings", resolve
    )
    monkeypatch.setattr(
        portfolio_holdings, "_compute_crypto_signals_for_position", compute
    )

    result = await portfolio_holdings._get_holdings_impl(
        account="upbit", market="crypto", minimum_value=0
    )

    resolve.assert_awaited_once_with(positions)
    by_symbol = {row["symbol"]: row for row in result["accounts"][0]["positions"]}
    assert by_symbol["KRW-BTC"]["strategy_signal"] == {
        "action": "sell",
        "reason": "stop_loss",
        "threshold_pct": -4.5,
    }
    assert by_symbol["KRW-ETH"]["strategy_signal"] == {
        "action": "sell",
        "reason": "mean_reversion_exit",
        "rsi_14": 50.0,
    }


@pytest.mark.asyncio
async def test_get_holdings_missing_crypto_instrument_keeps_position_without_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    positions = [_crypto_position("KRW-NOT-SEEDED", -6.0)]
    monkeypatch.setattr(
        portfolio_holdings,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(positions, [], "crypto", "upbit")),
    )
    monkeypatch.setattr(
        portfolio_holdings,
        "_resolve_crypto_instrument_ids_for_holdings",
        AsyncMock(return_value={}),
    )
    compute = AsyncMock()
    monkeypatch.setattr(
        portfolio_holdings, "_compute_crypto_signals_for_position", compute
    )

    result = await portfolio_holdings._get_holdings_impl(
        account="upbit", market="crypto", minimum_value=0
    )

    position = result["accounts"][0]["positions"][0]
    assert position["symbol"] == "KRW-NOT-SEEDED"
    assert "strategy_signal" not in position
    compute.assert_not_awaited()


def _kr_refresh_position(symbol: str = "005930") -> dict[str, object]:
    return {
        "instrument_type": "equity_kr",
        "symbol": symbol,
        "source": "manual",
        "current_price": None,
        "evaluation_amount": None,
        "profit_loss": None,
        "profit_rate": None,
    }


@pytest.mark.asyncio
async def test_kr_price_enrichment_db_hit_matches_legacy_result_and_skips_kis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-07-09", periods=2, freq="D"),
            "open": [60_000.0, 61_000.0],
            "high": [62_000.0, 63_000.0],
            "low": [59_000.0, 60_000.0],
            "close": [61_500.0, 62_000.0],
            "volume": [1_000.0, 1_100.0],
            "value": [61_500_000.0, 68_200_000.0],
        }
    )
    db_read = AsyncMock(return_value=db_frame)
    kis_quote = AsyncMock(return_value={"price": 62_000.0})
    monkeypatch.setattr(portfolio_holdings, "cache_first_kr", db_read)
    monkeypatch.setattr(portfolio_holdings, "_fetch_quote_equity_kr", kis_quote)

    actual = await portfolio_holdings._fetch_price_map_for_positions(
        [_kr_refresh_position()]
    )

    assert actual == (
        {("equity_kr", "005930"): 62_000.0},
        [],
        {},
    )
    db_read.assert_awaited_once_with("005930", 2)
    kis_quote.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cached",
    [None, pd.DataFrame(), pd.DataFrame({"close": [None]})],
)
async def test_kr_price_enrichment_db_miss_falls_back_to_legacy_kis_result(
    monkeypatch: pytest.MonkeyPatch,
    cached: pd.DataFrame | None,
) -> None:
    db_read = AsyncMock(return_value=cached)
    kis_quote = AsyncMock(return_value={"price": 62_000.0})
    monkeypatch.setattr(portfolio_holdings, "cache_first_kr", db_read)
    monkeypatch.setattr(portfolio_holdings, "_fetch_quote_equity_kr", kis_quote)

    actual = await portfolio_holdings._fetch_price_map_for_positions(
        [_kr_refresh_position()]
    )

    assert actual == ({("equity_kr", "005930"): 62_000.0}, [], {})
    kis_quote.assert_awaited_once_with("005930")
