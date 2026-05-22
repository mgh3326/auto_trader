"""ROB-296 — Binance Spot Demo Mode adapter (signed, read-only preflight).

This sub-package adds a parallel Spot Demo lane alongside the existing
Spot Testnet lane (``app/services/brokers/binance/testnet/``). The two
sub-packages share no code: env namespace, host allowlist, transport
factory, exceptions, and signing chokepoint are all duplicated to
preserve environment-specific fail-closed isolation.

Default behavior is fail-closed:
  * Missing ``BINANCE_SPOT_DEMO_ENABLED`` → ``BinanceSpotDemoDisabled``.
  * Missing credentials → ``BinanceSpotDemoMissingCredentials``.
  * Base URL outside ``SPOT_DEMO_HOSTS`` → ``BinanceLiveHostBlocked``.
  * Per-request host in ``TESTNET_HOSTS`` or ``PUBLIC_HOSTS`` →
    ``BinanceSpotDemoCrossAllowlistViolation``.

This PR (ROB-296) implemented:
  * Spot Demo config / env parsing.
  * Spot Demo host allowlist (single host: ``demo-api.binance.com``).
  * Spot Demo signed transport (HMAC-SHA256).
  * Read-only account preflight (``GET /api/v3/account``).
  * Default-disabled dry-run smoke CLI.

ROB-298 adds:
  * Mutation-capable execution client
    (``BinanceSpotDemoExecutionClient``) with per-call operator gate
    (``submit_order(..., confirm=True)`` required for HTTP). Without
    ``confirm=True`` the call returns a ``SpotDemoDryRunResult`` and
    dispatches zero HTTP.
  * ``/api/v3/order/test`` validation surface (``order_test``).
  * Read-side status queries (``get_open_orders``, ``get_order_status``).
  * Persistent ``BinanceDemoOrderLedger`` (state machine + service +
    repository) — see the ledger sub-package.

Out of scope (deferred):
  * Scheduler / TaskIQ / Prefect / Hermes activation.
  * Ed25519 signing (preflight surfaces auth rejection as
    ``BinanceSpotDemoUnsupportedAuth`` if the server refuses HMAC).
"""

from __future__ import annotations

from app.services.brokers.binance.spot_demo.dry_run import (
    SpotDemoPlannedOrder,
    plan_spot_demo_order,
)
from app.services.brokers.binance.spot_demo.errors import (
    BinanceSpotDemoCrossAllowlistViolation,
    BinanceSpotDemoDisabled,
    BinanceSpotDemoMissingCredentials,
    BinanceSpotDemoUnsupportedAuth,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
    SpotDemoDryRunResult,
)
from app.services.brokers.binance.spot_demo.host_allowlist import (
    SPOT_DEMO_HOSTS,
    assert_spot_demo_host,
)
from app.services.brokers.binance.spot_demo.preflight import (
    SpotDemoPreflightClient,
    SpotDemoPreflightResult,
)

__all__ = [
    "SPOT_DEMO_HOSTS",
    "assert_spot_demo_host",
    "BinanceSpotDemoDisabled",
    "BinanceSpotDemoMissingCredentials",
    "BinanceSpotDemoCrossAllowlistViolation",
    "BinanceSpotDemoUnsupportedAuth",
    "BinanceSpotDemoExecutionClient",
    "SpotDemoDryRunResult",
    "SpotDemoPreflightClient",
    "SpotDemoPreflightResult",
    "SpotDemoPlannedOrder",
    "plan_spot_demo_order",
]
