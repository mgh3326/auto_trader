"""Read-only US upside shadow instrumentation.

This module accepts a previously captured bounded cohort, evaluates only the
three frozen counterfactual arms, and writes operator-selected local records.
It has no network, database, broker, eligibility, proposal, or scheduling
integration.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_SHA256 = "2b8044a1cc39fbd830f68a3253ebdb20db987f2d9f76763edec8a2e3af3a5fe6"
SCHEMA_VERSION = "us-upside-shadow-instrumentation-v1"

GateState = Literal["pass", "fail", "unknown"]
FreshnessState = Literal["fresh", "stale", "unknown"]
ConsensusState = Literal[
    "value", "missing", "stale", "error", "timeout", "unknown", "unqueried"
]
SupportStrength = Literal["strong", "weak", "none", "unknown"]

_TERMINAL_CONSENSUS_STATES: frozenset[str] = frozenset(
    {"value", "missing", "stale", "error"}
)
_FEASIBILITY_FIELDS = (
    "sector",
    "dedupe",
    "cash",
    "whole_share",
)


class StrictModel(BaseModel):
    """Base for the versioned JSON contract; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceCoverage(StrictModel):
    """Per-source census, including every bounded-read loss point."""

    source_id: str = Field(min_length=1)
    upstream_total_known: int | None = Field(..., ge=0)
    upstream_total_unknown: bool
    returned_count: int = Field(ge=0)
    timeout_or_error_count: int = Field(ge=0)
    unqueried_count: int = Field(ge=0)
    top_n_cap: int | None = Field(..., ge=1)
    outside_top_n_count: int = Field(ge=0)
    deduped_unique_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _require_explicit_total_state(self) -> SourceCoverage:
        if self.upstream_total_unknown == (self.upstream_total_known is not None):
            raise ValueError(
                "exactly one of upstream_total_known and upstream_total_unknown "
                "must be set"
            )
        if self.top_n_cap is None and self.outside_top_n_count != 0:
            raise ValueError(
                "outside_top_n_count must be zero when no top_n_cap is declared"
            )
        return self


class MatchedSource(StrictModel):
    source_id: str = Field(min_length=1)
    rank: int | None = Field(default=None, ge=1)


class TickHandling(StrictModel):
    rule: str = Field(min_length=1)
    raw_limit: float | None = Field(..., gt=0)
    snapped_limit: float | None = Field(..., gt=0)
    direction: Literal["down", "up", "none", "unknown"]


class Feasibility(StrictModel):
    sector: str | None = Field(...)
    sector_feasibility: GateState
    dedupe_feasibility: GateState
    cash_feasibility: GateState
    whole_share_feasibility: GateState
    would_size: float | None = Field(..., gt=0)
    required_cash: float | None = Field(..., ge=0)


class HypotheticalLimitTouch(StrictModel):
    """Optional next-session high/low observation, not an execution record."""

    next_session_high: float = Field(gt=0)
    next_session_low: float = Field(gt=0)
    limit_touched: bool

    @model_validator(mode="after")
    def _high_is_not_below_low(self) -> HypotheticalLimitTouch:
        if self.next_session_high < self.next_session_low:
            raise ValueError("next_session_high must be greater than or equal to low")
        return self


class CandidateSnapshot(StrictModel):
    """All decision-time facts supplied by the read-only upstream capture."""

    symbol: str = Field(min_length=1)
    matched_sources: tuple[MatchedSource, ...]
    freshness: FreshnessState
    consensus_status: ConsensusState
    target_honesty: Literal["honest", "not_honest", "unknown"]
    target_as_of: str | None = Field(...)
    analyst_count: int | None = Field(..., ge=0)
    current_price: float | None = Field(..., gt=0)
    target: float | None = Field(..., gt=0)
    rsi: float | None = Field(..., ge=0, le=100)
    support_price: float | None = Field(..., gt=0)
    support_strength: SupportStrength
    independent_support_families: tuple[str, ...]
    non_upside_gate_bits: dict[str, GateState] = Field(min_length=1)
    proposed_limit: float | None = Field(..., gt=0)
    tick_handling: TickHandling
    feasibility: Feasibility
    hypothetical_limit_touch: HypotheticalLimitTouch | None = Field(...)

    @field_validator("independent_support_families")
    @classmethod
    def _require_named_families(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not family.strip() for family in value):
            raise ValueError("independent_support_families cannot contain blanks")
        return value

    @field_validator("non_upside_gate_bits")
    @classmethod
    def _keep_upside_out_of_base_gates(
        cls, value: dict[str, GateState]
    ) -> dict[str, GateState]:
        if any(not name.strip() for name in value):
            raise ValueError("non_upside_gate_bits cannot contain blank names")
        if any("upside" in name.lower() for name in value):
            raise ValueError("non_upside_gate_bits cannot contain an upside gate")
        return value

    @model_validator(mode="after")
    def _keep_tick_record_consistent(self) -> CandidateSnapshot:
        if self.proposed_limit != self.tick_handling.snapped_limit:
            raise ValueError(
                "proposed_limit must exactly equal tick_handling.snapped_limit"
            )
        return self


