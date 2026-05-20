"""Hermes context export + composition ingest schemas (ROB-287).

These schemas describe the frozen contract between auto_trader (deterministic
evidence + persistence) and Hermes (LLM reasoning + composition). The
direction is Hermes-initiated: Hermes pulls a context packet via
``HermesContextExporter``, performs in-Hermes reasoning, and pushes a
structured composition back through
``HermesCompositionIngestService.ingest_composition``.

Hard invariants:

* No fields here drive broker / order / watch / order-intent mutation.
* The exporter and ingest service never call an external LLM in-process.
* Hermes-produced items are persisted only through
  :class:`InvestmentReportIngestionService`, which applies the
  authoritative stale gate + idempotency.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.investment_reports import IngestReportItem
from app.schemas.investment_stages import StageArtifactPayload

HERMES_CONTEXT_VERSION = "hermes-context.v1"
HERMES_COMPOSITION_VERSION = "hermes-composition.v1"


class HermesCitedSnapshot(BaseModel):
    """One snapshot referenced by the context packet."""

    model_config = ConfigDict(extra="forbid")

    snapshot_uuid: uuid.UUID
    snapshot_kind: str
    payload_path: str | None = None


class HermesStageInput(BaseModel):
    """One deterministic stage rendered for Hermes consumption."""

    model_config = ConfigDict(extra="forbid")

    stage_type: str
    artifact: StageArtifactPayload
    cited_snapshots: list[HermesCitedSnapshot] = Field(default_factory=list)


class HermesContextConstraints(BaseModel):
    """Invariants Hermes MUST respect when composing items."""

    model_config = ConfigDict(extra="forbid")

    advisory_only: Literal[True] = True
    requires_user_approval: Literal[True] = True
    forbids_broker_mutation: Literal[True] = True
    forbids_order_intent_mutation: Literal[True] = True
    forbids_watch_mutation: Literal[True] = True


class HermesContextPayload(BaseModel):
    """Frozen, auditable context packet exported to Hermes.

    Contains every input Hermes needs to compose a report without
    fetching any further data — bundle metadata, freshness/coverage,
    unavailable sources, deterministic stage inputs, cited snapshot
    UUIDs, and the constraint set.
    """

    model_config = ConfigDict(extra="forbid")

    context_version: Literal["hermes-context.v1"] = HERMES_CONTEXT_VERSION
    snapshot_bundle_uuid: uuid.UUID
    bundle_status: str
    market: str
    market_session: str | None = None
    account_scope: str | None = None
    policy_version: str
    coverage_summary: dict[str, Any] = Field(default_factory=dict)
    freshness_summary: dict[str, Any] = Field(default_factory=dict)
    unavailable_sources: dict[str, Any] = Field(default_factory=dict)
    source_conflicts: dict[str, Any] = Field(default_factory=dict)
    stage_inputs: list[HermesStageInput] = Field(default_factory=list)
    cited_snapshots: list[HermesCitedSnapshot] = Field(default_factory=list)
    constraints: HermesContextConstraints = Field(
        default_factory=HermesContextConstraints
    )


class HermesCompositionResult(BaseModel):
    """Structured composition returned by Hermes for ingestion.

    Mirrors the user-facing fields of :class:`IngestReportRequest` while
    pinning the source context (``snapshot_bundle_uuid``,
    ``hermes_run_id``) so the ingest path can preserve provenance.
    Items must respect the advisory-only / requires-user-approval
    invariants — the validator below rejects anything else outright
    because the request never reaches a broker mutation path on the
    server side.
    """

    model_config = ConfigDict(extra="forbid")

    composition_version: Literal["hermes-composition.v1"] = HERMES_COMPOSITION_VERSION
    snapshot_bundle_uuid: uuid.UUID
    hermes_run_id: str
    title: str
    summary: str
    risk_summary: str | None = None
    thesis_text: str | None = None
    no_action_note: str | None = None
    items: list[IngestReportItem] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    cited_snapshot_uuids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("items")
    @classmethod
    def _enforce_review_only(
        cls, items: list[IngestReportItem]
    ) -> list[IngestReportItem]:
        bad: list[str] = []
        for item in items:
            if item.operation not in {"review", "cancel", "keep"}:
                bad.append(
                    f"{item.client_item_key}: operation={item.operation!r} "
                    "(only review/cancel/keep allowed)"
                )
            if item.apply_policy != "requires_user_approval":
                bad.append(
                    f"{item.client_item_key}: apply_policy="
                    f"{item.apply_policy!r} (must be requires_user_approval)"
                )
        if bad:
            raise ValueError(
                "Hermes composition items violate advisory-only invariants: "
                + "; ".join(bad)
            )
        return items


class HermesCompositionIngestRequest(BaseModel):
    """Envelope around a Hermes-produced composition for ingestion.

    Carries everything ``InvestmentReportIngestionService`` needs that
    isn't already in the composition itself: kst_date, report_type and
    generator_version overrides (default to ``snapshot_backed_advisory_v1``
    and ``hermes-composition.v1``), and the originating Hermes context.
    """

    model_config = ConfigDict(extra="forbid")

    composition: HermesCompositionResult
    kst_date: str
    market: str
    market_session: str | None = None
    account_scope: str | None = None
    report_type: str = "snapshot_backed_advisory_v1"
    generator_version: str = HERMES_COMPOSITION_VERSION
    policy_version: str = "intraday_action_report_v1"
    status: Literal["draft", "published"] = "draft"
    created_by_profile: str = "HERMES_ADVISOR"
