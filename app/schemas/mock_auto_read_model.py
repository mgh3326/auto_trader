"""ROB-1272 (J7) — cross-lane mock/paper/demo observation read model schemas.

This module is deliberately pure. It defines immutable records, derives
deterministic identifiers, and enforces the coverage/observation invariants
signed for J7. It performs no database access, no file access, no network
call, and imports no broker adapter, ledger service, or scheduler module.

Two row types are kept separate on purpose:

``LaneCoverageRow``
    Exactly one row per canonical J2A lane, whether or not any evidence
    exists. A lane is never dropped for lack of evidence.

``LifecycleObservationRow``
    One row per *observed* lifecycle event that carries explicit J2B lineage
    (``decision_intent_id`` / ``execution_plan_id`` / ``order_attempt_id``).
    Native records without that lineage are never converted into a lifecycle
    row; they surface through ``unlinked_evidence_count`` and
    ``source_anomaly_codes`` instead.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.schemas.execution_contracts import EvidenceTier

SCHEMA_VERSION: Final[str] = "mock-auto-read-model-v1"

QuoteCurrency = Literal["KRW", "USD", "USDT"]


class EvidenceClass(StrEnum):
    """The three evidence classes a lane source may belong to."""

    DB_LEDGER = "db_ledger"
    FILE_JOURNAL = "file_journal"
    BROKER_READBACK = "broker_readback"


class LifecycleStage(StrEnum):
    """Normalized lifecycle stage vocabulary (exact J7 semantics).

    ``planned``     immutable intent/plan exists; no broker mutation evidence.
    ``acked``       broker ACK / order id, or a native accepted|open|pending
                    status. This is **not** a fill.
    ``filled``      broker fill evidence with ``filled_quantity > 0``.
    ``reconciled``  terminal order outcome **and** account/position
                    convergence evidence, both present.
    """

    PLANNED = "planned"
    ACKED = "acked"
    FILLED = "filled"
    RECONCILED = "reconciled"


#: Stages that may only exist when the lane-specific plan and attempt IDs are
#: preserved. ``planned`` is the only stage that can precede a plan/attempt.
STAGES_REQUIRING_PLAN_AND_ATTEMPT: Final[frozenset[LifecycleStage]] = frozenset(
    {LifecycleStage.ACKED, LifecycleStage.FILLED, LifecycleStage.RECONCILED}
)


class ReadModelReject(StrEnum):
    """Exact fail-closed reason codes raised by this schema layer."""

    BLANK_REQUIRED_FIELD = "blank_required_field"
    EVIDENCE_REFS_EMPTY = "evidence_refs_empty"
    EVIDENCE_REFS_NOT_CANONICAL = "evidence_refs_not_canonical"
    EVIDENCE_REFS_DUPLICATE = "evidence_refs_duplicate"
    LINEAGE_ALIAS_COLLAPSE = "lineage_alias_collapse"
    LINEAGE_MISSING_FOR_STAGE = "lineage_missing_for_stage"
    OBSERVATION_ID_NOT_DERIVED = "observation_id_not_derived"
    OBSERVATION_ID_DUPLICATE = "observation_id_duplicate"
    FILL_WITHOUT_QUANTITY = "fill_without_quantity"
    FILL_EVIDENCE_MISSING = "fill_evidence_missing"
    PARTIAL_FILL_COLLAPSED = "partial_fill_collapsed"
    RECONCILE_WITHOUT_CONVERGENCE = "reconcile_without_convergence"
    RECONCILE_WITHOUT_TERMINAL_OUTCOME = "reconcile_without_terminal_outcome"
    COVERAGE_ROW_COUNT_MISMATCH = "coverage_row_count_mismatch"
    COVERAGE_LANE_SET_MISMATCH = "coverage_lane_set_mismatch"
    COVERAGE_DUPLICATE_LANE = "coverage_duplicate_lane"
    COVERAGE_EVIDENCE_CLASS_MISMATCH = "coverage_evidence_class_mismatch"
    COVERAGE_OBSERVATION_COUNT_MISMATCH = "coverage_observation_count_mismatch"
    COVERAGE_NO_EVIDENCE_REASON_MISMATCH = "coverage_no_evidence_reason_mismatch"
    COVERAGE_CONFIGURED_BUT_EMPTY_FALSE_PASS = (
        "coverage_configured_but_empty_false_pass"
    )
    COVERAGE_SOURCE_IDS_NOT_CANONICAL = "coverage_source_ids_not_canonical"
    COVERAGE_UNKNOWN_SOURCE_ID = "coverage_unknown_source_id"
    ANOMALY_COUNT_MISMATCH = "anomaly_count_mismatch"
    HOLD_COUNT_MISMATCH = "hold_count_mismatch"
    UNLINKED_COUNT_MISMATCH = "unlinked_count_mismatch"
    SOURCE_ID_NOT_UNIQUE = "source_id_not_unique"
    CURRENCY_MIXING_FORBIDDEN = "currency_mixing_forbidden"
    SYNTHETIC_MIXING_FORBIDDEN = "synthetic_mixing_forbidden"
    FORBIDDEN_FIELD_NAME = "forbidden_field_name"


READ_MODEL_REJECT_CODES: Final[frozenset[str]] = frozenset(
    code.value for code in ReadModelReject
)

#: snake_case name parts that may never appear in a J7 response field. J7 is an
#: observation layer: it does not compute FX, parity, profitability, or any
#: strategy score.
FORBIDDEN_FIELD_NAME_PARTS: Final[frozenset[str]] = frozenset(
    {
        "fx",
        "parity",
        "profit",
        "profitability",
        "pnl",
        "roi",
        "score",
        "winner",
        "promotion",
        "ranking",
        "rank",
        "usdkrw",
        "converted",
        "conversion",
    }
)


def _reject(code: ReadModelReject, detail: str) -> ValueError:
    return ValueError(f"{code.value}: {detail}")


def _require_non_blank(value: str, field: str) -> str:
    if not value.strip():
        raise _reject(ReadModelReject.BLANK_REQUIRED_FIELD, field)
    return value


NonBlank = Annotated[str, StringConstraints(min_length=1)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]


def normalize_observation_datetime(value: datetime) -> str:
    """UTC ISO-8601 with microsecond precision — the canonical hash form."""

    return value.astimezone(UTC).isoformat(timespec="microseconds")


class _ReadModelRecord(BaseModel):
    """Strict, frozen, side-effect-free base for every J7 record."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class EvidenceRef(_ReadModelRecord):
    """One pointer to a persisted evidence record. Never a broker call."""

    evidence_class: EvidenceClass
    source_id: NonBlank
    native_key: NonBlank
    as_of: datetime

    @model_validator(mode="after")
    def _non_blank(self) -> EvidenceRef:
        _require_non_blank(self.source_id, "source_id")
        _require_non_blank(self.native_key, "native_key")
        return self

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return (
            self.evidence_class.value,
            self.source_id,
            self.native_key,
            normalize_observation_datetime(self.as_of),
        )

    def canonical(self) -> dict[str, str]:
        return {
            "evidence_class": self.evidence_class.value,
            "source_id": self.source_id,
            "native_key": self.native_key,
            "as_of": normalize_observation_datetime(self.as_of),
        }


