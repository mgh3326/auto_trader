"""Broker capability metadata registry.

Declares which markets each broker supports and whether paper/live modes are
available. Kiwoom currently exposes mock-only KR equity support via
``app/services/brokers/kiwoom`` (see ROB-97); live remains unsupported.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class Market(StrEnum):
    KR_EQUITY = "kr_equity"
    US_EQUITY = "us_equity"
    CRYPTO = "crypto"


class Broker(StrEnum):
    KIS = "kis"
    KIWOOM = "kiwoom"
    UPBIT = "upbit"
    BINANCE = "binance"
    ALPACA = "alpaca"


@dataclass(frozen=True)
class BrokerCapability:
    broker: Broker
    markets: frozenset[Market]
    supports_paper: bool
    supports_live: bool


@dataclass(frozen=True, slots=True)
class PaperBrokerCapabilities:
    """Immutable, executable capability description for a paper venue."""

    venue: Broker
    market: Market
    account_mode: str
    products: frozenset[str]
    symbols: frozenset[str]
    sides: frozenset[str]
    order_types: frozenset[str]
    time_in_force: frozenset[str]
    sizing_modes: frozenset[str]
    quote_source: str
    session_notes: tuple[str, ...]
    rate_limit_notes: tuple[str, ...]
    fill_model_known: bool
    supports_preview: bool
    supports_submit: bool
    supports_cancel: bool
    supports_get_order: bool
    supports_reconcile: bool
    supports_link_native_order: bool

    def supports(self, operation: str) -> bool:
        """Return the advertised support bit for a canonical port operation."""

        attribute = f"supports_{str(operation)}"
        return bool(getattr(self, attribute, False))


BROKER_CAPABILITIES: Mapping[Broker, BrokerCapability] = {
    Broker.KIS: BrokerCapability(
        broker=Broker.KIS,
        markets=frozenset({Market.KR_EQUITY, Market.US_EQUITY}),
        supports_paper=True,
        supports_live=True,
    ),
    Broker.KIWOOM: BrokerCapability(
        broker=Broker.KIWOOM,
        markets=frozenset({Market.KR_EQUITY}),
        supports_paper=True,
        supports_live=False,
    ),
    Broker.UPBIT: BrokerCapability(
        broker=Broker.UPBIT,
        markets=frozenset({Market.CRYPTO}),
        supports_paper=False,
        supports_live=True,
    ),
}


PAPER_BROKER_CAPABILITIES: Mapping[Broker, PaperBrokerCapabilities] = MappingProxyType(
    {
        Broker.BINANCE: PaperBrokerCapabilities(
            venue=Broker.BINANCE,
            market=Market.CRYPTO,
            account_mode="demo",
            products=frozenset({"spot"}),
            symbols=frozenset({"BTCUSDT", "ETHUSDT"}),
            sides=frozenset({"buy"}),
            order_types=frozenset({"market"}),
            time_in_force=frozenset(),
            sizing_modes=frozenset({"notional"}),
            quote_source="binance_public_spot",
            session_notes=(
                "one submit is an internally reconciled round trip using two native ledger rows",
            ),
            rate_limit_notes=(),
            fill_model_known=False,
            supports_preview=True,
            supports_submit=True,
            supports_cancel=False,
            supports_get_order=True,
            supports_reconcile=False,
            supports_link_native_order=True,
        ),
        Broker.ALPACA: PaperBrokerCapabilities(
            venue=Broker.ALPACA,
            market=Market.CRYPTO,
            account_mode="paper",
            products=frozenset({"crypto"}),
            symbols=frozenset({"BTC/USD", "ETH/USD"}),
            sides=frozenset({"buy", "sell"}),
            order_types=frozenset({"limit"}),
            time_in_force=frozenset({"gtc", "ioc"}),
            sizing_modes=frozenset({"qty"}),
            quote_source="binance_public_spot",
            session_notes=("native lifecycle remains in the Alpaca paper ledger",),
            rate_limit_notes=(),
            fill_model_known=False,
            supports_preview=True,
            supports_submit=True,
            supports_cancel=True,
            supports_get_order=True,
            supports_reconcile=False,
            supports_link_native_order=False,
        ),
    }
)


def get_paper_capabilities(venue: Broker | str) -> PaperBrokerCapabilities | None:
    """Return the canonical V1 paper capability, or ``None`` when unsupported."""

    try:
        broker = venue if isinstance(venue, Broker) else Broker(venue)
    except ValueError:
        return None
    return PAPER_BROKER_CAPABILITIES.get(broker)


__all__ = [
    "Broker",
    "BrokerCapability",
    "BROKER_CAPABILITIES",
    "Market",
    "PaperBrokerCapabilities",
    "PAPER_BROKER_CAPABILITIES",
    "get_paper_capabilities",
]