class InstrumentationInput(StrictModel):
    """A single bounded US-session capture supplied to the manual CLI."""

    session_id: str = Field(min_length=1)
    contract_sha: str
    policy_sha: str = Field(min_length=12)
    code_sha: str = Field(min_length=12)
    source_corpus_as_of: str = Field(min_length=1)
    decision_cutoff: str = Field(min_length=1)
    universe_hash: str = Field(min_length=12)
    sources: tuple[SourceCoverage, ...] = Field(min_length=1)
    candidates: tuple[CandidateSnapshot, ...]

    @field_validator("contract_sha")
    @classmethod
    def _require_frozen_contract(cls, value: str) -> str:
        if value != CONTRACT_SHA256:
            raise ValueError("contract_sha does not match the frozen §Q4 contract")
        return value

    @model_validator(mode="after")
    def _validate_source_references(self) -> InstrumentationInput:
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("sources must have unique source_id values")
        known_source_ids = set(source_ids)
        for candidate in self.candidates:
            for match in candidate.matched_sources:
                if match.source_id not in known_source_ids:
                    raise ValueError(
                        f"candidate {candidate.symbol} references unknown source "
                        f"{match.source_id}"
                    )
        return self


class ArmDefinition(StrictModel):
    arm_id: Literal["A40", "B30", "C25"]
    upside_min_pct: int = Field(ge=0)
    required_support_strength: Literal["strong"] | None = None
    independent_family_min: int | None = Field(default=None, ge=1)
    final_discount_min_pct: int | None = Field(default=None, ge=0)
    final_discount_max_pct: int | None = Field(default=None, ge=0)
    diagnostic_only: bool
    shadow_only: Literal[True] = True

    @model_validator(mode="after")
    def _require_complete_distance_range(self) -> ArmDefinition:
        values = (self.final_discount_min_pct, self.final_discount_max_pct)
        if (values[0] is None) != (values[1] is None):
            raise ValueError("discount range must be fully specified or absent")
        if values[0] is not None and values[0] > values[1]:
            raise ValueError("discount range minimum cannot exceed its maximum")
        return self


FROZEN_ARMS: tuple[ArmDefinition, ...] = (
    ArmDefinition(
        arm_id="A40",
        upside_min_pct=40,
        diagnostic_only=False,
    ),
    ArmDefinition(
        arm_id="B30",
        upside_min_pct=30,
        required_support_strength="strong",
        independent_family_min=3,
        final_discount_min_pct=12,
        final_discount_max_pct=15,
        diagnostic_only=False,
    ),
    ArmDefinition(
        arm_id="C25",
        upside_min_pct=25,
        required_support_strength="strong",
        independent_family_min=3,
        final_discount_min_pct=11,
        final_discount_max_pct=15,
        diagnostic_only=True,
    ),
)


class ArmGateResult(StrictModel):
    arm_id: Literal["A40", "B30", "C25"]
    gate_bits: dict[str, bool]
    reject_reasons: tuple[str, ...]
    would_select: bool
    shadow_only: Literal[True] = True


class CandidateInstrumentationRecord(CandidateSnapshot):
    upside_pct: float | None
    support_distance_pct: float | None
    independent_support_family_count: int = Field(ge=0)
    final_discount_from_current_pct: float | None
    arithmetic_limit_basis_upside_pct: float | None
    arm_results: tuple[ArmGateResult, ...]


class CoverageSummary(StrictModel):
    upstream_total_unknown_source_count: int = Field(ge=0)
    timeout_or_error_count: int = Field(ge=0)
    unqueried_count: int = Field(ge=0)
    outside_top_n_count: int = Field(ge=0)
    candidate_consensus_status_counts: dict[ConsensusState, int]
    coverage_complete: bool


