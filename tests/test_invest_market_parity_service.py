from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services.invest_view_model.market_parity_service import (
    ParityQuote,
    build_market_parity,
)

_AS_OF = datetime(2026, 5, 14, 0, 0, tzinfo=UTC)


class _StubParityProvider:
    def __init__(
        self, *, fail_proxy: bool = False, include_synthetic: bool = True
    ) -> None:
        self.fail_proxy = fail_proxy
        self.include_synthetic = include_synthetic

    async def get_index_quote(self, symbol: str) -> ParityQuote | None:
        assert symbol == "KOSPI"
        return ParityQuote(
            symbol=symbol, price=Decimal("100"), source="fixture", as_of=_AS_OF
        )

    async def get_proxy_quote(self, symbol: str) -> ParityQuote | None:
        if self.fail_proxy:
            raise RuntimeError("proxy fixture unavailable")
        assert symbol == "EWY"
        return ParityQuote(
            symbol=symbol, price=Decimal("10"), source="fixture", as_of=_AS_OF
        )

    async def get_fx_rate(self, pair: str) -> ParityQuote | None:
        assert pair == "USD/KRW"
        return ParityQuote(
            symbol=pair, price=Decimal("11"), source="fixture", as_of=_AS_OF
        )

    async def get_stablecoin_rate(self, pair: str) -> ParityQuote | None:
        assert pair == "USDT/KRW"
        return ParityQuote(
            symbol=pair, price=Decimal("11.55"), source="fixture", as_of=_AS_OF
        )

    async def get_crypto_kimchi_premium(self, symbol: str) -> dict[str, Any] | None:
        assert symbol == "BTC"
        return {"symbol": "BTC", "premium_pct": 2.41, "as_of": _AS_OF.isoformat()}

    async def get_synthetic_quote(self, symbol: str) -> ParityQuote | None:
        if not self.include_synthetic:
            return None
        prices = {"xyz:SMSN": Decimal("8"), "xyz:SKHX": Decimal("5")}
        return ParityQuote(
            symbol=symbol, price=prices[symbol], source="fixture", as_of=_AS_OF
        )

    async def get_kr_stock_quote(self, symbol: str) -> ParityQuote | None:
        prices = {"005930": Decimal("88"), "000660": Decimal("60")}
        return ParityQuote(
            symbol=symbol, price=prices[symbol], source="fixture", as_of=_AS_OF
        )


class _ApprovalGatedProvider(_StubParityProvider):
    def __init__(self) -> None:
        super().__init__(include_synthetic=False)

    async def get_proxy_quote(self, symbol: str) -> ParityQuote | None:
        _ = symbol
        return None

    async def get_fx_rate(self, pair: str) -> ParityQuote | None:
        _ = pair
        return None

    async def get_stablecoin_rate(self, pair: str) -> ParityQuote | None:
        _ = pair
        return None

    async def get_kr_stock_quote(self, symbol: str) -> ParityQuote | None:
        _ = symbol
        return None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_market_parity_calculates_stubbed_cards() -> None:
    response = await build_market_parity(_StubParityProvider())

    assert response.state == "fresh"
    cards = {card.id: card for card in response.cards}

    index = cards["ewy-kospi-implied-parity"]
    assert index.dataState == "fresh"
    assert index.basePrice == 100
    assert index.proxyPrice == 10
    assert index.fxRate == 11
    assert index.impliedValue == 110
    assert index.premiumPct == 10
    assert index.tone == "premium"

    stablecoin = cards["usdt-krw-usd-krw-premium"]
    assert stablecoin.dataState == "fresh"
    assert stablecoin.usdKrw == 11
    assert stablecoin.usdtKrw == 11.55
    assert stablecoin.premiumPct == 5

    sms = cards["005930-xyz-smsn"]
    assert sms.syntheticSymbol == "xyz:SMSN"
    assert sms.basePrice == 88
    assert sms.syntheticPrice == 8
    assert sms.impliedValue == 88
    assert sms.premiumPct == 0
    assert sms.tone == "flat"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_market_parity_defaults_to_approval_gated_missing_cards() -> None:
    response = await build_market_parity(_ApprovalGatedProvider())

    assert response.state == "partial"
    cards = {card.id: card for card in response.cards}
    assert cards["ewy-kospi-implied-parity"].dataState == "missing"
    assert cards["ewy-kospi-implied-parity"].emptyReason == "proxy_quote_missing"
    assert cards["usdt-krw-usd-krw-premium"].dataState == "missing"
    assert cards["usdt-krw-usd-krw-premium"].emptyReason == "fx_source_not_configured"
    assert cards["005930-xyz-smsn"].dataState == "disabled"
    assert cards["005930-xyz-smsn"].emptyReason == "hyperliquid_source_not_approved"
    assert "hyperliquid_source_not_approved" in cards["005930-xyz-smsn"].source.warnings
    assert any("raoni.xyz" in note for note in response.notes)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_market_parity_redacts_provider_exception_to_warning() -> None:
    response = await build_market_parity(_StubParityProvider(fail_proxy=True))

    assert response.state == "partial"
    index = next(
        card for card in response.cards if card.id == "ewy-kospi-implied-parity"
    )
    assert index.dataState == "missing"
    assert index.emptyReason == "proxy_quote_missing"
    assert any("proxy:EWY" in warning for warning in response.warnings)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_market_parity_can_hide_disabled_cards() -> None:
    response = await build_market_parity(
        _ApprovalGatedProvider(), include_disabled=False, limit=20
    )

    assert all(card.dataState != "disabled" for card in response.cards)
    assert {card.type for card in response.cards} == {
        "index_implied_parity",
        "stablecoin_fx_premium",
        "crypto_kimchi_premium",
    }