def canonical_evidence_refs(refs: Iterable[EvidenceRef]) -> tuple[EvidenceRef, ...]:
    """Sort refs into the one canonical order and reject duplicates."""

    ordered = sorted(refs, key=lambda ref: ref.sort_key)
    keys = [ref.sort_key for ref in ordered]
    if len(set(keys)) != len(keys):
        raise _reject(ReadModelReject.EVIDENCE_REFS_DUPLICATE, str(keys))
    return tuple(ordered)


_OBSERVATION_ID_DOMAIN: Final[str] = "j7-observation-v1:"


def derive_observation_id(
    *,
    lane_id: str,
    decision_intent_id: str,
    execution_plan_id: str | None,
    order_attempt_id: str | None,
    cycle_id: str | None,
    idempotency_key: str | None,
    stage: LifecycleStage,
    evidence_refs: Sequence[EvidenceRef],
) -> str:
    """Deterministic observation identity.

    Derived from the lane, the *explicit* J2B lineage identifiers, the stage,
    and the canonical evidence refs. Caller-selected or sentinel IDs are not
    representable: :class:`LifecycleObservationRow` re-derives and compares.
    """

    payload = {
        "lane_id": lane_id,
        "decision_intent_id": decision_intent_id,
        "execution_plan_id": execution_plan_id,
        "order_attempt_id": order_attempt_id,
        "cycle_id": cycle_id,
        "idempotency_key": idempotency_key,
        "stage": stage.value,
        "evidence_refs": [ref.canonical() for ref in evidence_refs],
    }
    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return f"{_OBSERVATION_ID_DOMAIN}{digest}"