class SessionInterpretation(StrictModel):
    conclusion: Literal[
        "upside_dominant_constraint_bounded_cohort",
        "earlier_source_freshness_or_support_constraint",
        "coverage_insufficient_no_threshold_conclusion",
        "no_threshold_conclusion",
    ]
    reason: str
    passes_all_non_upside_gates: int = Field(ge=0)
    survivor_consensus_statuses: tuple[ConsensusState, ...]
    upside_band_counts: dict[str, int]
    fanout_performance_or_alpha_inferred: Literal[False] = False
    threshold_tuning_permitted: Literal[False] = False
    if_three_sessions_b30_and_c25_are_zero: Literal[
        "diagnose_target_coverage_or_support_source_contract"
    ] = "diagnose_target_coverage_or_support_source_contract"


class ReadOnlySafety(StrictModel):
    eligibility_connections: Literal[0] = 0
    proposals_created: Literal[0] = 0
    orders_created: Literal[0] = 0
    broker_calls: Literal[0] = 0
    database_writes: Literal[0] = 0
    scheduler_registrations: Literal[0] = 0
    threshold_overrides: Literal[0] = 0


class SessionRecord(StrictModel):
    schema_version: Literal["us-upside-shadow-instrumentation-v1"] = SCHEMA_VERSION
    session_id: str
    contract_sha: str
    policy_sha: str
    code_sha: str
    source_corpus_as_of: str
    decision_cutoff: str
    universe_hash: str
    input_hash: str
    frozen_arms: tuple[ArmDefinition, ...]
    sources: tuple[SourceCoverage, ...]
    candidates: tuple[CandidateInstrumentationRecord, ...]
    arm_shadow_counts: dict[Literal["A40", "B30", "C25"], int]
    coverage: CoverageSummary
    interpretation: SessionInterpretation
    read_only_safety: ReadOnlySafety


class ThreeSessionReading(StrictModel):
    session_ids: tuple[str, str, str]
    a40_shadow_count: int = Field(ge=0)
    b30_shadow_count: int = Field(ge=0)
    c25_shadow_count: int = Field(ge=0)
    next_step: Literal[
        "diagnose_target_coverage_or_support_source_contract",
        "continue_bounded_cohort_reading_without_tuning",
    ]
    threshold_tuning_permitted: Literal[False] = False


def _round(value: float) -> float:
    return round(value, 6)


def _upside_pct(candidate: CandidateSnapshot) -> float | None:
    if candidate.current_price is None or candidate.target is None:
        return None
    return _round((candidate.target / candidate.current_price - 1) * 100)


def _support_distance_pct(candidate: CandidateSnapshot) -> float | None:
    if candidate.current_price is None or candidate.support_price is None:
        return None
    return _round(
        (candidate.current_price - candidate.support_price)
        / candidate.current_price
        * 100
    )


def _final_discount_pct(candidate: CandidateSnapshot) -> float | None:
    if candidate.current_price is None or candidate.proposed_limit is None:
        return None
    return _round(
        (candidate.current_price - candidate.proposed_limit)
        / candidate.current_price
        * 100
    )


def _arithmetic_limit_basis_upside_pct(candidate: CandidateSnapshot) -> float | None:
    if candidate.target is None or candidate.proposed_limit is None:
        return None
    return _round((candidate.target / candidate.proposed_limit - 1) * 100)


def _family_count(candidate: CandidateSnapshot) -> int:
    return len({family.strip() for family in candidate.independent_support_families})


def _evaluate_arm(
    candidate: CandidateSnapshot,
    arm: ArmDefinition,
    *,
    upside_pct: float | None,
    final_discount_pct: float | None,
) -> ArmGateResult:
    gate_bits: dict[str, bool] = {}
    reject_reasons: list[str] = []

    for name, state in sorted(candidate.non_upside_gate_bits.items()):
        passed = state == "pass"
        gate_bits[f"non_upside:{name}"] = passed
        if not passed:
            reject_reasons.append(f"non_upside_gate_{name}_{state}")

    upside_passed = upside_pct is not None and upside_pct >= arm.upside_min_pct
    gate_bits["upside_minimum"] = upside_passed
    if not upside_passed:
        reason = (
            "upside_missing"
            if upside_pct is None
            else f"upside_below_{arm.upside_min_pct}pct"
        )
        reject_reasons.append(reason)

    if arm.required_support_strength is not None:
        strength_passed = candidate.support_strength == arm.required_support_strength
        gate_bits["support_strength_strong"] = strength_passed
        if not strength_passed:
            reject_reasons.append("support_strength_not_strong")

    if arm.independent_family_min is not None:
        family_passed = _family_count(candidate) >= arm.independent_family_min
        gate_bits["independent_support_family_minimum"] = family_passed
        if not family_passed:
            reject_reasons.append(
                f"independent_support_families_below_{arm.independent_family_min}"
            )

    if arm.final_discount_min_pct is not None:
        assert arm.final_discount_max_pct is not None
        distance_passed = (
            final_discount_pct is not None
            and arm.final_discount_min_pct
            <= final_discount_pct
            <= arm.final_discount_max_pct
        )
        gate_bits["final_discount_distance"] = distance_passed
        if not distance_passed:
            reason = (
                "final_discount_missing"
                if final_discount_pct is None
                else (
                    "final_discount_outside_"
                    f"{arm.final_discount_min_pct}_to_{arm.final_discount_max_pct}_pct"
                )
            )
            reject_reasons.append(reason)

    for name in _FEASIBILITY_FIELDS:
        state = getattr(candidate.feasibility, f"{name}_feasibility")
        passed = state == "pass"
        gate_bits[f"{name}_feasible"] = passed
        if not passed:
            reject_reasons.append(f"{name}_feasibility_{state}")

    return ArmGateResult(
        arm_id=arm.arm_id,
        gate_bits=gate_bits,
        reject_reasons=tuple(reject_reasons),
        would_select=not reject_reasons,
    )


