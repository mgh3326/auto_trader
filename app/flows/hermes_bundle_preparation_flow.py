"""ROB-287 Phase B — Prefect activation surface for the Hermes pull cycle.

Hermes pulls context + posts back results via the HTTP contract from
Phase A (``/trading/api/investment-reports/hermes/*``). For that pull
loop to find a fresh bundle, *something* must call
:class:`SnapshotBundleEnsureService.ensure` on a regular cadence —
this flow is that something. It is the same primitive
``investment_snapshots_refresh_flow`` already exposes, with one
addition: an explicit ``HERMES_BUNDLE_PREPARATION_ENABLED`` gate that
short-circuits the flow to a structured dry-run when off.

Operational stance (matches ROB-204):

* Default ``HERMES_BUNDLE_PREPARATION_ENABLED=false`` → flow returns
  ``{"status": "disabled", ...}`` without instantiating the ensure
  service. Zero DB writes, zero broker calls, zero notifications.
* When operators flip the env var to ``true`` on the Prefect worker,
  the same flow body invokes ``SnapshotBundleEnsureService.ensure``
  and returns the bundle summary.
* Production cutover (Prefect deployment registration in
  ``robin-prefect-automations``) is a separate, operator-gated step.
  Nothing in this repo schedules the flow.

Hard invariants:

* No external LLM is called.
* No broker / order / watch / order-intent mutation reachable.
* No Hermes-side webhook is fired; Hermes pulls when it pulls.
* When the gate is off, the flow body never touches
  ``AsyncSessionLocal`` — provable by a pytest hook that asserts the
  session factory is not called in the disabled path.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ``prefect`` is a worker-only runtime dependency. When the package is not
# installed (the default in this repo) we fall back to identity
# decorators so the module is importable from regular tests + scripts.
# The Prefect worker installs ``prefect`` and the decorators flip back
# to real ``@flow`` / ``@task``.
try:  # pragma: no cover - imported at module level
    from prefect import flow, task
except ImportError:  # pragma: no cover - exercised when prefect absent

    def _identity_decorator(*args: Any, **kwargs: Any) -> Any:
        if args and callable(args[0]) and not kwargs:
            return args[0]

        def _wrap(fn: Any) -> Any:
            return fn

        return _wrap

    flow = _identity_decorator  # type: ignore[assignment]
    task = _identity_decorator  # type: ignore[assignment]

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
from app.services.action_report.common.snapshot_bundle import (
    SnapshotBundleEnsureService,
)
from app.services.action_report.snapshot_backed.collectors.registry import (
    production_collector_registry,
)


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def _disabled_summary() -> dict[str, Any]:
    return {
        "status": "disabled",
        "message": (
            "HERMES_BUNDLE_PREPARATION_ENABLED is False — flow exited "
            "without preparing a bundle. Flip the env var on the Prefect "
            "worker to enable; see docs/runbooks/hermes-report-generation.md."
        ),
        "gate": "HERMES_BUNDLE_PREPARATION_ENABLED",
    }


async def run_hermes_bundle_preparation(
    *,
    market: str = "kr",
    account_scope: str | None = "kis_live",
    policy_version: str = "intraday_action_report_v1",
    purpose: str = "hermes_report_generation",
    requested_by: str = "hermes",
    symbols: list[str] | None = None,
    candidate_limit: int | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Coroutine body — separated from the Prefect ``@task`` so tests can
    invoke it directly without a Prefect runtime. The behaviour is
    identical."""
    if not settings.HERMES_BUNDLE_PREPARATION_ENABLED:
        return _disabled_summary()

    request = EnsureBundleRequest(
        purpose=purpose,
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        policy_version=policy_version,
        mode="ensure_fresh",
        symbols=symbols,
        candidate_limit=candidate_limit,
        requested_by=requested_by,  # type: ignore[arg-type]
        user_id=user_id,
    )

    async with _session_factory()() as session:
        service = SnapshotBundleEnsureService(
            session, collectors=production_collector_registry(session)
        )
        response = await service.ensure(request)
        await session.commit()

    return {
        "status": "ok",
        "bundle_uuid": str(response.bundle_uuid),
        "bundle_status": response.status,
        "freshness_summary": dict(response.freshness_summary or {}),
        "coverage_summary": dict(response.coverage_summary or {}),
        "missing_sources": list(response.missing_sources),
        "warnings": list(response.warnings),
        "created": response.created,
        "request_envelope": {
            "purpose": purpose,
            "market": market,
            "account_scope": account_scope,
            "policy_version": policy_version,
            "requested_by": requested_by,
        },
    }


@task(name="hermes_bundle_preparation")
async def hermes_bundle_preparation_task(
    *,
    market: str = "kr",
    account_scope: str | None = "kis_live",
    policy_version: str = "intraday_action_report_v1",
    purpose: str = "hermes_report_generation",
    requested_by: str = "hermes",
    symbols: list[str] | None = None,
    candidate_limit: int | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    return await run_hermes_bundle_preparation(
        market=market,
        account_scope=account_scope,
        policy_version=policy_version,
        purpose=purpose,
        requested_by=requested_by,
        symbols=symbols,
        candidate_limit=candidate_limit,
        user_id=user_id,
    )


@flow(name="rob-287 hermes bundle preparation")
async def hermes_bundle_preparation_flow(
    *,
    market: str = "kr",
    account_scope: str | None = "kis_live",
    policy_version: str = "intraday_action_report_v1",
    purpose: str = "hermes_report_generation",
    requested_by: str = "hermes",
    symbols: list[str] | None = None,
    candidate_limit: int | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Top-level Prefect flow.

    No deployment is registered in this PR; activation lives in
    ``robin-prefect-automations`` as a paused deployment that operators
    unpause after they've set ``HERMES_BUNDLE_PREPARATION_ENABLED=true``
    on the worker.
    """
    return await hermes_bundle_preparation_task(
        market=market,
        account_scope=account_scope,
        policy_version=policy_version,
        purpose=purpose,
        requested_by=requested_by,
        symbols=symbols,
        candidate_limit=candidate_limit,
        user_id=user_id,
    )


__all__ = [
    "hermes_bundle_preparation_flow",
    "hermes_bundle_preparation_task",
    "run_hermes_bundle_preparation",
]
