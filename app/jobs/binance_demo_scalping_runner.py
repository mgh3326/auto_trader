"""ROB-307 PR4 — env-wired entrypoint for the Demo scalping scheduler tick.

Scheduler-agnostic runner (TaskIQ task today; a Prefect deployment could
call the same entrypoint). **Default-OFF, two-key gate:**

* ``BINANCE_DEMO_SCALPING_ENABLED`` — the feature gate (shared with the
  observe/execute paths), AND
* ``BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED`` — the scheduler kill switch.

Both must be truthy or the tick is a no-op that builds zero clients and
touches no DB. Even when both are on, real orders are placed only if
``BINANCE_DEMO_SCALPING_SCHEDULER_CONFIRM`` is also truthy; otherwise the
tick runs the signals + risk re-checks but every ``execute_monitored`` is
a dry-run (zero broker mutation).

No schedule is registered anywhere — production recurrence + activation
is a separate operator gate (see the runbook).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

from app.services.brokers.binance.demo_scalping.contract import DEFAULT_ALLOWLIST

logger = logging.getLogger(__name__)

_BASE_ENV = "BINANCE_DEMO_SCALPING_ENABLED"
_SCHED_ENV = "BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED"
_CONFIRM_ENV = "BINANCE_DEMO_SCALPING_SCHEDULER_CONFIRM"
_PRODUCTS = ("spot", "usdm_futures")


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


async def run_demo_scalping_tick(*, now: dt.datetime | None = None) -> dict[str, Any]:
    """Run one scheduler tick if both gates are on; else a no-op."""
    base = _truthy(os.environ.get(_BASE_ENV))
    scheduler = _truthy(os.environ.get(_SCHED_ENV))
    if not (base and scheduler):
        logger.info(
            "demo scalping scheduler gate off (%s=%s, %s=%s) — no-op tick",
            _BASE_ENV,
            base,
            _SCHED_ENV,
            scheduler,
        )
        return {
            "status": "disabled",
            "base_enabled": base,
            "scheduler_enabled": scheduler,
        }

    confirm = _truthy(os.environ.get(_CONFIRM_ENV))
    now = now or dt.datetime.now(dt.UTC)
    symbols = sorted(DEFAULT_ALLOWLIST)

    # Lazy imports so the disabled path triggers zero engine/credential setup.
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.binance.demo_scalping.market_data import (
        DemoScalpingMarketData,
    )
    from app.services.brokers.binance.demo_scalping_exec.executor import (
        DemoScalpingExecutor,
    )
    from app.services.brokers.binance.demo_scalping_exec.reference import (
        DemoReferenceData,
    )
    from app.services.brokers.binance.demo_scalping_exec.scheduler import (
        run_scalping_tick,
    )
    from app.services.brokers.binance.futures_demo.execution_client import (
        BinanceFuturesDemoExecutionClient,
    )
    from app.services.brokers.binance.spot_demo.execution_client import (
        BinanceSpotDemoExecutionClient,
    )

    spot_client = BinanceSpotDemoExecutionClient.from_env()
    futures_client = BinanceFuturesDemoExecutionClient.from_env()
    market_data = DemoScalpingMarketData()
    reference = DemoReferenceData()

    class _SessionScopedExecutor:
        """Commit one complete round-trip before the next root reservation."""

        def __init__(self, *, product: str, client: Any) -> None:
            self._product = product
            self._client = client

        async def execute_monitored(self, *args: Any, **kwargs: Any) -> Any:
            async with AsyncSessionLocal() as session:
                executor = DemoScalpingExecutor(
                    product=self._product,
                    client=self._client,
                    session=session,
                    reference=reference,
                    now=now,
                    market_data=market_data,
                )
                result = await executor.execute_monitored(*args, **kwargs)
                await session.commit()
                return result

    try:
        executors = {
            "spot": _SessionScopedExecutor(product="spot", client=spot_client),
            "usdm_futures": _SessionScopedExecutor(
                product="usdm_futures", client=futures_client
            ),
        }
        summary = await run_scalping_tick(
            executors=executors,
            market_data=market_data,
            symbols=symbols,
            products=list(_PRODUCTS),
            now=now,
            confirm=confirm,
            enabled=True,
        )
    finally:
        await market_data.aclose()
        await reference.aclose()
        for client in (spot_client, futures_client):
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()

    return {"status": "ran", "confirm": confirm, **summary.to_evidence_dict()}
