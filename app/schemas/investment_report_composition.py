"""Report composition + news-citation schemas (ROB-287, ROB-423).

Shared shapes for a structured report composition and the news articles it
cites. :mod:`app.services.investment_reports.news_persistence` matches the
citations against the bundle's news snapshot, and
:class:`~app.services.investment_reports.investment_report_news_service.InvestmentReportNewsService`
persists the matches.

Hard invariants:

* No fields here drive broker / order / watch / order-intent mutation.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.investment_reports import IngestReportItem

# Persisted wire value of ``composition_version``; do not rename.
REPORT_COMPOSITION_VERSION = "hermes-composition.v1"

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


class ReportNewsCitation(BaseModel):
    """A news article a composition actually used, with judgment annotations.

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
    def _require_ref(self) -> ReportNewsCitation:
        if not self.external_article_id and not self.canonical_url:
            raise ValueError("news citation needs external_article_id or canonical_url")
        return self


class ReportCompositionResult(BaseModel):
    """Structured composition of an advisory report.

    Mirrors the user-facing fields of :class:`IngestReportRequest` while
    pinning the source context (``snapshot_bundle_uuid``,
    ``hermes_run_id``) so provenance is preserved.
    Items must respect the advisory-only / requires-user-approval
    invariants — the validator below rejects anything else outright
    because the request never reaches a broker mutation path on the
    server side.
    """

    model_config = ConfigDict(extra="forbid")

    composition_version: Literal["hermes-composition.v1"] = REPORT_COMPOSITION_VERSION
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
    # this composition consumed. Empty for legacy composition.
    symbol_intermediate_report_uuids: list[uuid.UUID] = Field(default_factory=list)
    # ROB-308: dimension reports (ROB-306) this composition consumed. Empty for
    # legacy composition.
    dimension_report_uuids: list[uuid.UUID] = Field(default_factory=list)
    # ROB-423 — news articles used as overlay evidence. Empty for legacy
    # composition. Matched against the bundle's news snapshot; unmatched refs
    # are dropped + recorded (fail-open).
    news_citations: list[ReportNewsCitation] = Field(default_factory=list)

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
