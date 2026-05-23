"""ROB-298 PR 2 — Binance USD-M Futures Demo execution domain.

Sibling of ``spot_demo``. Independent env namespace
(``BINANCE_FUTURES_DEMO_*``), independent host allowlist
(``demo-fapi.binance.com`` only), independent transport. Shares only the
unified ``binance_demo_order_ledger`` table via ``BinanceDemoLedgerService``
(writes ``product='usdm_futures'`` rows).
"""

from __future__ import annotations

from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoCrossAllowlistViolation,
    BinanceFuturesDemoDisabled,
    BinanceFuturesDemoHedgeModeBlocked,
    BinanceFuturesDemoLeverageMismatch,
    BinanceFuturesDemoMissingCredentials,
    BinanceFuturesDemoReduceOnlyRequired,
    BinanceFuturesDemoUnsupportedAuth,
    BinanceFuturesDemoUnsupportedSymbol,
)
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
    FuturesDemoDryRunResult,
)
from app.services.brokers.binance.futures_demo.host_allowlist import (
    FUTURES_DEMO_HOSTS,
    assert_futures_demo_host,
)
from app.services.brokers.binance.futures_demo.preflight import (
    FuturesDemoPreflightClient,
    FuturesDemoPreflightResult,
)

__all__ = [
    "FUTURES_DEMO_HOSTS",
    "assert_futures_demo_host",
    "BinanceFuturesDemoDisabled",
    "BinanceFuturesDemoMissingCredentials",
    "BinanceFuturesDemoCrossAllowlistViolation",
    "BinanceFuturesDemoUnsupportedAuth",
    "BinanceFuturesDemoHedgeModeBlocked",
    "BinanceFuturesDemoLeverageMismatch",
    "BinanceFuturesDemoReduceOnlyRequired",
    "BinanceFuturesDemoUnsupportedSymbol",
    "BinanceFuturesDemoExecutionClient",
    "FuturesDemoDryRunResult",
    "FuturesDemoPreflightClient",
    "FuturesDemoPreflightResult",
]
