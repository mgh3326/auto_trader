"""Unit tests for InvestQuoteService."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.services.invest_quote_service import InvestQuoteService


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_kr_prices() -> None:
    # Mock client and market data
    kis_client = MagicMock()
    db = MagicMock()

    service = InvestQuoteService(kis_client, db)

    # Mock MarketDataClient.inquire_price
    service._market_data = AsyncMock()

    # Mock return value: DataFrame indexed by code
    df = pd.DataFrame([{"close": 70000.0}], index=["005930"])
    service._market_data.inquire_price.return_value = df

    prices = await service.fetch_kr_prices(["005930"])

    assert prices == pytest.approx({"005930": 70000.0})
    service._market_data.inquire_price.assert_called_once_with("005930", market="J")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_us_prices_uses_live_last_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-708: US /invest price must come from inquire_overseas_price
    (HHDFS00000300, live last) — NOT inquire_overseas_daily_price (daily close)."""
    kis_client = MagicMock()
    db = MagicMock()

    service = InvestQuoteService(kis_client, db)

    mock_get_exchange = AsyncMock(return_value="NASD")
    monkeypatch.setattr(
        "app.services.invest_quote_service.get_us_exchange_by_symbol", mock_get_exchange
    )

    service._market_data = AsyncMock()
    # inquire_overseas_price returns the single-row live-last frame
    # ([close, previous_close, volume]); close == live `last`.
    live_df = pd.DataFrame([{"close": 150.0, "previous_close": 148.0, "volume": 1000}])
    service._market_data.inquire_overseas_price.return_value = live_df

    prices = await service.fetch_us_prices(["AAPL"])

    assert prices == pytest.approx({"AAPL": 150.0})
    mock_get_exchange.assert_called_once_with("AAPL", db)
    service._market_data.inquire_overseas_price.assert_called_once_with(
        "AAPL", exchange_code="NASD"
    )
    # The daily-close endpoint must no longer be used for a "current price".
    service._market_data.inquire_overseas_daily_price.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_kr_prices_falls_back_to_toss_then_snapshot(monkeypatch):
    from decimal import Decimal

    from app.services.brokers.toss.dto import TossPrice

    kis_client = MagicMock()
    db = MagicMock()

    class _FakeToss:  # injected via 3rd param -> exercises Toss layer w/o network
        async def prices(self, symbols):
            return [
                TossPrice(
                    symbol="B", timestamp=None, last_price=Decimal("20"), currency="KRW"
                )
                for s in symbols
                if s == "B"
            ]

    service = InvestQuoteService(kis_client, db, toss_client=_FakeToss())

    # KIS: A ok, B/C fail
    service._market_data = AsyncMock()

    async def _inquire(code, market="J"):
        if code == "A":
            return pd.DataFrame([{"close": 10.0}], index=["A"])
        raise RuntimeError("KIS down")

    service._market_data.inquire_price.side_effect = _inquire
    # snapshot resolves C (Toss had nothing for C)
    service._snapshot_latest = AsyncMock(return_value={"C": 30.0})

    out = await service.fetch_kr_prices(["A", "B", "C"])
    assert out == pytest.approx({"A": 10.0, "B": 20.0, "C": 30.0})
    service._snapshot_latest.assert_awaited_once_with("kr", ["C"])


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_us_prices_toss_disabled_uses_snapshot(monkeypatch):
    monkeypatch.setattr(
        "app.services.invest_quote_service.settings.toss_api_enabled",
        False,
        raising=False,
    )
    kis_client = MagicMock()
    db = MagicMock()
    service = InvestQuoteService(kis_client, db)  # no toss_client, disabled

    service._market_data = AsyncMock()
    service._market_data.inquire_overseas_price.side_effect = RuntimeError("KIS down")
    monkeypatch.setattr(
        "app.services.invest_quote_service.get_us_exchange_by_symbol",
        AsyncMock(return_value="NASD"),
    )
    service._snapshot_latest = AsyncMock(return_value={"AAPL": 222.5})

    out = await service.fetch_us_prices(["AAPL"])
    assert out == pytest.approx({"AAPL": 222.5})
    service._snapshot_latest.assert_awaited_once_with("us", ["AAPL"])


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_kr_prices_all_layers_down_returns_none(monkeypatch):
    kis_client = MagicMock()
    db = MagicMock()
    service = InvestQuoteService(kis_client, db)  # toss disabled by default
    service._market_data = AsyncMock()
    service._market_data.inquire_price.side_effect = RuntimeError("KIS down")
    service._snapshot_latest = AsyncMock(return_value={})  # no snapshot

    out = await service.fetch_kr_prices(["A", "B"])
    assert out == {"A": None, "B": None}  # never raises


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_kr_prices_toss_enabled_but_misconfigured_is_fail_open(
    monkeypatch,
):
    # Toss enabled but from_settings() raises (empty creds). Construction runs
    # OUTSIDE _resolve's try/finally, so it MUST be guarded — the call must fall
    # through to snapshot and never propagate out of fetch_kr_prices.
    monkeypatch.setattr(
        "app.services.invest_quote_service.settings.toss_api_enabled",
        True,
        raising=False,
    )

    def _boom(*_a, **_k):
        raise RuntimeError("TOSS_API_CLIENT_SECRET is empty")

    monkeypatch.setattr(
        "app.services.invest_quote_service.TossReadClient.from_settings", _boom
    )
    kis_client = MagicMock()
    db = MagicMock()
    service = InvestQuoteService(kis_client, db)  # no injected toss_client
    service._market_data = AsyncMock()
    service._market_data.inquire_price.side_effect = RuntimeError("KIS down")
    service._snapshot_latest = AsyncMock(return_value={"A": 11.0})

    out = await service.fetch_kr_prices(["A"])
    assert out == pytest.approx({"A": 11.0})  # snapshot filled, never raised
    service._snapshot_latest.assert_awaited_once_with("kr", ["A"])


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_us_prices_empty_live_last_falls_through_to_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-708: when KIS live-last has no price (market closed / after-hours),
    inquire_overseas_price returns an EMPTY frame -> None -> the resolver must
    fall through to snapshot rather than surfacing a stale daily close."""
    monkeypatch.setattr(
        "app.services.invest_quote_service.settings.toss_api_enabled",
        False,
        raising=False,
    )
    kis_client = MagicMock()
    db = MagicMock()
    service = InvestQuoteService(kis_client, db)  # toss disabled

    monkeypatch.setattr(
        "app.services.invest_quote_service.get_us_exchange_by_symbol",
        AsyncMock(return_value="NASD"),
    )
    service._market_data = AsyncMock()
    # Empty frame == _build_overseas_price_frame's "last is None / <= 0" case.
    service._market_data.inquire_overseas_price.return_value = pd.DataFrame(
        columns=["close", "previous_close", "volume"]
    )
    service._snapshot_latest = AsyncMock(return_value={"AAPL": 199.0})

    out = await service.fetch_us_prices(["AAPL"])

    assert out == pytest.approx({"AAPL": 199.0})  # from snapshot, not KIS daily
    service._snapshot_latest.assert_awaited_once_with("us", ["AAPL"])
