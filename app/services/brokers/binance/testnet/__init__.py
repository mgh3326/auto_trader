"""ROB-286 — Binance Spot testnet execution adapter (signed).

This sub-package is the ONLY place in the codebase where signed Binance
endpoints are constructed. It is structurally isolated from the public
read-only adapter in ``app/services/brokers/binance/`` (Child B). Two
disjoint host allowlists with cross-allowlist guards prove that signed
requests cannot reach live Binance hosts.

Default behavior is fail-closed: missing credentials, missing
``BINANCE_TESTNET_ENABLED`` env, or a base URL pointing to a live host
all raise at adapter init. Submission requires an explicit per-call
``confirm=True`` flag every time — config-level "enabled" alone is
insufficient.

See ``docs/plans/ROB-286-binance-testnet-scalping-mvp-implementation-plan.md``
for the locked decisions and safety invariants.
"""

from __future__ import annotations

from app.services.brokers.binance.testnet.errors import (
    BinanceInvalidStateTransition,
    BinanceMissingCredentials,
    BinanceReduceOnlyRequired,
    BinanceTestnetCrossAllowlistViolation,
    BinanceTestnetDisabled,
)
from app.services.brokers.binance.testnet.host_allowlist import (
    TESTNET_HOSTS,
    assert_testnet_host,
)

__all__ = [
    "TESTNET_HOSTS",
    "assert_testnet_host",
    "BinanceTestnetDisabled",
    "BinanceMissingCredentials",
    "BinanceInvalidStateTransition",
    "BinanceReduceOnlyRequired",
    "BinanceTestnetCrossAllowlistViolation",
]
