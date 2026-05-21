"""ROB-287 — MCP wiring for the Hermes-initiated composition contract.

Three tools expose the existing service layer from PR #898 directly to
Hermes:

* ``investment_report_prepare_bundle`` — ensure (reuse or create) the
  snapshot bundle Hermes will compose against. Deterministic bundle
  preparation; no in-process LLM, no broker / order / watch /
  order-intent side effect.
* ``investment_report_get_hermes_context`` — return a frozen
  :class:`HermesContextPayload` derived from a persisted bundle. Pure
  read.
* ``investment_report_create_from_hermes_composition`` — validate a
  Hermes-produced :class:`HermesCompositionResult` and persist through
  the existing :class:`InvestmentReportIngestionService` so idempotency
  + stale gate + bundle linkage stay authoritative.

All three are gated by ``settings.SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED``
— the same flag the existing ``investment_report_generate_from_bundle``
tool uses. No new env flag, no HTTP route, no token auth, no
operational activation/Prefect wiring (those are explicit follow-ups).
The fourth tool from the Linear locked decision —
``investment_stage_artifacts_ingest_from_hermes`` — is intentionally
deferred until the stage-artifact external-ingest contract is
specified (stage_run identity, idempotency, partial-stage semantics,
UI exposure).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.schemas.hermes_composition import (
    HermesCompositionIngestRequest,
    HermesCompositionResult,
)
from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
from app.services.action_report.common.snapshot_bundle import (
    SnapshotBundleEnsureService,
)
from app.services.investment_stages.hermes_context import (
    HermesContextExporter,
    HermesContextExportError,
)
from app.services.investment_stages.hermes_ingest import (
    HermesCompositionIngestError,
    HermesCompositionIngestService,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


INVESTMENT_HERMES_TOOL_NAMES: set[str] = {
    "investment_report_prepare_bundle",
    "investment_report_get_hermes_context",
    "investment_report_create_from_hermes_composition",
}


_DISABLED_PAYLOAD: dict[str, Any] = {
    "success": False,
    "error": "snapshot_backed_report_generator_disabled",
    "hint": (
        "Set SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true on the MCP "
        "host to enable the Hermes composition contract."
    ),
}


def _disabled_check() -> dict[str, Any] | None:
    if not settings.SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED:
        return dict(_DISABLED_PAYLOAD)
    return None


def _parse_bundle_uuid(raw: str) -> uuid.UUID | dict[str, Any]:
    try:
        return uuid.UUID(raw)
    except ValueError:
        return {
            "success": False,
            "error": "invalid_uuid",
            "snapshot_bundle_uuid": raw,
        }


# ---------------------------------------------------------------------------
# investment_report_prepare_bundle
# ---------------------------------------------------------------------------
async def investment_report_prepare_bundle_impl(
    market: str,
    account_scope: str | None = None,
    policy_version: str = "intraday_action_report_v1",
    mode: str = "ensure_fresh",
    symbols: list[str] | None = None,
    candidate_limit: int | None = None,
    requested_by: str = "hermes",
    purpose: str = "report_generation",
) -> dict[str, Any]:
    """Ensure a snapshot bundle exists for Hermes to compose against.

    Deterministic bundle preparation — reuses a fresh bundle when one
    exists, otherwise asks the ensure service to materialise a new run
    + bundle from the read-only collector registry. No in-process LLM
    is invoked. No broker / order / watch / order-intent state is
    touched. The returned ``bundle_uuid`` is the handle Hermes passes
    to ``investment_report_get_hermes_context`` next.
    """
    disabled = _disabled_check()
    if disabled is not None:
        return disabled

    request = EnsureBundleRequest(
        purpose=purpose,
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        policy_version=policy_version,
        mode=mode,  # type: ignore[arg-type]
        symbols=symbols,
        candidate_limit=candidate_limit,
        requested_by=requested_by,  # type: ignore[arg-type]
    )
    async with AsyncSessionLocal() as db:
        svc = SnapshotBundleEnsureService(db)
        response = await svc.ensure(request)
        await db.commit()
    return {"success": True, **response.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# investment_report_get_hermes_context
# ---------------------------------------------------------------------------
async def investment_report_get_hermes_context_impl(
    snapshot_bundle_uuid: str,
) -> dict[str, Any]:
    """Return the frozen Hermes context packet for a persisted bundle.

    Runs the deterministic stage set against bundle snapshots already
    in memory and returns a :class:`HermesContextPayload` Hermes can
    use as the sole input to its out-of-process composition. Read-only:
    no stage_run / artifact rows are persisted, no provider is
    instantiated.
    """
    disabled = _disabled_check()
    if disabled is not None:
        return disabled

    parsed = _parse_bundle_uuid(snapshot_bundle_uuid)
    if isinstance(parsed, dict):
        return parsed

    async with AsyncSessionLocal() as db:
        exporter = HermesContextExporter(db)
        try:
            payload = await exporter.export(snapshot_bundle_uuid=parsed)
        except HermesContextExportError as exc:
            return {
                "success": False,
                "error": "snapshot_bundle_not_found",
                "snapshot_bundle_uuid": snapshot_bundle_uuid,
                "detail": str(exc),
            }
    return {"success": True, **payload.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# investment_report_create_from_hermes_composition
# ---------------------------------------------------------------------------
async def investment_report_create_from_hermes_composition_impl(
    composition: dict[str, Any],
    kst_date: str,
    market: str,
    market_session: str | None = None,
    account_scope: str | None = None,
    report_type: str = "snapshot_backed_advisory_v1",
    generator_version: str = "hermes-composition.v1",
    policy_version: str = "intraday_action_report_v1",
    status: str = "draft",
    created_by_profile: str = "HERMES_ADVISOR",
) -> dict[str, Any]:
    """Validate a Hermes-produced composition and ingest it as a report.

    The composition body is validated via
    :class:`HermesCompositionResult` — items that violate the
    advisory-only invariants
    (``operation`` ∈ {review, cancel, keep},
    ``apply_policy='requires_user_approval'``) are rejected up front.
    The envelope is then routed through
    :class:`HermesCompositionIngestService` which threads the request
    through the existing
    :class:`InvestmentReportIngestionService` so idempotency keys,
    stale gate, and bundle linkage stay authoritative.

    Reingest of the same Hermes composition (matching ``report_type``
    + ``market`` + ``market_session`` + ``account_scope`` +
    ``execution_mode`` + ``kst_date`` + ``generator_version``) is
    idempotent — the existing report row is returned untouched.
    """
    disabled = _disabled_check()
    if disabled is not None:
        return disabled

    try:
        composition_obj = HermesCompositionResult.model_validate(composition)
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "error": "invalid_hermes_composition",
            "detail": str(exc),
        }

    request = HermesCompositionIngestRequest(
        composition=composition_obj,
        kst_date=kst_date,
        market=market,
        market_session=market_session,
        account_scope=account_scope,
        report_type=report_type,
        generator_version=generator_version,
        policy_version=policy_version,
        status=status,  # type: ignore[arg-type]
        created_by_profile=created_by_profile,
    )

    async with AsyncSessionLocal() as db:
        svc = HermesCompositionIngestService(db)
        try:
            report = await svc.ingest_composition(request)
        except HermesCompositionIngestError as exc:
            return {
                "success": False,
                "error": "snapshot_bundle_not_found",
                "snapshot_bundle_uuid": str(composition_obj.snapshot_bundle_uuid),
                "detail": str(exc),
            }
        await db.commit()

    return {
        "success": True,
        "report_uuid": str(report.report_uuid),
        "idempotency_key": report.idempotency_key,
        "snapshot_bundle_uuid": str(composition_obj.snapshot_bundle_uuid),
        "status": report.status,
        "items_count": len(composition_obj.items),
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_investment_hermes_tools(mcp: FastMCP) -> None:
    mcp.tool(
        name="investment_report_prepare_bundle",
        description=(
            "ROB-287 — ensure a snapshot bundle exists for Hermes to compose "
            "against. Deterministic bundle preparation; no in-process LLM, "
            "no broker / order / watch / order-intent side effect. Gated by "
            "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED."
        ),
    )(investment_report_prepare_bundle_impl)
    mcp.tool(
        name="investment_report_get_hermes_context",
        description=(
            "ROB-287 — return the frozen HermesContextPayload for a "
            "persisted bundle. Read-only; runs the deterministic v1 stage "
            "set in-process and returns stage_inputs + cited snapshot UUIDs "
            "+ advisory-only constraint set. Gated by "
            "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED."
        ),
    )(investment_report_get_hermes_context_impl)
    mcp.tool(
        name="investment_report_create_from_hermes_composition",
        description=(
            "ROB-287 — validate a HermesCompositionResult and ingest it as "
            "an investment_report. Items must obey "
            "operation∈{review,cancel,keep} + "
            "apply_policy='requires_user_approval'; the envelope is routed "
            "through the existing InvestmentReportIngestionService so "
            "idempotency + stale gate + bundle linkage stay authoritative. "
            "Gated by SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED."
        ),
    )(investment_report_create_from_hermes_composition_impl)


__all__ = [
    "INVESTMENT_HERMES_TOOL_NAMES",
    "investment_report_create_from_hermes_composition_impl",
    "investment_report_get_hermes_context_impl",
    "investment_report_prepare_bundle_impl",
    "register_investment_hermes_tools",
]
