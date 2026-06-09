"""ROB-287 — MCP wiring for the Hermes-initiated composition contract.

Four tools expose the service layer (PRs #898 + #905) directly to Hermes:

* ``investment_report_prepare_bundle`` — ensure (reuse or create) the
  snapshot bundle Hermes will compose against. Deterministic bundle
  preparation; no in-process LLM, no broker / order / watch /
  order-intent side effect.
* ``investment_report_get_hermes_context`` — return a frozen
  :class:`HermesContextPayload` derived from a persisted bundle. Pure
  read.
* ``investment_stage_artifacts_ingest_from_hermes`` — append-only
  ingest of one or more Hermes-produced stage artifacts. Hermes
  generates ``run_uuid`` client-side (§D1); auto_trader creates the
  run row on first ingest and reuses it for subsequent calls.
  ``(run_uuid, stage_type)`` is the idempotency key — identical
  payload returns the stored row, differing payload is rejected
  (§D3). Leaves ``run.status='running'`` — composition ingest is the
  canonical finalizer (§D4).
* ``investment_report_create_from_hermes_composition`` — validate a
  Hermes-produced :class:`HermesCompositionResult` and persist through
  the existing :class:`InvestmentReportIngestionService` so idempotency
  + stale gate + bundle linkage stay authoritative. Also auto-finalises
  any matching ``running`` Hermes stage run when
  ``composition.metadata.investment_stage_run_uuid`` points at one.

All four are gated by ``settings.SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED``
— the same flag the existing ``investment_report_generate_from_bundle``
tool uses. No new env flag, no HTTP route, no token auth, no
operational activation / Prefect wiring (those are explicit follow-up
PRs per the user's Plan B directive).
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.schemas.hermes_composition import (
    HermesCompositionIngestRequest,
    HermesCompositionResult,
    HermesStageArtifactsIngestRequest,
)
from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
from app.services.action_report.common.snapshot_bundle import (
    SnapshotBundleEnsureService,
)
from app.services.action_report.snapshot_backed.collectors.registry import (
    production_collector_registry,
)
from app.services.investment_reports.delta_service import DeltaService
from app.services.investment_stages.hermes_context import (
    HermesContextExporter,
    HermesContextExportError,
)
from app.services.investment_stages.hermes_ingest import (
    HermesCompositionIngestError,
    HermesCompositionIngestService,
    HermesStageArtifactsIngestError,
    HermesStageArtifactsIngestService,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


logger = logging.getLogger(__name__)


INVESTMENT_HERMES_TOOL_NAMES: set[str] = {
    "investment_report_prepare_bundle",
    "investment_report_get_hermes_context",
    "investment_report_create_from_hermes_composition",
    "investment_stage_artifacts_ingest_from_hermes",
    "investment_report_prepare_intraday_context",
}


INTRADAY_UPDATE_REPORT_TYPE = "intraday_update_v1"


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
    user_id: int | None = None,
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
        user_id=user_id,
    )
    async with AsyncSessionLocal() as db:
        svc = SnapshotBundleEnsureService(
            db, collectors=production_collector_registry(db)
        )
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
# investment_report_prepare_intraday_context (ROB-376 item 2)
# ---------------------------------------------------------------------------
async def investment_report_prepare_intraday_context_impl(
    snapshot_bundle_uuid: str,
    baseline_report_uuid: str,
    near_pct: float = 1.0,
    account_type: str = "live",
) -> dict[str, Any]:
    """Assemble an intraday_update Hermes context: the bundle's deterministic
    context + an ``intraday_delta_block`` (report-vs-now/prior delta) keyed to
    ``baseline_report_uuid``. Read-only; no in-process LLM; no broker / order /
    watch / order-intent mutation. Fail-open: a delta failure leaves the rest
    of the context intact. Gated by SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED.
    """
    disabled = _disabled_check()
    if disabled is not None:
        return disabled

    parsed_bundle = _parse_bundle_uuid(snapshot_bundle_uuid)
    if isinstance(parsed_bundle, dict):
        return parsed_bundle

    from app.core.timezone import now_kst

    base_uuid: uuid.UUID | None = None
    async with AsyncSessionLocal() as db:
        exporter = HermesContextExporter(db)
        try:
            payload = await exporter.export(snapshot_bundle_uuid=parsed_bundle)
        except HermesContextExportError as exc:
            return {
                "success": False,
                "error": "snapshot_bundle_not_found",
                "snapshot_bundle_uuid": snapshot_bundle_uuid,
                "detail": str(exc),
            }

        # Delta is fail-open: errors ride inside intraday_delta_block, never
        # flip the context's success.
        try:
            base_uuid = uuid.UUID(baseline_report_uuid)
        except (ValueError, AttributeError, TypeError):
            delta_block: dict[str, Any] = {
                "success": False,
                "error": "invalid_report_uuid",
            }
        else:
            try:
                delta_block = await DeltaService(db).compute_delta(
                    base_uuid,
                    near_pct=near_pct,
                    account_type=account_type,
                    computed_at_kst=now_kst().isoformat(),
                )
            except Exception as exc:  # noqa: BLE001 — fail-open
                logger.exception("intraday delta computation failed")
                delta_block = {"unavailable": str(exc) or exc.__class__.__name__}

    # Echo baseline only when the delta actually succeeded against it.
    payload.baseline_report_uuid = base_uuid if delta_block.get("success") else None
    payload.intraday_delta_block = delta_block
    return {
        "success": True,
        "report_type_hint": INTRADAY_UPDATE_REPORT_TYPE,
        **payload.model_dump(mode="json"),
    }


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
# investment_stage_artifacts_ingest_from_hermes
# ---------------------------------------------------------------------------
async def investment_stage_artifacts_ingest_from_hermes_impl(
    run_envelope: dict[str, Any],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Ingest one or more Hermes-produced stage artifacts.

    Locked decisions (signed off 2026-05-21):

    * §D1 — Hermes generates ``run_uuid`` client-side; auto_trader creates
      the run row on the first ingest, reuses it on subsequent calls with
      the same id.
    * §D3 — Append-only idempotency on ``(run_uuid, stage_type)``. Same key
      with identical payload returns the stored row; differing payload is
      rejected with ``error='artifact_content_conflict'``.
    * §D4 — This tool does NOT advance ``run.status`` to a terminal state;
      ``investment_report_create_from_hermes_composition`` finalises a
      ``running`` run when its composition references the same ``run_uuid``.
    * §D5 — Artifacts may arrive in any order; auto_trader does not enforce
      stage dependencies at the ingest layer.
    * §D8 — Multiple artifacts allowed per call; duplicate ``stage_type``
      values in a single call are rejected at the schema validator.
    * §TS6 — Empty ``artifacts`` list is rejected.

    Hard invariants:

    * No external LLM is called.
    * No broker / order / watch / order-intent mutation path is touched.
    * Append-only: a successfully stored artifact is never mutated.

    Gated by ``settings.SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED``. When
    the flag is off, returns a structured ``success=False`` envelope.
    """
    disabled = _disabled_check()
    if disabled is not None:
        return disabled

    try:
        request = HermesStageArtifactsIngestRequest.model_validate(
            {"run_envelope": run_envelope, "artifacts": artifacts}
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "error": "invalid_stage_artifacts_request",
            "detail": str(exc),
        }

    async with AsyncSessionLocal() as db:
        svc = HermesStageArtifactsIngestService(db)
        try:
            response = await svc.ingest_stage_artifacts(request)
        except HermesStageArtifactsIngestError as exc:
            return {
                "success": False,
                "error": exc.code,
                "detail": str(exc),
            }
        await db.commit()

    return {
        "success": True,
        "run_uuid": str(response.run.run_uuid),
        "run_status": response.run.status,
        "snapshot_bundle_uuid": str(response.run.snapshot_bundle_uuid),
        "artifacts": [
            {
                "stage_type": r.stage_type,
                "artifact_uuid": str(r.artifact.artifact_uuid),
                "idempotent_existing": r.idempotent_existing,
            }
            for r in response.results
        ],
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
            "Auto-finalises any matching Hermes stage run "
            "(metadata.investment_stage_run_uuid) from 'running' to "
            "'completed' (§D4). "
            "Accepts any account_scope (kis_live | kis_mock | alpaca_paper | "
            "upbit_live) — this is the path for alpaca_paper / paper:<name> "
            "reports that investment_report_generate_from_bundle rejects. "
            "Gated by SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED."
        ),
    )(investment_report_create_from_hermes_composition_impl)
    mcp.tool(
        name="investment_stage_artifacts_ingest_from_hermes",
        description=(
            "ROB-287 — append-only ingest of Hermes-produced stage "
            "artifacts. Hermes generates the run_uuid client-side; "
            "auto_trader creates the run row on first ingest and reuses "
            "it for subsequent calls. Same (run_uuid, stage_type) with "
            "identical payload returns the stored row (idempotent); "
            "differing payload is rejected. Does NOT finalise the run — "
            "use investment_report_create_from_hermes_composition for "
            "that (§D4). No broker / order / watch / order-intent side "
            "effect. Gated by SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED."
        ),
    )(investment_stage_artifacts_ingest_from_hermes_impl)
    mcp.tool(
        name="investment_report_prepare_intraday_context",
        description=(
            "ROB-376 — assemble an intraday_update Hermes context: the bundle's "
            "deterministic context plus an intraday_delta_block (report-vs-now / "
            "report-vs-prior delta) keyed to baseline_report_uuid, for Hermes to "
            "compose an intraday_update_v1 report. Read-only, fail-open on the "
            "delta, no in-process LLM, no broker/order/watch mutation. Gated by "
            "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED."
        ),
    )(investment_report_prepare_intraday_context_impl)


__all__ = [
    "INVESTMENT_HERMES_TOOL_NAMES",
    "investment_report_create_from_hermes_composition_impl",
    "investment_report_get_hermes_context_impl",
    "investment_report_prepare_bundle_impl",
    "investment_stage_artifacts_ingest_from_hermes_impl",
    "investment_report_prepare_intraday_context_impl",
    "register_investment_hermes_tools",
]
