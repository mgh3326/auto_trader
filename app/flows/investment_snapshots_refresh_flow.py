"""Prefect wrapper for ROB-269 snapshot bundle refresh.

Mirrors the ``invest_screener_snapshots_us_flow.py`` pattern: the flow is
**importable only when Prefect is installed**, and **no deployment is
registered in this PR**. A Prefect worker / scheduler picking this up
is an explicit ops step gated by unpausing the future deployment
registration. Until then, the function is exercised in static file
checks + manual runs only.

Dependency caveat — Prefect is NOT currently a project dependency in
``pyproject.toml``. Importing this module in the current dev/CI
environment raises ``ModuleNotFoundError: No module named 'prefect'``.
That is intentional: the file is validated statically (file-text checks
for ``@flow`` / ``@task`` / required defaults) by
``tests/test_investment_snapshots_refresh_flow.py``, mirroring the
ROB-204 ``invest_screener_snapshots_us_flow.py`` pattern. The runtime
import test is ``skipif(True, reason="prefect not yet a project
dependency; import verified when added")`` and flips on once Prefect
lands as a dep through a separate ops change.

ROB-314 scope decision: this refresh flow deliberately stays on the
default *empty* collector registry. Production collectors are wired only
into the report-generation entrypoints (MCP ``investment_report_prepare_bundle``
and HTTP ``/hermes/prepare-bundle``); this deferred flow belongs to the separate
scheduler-activation track. Running
``ensure_snapshot_bundle`` here therefore returns a ``failed`` bundle (no data)
for new identity tuples, or ``reused`` for an existing fresh bundle, until that
track lands. Locked by ``tests/test_rob314_deferred_call_sites.py``.

Safety:
* Read-mostly snapshot service; the only DB writes are the Phase 1
  append-only INSERTs into ``review.investment_snapshot_*`` tables.
* No broker / order / watch-intent mutation.
* No live HTTP fetches in this flow file — the underlying ensure service
  uses the collector registry, which this deferred flow intentionally
  leaves at the default empty registry (see the ROB-314 note above).
"""

from __future__ import annotations

from typing import Any, cast

from prefect import flow, task
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.schemas.investment_snapshots_mcp import (
    EnsureBundleRequest,
    EnsureBundleResponse,
)
from app.services.action_report.common.snapshot_bundle import (
    SnapshotBundleEnsureService,
)


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def _response_to_dict(response: EnsureBundleResponse) -> dict[str, Any]:
    """JSON-safe shape for Prefect logs / downstream tasks."""
    return {
        "bundle_uuid": str(response.bundle_uuid) if response.bundle_uuid else None,
        "status": response.status,
        "created": response.created,
        "coverage_summary": response.coverage_summary,
        "freshness_summary": response.freshness_summary,
        "missing_sources": list(response.missing_sources),
        "warnings": list(response.warnings),
        "run_uuid": str(response.run_uuid) if response.run_uuid else None,
    }


async def run_snapshot_bundle_refresh(
    *,
    purpose: str = "kr_action_report",
    market: str = "kr",
    account_scope: str | None = "kis_live",
    policy_version: str = "intraday_action_report_v1",
    symbols: list[str] | None = None,
    candidate_limit: int | None = None,
) -> dict[str, Any]:
    """Ensure a snapshot bundle for the given identity tuple.

    Returns a JSON-safe summary of the resulting bundle. With the empty
    production collector registry this typically returns:

    * ``status='reused'`` when a fresh bundle exists for the identity tuple.
    * ``status='failed'`` when no fresh bundle exists and no collectors
      are registered to populate one.

    Phase 5 will register real collectors so this transitions to
    ``complete`` / ``partial`` for live operational use.
    """
    request = EnsureBundleRequest(
        purpose=purpose,
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        policy_version=policy_version,
        mode="ensure_fresh",
        symbols=symbols,
        candidate_limit=candidate_limit,
        requested_by="scheduler",
    )

    async with _session_factory()() as session:
        # ROB-314: deliberately NOT wired to production_collector_registry.
        # The scheduler refresh path belongs to the separate scheduler-
        # activation track; only the report-generation entrypoints (MCP
        # prepare_bundle, HTTP prepare-bundle) inject production
        # collectors. Locked by tests/test_rob314_deferred_call_sites.py.
        service = SnapshotBundleEnsureService(session)
        response = await service.ensure(request)
        await session.commit()

    return _response_to_dict(response)


@task(name="investment_snapshots_refresh")
async def investment_snapshots_refresh_task(
    *,
    purpose: str = "kr_action_report",
    market: str = "kr",
    account_scope: str | None = "kis_live",
    policy_version: str = "intraday_action_report_v1",
    symbols: list[str] | None = None,
    candidate_limit: int | None = None,
) -> dict[str, Any]:
    return await run_snapshot_bundle_refresh(
        purpose=purpose,
        market=market,
        account_scope=account_scope,
        policy_version=policy_version,
        symbols=symbols,
        candidate_limit=candidate_limit,
    )


@flow(name="rob-269 snapshot bundle refresh")
async def investment_snapshots_refresh_flow(
    *,
    purpose: str = "kr_action_report",
    market: str = "kr",
    account_scope: str | None = "kis_live",
    policy_version: str = "intraday_action_report_v1",
    symbols: list[str] | None = None,
    candidate_limit: int | None = None,
) -> dict[str, Any]:
    """Top-level flow. Returns the bundle summary dict.

    No Prefect deployment is registered in this PR; this function is
    callable via Prefect ``run_flow`` or directly (the @flow decorator
    still allows direct invocation).
    """
    return await investment_snapshots_refresh_task(
        purpose=purpose,
        market=market,
        account_scope=account_scope,
        policy_version=policy_version,
        symbols=symbols,
        candidate_limit=candidate_limit,
    )


__all__ = [
    "investment_snapshots_refresh_flow",
    "investment_snapshots_refresh_task",
    "run_snapshot_bundle_refresh",
]