class LifecycleObservationRow(_ReadModelRecord):
    """One observed lifecycle event carrying explicit J2B lineage."""

    lane_id: NonBlank
    decision_intent_id: NonBlank
    execution_plan_id: NonBlank | None
    order_attempt_id: NonBlank | None
    cycle_id: NonBlank | None
    idempotency_key: NonBlank | None
    stage: LifecycleStage
    observation_id: NonBlank
    evidence_refs: tuple[EvidenceRef, ...]
    synthetic: bool
    quote_currency: QuoteCurrency
    venue_basis: NonBlank
    native_status: NonBlank
    native_terminal_outcome: NonBlank | None = None
    partial_fill: bool
    filled_quantity: Decimal | None
    remaining_quantity: Decimal | None
    convergence_evidence_refs: tuple[EvidenceRef, ...] = ()
    anomaly_codes: tuple[str, ...] = ()
    on_hold: bool
    hold_reason_codes: tuple[str, ...] = ()
    evidence_tier: EvidenceTier
    observed_at: datetime

    @model_validator(mode="after")
    def _invariants(self) -> LifecycleObservationRow:
        _require_non_blank(self.lane_id, "lane_id")
        _require_non_blank(self.decision_intent_id, "decision_intent_id")
        _require_non_blank(self.venue_basis, "venue_basis")
        _require_non_blank(self.native_status, "native_status")

        if not self.evidence_refs:
            raise _reject(ReadModelReject.EVIDENCE_REFS_EMPTY, self.lane_id)
        if tuple(self.evidence_refs) != canonical_evidence_refs(self.evidence_refs):
            raise _reject(ReadModelReject.EVIDENCE_REFS_NOT_CANONICAL, self.lane_id)
        if self.convergence_evidence_refs and tuple(
            self.convergence_evidence_refs
        ) != canonical_evidence_refs(self.convergence_evidence_refs):
            raise _reject(ReadModelReject.EVIDENCE_REFS_NOT_CANONICAL, self.lane_id)

        # The three J2B lineage IDs are preserved separately. Reusing one value
        # for another axis is exactly the generic-alias collapse J2B forbids.
        distinct = [
            value
            for value in (
                self.decision_intent_id,
                self.execution_plan_id,
                self.order_attempt_id,
            )
            if value is not None
        ]
        if len(set(distinct)) != len(distinct):
            raise _reject(ReadModelReject.LINEAGE_ALIAS_COLLAPSE, self.lane_id)

        if self.stage in STAGES_REQUIRING_PLAN_AND_ATTEMPT and (
            self.execution_plan_id is None or self.order_attempt_id is None
        ):
            raise _reject(ReadModelReject.LINEAGE_MISSING_FOR_STAGE, self.stage.value)

        expected = derive_observation_id(
            lane_id=self.lane_id,
            decision_intent_id=self.decision_intent_id,
            execution_plan_id=self.execution_plan_id,
            order_attempt_id=self.order_attempt_id,
            cycle_id=self.cycle_id,
            idempotency_key=self.idempotency_key,
            stage=self.stage,
            evidence_refs=self.evidence_refs,
        )
        if self.observation_id != expected:
            raise _reject(
                ReadModelReject.OBSERVATION_ID_NOT_DERIVED, self.observation_id
            )

        if self.stage is LifecycleStage.FILLED:
            if self.filled_quantity is None or self.filled_quantity <= 0:
                raise _reject(
                    ReadModelReject.FILL_WITHOUT_QUANTITY, self.observation_id
                )
            if not any(
                ref.evidence_class is EvidenceClass.BROKER_READBACK
                or ref.evidence_class is EvidenceClass.DB_LEDGER
                for ref in self.evidence_refs
            ):
                raise _reject(
                    ReadModelReject.FILL_EVIDENCE_MISSING, self.observation_id
                )
        if (
            self.remaining_quantity is not None
            and self.remaining_quantity > 0
            and not self.partial_fill
            and self.stage is LifecycleStage.FILLED
        ):
            raise _reject(ReadModelReject.PARTIAL_FILL_COLLAPSED, self.observation_id)

        if self.stage is LifecycleStage.RECONCILED:
            if not self.convergence_evidence_refs:
                raise _reject(
                    ReadModelReject.RECONCILE_WITHOUT_CONVERGENCE, self.observation_id
                )
            if self.native_terminal_outcome is None:
                raise _reject(
                    ReadModelReject.RECONCILE_WITHOUT_TERMINAL_OUTCOME,
                    self.observation_id,
                )
        return self


