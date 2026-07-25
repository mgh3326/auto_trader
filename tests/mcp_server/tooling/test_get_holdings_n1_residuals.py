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
async def test_get_holdings_missing_crypto_instrument_still_gets_stop_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-830 defect 1 regression.

    Stop-loss is an indicator-independent safety signal (profit_rate only).
    A position whose crypto instrument id can't be resolved (unseeded
    symbol) must still get a stop-loss signal — only the RSI/voting
    *enrichment* is allowed to be skipped, matching the pre-batch behavior
    where a per-position lookup failure fell back to
    rsi_14=None/voting_result=None instead of dropping the signal outright.
    """
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
    assert position["strategy_signal"] == {
        "action": "sell",
        "reason": "stop_loss",
        "threshold_pct": -4.5,
    }
    # Indicator-dependent enrichment (RSI/voting) is still correctly
    # skipped for the unresolved instrument id — only the RED->GREEN
    # requirement here is that the indicator-independent signal survives.
    compute.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_holdings_batch_resolver_db_error_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-830 defect 2 regression.

    A batch resolver DB error (e.g. connection drop) must not propagate out
    of ``_get_holdings_impl`` — the caller expects holdings back even when
    the crypto instrument-id lookup fails. Indicator-independent signals
    (stop-loss) must still be computed from the raw positions.
    """
    positions = [
        _crypto_position("KRW-BTC", -6.0),
        _crypto_position("KRW-ETH", 10.0),
    ]
    monkeypatch.setattr(
        portfolio_holdings,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(positions, [], "crypto", "upbit")),
    )
    monkeypatch.setattr(
        portfolio_holdings,
        "_resolve_crypto_instrument_ids_for_holdings",
        AsyncMock(side_effect=RuntimeError("db unavailable")),
    )
    compute = AsyncMock()
    monkeypatch.setattr(
        portfolio_holdings, "_compute_crypto_signals_for_position", compute
    )

    result = await portfolio_holdings._get_holdings_impl(
        account="upbit", market="crypto", minimum_value=0
    )

    by_symbol = {row["symbol"]: row for row in result["accounts"][0]["positions"]}
    assert by_symbol["KRW-BTC"]["strategy_signal"] == {
        "action": "sell",
        "reason": "stop_loss",
        "threshold_pct": -4.5,
    }
    # KRW-ETH is profitable and has no RSI (indicator path skipped), so it
    # gets no signal — but the call must not have raised.
    assert "strategy_signal" not in by_symbol["KRW-ETH"]
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


def _kr_kis_snapshot_position(symbol: str = "005930") -> dict[str, object]:
    """A KIS-account KR holding whose balance snapshot is numerically complete.

    ``_collect_kis_positions`` fills these four fields in one bulk balance call
    (``prpr`` / ``evlu_amt`` / ``evlu_pfls_amt`` / ``evlu_pfls_rt``). ROB-902:
    such a holding must NOT trigger the per-symbol itemchartprice refresh —
    mirroring the pre-existing US snapshot exemption (PR #288 / ROB-365).
    """
    return {
        "instrument_type": "equity_kr",
        "symbol": symbol,
        "source": "kis_api",
        "current_price": 62_000.0,
        "evaluation_amount": 620_000.0,
        "profit_loss": 20_000.0,
        "profit_rate": 3.33,
    }


@pytest.mark.asyncio
async def test_kr_kis_snapshot_skips_price_refresh_and_makes_zero_kis_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-902: valid KIS-account KR snapshot fans out 0 DB reads and 0 KIS HTTP."""
    db_read = AsyncMock(return_value=None)
    kis_quote = AsyncMock(return_value={"price": 61_000.0})
    monkeypatch.setattr(portfolio_holdings, "cache_first_kr", db_read)
    monkeypatch.setattr(portfolio_holdings, "_fetch_quote_equity_kr", kis_quote)

    actual = await portfolio_holdings._fetch_price_map_for_positions(
        [_kr_kis_snapshot_position()]
    )

    # No equity pair enqueued -> empty price map, no errors.
    assert actual == ({}, [], {})
    db_read.assert_not_awaited()
    kis_quote.assert_not_awaited()


def test_kr_kis_snapshot_is_exempt_from_refresh() -> None:
    """ROB-902: the refresh predicate exempts a complete KIS-account KR snapshot."""
    assert (
        portfolio_holdings._position_needs_current_price_refresh(
            _kr_kis_snapshot_position()
        )
        is False
    )


def test_kr_kis_incomplete_snapshot_still_refreshes() -> None:
    """A KIS-account KR holding with a zero/absent price still needs a refresh."""
    incomplete = _kr_kis_snapshot_position()
    incomplete["current_price"] = None
    assert portfolio_holdings._position_needs_current_price_refresh(incomplete) is True


def test_kr_non_kis_snapshot_still_refreshes() -> None:
    """A manual/Toss KR holding (source != kis_api) is never exempt."""
    manual = _kr_kis_snapshot_position()
    manual["source"] = "manual"
    assert portfolio_holdings._position_needs_current_price_refresh(manual) is True


def test_us_kis_snapshot_remains_exempt() -> None:
    """Regression: the pre-existing US snapshot exemption is preserved."""
    us = _kr_kis_snapshot_position("AAPL")
    us["instrument_type"] = "equity_us"
    assert portfolio_holdings._position_needs_current_price_refresh(us) is False