class _OverlapProbeProvider(_StubParityProvider):
    """Proves index-card and kimchi-card run concurrently.

    get_index_quote sets ``index_started`` then blocks on ``kimchi_started``;
    get_crypto_kimchi_premium sets ``kimchi_started`` then blocks on
    ``index_started``. Under serial card building the index leg's inner
    wait_for(1.0) times out (kimchi has not started yet) -> index card 'missing'.
    Under asyncio.gather both events fire and both resolve -> index card 'fresh'.
    """

    def __init__(self) -> None:
        super().__init__()
        self.index_started = asyncio.Event()
        self.kimchi_started = asyncio.Event()

    async def get_index_quote(self, symbol: str) -> ParityQuote | None:
        self.index_started.set()
        await asyncio.wait_for(self.kimchi_started.wait(), timeout=1.0)
        return await super().get_index_quote(symbol)

    async def get_crypto_kimchi_premium(self, symbol: str) -> dict[str, Any] | None:
        self.kimchi_started.set()
        await asyncio.wait_for(self.index_started.wait(), timeout=1.0)
        return await super().get_crypto_kimchi_premium(symbol)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_market_parity_builds_cards_concurrently() -> None:
    response = await build_market_parity(_OverlapProbeProvider())
    cards = {card.id: card for card in response.cards}
    # Under serial scheduling the index leg would time out -> 'missing'.
    assert cards["ewy-kospi-implied-parity"].dataState == "fresh"
    assert cards["btc-kimchi-premium"].dataState == "fresh"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_provider_index_quote_uses_current_only(monkeypatch) -> None:
    import app.services.invest_view_model.market_parity_service as svc

    called = AsyncMock(
        return_value={
            "indices": [{"symbol": "KOSPI", "current": 2450.5, "source": "naver"}]
        }
    )
    monkeypatch.setattr(svc, "handle_get_market_index_current_only", called)

    quote = await svc.DefaultMarketParityProvider().get_index_quote("KOSPI")

    assert quote is not None
    assert quote.price == Decimal("2450.5")
    called.assert_awaited_once_with("KOSPI")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_provider_index_quote_uses_quote_asof_and_data_state(
    monkeypatch,
) -> None:
    import app.services.invest_view_model.market_parity_service as svc

    called = AsyncMock(
        return_value={
            "indices": [
                {
                    "symbol": "KOSPI",
                    "current": 2450.5,
                    "source": "naver",
                    "quote_asof": "2026-07-06T09:05:00+09:00",
                    "data_state": "stale",
                }
            ]
        }
    )
    monkeypatch.setattr(svc, "handle_get_market_index_current_only", called)

    quote = await svc.DefaultMarketParityProvider().get_index_quote("KOSPI")

    assert quote is not None
    assert quote.stale is True
    assert quote.as_of == datetime.fromisoformat("2026-07-06T09:05:00+09:00")