class EvidenceSourceBinding(_ReadModelRecord):
    """One read-only evidence source, bound by the orch source manifest."""

    source_id: NonBlank
    evidence_class: EvidenceClass
    read_only_reader_symbol: NonBlank
    logical_locator: NonBlank
    format_version: NonBlank
    lane_account_discriminator: NonBlank
    redaction_contract: NonBlank
    read_scope_note: NonBlank
    predecessor_job: NonBlank
    predecessor_merge_sha: GitSha
    predecessor_verifier_report_path: NonBlank
    predecessor_verifier_report_sha256: Sha256Hex


class PredecessorRecord(_ReadModelRecord):
    """One ordered element of ``J7_PREDECESSORS``."""

    job: NonBlank
    merge_sha: GitSha
    verifier_report_path: NonBlank
    verifier_report_sha256: Sha256Hex


class AncestorUnknown(_ReadModelRecord):
    """An unresolved axis inherited from a verified-but-incomplete ancestor."""

    job: NonBlank
    axis: NonBlank
    verifier_report_path: NonBlank
    verifier_report_sha256: Sha256Hex
    disposition: NonBlank


class AnomalyEntry(_ReadModelRecord):
    """One anomaly, always visible both as a list item and in the counts."""

    lane_id: NonBlank
    source_id: NonBlank | None
    code: NonBlank
    detail: NonBlank
    observation_id: NonBlank | None = None


class HoldEntry(_ReadModelRecord):
    """One hold, always visible both as a list item and in the counts."""

    lane_id: NonBlank
    observation_id: NonBlank
    hold_reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def _non_empty(self) -> HoldEntry:
        if not self.hold_reason_codes:
            raise _reject(ReadModelReject.BLANK_REQUIRED_FIELD, "hold_reason_codes")
        return self


class UnlinkedEvidenceEntry(_ReadModelRecord):
    """Native evidence that carries no J2B lineage.

    It is never converted into a lifecycle row and never silently dropped.
    """

    lane_id: NonBlank
    source_id: NonBlank
    evidence_class: EvidenceClass
    native_key: NonBlank
    reason: NonBlank


class LaneCoverageRow(_ReadModelRecord):
    """Exactly one row per canonical lane, evidence or not."""

    lane_id: NonBlank
    lane_status: NonBlank
    activation_status: NonBlank
    role: NonBlank | None
    role_pending_reason: NonBlank | None
    scheduler_owner: NonBlank | None
    writer: bool
    auto_order_enabled: bool
    quote_currency: QuoteCurrency
    synthetic: bool
    source_ids: tuple[str, ...]
    evidence_classes: tuple[EvidenceClass, ...]
    observed_evidence_classes: tuple[EvidenceClass, ...]
    lifecycle_observation_count: int = Field(ge=0)
    unlinked_evidence_count: int = Field(ge=0)
    source_anomaly_codes: tuple[str, ...]
    no_evidence_reason: str
    evidence_tier: EvidenceTier
    as_of: datetime

    @model_validator(mode="after")
    def _invariants(self) -> LaneCoverageRow:
        _require_non_blank(self.lane_id, "lane_id")
        _require_non_blank(self.lane_status, "lane_status")
        _require_non_blank(self.activation_status, "activation_status")

        if list(self.source_ids) != sorted(set(self.source_ids)):
            raise _reject(
                ReadModelReject.COVERAGE_SOURCE_IDS_NOT_CANONICAL, self.lane_id
            )
        for label, classes in (
            ("evidence_classes", self.evidence_classes),
            ("observed_evidence_classes", self.observed_evidence_classes),
        ):
            values = [item.value for item in classes]
            if values != sorted(set(values)):
                raise _reject(
                    ReadModelReject.COVERAGE_EVIDENCE_CLASS_MISMATCH,
                    f"{self.lane_id}:{label}",
                )

        # The reason field answers "is any evidence source bound to this lane",
        # not "did this lane observe anything". Those are different states:
        # a lane with a bound source that observed nothing reports a blank
        # reason and carries the zero through unlinked_evidence_count /
        # source_anomaly_codes instead.
        has_reason = bool(self.no_evidence_reason.strip())
        if (len(self.source_ids) == 0) != has_reason:
            raise _reject(
                ReadModelReject.COVERAGE_NO_EVIDENCE_REASON_MISMATCH,
                f"{self.lane_id}:source_ids={list(self.source_ids)}"
                f":reason={self.no_evidence_reason!r}",
            )

        if (
            self.source_ids
            and self.lifecycle_observation_count == 0
            and self.unlinked_evidence_count == 0
            and not self.source_anomaly_codes
        ):
            raise _reject(
                ReadModelReject.COVERAGE_CONFIGURED_BUT_EMPTY_FALSE_PASS, self.lane_id
            )
        return self


