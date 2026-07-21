"""Pure ROB-974 R3 section-5 gate dependence audit.

The adapter boundary deliberately accepts only complete atomic predicate vectors
for context-valid decision units.  It does not accept first-fail histograms and
does not evaluate strategy thresholds.  The R3 generator adapter must therefore
call the same predicate authority used for candidate generation, then supply the
booleans here in the exact closed order below.

No DB, network, broker, clock, randomness, or execution-engine imports.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from itertools import combinations

__all__ = [
    "R3_CONFIG_IDS",
    "S3_GATE_SCHEMA",
    "S4_GATE_SCHEMA",
    "AuditScope",
    "ContextValidDecisionUnit",
    "DominantRemoval",
    "FoldAuditMetrics",
    "GateAuditBatch",
    "GateAuditReport",
    "GateAuditValidationError",
    "GateDefinition",
    "GateSchema",
    "KappaMetric",
    "NamedRate",
    "NullableValue",
    "PairwiseRates",
    "PairwiseTable",
    "RateMetric",
    "RateRange",
    "build_gate_audit",
    "validate_gate_audit",
]

R3_CONFIG_IDS: tuple[str, ...] = tuple(f"S3-R3-{index:02d}" for index in range(3)) + (
    tuple(f"S4-R3-{index:02d}" for index in range(9))
)
_PHASES = ("TRAIN", "OOS")
_FOLD_ID = re.compile(r"fold-0[0-7]\Z")
_NULL_REASONS = (
    "zero_denominator",
    "zero_single_rate_product",
    "no_defined_fold_rates",
)


class GateAuditValidationError(ValueError):
    """The closed gate-audit identity, accounting, or arithmetic drifted."""


def _str(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be built-in str")
    return value


def _int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


def _bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be built-in bool")
    return value


def _sha256(value: object, name: str) -> str:
    text = _str(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{name} must be lowercase SHA-256")
    return text


@dataclass(frozen=True, slots=True)
class GateDefinition:
    name: str
    group: str

    def __post_init__(self) -> None:
        if not _str(self.name, "gate name") or not _str(self.group, "gate group"):
            raise ValueError("gate name/group must not be empty")


@dataclass(frozen=True, slots=True)
class DominantRemoval:
    name: str
    gate_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _str(self.name, "dominant removal name"):
            raise ValueError("dominant removal name must not be empty")
        if type(self.gate_names) is not tuple or not self.gate_names:
            raise TypeError("dominant removal gate_names must be a non-empty tuple")
        for gate_name in self.gate_names:
            _str(gate_name, "dominant removal gate name")
        if len(self.gate_names) != len(set(self.gate_names)):
            raise GateAuditValidationError("duplicate dominant removal gate")


@dataclass(frozen=True, slots=True)
class GateSchema:
    family: str
    gates: tuple[GateDefinition, ...]
    dominant_removals: tuple[DominantRemoval, ...]

    def __post_init__(self) -> None:
        if _str(self.family, "gate schema family") not in ("S3", "S4"):
            raise ValueError("gate schema family must be S3 or S4")
        if type(self.gates) is not tuple or any(
            type(gate) is not GateDefinition for gate in self.gates
        ):
            raise TypeError("gates must be an exact tuple of GateDefinition")
        if type(self.dominant_removals) is not tuple or any(
            type(item) is not DominantRemoval for item in self.dominant_removals
        ):
            raise TypeError(
                "dominant_removals must be an exact tuple of DominantRemoval"
            )
        names = tuple(gate.name for gate in self.gates)
        if not names or len(names) != len(set(names)):
            raise GateAuditValidationError("gate names must be non-empty and unique")
        removal_names = tuple(item.name for item in self.dominant_removals)
        if len(removal_names) != len(set(removal_names)):
            raise GateAuditValidationError("dominant removal names must be unique")
        for removal in self.dominant_removals:
            if any(name not in names for name in removal.gate_names):
                raise GateAuditValidationError("dominant removal names unknown gate")


S3_GATE_SCHEMA = GateSchema(
    family="S3",
    gates=(
        GateDefinition("market_direction", "M"),
        GateDefinition("market_magnitude", "M"),
        GateDefinition("market_breadth", "breadth"),
        GateDefinition("trend_sign_alignment", "S"),
        GateDefinition("trend_magnitude", "S"),
        GateDefinition("efficiency_ratio", "ER"),
        GateDefinition("pullback_depth", "pullback"),
        GateDefinition("vwap_reclaim", "reclaim"),
        GateDefinition("momentum", "momentum"),
        GateDefinition("prior_l_non_breakout", "non_breakout"),
        GateDefinition("volatility_percentile", "volatility"),
        GateDefinition("range_to_tp_capacity", "range_capacity"),
    ),
    dominant_removals=(
        DominantRemoval("S", ("trend_sign_alignment", "trend_magnitude")),
        DominantRemoval("M", ("market_direction", "market_magnitude")),
        DominantRemoval(
            "S+M",
            (
                "market_direction",
                "market_magnitude",
                "trend_sign_alignment",
                "trend_magnitude",
            ),
        ),
    ),
)

S4_GATE_SCHEMA = GateSchema(
    family="S4",
    gates=(
        # ``phi`` is computed after required raw context/finite/denominator
        # checks and rejected before ``evaluate_s4_gates`` is entered.  It is
        # therefore the first context-valid atomic eligibility predicate.  The
        # remaining entries exactly follow evaluate_s4_gates: convergence,
        # prior/current z, convergence fraction, rho, half-life, beta, D, TP,
        # and notional.  Degenerate estimation is required-context failure,
        # never a hidden phi failure or a sequential-order rewrite.
        GateDefinition("phi_open_unit_interval", "phi"),
        GateDefinition("convergence_sign", "convergence"),
        GateDefinition("prior_z_magnitude", "z"),
        GateDefinition("current_z_magnitude", "z"),
        GateDefinition("convergence_fraction", "convergence"),
        GateDefinition("rho", "rho"),
        GateDefinition("half_life", "half_life"),
        GateDefinition("beta_stability", "beta"),
        GateDefinition("d_min_distance", "d"),
        GateDefinition("distance_to_tp", "distance_to_tp"),
        GateDefinition("notional_feasibility", "notional"),
    ),
    dominant_removals=(
        DominantRemoval(
            "prior_current_z_magnitude",
            ("prior_z_magnitude", "current_z_magnitude"),
        ),
        DominantRemoval("d_min", ("d_min_distance",)),
        DominantRemoval(
            "z+d",
            ("prior_z_magnitude", "current_z_magnitude", "d_min_distance"),
        ),
    ),
)

_SCHEMA_BY_FAMILY = {"S3": S3_GATE_SCHEMA, "S4": S4_GATE_SCHEMA}


@dataclass(frozen=True, slots=True)
class AuditScope:
    phase: str
    family: str
    config_id: str
    campaign_identity_sha256: str
    experiment_identity_sha256: str
    config_identity_sha256: str

    def __post_init__(self) -> None:
        if _str(self.phase, "phase") not in _PHASES:
            raise ValueError("phase outside closed phase set TRAIN/OOS")
        family = _str(self.family, "family")
        if family not in ("S3", "S4"):
            raise ValueError("family must be S3 or S4")
        config_id = _str(self.config_id, "config_id")
        if config_id not in R3_CONFIG_IDS or not config_id.startswith(f"{family}-"):
            raise ValueError("family/config mismatch or config outside R3 roster")
        _sha256(self.campaign_identity_sha256, "campaign_identity_sha256")
        _sha256(self.experiment_identity_sha256, "experiment_identity_sha256")
        _sha256(self.config_identity_sha256, "config_identity_sha256")


@dataclass(frozen=True, slots=True)
class ContextValidDecisionUnit:
    unit_id: str
    gate_results: tuple[tuple[str, bool], ...]

    def __post_init__(self) -> None:
        if not _str(self.unit_id, "unit_id"):
            raise ValueError("unit_id must not be empty")
        if type(self.gate_results) is not tuple:
            raise TypeError("gate_results must be an exact tuple")
        for item in self.gate_results:
            if type(item) is not tuple or len(item) != 2:
                raise TypeError("gate result entries must be exact name/bool tuples")
            _str(item[0], "gate result name")
            _bool(item[1], "gate result value")


@dataclass(frozen=True, slots=True)
class GateAuditBatch:
    scope: AuditScope
    fold_id: str
    gate_schema: GateSchema
    evaluated_decision_units: int
    context_valid_denominator: int
    required_context_failures: int
    units: tuple[ContextValidDecisionUnit, ...]

    def __post_init__(self) -> None:
        if type(self.scope) is not AuditScope:
            raise TypeError("scope must be exact AuditScope")
        fold_id = _str(self.fold_id, "fold_id")
        if _FOLD_ID.fullmatch(fold_id) is None:
            raise ValueError("fold_id must be one of fold-00..fold-07")
        if type(self.gate_schema) is not GateSchema:
            raise TypeError("gate_schema must be exact GateSchema")
        if self.gate_schema != _SCHEMA_BY_FAMILY[self.scope.family]:
            raise GateAuditValidationError(
                "unknown, missing, reordered, or regrouped gate schema"
            )
        for name in (
            "evaluated_decision_units",
            "context_valid_denominator",
            "required_context_failures",
        ):
            value = _int(getattr(self, name), name)
            if value < 0:
                raise GateAuditValidationError(f"{name} must not be negative")
        if type(self.units) is not tuple or any(
            type(unit) is not ContextValidDecisionUnit for unit in self.units
        ):
            raise TypeError("units must contain exact ContextValidDecisionUnit values")
        if self.context_valid_denominator != len(self.units) or (
            self.evaluated_decision_units
            != self.context_valid_denominator + self.required_context_failures
        ):
            raise GateAuditValidationError(
                "decision/context denominator equation failed"
            )
        unit_ids = tuple(unit.unit_id for unit in self.units)
        if len(unit_ids) != len(set(unit_ids)):
            raise GateAuditValidationError("duplicate decision unit identity")
        expected_names = tuple(gate.name for gate in self.gate_schema.gates)
        for unit in self.units:
            if tuple(name for name, _ in unit.gate_results) != expected_names:
                raise GateAuditValidationError("atomic gate keys/order mismatch")


@dataclass(frozen=True, slots=True)
class RateMetric:
    numerator: int
    denominator: int
    value: float | None
    reason: str | None

    def __post_init__(self) -> None:
        numerator = _int(self.numerator, "rate numerator")
        denominator = _int(self.denominator, "rate denominator")
        if numerator < 0 or denominator < 0 or numerator > denominator:
            raise GateAuditValidationError("invalid rate counts")
        if denominator == 0:
            if (
                numerator != 0
                or self.value is not None
                or self.reason != "zero_denominator"
            ):
                raise GateAuditValidationError("zero denominator must be closed null")
            return
        if self.reason is not None or type(self.value) is not float:
            raise GateAuditValidationError(
                "defined rate must be finite float without reason"
            )
        if not math.isfinite(self.value) or self.value != numerator / denominator:
            raise GateAuditValidationError("rate value/count mismatch")

    @classmethod
    def from_counts(cls, numerator: int, denominator: int) -> RateMetric:
        if denominator == 0:
            return cls(numerator, denominator, None, "zero_denominator")
        return cls(numerator, denominator, numerator / denominator, None)

    def as_tuple(self) -> tuple[int, int, float | None, str | None]:
        return self.numerator, self.denominator, self.value, self.reason


@dataclass(frozen=True, slots=True)
class NullableValue:
    value: float | None
    reason: str | None

    def __post_init__(self) -> None:
        if self.value is None:
            if self.reason not in _NULL_REASONS:
                raise GateAuditValidationError("null scalar requires closed reason")
        elif (
            type(self.value) is not float
            or not math.isfinite(self.value)
            or self.reason is not None
        ):
            raise GateAuditValidationError(
                "defined scalar must be finite without reason"
            )

    def as_tuple(self) -> tuple[float | None, str | None]:
        return self.value, self.reason


@dataclass(frozen=True, slots=True)
class NamedRate:
    name: str
    rate: RateMetric

    def __post_init__(self) -> None:
        if not _str(self.name, "named rate name"):
            raise ValueError("named rate name must not be empty")
        if type(self.rate) is not RateMetric:
            raise TypeError("named rate must use exact RateMetric")


@dataclass(frozen=True, slots=True)
class KappaMetric:
    joint_rate: RateMetric
    single_rate_product: NullableValue
    kappa: NullableValue


@dataclass(frozen=True, slots=True)
class PairwiseRates:
    n00: RateMetric
    n01: RateMetric
    n10: RateMetric
    n11: RateMetric


@dataclass(frozen=True, slots=True)
class PairwiseTable:
    first_gate: str
    second_gate: str
    denominator: int
    n00: int
    n01: int
    n10: int
    n11: int
    rates: PairwiseRates

    def __post_init__(self) -> None:
        _str(self.first_gate, "first_gate")
        _str(self.second_gate, "second_gate")
        denominator = _int(self.denominator, "pairwise denominator")
        counts = tuple(
            _int(getattr(self, name), name) for name in ("n00", "n01", "n10", "n11")
        )
        if denominator < 0 or any(count < 0 for count in counts):
            raise GateAuditValidationError("pairwise counts must not be negative")
        if sum(counts) != denominator:
            raise GateAuditValidationError(
                "pairwise cell sum does not match denominator"
            )
        if type(self.rates) is not PairwiseRates or tuple(
            getattr(self.rates, name) for name in ("n00", "n01", "n10", "n11")
        ) != tuple(RateMetric.from_counts(count, denominator) for count in counts):
            raise GateAuditValidationError("pairwise rates do not match raw cells")

    @classmethod
    def from_counts(
        cls,
        *,
        first_gate: str,
        second_gate: str,
        denominator: int,
        n00: int,
        n01: int,
        n10: int,
        n11: int,
    ) -> PairwiseTable:
        rates = PairwiseRates(
            n00=RateMetric.from_counts(n00, denominator),
            n01=RateMetric.from_counts(n01, denominator),
            n10=RateMetric.from_counts(n10, denominator),
            n11=RateMetric.from_counts(n11, denominator),
        )
        return cls(first_gate, second_gate, denominator, n00, n01, n10, n11, rates)


@dataclass(frozen=True, slots=True)
class FoldAuditMetrics:
    fold_id: str
    evaluated_decision_units: int
    context_valid_denominator: int
    required_context_failures: int
    required_context_rate: RateMetric
    joint_pass_rate: RateMetric
    single_gate_pass_rates: tuple[NamedRate, ...]
    sequential_conditional_pass_rates: tuple[NamedRate, ...]
    leave_one_gate_out_rates: tuple[NamedRate, ...]
    dominant_removed_rates: tuple[NamedRate, ...]
    kappa: KappaMetric
    pairwise: tuple[PairwiseTable, ...]


@dataclass(frozen=True, slots=True)
class RateRange:
    name: str
    minimum: float | None
    maximum: float | None
    reason: str | None

    def __post_init__(self) -> None:
        if not _str(self.name, "rate range name"):
            raise ValueError("rate range name must not be empty")
        if self.minimum is None or self.maximum is None:
            if (
                self.minimum is not None
                or self.maximum is not None
                or self.reason != "no_defined_fold_rates"
            ):
                raise GateAuditValidationError("undefined range must be closed null")
        elif (
            type(self.minimum) is not float
            or type(self.maximum) is not float
            or not math.isfinite(self.minimum)
            or not math.isfinite(self.maximum)
            or self.minimum > self.maximum
            or self.reason is not None
        ):
            raise GateAuditValidationError("invalid defined rate range")


@dataclass(frozen=True, slots=True)
class GateAuditReport:
    schema_version: str
    scope: AuditScope
    gate_schema: GateSchema
    diagnostic_only: bool
    threshold_authority: bool
    folds: tuple[FoldAuditMetrics, ...]
    pooled: FoldAuditMetrics
    fold_rate_ranges: tuple[RateRange, ...]

    def __post_init__(self) -> None:
        if self.schema_version != "rob974-r3-gate-audit-v1":
            raise GateAuditValidationError("gate audit schema version drift")
        if (
            type(self.scope) is not AuditScope
            or type(self.gate_schema) is not GateSchema
        ):
            raise TypeError("report scope/schema exact type required")
        _bool(self.diagnostic_only, "diagnostic_only")
        _bool(self.threshold_authority, "threshold_authority")
        expected_flags = (self.scope.phase == "OOS", self.scope.phase == "TRAIN")
        if (self.diagnostic_only, self.threshold_authority) != expected_flags:
            raise GateAuditValidationError("phase authority flags drift")
        if (
            type(self.folds) is not tuple
            or not self.folds
            or any(type(fold) is not FoldAuditMetrics for fold in self.folds)
        ):
            raise TypeError("report folds must be non-empty exact tuple")
        if type(self.pooled) is not FoldAuditMetrics or self.pooled.fold_id != "POOLED":
            raise GateAuditValidationError("report pooled metric missing")
        if type(self.fold_rate_ranges) is not tuple or any(
            type(item) is not RateRange for item in self.fold_rate_ranges
        ):
            raise TypeError("fold rate ranges must be exact tuple")


def _rate(passed: int, denominator: int) -> RateMetric:
    return RateMetric.from_counts(passed, denominator)


def _compute_metrics(
    *,
    fold_id: str,
    evaluated_decision_units: int,
    required_context_failures: int,
    units: tuple[ContextValidDecisionUnit, ...],
    schema: GateSchema,
) -> FoldAuditMetrics:
    names = tuple(gate.name for gate in schema.gates)
    rows = tuple(tuple(value for _, value in unit.gate_results) for unit in units)
    denominator = len(rows)
    single = tuple(
        NamedRate(name, _rate(sum(row[index] for row in rows), denominator))
        for index, name in enumerate(names)
    )

    sequential_items: list[NamedRate] = []
    eligible = tuple(True for _ in rows)
    for index, name in enumerate(names):
        eligible_count = sum(eligible)
        passed_count = sum(
            is_eligible and row[index]
            for row, is_eligible in zip(rows, eligible, strict=True)
        )
        sequential_items.append(NamedRate(name, _rate(passed_count, eligible_count)))
        eligible = tuple(
            is_eligible and row[index]
            for row, is_eligible in zip(rows, eligible, strict=True)
        )

    joint_count = sum(all(row) for row in rows)
    leave_one_out = tuple(
        NamedRate(
            name,
            _rate(
                sum(
                    all(
                        value for position, value in enumerate(row) if position != index
                    )
                    for row in rows
                ),
                denominator,
            ),
        )
        for index, name in enumerate(names)
    )
    dominant = tuple(
        NamedRate(
            removal.name,
            _rate(
                sum(
                    all(
                        value
                        for name, value in zip(names, row, strict=True)
                        if name not in removal.gate_names
                    )
                    for row in rows
                ),
                denominator,
            ),
        )
        for removal in schema.dominant_removals
    )

    joint_rate = _rate(joint_count, denominator)
    if denominator == 0:
        product = NullableValue(None, "zero_denominator")
        kappa = NullableValue(None, "zero_denominator")
    else:
        product_value = math.prod(item.rate.value for item in single)
        product = NullableValue(float(product_value), None)
        if product_value == 0.0:
            kappa = NullableValue(None, "zero_single_rate_product")
        else:
            assert joint_rate.value is not None
            kappa = NullableValue(float(joint_rate.value / product_value), None)

    pairwise: list[PairwiseTable] = []
    for first_index, second_index in combinations(range(len(names)), 2):
        counts = [0, 0, 0, 0]
        for row in rows:
            first = row[first_index]
            second = row[second_index]
            counts[(2 if first else 0) + (1 if second else 0)] += 1
        pairwise.append(
            PairwiseTable.from_counts(
                first_gate=names[first_index],
                second_gate=names[second_index],
                denominator=denominator,
                n00=counts[0],
                n01=counts[1],
                n10=counts[2],
                n11=counts[3],
            )
        )

    return FoldAuditMetrics(
        fold_id=fold_id,
        evaluated_decision_units=evaluated_decision_units,
        context_valid_denominator=denominator,
        required_context_failures=required_context_failures,
        required_context_rate=_rate(denominator, evaluated_decision_units),
        joint_pass_rate=joint_rate,
        single_gate_pass_rates=single,
        sequential_conditional_pass_rates=tuple(sequential_items),
        leave_one_gate_out_rates=leave_one_out,
        dominant_removed_rates=dominant,
        kappa=KappaMetric(joint_rate, product, kappa),
        pairwise=tuple(pairwise),
    )


def _rate_entries(metric: FoldAuditMetrics) -> tuple[tuple[str, float | None], ...]:
    entries: list[tuple[str, float | None]] = [
        ("required_context", metric.required_context_rate.value),
        ("joint", metric.joint_pass_rate.value),
    ]
    for prefix, items in (
        ("single", metric.single_gate_pass_rates),
        ("sequential", metric.sequential_conditional_pass_rates),
        ("leave_one_out", metric.leave_one_gate_out_rates),
        ("dominant_removed", metric.dominant_removed_rates),
    ):
        entries.extend((f"{prefix}:{item.name}", item.rate.value) for item in items)
    entries.append(("kappa", metric.kappa.kappa.value))
    for table in metric.pairwise:
        pair_name = f"pair:{table.first_gate}:{table.second_gate}"
        entries.extend(
            (
                f"{pair_name}:{cell}",
                getattr(table.rates, cell).value,
            )
            for cell in ("n00", "n01", "n10", "n11")
        )
    return tuple(entries)


def _ranges(folds: tuple[FoldAuditMetrics, ...]) -> tuple[RateRange, ...]:
    entries_by_fold = tuple(dict(_rate_entries(fold)) for fold in folds)
    names = tuple(entries_by_fold[0])
    result: list[RateRange] = []
    for name in names:
        values = tuple(
            entries[name] for entries in entries_by_fold if entries[name] is not None
        )
        if not values:
            result.append(RateRange(name, None, None, "no_defined_fold_rates"))
        else:
            result.append(RateRange(name, float(min(values)), float(max(values)), None))
    return tuple(result)


def _build(
    *, expected_scope: AuditScope, batches: tuple[GateAuditBatch, ...]
) -> GateAuditReport:
    if type(expected_scope) is not AuditScope:
        raise TypeError("expected_scope must be exact AuditScope")
    if (
        type(batches) is not tuple
        or not batches
        or any(type(batch) is not GateAuditBatch for batch in batches)
    ):
        raise TypeError("batches must be a non-empty exact tuple of GateAuditBatch")
    if any(batch.scope != expected_scope for batch in batches):
        raise GateAuditValidationError("batch TRAIN/OOS or identity scope drift")
    expected_schema = _SCHEMA_BY_FAMILY[expected_scope.family]
    if any(batch.gate_schema != expected_schema for batch in batches):
        raise GateAuditValidationError("batch gate schema drift")
    fold_ids = tuple(batch.fold_id for batch in batches)
    if len(fold_ids) != len(set(fold_ids)):
        raise GateAuditValidationError("duplicate fold identity")
    if fold_ids != tuple(sorted(fold_ids)):
        raise GateAuditValidationError("fold order drift")

    folds = tuple(
        _compute_metrics(
            fold_id=batch.fold_id,
            evaluated_decision_units=batch.evaluated_decision_units,
            required_context_failures=batch.required_context_failures,
            units=batch.units,
            schema=expected_schema,
        )
        for batch in batches
    )
    pooled = _compute_metrics(
        fold_id="POOLED",
        evaluated_decision_units=sum(
            batch.evaluated_decision_units for batch in batches
        ),
        required_context_failures=sum(
            batch.required_context_failures for batch in batches
        ),
        units=tuple(unit for batch in batches for unit in batch.units),
        schema=expected_schema,
    )
    return GateAuditReport(
        schema_version="rob974-r3-gate-audit-v1",
        scope=expected_scope,
        gate_schema=expected_schema,
        diagnostic_only=expected_scope.phase == "OOS",
        threshold_authority=expected_scope.phase == "TRAIN",
        folds=folds,
        pooled=pooled,
        fold_rate_ranges=_ranges(folds),
    )


def build_gate_audit(
    *, expected_scope: AuditScope, batches: tuple[GateAuditBatch, ...]
) -> GateAuditReport:
    """Build an exact fold/pooled report from complete atomic gate vectors."""
    return _build(expected_scope=expected_scope, batches=batches)


def validate_gate_audit(
    *, report: GateAuditReport, batches: tuple[GateAuditBatch, ...]
) -> None:
    """Recompute every field, rejecting arithmetic or label mutation.

    Evidence writers should call this immediately before serialization.  It
    detects pair/LOO/sequential/pooled drift without trusting stored rates.
    """
    if type(report) is not GateAuditReport:
        raise TypeError("report must be exact GateAuditReport")
    expected = _build(expected_scope=report.scope, batches=batches)
    if report != expected:
        raise GateAuditValidationError("report does not match atomic unit evidence")
