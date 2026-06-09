"""Prefect wrapper for crypto invest_crypto_screener_snapshots refresh (ROB-443 PR0).

The flow is importable only; no deployment is registered in this PR. Writes are
runtime-gated by ``INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED`` (shared with the
KR/US screener-snapshot flows) so accidental manual runs stay dry-run unless an
operator explicitly enables the Prefect worker env.

Activating this makes the crypto screener snapshot-backed like KR/US: the read
path (``screener_service``) already prefers ``invest_crypto_screener_snapshots``
and only falls back to the live tvscreener query when the partition is
missing/stale. Today the partition is never built (no flow), so crypto always
serves live; this flow is the missing build trigger.
"""

from __future__ import annotations

from typing import Any

from prefect import flow, task

from app.core.config import settings
from app.jobs.invest_crypto_screener_snapshots import (
    CryptoSnapshotBuildRequest,
    run_crypto_snapshot_build,
)


async def run_crypto_snapshot_refresh(
    *,
    all_markets: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run the crypto snapshot builder with env-gated commit behavior.

    ``commit`` is taken from ``INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED`` (default
    ``False`` → dry-run/rollback). Operators flip the env on the Prefect worker to
    persist; the builder itself never writes when the gate is off.
    """
    commit_enabled = bool(settings.invest_screener_snapshots_commit_enabled)
    return await run_crypto_snapshot_build(
        CryptoSnapshotBuildRequest(
            limit=limit,
            all_markets=all_markets,
            commit=commit_enabled,
        )
    )


@task(name="invest_crypto_screener_snapshots_refresh")
async def invest_crypto_screener_snapshots_task(
    *,
    all_markets: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    return await run_crypto_snapshot_refresh(all_markets=all_markets, limit=limit)


@flow(name="invest_crypto_screener_snapshots")
async def invest_crypto_screener_snapshots_flow(
    *,
    all_markets: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    """Periodic crypto screener snapshot refresh; deployment registration deferred."""
    return await invest_crypto_screener_snapshots_task(
        all_markets=all_markets, limit=limit
    )