class ManifestRef(_ReadModelRecord):
    """The orch-stamped source binding manifest this response was built from."""

    path: NonBlank
    sha256: Sha256Hex


class ReadModelNotes(_ReadModelRecord):
    """Exact semantics that must travel with every response."""

    read_only: Literal[True] = True
    role_semantics: NonBlank
    scheduler_owner_absent_meaning: NonBlank
    lineage_requirement: NonBlank
    aggregation_boundary: NonBlank


class MockAutoReadModelResponse(_ReadModelRecord):
    """Coverage, observations, anomalies, holds and unlinked evidence.

    The four collections are returned *separately*; none is folded into
    another, and every anomaly/hold/unlinked record appears both as a list
    entry and in its count map.
    """

    schema_version: Literal["mock-auto-read-model-v1"] = SCHEMA_VERSION
    as_of: datetime
    manifest: ManifestRef
    notes: ReadModelNotes
    source_bindings: tuple[EvidenceSourceBinding, ...]
    predecessors: tuple[PredecessorRecord, ...]
    ancestor_unknowns: tuple[AncestorUnknown, ...]
    coverage_rows: tuple[LaneCoverageRow, ...]
    lifecycle_rows: tuple[LifecycleObservationRow, ...]
    anomalies: tuple[AnomalyEntry, ...]
    anomaly_counts: dict[str, int]
    holds: tuple[HoldEntry, ...]
    hold_counts: dict[str, int]
    unlinked_evidence: tuple[UnlinkedEvidenceEntry, ...]
    unlinked_evidence_counts: dict[str, int]

    @model_validator(mode="after")
    def _invariants(self) -> MockAutoReadModelResponse:
        source_ids = [binding.source_id for binding in self.source_bindings]
        if len(set(source_ids)) != len(source_ids):
            raise _reject(ReadModelReject.SOURCE_ID_NOT_UNIQUE, str(sorted(source_ids)))
        known = set(source_ids)

        lane_ids = [row.lane_id for row in self.coverage_rows]
        if len(set(lane_ids)) != len(lane_ids):
            raise _reject(
                ReadModelReject.COVERAGE_DUPLICATE_LANE, str(sorted(lane_ids))
            )

        observation_ids = [row.observation_id for row in self.lifecycle_rows]
        if len(set(observation_ids)) != len(observation_ids):
            raise _reject(
                ReadModelReject.OBSERVATION_ID_DUPLICATE, str(sorted(observation_ids))
            )

        by_lane: dict[str, list[LifecycleObservationRow]] = {}
        for row in self.lifecycle_rows:
            by_lane.setdefault(row.lane_id, []).append(row)

        for coverage in self.coverage_rows:
            unknown = sorted(set(coverage.source_ids) - known)
            if unknown:
                raise _reject(
                    ReadModelReject.COVERAGE_UNKNOWN_SOURCE_ID,
                    f"{coverage.lane_id}:{unknown}",
                )
            bound = sorted(
                {
                    binding.evidence_class.value
                    for binding in self.source_bindings
                    if binding.source_id in coverage.source_ids
                }
            )
            if [item.value for item in coverage.evidence_classes] != bound:
                raise _reject(
                    ReadModelReject.COVERAGE_EVIDENCE_CLASS_MISMATCH,
                    f"{coverage.lane_id}:bound",
                )

            rows = by_lane.get(coverage.lane_id, [])
            if coverage.lifecycle_observation_count != len(rows):
                raise _reject(
                    ReadModelReject.COVERAGE_OBSERVATION_COUNT_MISMATCH,
                    f"{coverage.lane_id}:{coverage.lifecycle_observation_count}!={len(rows)}",
                )
            observed = sorted(
                {ref.evidence_class.value for row in rows for ref in row.evidence_refs}
            )
            if [item.value for item in coverage.observed_evidence_classes] != observed:
                raise _reject(
                    ReadModelReject.COVERAGE_EVIDENCE_CLASS_MISMATCH,
                    f"{coverage.lane_id}:observed",
                )
            for row in rows:
                if row.quote_currency != coverage.quote_currency:
                    raise _reject(
                        ReadModelReject.CURRENCY_MIXING_FORBIDDEN,
                        f"{coverage.lane_id}:{row.quote_currency}",
                    )
                if row.synthetic != coverage.synthetic:
                    raise _reject(
                        ReadModelReject.SYNTHETIC_MIXING_FORBIDDEN,
                        f"{coverage.lane_id}:{row.observation_id}",
                    )

            unlinked = [
                entry
                for entry in self.unlinked_evidence
                if entry.lane_id == coverage.lane_id
            ]
            if coverage.unlinked_evidence_count != len(unlinked):
                raise _reject(
                    ReadModelReject.UNLINKED_COUNT_MISMATCH,
                    f"{coverage.lane_id}:{coverage.unlinked_evidence_count}!={len(unlinked)}",
                )

        _require_count_parity(
            [entry.code for entry in self.anomalies],
            self.anomaly_counts,
            ReadModelReject.ANOMALY_COUNT_MISMATCH,
        )
        _require_count_parity(
            [code for entry in self.holds for code in entry.hold_reason_codes],
            self.hold_counts,
            ReadModelReject.HOLD_COUNT_MISMATCH,
        )
        _require_count_parity(
            [entry.lane_id for entry in self.unlinked_evidence],
            self.unlinked_evidence_counts,
            ReadModelReject.UNLINKED_COUNT_MISMATCH,
        )
        return self


