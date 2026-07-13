from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.services.brokers.capabilities import (
    PAPER_BROKER_CAPABILITIES,
    Broker,
    Market,
    get_paper_capabilities,
)


def test_paper_capability_registry_has_only_v1_venues() -> None:
    assert set(PAPER_BROKER_CAPABILITIES) == {Broker.BINANCE, Broker.ALPACA}


def test_binance_spot_demo_capability_is_exact_and_truthful() -> None:
    capability = get_paper_capabilities(Broker.BINANCE)

    assert capability is not None
    assert capability.venue is Broker.BINANCE
    assert capability.market is Market.CRYPTO
    assert capability.account_mode == "demo"
    assert capability.products == frozenset({"spot"})
    assert capability.symbols == frozenset({"BTCUSDT", "ETHUSDT"})
    assert capability.sides == frozenset({"buy"})
    assert capability.order_types == frozenset({"market"})
    assert capability.time_in_force == frozenset()
    assert capability.sizing_modes == frozenset({"notional"})
    assert capability.quote_source == "binance_public_spot"
    assert capability.fill_model_known is False
    assert capability.supports_preview is True
    assert capability.supports_submit is True
    assert capability.supports_cancel is False
    assert capability.supports_get_order is True
    assert capability.supports_reconcile is False
    assert capability.supports_link_native_order is True
    assert any("two native ledger rows" in note for note in capability.session_notes)
    assert capability.rate_limit_notes == ()


def test_alpaca_crypto_paper_capability_is_exact_and_truthful() -> None:
    capability = get_paper_capabilities("alpaca")

    assert capability is not None
    assert capability.venue is Broker.ALPACA
    assert capability.market is Market.CRYPTO
    assert capability.account_mode == "paper"
    assert capability.products == frozenset({"crypto"})
    assert capability.symbols == frozenset({"BTC/USD", "ETH/USD"})
    assert capability.sides == frozenset({"buy", "sell"})
    assert capability.order_types == frozenset({"limit"})
    assert capability.time_in_force == frozenset({"gtc", "ioc"})
    assert capability.sizing_modes == frozenset({"qty"})
    assert capability.quote_source == "binance_public_spot"
    assert capability.fill_model_known is False
    assert capability.supports_preview is True
    assert capability.supports_submit is True
    assert capability.supports_cancel is True
    assert capability.supports_get_order is True
    assert capability.supports_reconcile is False
    assert capability.supports_link_native_order is False
    assert capability.rate_limit_notes == ()


def test_non_v1_or_unknown_venue_has_no_paper_capability() -> None:
    assert get_paper_capabilities(Broker.KIS) is None
    assert get_paper_capabilities("not-a-broker") is None


def test_paper_capability_is_frozen() -> None:
    capability = PAPER_BROKER_CAPABILITIES[Broker.BINANCE]

    with pytest.raises(FrozenInstanceError):
        capability.account_mode = "live"  # type: ignore[misc]
