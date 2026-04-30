"""Tests for broker capability metadata registry.

Pins the per-broker market sets and supports_paper/supports_live flags.
No production code consumes BROKER_CAPABILITIES yet; this test pins the
registry for forward-looking planning and prevents silent drift.
"""

from __future__ import annotations

from app.services.brokers.capabilities import (
    BROKER_CAPABILITIES,
    Broker,
    BrokerCapability,
    Market,
)


class TestKisCapabilities:
    def test_kis_supports_kr_and_us_equity(self) -> None:
        cap = BROKER_CAPABILITIES[Broker.KIS]
        assert cap.markets == frozenset({Market.KR_EQUITY, Market.US_EQUITY})

    def test_kis_does_not_support_crypto(self) -> None:
        cap = BROKER_CAPABILITIES[Broker.KIS]
        assert Market.CRYPTO not in cap.markets

    def test_kis_supports_paper(self) -> None:
        cap = BROKER_CAPABILITIES[Broker.KIS]
        assert cap.supports_paper is True

    def test_kis_supports_live(self) -> None:
        cap = BROKER_CAPABILITIES[Broker.KIS]
        assert cap.supports_live is True

    def test_kis_broker_field(self) -> None:
        cap = BROKER_CAPABILITIES[Broker.KIS]
        assert cap.broker is Broker.KIS


class TestKiwoomCapabilities:
    def test_kiwoom_capability_metadata_only(self) -> None:
        cap = BROKER_CAPABILITIES[Broker.KIWOOM]
        assert cap.supports_paper is False
        assert cap.supports_live is False

    def test_kiwoom_supports_kr_and_us_equity(self) -> None:
        cap = BROKER_CAPABILITIES[Broker.KIWOOM]
        assert cap.markets == frozenset({Market.KR_EQUITY, Market.US_EQUITY})

    def test_kiwoom_broker_field(self) -> None:
        cap = BROKER_CAPABILITIES[Broker.KIWOOM]
        assert cap.broker is Broker.KIWOOM


class TestUpbitCapabilities:
    def test_upbit_supports_only_crypto(self) -> None:
        cap = BROKER_CAPABILITIES[Broker.UPBIT]
        assert cap.markets == frozenset({Market.CRYPTO})
        assert Market.KR_EQUITY not in cap.markets
        assert Market.US_EQUITY not in cap.markets

    def test_upbit_does_not_support_paper(self) -> None:
        cap = BROKER_CAPABILITIES[Broker.UPBIT]
        assert cap.supports_paper is False

    def test_upbit_supports_live(self) -> None:
        cap = BROKER_CAPABILITIES[Broker.UPBIT]
        assert cap.supports_live is True


class TestBrokerCapabilityRegistry:
    def test_all_brokers_registered(self) -> None:
        assert set(BROKER_CAPABILITIES.keys()) == {
            Broker.KIS,
            Broker.KIWOOM,
            Broker.UPBIT,
        }

    def test_capabilities_are_frozen(self) -> None:
        cap = BROKER_CAPABILITIES[Broker.KIS]
        assert isinstance(cap, BrokerCapability)
        assert isinstance(cap.markets, frozenset)
