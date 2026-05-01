"""Broker-agnostic mock trading instance registry.

This registry names user-facing paper/mock trading instances independently from
the broker implementation currently backing them.  For example, ``paper_001`` is
"모의투자1" even though its first backend mapping is KIS official mock.

The registry is metadata-only: it does not execute orders, read credentials, or
instantiate broker clients.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class MockTradingInstanceError(ValueError):
    """Raised when a mock trading instance cannot be resolved safely."""


class BrokerBackend(StrEnum):
    """Broker backend identifiers allowed for mock trading instances."""

    KIS_MOCK = "kis_mock"


class MarketScope(StrEnum):
    """Market scope for a mock trading instance."""

    KR = "kr"


@dataclass(frozen=True)
class MockTradingInstance:
    """User-facing mock trading account/strategy instance metadata."""

    slug: str
    display_name: str
    broker_backend: BrokerBackend
    broker_account_ref: str
    market_scope: MarketScope
    strategy_profile: str
    persona_profile: str

    @property
    def is_live_backend(self) -> bool:
        """Return True if this instance points at a live backend."""

        return "live" in self.broker_backend.value


PAPER_001 = MockTradingInstance(
    slug="paper_001",
    display_name="모의투자1",
    broker_backend=BrokerBackend.KIS_MOCK,
    # Reference the env/config key name only.  Never commit the actual KIS mock
    # account number or credentials in this registry.
    broker_account_ref="env:KIS_MOCK_ACCOUNT_NO",
    market_scope=MarketScope.KR,
    strategy_profile="balanced_kr_mock",
    persona_profile="paper_001",
)

MOCK_TRADING_INSTANCES: Mapping[str, MockTradingInstance] = MappingProxyType(
    {PAPER_001.slug: PAPER_001}
)


def get_mock_trading_instance(slug: str) -> MockTradingInstance:
    """Resolve a mock trading instance by slug, failing closed if unknown."""

    normalized_slug = str(slug or "").strip()
    if not normalized_slug:
        raise MockTradingInstanceError("Mock trading instance slug is required")

    instance = MOCK_TRADING_INSTANCES.get(normalized_slug)
    if instance is None:
        raise MockTradingInstanceError(
            f"Unknown mock trading instance: {normalized_slug!r}"
        )
    if instance.is_live_backend:
        raise MockTradingInstanceError(
            f"Mock trading instance {normalized_slug!r} resolves to live backend"
        )
    return instance


__all__ = [
    "BrokerBackend",
    "MarketScope",
    "MockTradingInstance",
    "MockTradingInstanceError",
    "MOCK_TRADING_INSTANCES",
    "PAPER_001",
    "get_mock_trading_instance",
]
