"""Broker capability metadata registry.

Declares which markets each broker supports and whether paper/live modes are
available. Metadata-only: no order routing logic consumes this yet.
Forward-looking Kiwoom entry is included for planning; no Kiwoom client exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class Market(StrEnum):
    KR_EQUITY = "kr_equity"
    US_EQUITY = "us_equity"
    CRYPTO = "crypto"


class Broker(StrEnum):
    KIS = "kis"
    KIWOOM = "kiwoom"
    UPBIT = "upbit"


@dataclass(frozen=True)
class BrokerCapability:
    broker: Broker
    markets: frozenset[Market]
    supports_paper: bool
    supports_live: bool


BROKER_CAPABILITIES: Mapping[Broker, BrokerCapability] = {
    Broker.KIS: BrokerCapability(
        broker=Broker.KIS,
        markets=frozenset({Market.KR_EQUITY, Market.US_EQUITY}),
        supports_paper=True,
        supports_live=True,
    ),
    Broker.KIWOOM: BrokerCapability(
        broker=Broker.KIWOOM,
        markets=frozenset({Market.KR_EQUITY, Market.US_EQUITY}),
        supports_paper=False,
        supports_live=False,
    ),
    Broker.UPBIT: BrokerCapability(
        broker=Broker.UPBIT,
        markets=frozenset({Market.CRYPTO}),
        supports_paper=False,
        supports_live=True,
    ),
}


__all__ = [
    "Broker",
    "BrokerCapability",
    "BROKER_CAPABILITIES",
    "Market",
]