def _require_count_parity(
    values: Sequence[str], counts: Mapping[str, int], code: ReadModelReject
) -> None:
    """A list entry that never reaches the counts (or vice versa) is hiding."""

    derived: dict[str, int] = {}
    for value in values:
        derived[value] = derived.get(value, 0) + 1
    if dict(counts) != derived:
        raise _reject(code, f"{dict(counts)} != {derived}")


def forbidden_field_names(model: type[BaseModel]) -> tuple[str, ...]:
    """Field names that would introduce FX/parity/profitability/score output."""

    found: list[str] = []
    for name in model.model_fields:
        parts = set(name.lower().split("_"))
        if parts & FORBIDDEN_FIELD_NAME_PARTS:
            found.append(name)
    return tuple(sorted(found))


def assert_no_forbidden_fields(models: Iterable[type[BaseModel]]) -> None:
    for model in models:
        found = forbidden_field_names(model)
        if found:
            raise _reject(
                ReadModelReject.FORBIDDEN_FIELD_NAME, f"{model.__name__}:{found}"
            )


__all__ = [
    "FORBIDDEN_FIELD_NAME_PARTS",
    "READ_MODEL_REJECT_CODES",
    "SCHEMA_VERSION",
    "STAGES_REQUIRING_PLAN_AND_ATTEMPT",
    "AncestorUnknown",
    "AnomalyEntry",
    "EvidenceClass",
    "EvidenceRef",
    "EvidenceSourceBinding",
    "EvidenceTier",
    "HoldEntry",
    "LaneCoverageRow",
    "LifecycleObservationRow",
    "LifecycleStage",
    "ManifestRef",
    "MockAutoReadModelResponse",
    "PredecessorRecord",
    "QuoteCurrency",
    "ReadModelNotes",
    "ReadModelReject",
    "UnlinkedEvidenceEntry",
    "assert_no_forbidden_fields",
    "canonical_evidence_refs",
    "derive_observation_id",
    "forbidden_field_names",
    "normalize_observation_datetime",
]