def _coverage_summary(snapshot: InstrumentationInput) -> CoverageSummary:
    source_timeout_or_error_count = sum(
        source.timeout_or_error_count for source in snapshot.sources
    )
    source_unqueried_count = sum(source.unqueried_count for source in snapshot.sources)
    consensus_counts = Counter(
        candidate.consensus_status for candidate in snapshot.candidates
    )
    candidate_timeout_or_error_count = consensus_counts["timeout"]
    candidate_unqueried_count = consensus_counts["unqueried"]
    unknown_source_count = sum(
        source.upstream_total_unknown for source in snapshot.sources
    )
    candidate_unknown_count = consensus_counts["unknown"]

    timeout_or_error_count = (
        source_timeout_or_error_count + candidate_timeout_or_error_count
    )
    unqueried_count = source_unqueried_count + candidate_unqueried_count
    coverage_complete = not (
        unknown_source_count
        or candidate_unknown_count
        or timeout_or_error_count
        or unqueried_count
    )
    return CoverageSummary(
        upstream_total_unknown_source_count=unknown_source_count,
        timeout_or_error_count=timeout_or_error_count,
        unqueried_count=unqueried_count,
        outside_top_n_count=sum(
            source.outside_top_n_count for source in snapshot.sources
        ),
        candidate_consensus_status_counts={
            status: consensus_counts[status]
            for status in (
                "value",
                "missing",
                "stale",
                "error",
                "timeout",
                "unknown",
                "unqueried",
            )
        },
        coverage_complete=coverage_complete,
    )


def _interpret_session(
    candidate_records: Sequence[CandidateInstrumentationRecord],
    coverage: CoverageSummary,
) -> SessionInterpretation:
    survivors = [
        candidate
        for candidate in candidate_records
        if all(state == "pass" for state in candidate.non_upside_gate_bits.values())
    ]
    survivor_statuses = tuple(
        sorted({candidate.consensus_status for candidate in survivors})
    )
    band_counts = {
        "below_25": 0,
        "25_to_40": 0,
        "ge_40": 0,
        "upside_missing": 0,
    }
    for candidate in survivors:
        if candidate.upside_pct is None:
            band_counts["upside_missing"] += 1
        elif candidate.upside_pct >= 40:
            band_counts["ge_40"] += 1
        elif candidate.upside_pct >= 25:
            band_counts["25_to_40"] += 1
        else:
            band_counts["below_25"] += 1

    if not coverage.coverage_complete:
        return SessionInterpretation(
            conclusion="coverage_insufficient_no_threshold_conclusion",
            reason=(
                "timeout, unqueried, or unknown coverage remains; threshold "
                "conclusion is unavailable"
            ),
            passes_all_non_upside_gates=len(survivors),
            survivor_consensus_statuses=survivor_statuses,
            upside_band_counts=band_counts,
        )
    if not survivors:
        return SessionInterpretation(
            conclusion="earlier_source_freshness_or_support_constraint",
            reason="no candidate survived the declared non-upside gates",
            passes_all_non_upside_gates=0,
            survivor_consensus_statuses=survivor_statuses,
            upside_band_counts=band_counts,
        )
    terminal_statuses = all(
        status in _TERMINAL_CONSENSUS_STATES for status in survivor_statuses
    )
    if terminal_statuses and band_counts["ge_40"] == 0 and band_counts["25_to_40"] > 0:
        return SessionInterpretation(
            conclusion="upside_dominant_constraint_bounded_cohort",
            reason=(
                "all survivor consensus states are terminal, no timeout remains, "
                "and only the 25-to-40 upside band has candidates"
            ),
            passes_all_non_upside_gates=len(survivors),
            survivor_consensus_statuses=survivor_statuses,
            upside_band_counts=band_counts,
        )
    return SessionInterpretation(
        conclusion="no_threshold_conclusion",
        reason="the bounded cohort does not satisfy a fixed dominant-constraint rule",
        passes_all_non_upside_gates=len(survivors),
        survivor_consensus_statuses=survivor_statuses,
        upside_band_counts=band_counts,
    )


