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
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.investment_reports import IngestReportItem
from app.schemas.investment_stages import StageArtifactPayload

HERMES_CONTEXT_VERSION = "hermes-context.v1"
HERMES_COMPOSITION_VERSION = "hermes-composition.v1"
HERMES_STAGE_ARTIFACTS_VERSION = "hermes-stage-artifacts.v1"


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
    # ROB-318 Phase 3 (PR-B) — deterministic data-sufficiency signals Hermes
    # reads as input. Optional (default empty / None) so a lagging Hermes
    # version degrades gracefully. ``why_no_action`` here reflects data/stale
    # gating only (Hermes has not produced items yet).
    data_sufficiency_by_source: dict[str, Any] = Field(default_factory=dict)
    report_quality_summary: dict[str, Any] | None = None
    why_no_action: dict[str, Any] | None = None
    dimension_evidence: dict[str, Any] = Field(default_factory=dict)
    dimension_reports: list[dict[str, Any]] = Field(default_factory=list)
    symbol_intermediate_reports: list[dict[str, Any]] = Field(default_factory=list)
    stage_inputs: list[HermesStageInput] = Field(default_factory=list)
    cited_snapshots: list[HermesCitedSnapshot] = Field(default_factory=list)
    constraints: HermesContextConstraints = Field(
        default_factory=HermesContextConstraints
    )
    # ROB-376 item 2 — intraday_update report continuity. Optional/additive
    # (no context_version bump). Populated only by the intraday context tool;
    # the base get_hermes_context path leaves both None.
    baseline_report_uuid: uuid.UUID | None = None
    intraday_delta_block: dict[str, Any] | None = None


NewsRelevanceLiteral = Literal["direct", "related", "market_context", "crypto_context"]
NewsRoleLiteral = Literal[
    "catalyst", "risk", "confirmation", "contradiction", "neutral", "noise"
]
NewsDecisionImpactLiteral = Literal[
    "strengthen_buy",
    "weaken_buy",
    "strengthen_sell",
    "weaken_sell",
    "hold_watch",
    "no_action",
]


class HermesNewsCitation(BaseModel):
    """A news article Hermes actually used, with its judgment annotations.

    auto_trader matches this against the bundle's news snapshot articles by
    ``external_article_id`` (preferred) or ``canonical_url`` and persists only
    matches. At least one of the two refs is required. ``client_item_key`` links
    the citation to a specific report item (the same key used in the composed
    ``IngestReportItem``); ``section_key`` is for report-level citations.
    """

    model_config = ConfigDict(extra="forbid")

    external_article_id: str | None = None
    canonical_url: str | None = None
    symbol: str = Field(min_length=1)
    relevance: NewsRelevanceLiteral
    role: NewsRoleLiteral
    decision_impact: NewsDecisionImpactLiteral
    selection_reason: str | None = None
    confidence: Decimal | None = None
    client_item_key: str | None = None
    section_key: str | None = None

    @model_validator(mode="after")
    def _require_ref(self) -> HermesNewsCitation:
        if not self.external_article_id and not self.canonical_url:
            raise ValueError("news citation needs external_article_id or canonical_url")
        return self


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
    # ROB-301 D3: optional references to the symbol-scoped intermediate reports
    # this composition consumed. Empty for legacy composition — the current item
    # path is then byte-identical (REGRESSION). Validated for existence (and
    # run membership) on ingest.
    symbol_intermediate_report_uuids: list[uuid.UUID] = Field(default_factory=list)
    # ROB-308: dimension reports (ROB-306) this composition consumed. Empty for
    # legacy composition. Validated for existence + run membership on ingest.
    dimension_report_uuids: list[uuid.UUID] = Field(default_factory=list)
    # ROB-423 — news articles Hermes used as overlay evidence. Empty for legacy
    # composition (byte-identical path). Matched against the bundle's news
    # snapshot on ingest; unmatched refs are dropped + recorded (fail-open).
    news_citations: list[HermesNewsCitation] = Field(default_factory=list)

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


class HermesStageRunEnvelope(BaseModel):
    """Run-scope metadata Hermes attaches to each stage-artifact ingest call.

    Locked decisions for stage-artifacts ingest (sign-off 2026-05-21):

    * **D1 — run_uuid ownership**: Hermes generates ``run_uuid`` client-side
      (UUID v4). auto_trader creates the run row on the first ingest and
      reuses it for subsequent calls with the same ``run_uuid``. If a
      pre-existing run row's envelope fields differ from this one, the
      ingest is rejected for consistency (a single ``run_uuid`` must map
      to a single ``(bundle_uuid, market, market_session, account_scope,
      policy_version, generator_version)`` tuple).
    * **D4 — finalization**: stage-artifact ingest leaves ``run.status``
      at ``running``. The downstream
      ``HermesCompositionIngestService.ingest_composition`` auto-flips the
      run to ``completed`` when ``composition.metadata.investment_stage_run_uuid``
      matches an existing ``running`` run row.
    """

    model_config = ConfigDict(extra="forbid")

    run_uuid: uuid.UUID
    snapshot_bundle_uuid: uuid.UUID
    market: str
    market_session: str | None = None
    account_scope: str | None = None
    policy_version: str = "intraday_action_report_v1"
    generator_version: str = HERMES_STAGE_ARTIFACTS_VERSION
    hermes_run_id: str | None = None


class HermesStageArtifactsIngestRequest(BaseModel):
    """Top-level envelope for stage-artifacts ingest.

    Locked decisions:

    * **D2 — schema**: artifacts are validated as :class:`StageArtifactPayload`
      directly. No Hermes-specific extension fields — extending the artifact
      shape requires bumping ``HERMES_STAGE_ARTIFACTS_VERSION`` and the
      ``stage_type`` DB CHECK.
    * **D5 — ordering**: artifacts may arrive in any order. auto_trader does
      NOT enforce stage dependencies at the ingest layer; Hermes orchestrates
      its own pipeline.
    * **D8 — batching**: multiple artifacts allowed per call.
    * **TS6 — empty list reject**: an empty ``artifacts`` list is rejected;
      "create a run row with no artifacts" is not a supported use case in v1.
    """

    model_config = ConfigDict(extra="forbid")

    request_version: Literal["hermes-stage-artifacts.v1"] = (
        HERMES_STAGE_ARTIFACTS_VERSION
    )
    run_envelope: HermesStageRunEnvelope
    artifacts: list[StageArtifactPayload] = Field(min_length=1)

    @field_validator("artifacts")
    @classmethod
    def _enforce_unique_stage_types(
        cls, artifacts: list[StageArtifactPayload]
    ) -> list[StageArtifactPayload]:
        seen: set[str] = set()
        duplicates: list[str] = []
        for art in artifacts:
            if art.stage_type in seen:
                duplicates.append(art.stage_type)
            else:
                seen.add(art.stage_type)
        if duplicates:
            raise ValueError(
                "Hermes stage-artifacts ingest cannot include duplicate "
                f"stage_type values in a single call: {duplicates!r}. The "
                "ledger's (run_uuid, stage_type) UNIQUE constraint forbids "
                "two artifacts with the same key; split into separate calls "
                "if reattempting an artifact intentionally."
            )
        return artifacts
