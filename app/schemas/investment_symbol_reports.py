"""Hermes symbol-reduction ingest schemas (ROB-301).

Frozen contract for the SYMBOL axis, mirroring the stage-artifacts ingest
contract in :mod:`app.schemas.hermes_composition`. Direction is Hermes-initiated
(D9): Hermes pulls context, composes per-symbol judgments out-of-process, and
PUSHES them here via ``investment_symbol_reports_ingest_from_hermes``. auto_trader
only validates + persists; it never calls an LLM in-process.

Single source of truth
----------------------
``decision_bucket`` / ``side`` are validated against the canonical tuples defined
on the ORM model (:mod:`app.models.investment_symbol_intermediate_reports`), so
the schema cannot drift from the DB CHECK constraints (ROB-301 D5).

D11 enforcement
---------------
* ``verdict`` is NOT a field here. ``extra="forbid"`` therefore rejects any
  caller-supplied verdict — the service derives it from ``(decision_bucket,
  side, availability)``.
* ``content_hash`` / ``idempotency_key`` / ``artifact_version`` are computed by
  the service, never accepted from Hermes.
* When ``data_available`` is False, the service pins ``verdict=unavailable`` +
  ``decision_bucket=deferred_no_action`` + ``unavailable_reason=data_unavailable``.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.investment_symbol_intermediate_reports import DECISION_BUCKETS
from app.schemas.hermes_composition import HermesStageRunEnvelope

HERMES_SYMBOL_REPORTS_VERSION = "hermes-symbol-reports.v1"

# PR1 sanity guard on a single push (Codex #14: max response size). This is a
# malformed/abuse guard, NOT the tuned per-batch cap — that value is deferred
# (ROB-301 D8). PR1 fixtures use 2 symbols.
MAX_SYMBOL_REPORTS_PER_CALL = 100

_SIDES = ("buy", "sell")


class HermesSymbolReductionResult(BaseModel):
    """One per-symbol judgment pushed by Hermes.

    Carries the structured evidence + the operator-facing ``decision_bucket``
    and the factual ``side``. The service derives ``verdict`` and computes the
    persistence keys; those are intentionally absent here.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str
    symbol_name: str | None = None
    # False => Hermes explicitly has no actionable judgment for this symbol
    # (data_unavailable). The service then ignores decision_bucket/side.
    data_available: bool = True
    # Required when data_available (enforced below). Validated against the
    # canonical tuple — no Literal duplication (D5).
    decision_bucket: str | None = None
    # Factual direction; the service maps (decision_bucket, side) -> verdict.
    side: str | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    summary: str | None = None
    rationale: str | None = None
    buy_evidence: list[Any] | None = None
    sell_evidence: list[Any] | None = None
    risk_evidence: list[Any] | None = None
    missing_data: list[Any] | None = None
    freshness_summary: dict[str, Any] | None = None
    cited_snapshot_uuids: list[uuid.UUID] = Field(default_factory=list)
    source_stage_artifact_uuids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("decision_bucket")
    @classmethod
    def _bucket_in_vocab(cls, value: str | None) -> str | None:
        if value is not None and value not in DECISION_BUCKETS:
            raise ValueError(f"decision_bucket={value!r} not in {DECISION_BUCKETS!r}")
        return value

    @field_validator("side")
    @classmethod
    def _side_in_vocab(cls, value: str | None) -> str | None:
        if value is not None and value not in _SIDES:
            raise ValueError(f"side={value!r} not in {_SIDES!r}")
        return value

    @model_validator(mode="after")
    def _require_bucket_when_available(self) -> HermesSymbolReductionResult:
        if self.data_available and self.decision_bucket is None:
            raise ValueError(
                f"{self.symbol}: decision_bucket is required when data_available"
            )
        return self


class HermesSymbolReportsIngestRequest(BaseModel):
    """Top-level envelope for symbol-reports ingest (push).

    Reuses :class:`HermesStageRunEnvelope` — a symbol report belongs to the SAME
    stage run as the cross-symbol artifacts it consumes (ROB-301 D2). An empty
    list is rejected (TS6 parity). Duplicate symbols in one call are rejected to
    mirror the ``(run_uuid, symbol, report_kind, artifact_version)`` UNIQUE.
    """

    model_config = ConfigDict(extra="forbid")

    request_version: Literal["hermes-symbol-reports.v1"] = HERMES_SYMBOL_REPORTS_VERSION
    run_envelope: HermesStageRunEnvelope
    report_kind: Literal["final_report_symbol"] = "final_report_symbol"
    symbol_reports: list[HermesSymbolReductionResult] = Field(
        min_length=1, max_length=MAX_SYMBOL_REPORTS_PER_CALL
    )

    @field_validator("symbol_reports")
    @classmethod
    def _enforce_unique_symbols(
        cls, reports: list[HermesSymbolReductionResult]
    ) -> list[HermesSymbolReductionResult]:
        seen: set[str] = set()
        duplicates: list[str] = []
        for report in reports:
            if report.symbol in seen:
                duplicates.append(report.symbol)
            else:
                seen.add(report.symbol)
        if duplicates:
            raise ValueError(
                "symbol-reports ingest cannot include duplicate symbols in a "
                f"single call: {duplicates!r}. The (run_uuid, symbol, "
                "report_kind, artifact_version) UNIQUE forbids it."
            )
        return reports