def evaluate_instrumentation(
    snapshot: InstrumentationInput, *, input_hash: str
) -> SessionRecord:
    """Evaluate frozen shadow arms without contacting any runtime surface."""

    candidate_records: list[CandidateInstrumentationRecord] = []
    arm_shadow_counts: dict[Literal["A40", "B30", "C25"], int] = {
        "A40": 0,
        "B30": 0,
        "C25": 0,
    }
    for candidate in snapshot.candidates:
        upside_pct = _upside_pct(candidate)
        final_discount_pct = _final_discount_pct(candidate)
        arm_results = tuple(
            _evaluate_arm(
                candidate,
                arm,
                upside_pct=upside_pct,
                final_discount_pct=final_discount_pct,
            )
            for arm in FROZEN_ARMS
        )
        for result in arm_results:
            if result.would_select:
                arm_shadow_counts[result.arm_id] += 1
        candidate_records.append(
            CandidateInstrumentationRecord(
                **candidate.model_dump(),
                upside_pct=upside_pct,
                support_distance_pct=_support_distance_pct(candidate),
                independent_support_family_count=_family_count(candidate),
                final_discount_from_current_pct=final_discount_pct,
                arithmetic_limit_basis_upside_pct=(
                    _arithmetic_limit_basis_upside_pct(candidate)
                ),
                arm_results=arm_results,
            )
        )

    coverage = _coverage_summary(snapshot)
    return SessionRecord(
        session_id=snapshot.session_id,
        contract_sha=snapshot.contract_sha,
        policy_sha=snapshot.policy_sha,
        code_sha=snapshot.code_sha,
        source_corpus_as_of=snapshot.source_corpus_as_of,
        decision_cutoff=snapshot.decision_cutoff,
        universe_hash=snapshot.universe_hash,
        input_hash=input_hash,
        frozen_arms=FROZEN_ARMS,
        sources=snapshot.sources,
        candidates=tuple(candidate_records),
        arm_shadow_counts=arm_shadow_counts,
        coverage=coverage,
        interpretation=_interpret_session(candidate_records, coverage),
        read_only_safety=ReadOnlySafety(),
    )


def append_session_jsonl(path: Path, record: SessionRecord) -> None:
    """Append one local log record to an explicitly selected output path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as artifact:
        artifact.write(
            json.dumps(
                record.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
            )
        )
        artifact.write("\n")


def load_session_jsonl(path: Path) -> tuple[SessionRecord, ...]:
    """Load the local records used for the fixed three-session reading."""

    records: list[SessionRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(SessionRecord.model_validate_json(line))
    return tuple(records)


def read_three_completed_sessions(
    records: Sequence[SessionRecord],
) -> ThreeSessionReading:
    """Apply the fixed post-three-session guard without exposing a tuning action."""

    if len(records) != 3:
        raise ValueError("exactly three completed session records are required")
    if len({record.contract_sha for record in records}) != 1:
        raise ValueError("three-session reading requires one frozen contract_sha")
    if len({record.policy_sha for record in records}) != 1:
        raise ValueError("three-session reading requires one policy_sha")
    if len({record.code_sha for record in records}) != 1:
        raise ValueError("three-session reading requires one code_sha")

    counts = {
        arm_id: sum(record.arm_shadow_counts[arm_id] for record in records)
        for arm_id in ("A40", "B30", "C25")
    }
    next_step: Literal[
        "diagnose_target_coverage_or_support_source_contract",
        "continue_bounded_cohort_reading_without_tuning",
    ]
    if counts["B30"] == 0 and counts["C25"] == 0:
        next_step = "diagnose_target_coverage_or_support_source_contract"
    else:
        next_step = "continue_bounded_cohort_reading_without_tuning"
    return ThreeSessionReading(
        session_ids=(
            records[0].session_id,
            records[1].session_id,
            records[2].session_id,
        ),
        a40_shadow_count=counts["A40"],
        b30_shadow_count=counts["B30"],
        c25_shadow_count=counts["C25"],
        next_step=next_step,
    )
