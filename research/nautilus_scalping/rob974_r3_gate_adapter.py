"""Production source adapter for ROB-974 R3 gate-dependence evidence.

This additive module leaves every frozen R2 H3/H2 byte untouched.  It emits
complete decision-unit grids, retains finite S4 phi observations outside the
open unit interval, and turns exactly eight walk-forward folds into the atomic
batch DTOs consumed by :mod:`rob974_r3_gate_metrics`.
"""

from __future__ import annotations

import dataclasses
import json
import math
from dataclasses import dataclass, field

import rob974_h3_s3 as s3
import rob974_h3_s4 as s4
from rob944_folds import Fold
from rob974_features import FOUR_HOUR_MS, SYMBOLS
from rob974_h3_manifest import get_config
from rob974_h4_contracts import exact_h4_folds
from rob974_r3_evidence_context import (
    R3ProductionEvidenceContext,
    require_r3_production_evidence_context,
)
from rob974_r3_gate_metrics import (
    S3_GATE_SCHEMA,
    S4_GATE_SCHEMA,
    AuditScope,
    ContextValidDecisionUnit,
    GateAuditBatch,
    GateAuditReport,
    GateAuditValidationError,
    build_gate_audit,
)
from rob974_r3_h3_adapter import (
    R3S4GateObservation,
    evaluate_r3_s3_atoms,
    evaluate_r3_s4_atoms,
)
from rob974_r3_manifest import (
    FROZEN_R3_ROSTER,
    R3S3Config,
    R3S4Config,
    assert_registered_r3_config,
)

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "R3_GATE_UNIT_ID_VERSION",
    "R3_S4_CONTEXT_FAILURES",
    "ProductionFoldGateSource",
    "ProductionGateCampaignEvidence",
    "ProductionGateEvidenceError",
    "R3S4ObservationOutcome",
    "build_production_gate_audit",
    "build_production_gate_batches",
    "build_production_gate_campaign_evidence",
    "build_r3_s3_fold_source",
    "build_r3_s4_fold_source",
    "canonical_gate_unit_id",
    "observe_r3_s4_pair",
    "production_gate_audit_scope",
    "r3_gate_config_identity_sha256",
]

R3_GATE_UNIT_ID_VERSION = "rob974-r3-gate-unit-v1"
R3_S4_CONTEXT_FAILURES = (
    "missing_required_context",
    "nonfinite_required_input",
    "degenerate_beta_market_variance",
    "degenerate_rho_variance",
    "degenerate_phi_denominator",
)
_PRODUCTION_EVIDENCE_ENVELOPE_SEAL = object()


class ProductionGateEvidenceError(GateAuditValidationError):
    """Campaign-level production gate evidence is incomplete or misbound."""


def canonical_gate_unit_id(
    *, family: str, decision_ts: int, symbol_or_pair: str
) -> str:
    if family not in ("S3", "S4"):
        raise ValueError("family must be S3 or S4")
    if type(decision_ts) is not int:
        raise TypeError("decision_ts must be built-in int")
    if type(symbol_or_pair) is not str or not symbol_or_pair:
        raise TypeError("symbol_or_pair must be a non-empty built-in str")
    return json.dumps(
        [R3_GATE_UNIT_ID_VERSION, family, decision_ts, symbol_or_pair],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def r3_gate_config_identity_sha256(config: R3S3Config | R3S4Config) -> str:
    if type(config) not in (R3S3Config, R3S4Config):
        raise TypeError("config must be an exact R3 config DTO")
    assert_registered_r3_config(config)
    family = "S3" if type(config) is R3S3Config else "S4"
    parameters = {
        field.name: getattr(config, field.name)
        for field in dataclasses.fields(config)
        if field.name not in ("config_id", "planning_class")
    }
    return canonical_sha256(
        {
            "schema_version": "rob974-r3-config-identity-v1",
            "family": family,
            "config_id": config.config_id,
            "parameters": parameters,
        }
    )


def _scope_from_checked_context(
    *,
    evidence_context: R3ProductionEvidenceContext,
    config: R3S3Config | R3S4Config,
    phase: str,
) -> AuditScope:
    if type(config) not in (R3S3Config, R3S4Config):
        raise TypeError("config must be an exact R3 config DTO")
    assert_registered_r3_config(config)
    if phase not in evidence_context.phases:
        raise ProductionGateEvidenceError(
            "production gate phase must be exact TRAIN or OOS"
        )
    experiment_by_config = dict(evidence_context.ordered_mapping)
    try:
        experiment_id = experiment_by_config[config.config_id]
    except KeyError as exc:  # pragma: no cover - context independently seals this
        raise ProductionGateEvidenceError(
            "config is absent from the issued exact-12 mapping"
        ) from exc
    return AuditScope(
        phase=phase,
        family="S3" if type(config) is R3S3Config else "S4",
        config_id=config.config_id,
        campaign_identity_sha256=evidence_context.campaign_identity_sha256,
        experiment_identity_sha256=experiment_id,
        config_identity_sha256=r3_gate_config_identity_sha256(config),
    )


def production_gate_audit_scope(
    *,
    evidence_context: object,
    config: R3S3Config | R3S4Config,
    phase: str,
) -> AuditScope:
    """Derive, rather than accept, one plan-bound production report scope."""

    checked = require_r3_production_evidence_context(evidence_context)
    return _scope_from_checked_context(
        evidence_context=checked,
        config=config,
        phase=phase,
    )


@dataclass(frozen=True, slots=True)
class R3S4ObservationOutcome:
    decision_ts: int
    pair: str
    observation: R3S4GateObservation | None
    context_failure_reason: str | None

    def __post_init__(self) -> None:
        if type(self.decision_ts) is not int:
            raise TypeError("decision_ts must be built-in int")
        if type(self.pair) is not str or self.pair not in s4.PAIR_ORDER:
            raise ValueError("pair outside frozen S4 order")
        if (self.observation is None) == (self.context_failure_reason is None):
            raise ValueError(
                "outcome must contain exactly observation or context failure"
            )
        if self.observation is not None:
            if type(self.observation) is not R3S4GateObservation:
                raise TypeError("observation must be exact R3S4GateObservation")
            if (
                self.observation.decision_ts != self.decision_ts
                or self.observation.pair != self.pair
            ):
                raise ValueError("S4 observation identity mismatch")
        elif self.context_failure_reason not in R3_S4_CONTEXT_FAILURES:
            raise ValueError("S4 context failure reason outside closed taxonomy")


GateSourceUnit = s3.S3FormulaUnit | R3S4ObservationOutcome


@dataclass(frozen=True, slots=True)
class ProductionFoldGateSource:
    fold: Fold
    units: tuple[GateSourceUnit, ...]

    def __post_init__(self) -> None:
        if type(self.fold) is not Fold:
            raise TypeError("fold must be exact ROB-944 Fold")
        if type(self.units) is not tuple:
            raise TypeError("units must be an exact tuple")


def _context_failure(
    decision_ts: int, pair: str, reason: str
) -> R3S4ObservationOutcome:
    return R3S4ObservationOutcome(decision_ts, pair, None, reason)


def observe_r3_s4_pair(
    feature_context: s3.FeatureContext,
    config: R3S4Config,
    decision_ts: int,
    pair: str,
) -> R3S4ObservationOutcome:
    """Extract all atomic inputs without treating finite phi outside (0, 1) as context loss."""

    if type(feature_context) is not s3.FeatureContext:
        raise TypeError("feature_context must be exact FeatureContext")
    if type(config) is not R3S4Config:
        raise TypeError("config must be exact R3S4Config")
    assert_registered_r3_config(config)
    if type(decision_ts) is not int or decision_ts % FOUR_HOUR_MS:
        raise ValueError("decision_ts must be an exact UTC 4h close")
    if type(pair) is not str or pair not in s4.PAIR_ORDER:
        raise ValueError("pair outside frozen S4 order")
    left, right = pair.split("-", maxsplit=1)
    symbols = (f"{left}USDT", f"{right}USDT")

    position = next(
        (
            index
            for index, snapshot in enumerate(feature_context.snapshots)
            if snapshot.decision_ts == decision_ts
        ),
        None,
    )
    if position is None or position < config.W:
        return _context_failure(decision_ts, pair, "missing_required_context")
    combined = feature_context.snapshots[position - config.W : position + 1]
    if len(combined) != config.W + 1 or any(
        later.decision_ts != earlier.decision_ts + FOUR_HOUR_MS
        for earlier, later in zip(combined, combined[1:], strict=False)
    ):
        return _context_failure(decision_ts, pair, "missing_required_context")
    for symbol in symbols:
        by_close = {bar.close_ts: bar for bar in feature_context.bars_for(symbol)}
        selected = tuple(by_close.get(snapshot.decision_ts) for snapshot in combined)
        if any(bar is None for bar in selected):
            return _context_failure(decision_ts, pair, "missing_required_context")
        exact_bars = tuple(bar for bar in selected if bar is not None)
        if any(
            later.ts != earlier.close_ts or later.is_segment_start
            for earlier, later in zip(exact_bars, exact_bars[1:], strict=False)
        ):
            return _context_failure(decision_ts, pair, "missing_required_context")

    prior_window = s4._window(feature_context, combined[:-1], symbols)
    current_window = s4._window(feature_context, combined[1:], symbols)
    if prior_window is None or current_window is None:
        return _context_failure(decision_ts, pair, "missing_required_context")
    required = (
        current_window.returns_a
        + current_window.returns_b
        + current_window.market
        + current_window.closes_a
        + current_window.closes_b
        + prior_window.returns_a
        + prior_window.returns_b
        + prior_window.market
        + prior_window.closes_a
        + prior_window.closes_b
    )
    if not s4._all_finite(required):
        return _context_failure(decision_ts, pair, "nonfinite_required_input")

    beta_a = s4.compute_clipped_beta(current_window.returns_a, current_window.market)
    beta_b = s4.compute_clipped_beta(current_window.returns_b, current_window.market)
    prior_beta_a = s4.compute_clipped_beta(prior_window.returns_a, prior_window.market)
    prior_beta_b = s4.compute_clipped_beta(prior_window.returns_b, prior_window.market)
    if None in (beta_a, beta_b, prior_beta_a, prior_beta_b):
        return _context_failure(decision_ts, pair, "degenerate_beta_market_variance")
    assert beta_a is not None and beta_b is not None
    assert prior_beta_a is not None and prior_beta_b is not None
    half = config.W // 2
    split_betas = (
        s4.compute_clipped_beta(
            current_window.returns_a[:half], current_window.market[:half]
        ),
        s4.compute_clipped_beta(
            current_window.returns_a[half:], current_window.market[half:]
        ),
        s4.compute_clipped_beta(
            current_window.returns_b[:half], current_window.market[:half]
        ),
        s4.compute_clipped_beta(
            current_window.returns_b[half:], current_window.market[half:]
        ),
    )
    if any(value is None for value in split_betas):
        return _context_failure(decision_ts, pair, "degenerate_beta_market_variance")
    beta_a_first, beta_a_second, beta_b_first, beta_b_second = split_betas
    assert beta_a_first is not None and beta_a_second is not None
    assert beta_b_first is not None and beta_b_second is not None

    rho = s4.correlation(current_window.returns_a, current_window.returns_b)
    if rho is None:
        return _context_failure(decision_ts, pair, "degenerate_rho_variance")
    weight_a = beta_b / (beta_a + beta_b)
    weight_b = beta_a / (beta_a + beta_b)
    prior_weight_a = prior_beta_b / (prior_beta_a + prior_beta_b)
    prior_weight_b = prior_beta_a / (prior_beta_a + prior_beta_b)
    current_spreads = s4._spreads(current_window, weight_a, weight_b)
    prior_spreads = s4._spreads(prior_window, prior_weight_a, prior_weight_b)
    if not s4._all_finite(current_spreads + prior_spreads):
        return _context_failure(decision_ts, pair, "nonfinite_required_input")
    current_stats = s4.spread_statistics(current_spreads)
    prior_stats = s4.spread_statistics(prior_spreads)
    if current_stats is None or prior_stats is None:
        return _context_failure(decision_ts, pair, "nonfinite_required_input")
    z_current = (current_spreads[-1] - current_stats.mu) / current_stats.effective_scale
    z_prior = (prior_spreads[-1] - prior_stats.mu) / prior_stats.effective_scale
    phi, denominator_degenerate = s4._phi_raw(current_spreads, current_stats.mu)
    if phi is None:
        reason = (
            "degenerate_phi_denominator"
            if denominator_degenerate
            else "nonfinite_required_input"
        )
        return _context_failure(decision_ts, pair, reason)
    half_life = math.log(0.5) / math.log(phi) if 0.0 < phi < 1.0 else None
    if half_life is not None and not math.isfinite(half_life):
        return _context_failure(decision_ts, pair, "nonfinite_required_input")
    pair_returns = tuple(
        weight_a * a_return - weight_b * b_return
        for a_return, b_return in zip(
            current_window.returns_a, current_window.returns_b, strict=True
        )
    )
    sigma_pair = s4.population_sigma(pair_returns)
    if sigma_pair is None:
        return _context_failure(decision_ts, pair, "nonfinite_required_input")
    beta_stability = max(
        abs(beta_a_first - beta_a_second) / beta_a,
        abs(beta_b_first - beta_b_second) / beta_b,
    )
    d_fraction = abs(current_spreads[-1] - current_stats.mu)
    final = (
        phi,
        z_prior,
        z_current,
        rho,
        beta_stability,
        d_fraction,
        sigma_pair,
        weight_a,
        weight_b,
    )
    if not all(type(value) is float and math.isfinite(value) for value in final):
        return _context_failure(decision_ts, pair, "nonfinite_required_input")
    observation = R3S4GateObservation(
        config_id=config.config_id,
        decision_ts=decision_ts,
        pair=pair,
        phi=phi,
        z_prior=z_prior,
        z_current=z_current,
        rho=rho,
        half_life_4h_bars=half_life,
        beta_stability=beta_stability,
        d_bps=d_fraction * 10_000.0,
        d_fraction=d_fraction,
        sigma_pair=sigma_pair,
        weight_a=weight_a,
        weight_b=weight_b,
    )
    return R3S4ObservationOutcome(decision_ts, pair, observation, None)


def _phase_window(fold: Fold, phase: str) -> s3.EmitWindow:
    if phase == "TRAIN":
        return s3.EmitWindow(fold.train_start_ms, fold.train_end_ms)
    if phase == "OOS":
        return s3.EmitWindow(fold.oos_start_ms, fold.oos_end_ms)
    raise ValueError("phase must be TRAIN or OOS")


def build_r3_s3_fold_source(
    *, feature_context: s3.FeatureContext, fold: Fold, phase: str, config: R3S3Config
) -> ProductionFoldGateSource:
    if type(config) is not R3S3Config:
        raise TypeError("config must be exact R3S3Config")
    assert_registered_r3_config(config)
    anchor = get_config("S3-03")
    if type(anchor) is not s3.S3Config:
        raise AssertionError("frozen S3 anchor type drift")
    units = s3.s3_formula_grid(feature_context, _phase_window(fold, phase), anchor)
    rebound = tuple(
        s3.S3FormulaUnit(
            unit.decision_ts,
            unit.symbol,
            (
                dataclasses.replace(unit.metrics, config_id=config.config_id)
                if unit.metrics is not None
                else None
            ),
        )
        for unit in units
    )
    return ProductionFoldGateSource(fold, rebound)


def build_r3_s4_fold_source(
    *, feature_context: s3.FeatureContext, fold: Fold, phase: str, config: R3S4Config
) -> ProductionFoldGateSource:
    if type(config) is not R3S4Config:
        raise TypeError("config must be exact R3S4Config")
    assert_registered_r3_config(config)
    window = _phase_window(fold, phase)
    units = tuple(
        observe_r3_s4_pair(feature_context, config, decision_ts, pair)
        for decision_ts in s3.expected_decision_closes(window)
        for pair in s4.PAIR_ORDER
    )
    return ProductionFoldGateSource(fold, units)


def _source_identity(unit: GateSourceUnit) -> tuple[int, str]:
    if type(unit) is s3.S3FormulaUnit:
        return unit.decision_ts, unit.symbol
    if type(unit) is R3S4ObservationOutcome:
        return unit.decision_ts, unit.pair
    raise TypeError("production source contains an unknown unit type")


def build_production_gate_batches(
    *,
    evidence_context: object,
    phase: str,
    config: R3S3Config | R3S4Config,
    fold_sources: tuple[ProductionFoldGateSource, ...],
) -> tuple[GateAuditBatch, ...]:
    """Build exact-eight, complete-grid TRAIN/OOS batches in canonical order."""

    checked_context = require_r3_production_evidence_context(evidence_context)
    scope = _scope_from_checked_context(
        evidence_context=checked_context,
        config=config,
        phase=phase,
    )
    if (
        type(fold_sources) is not tuple
        or len(fold_sources) != 8
        or any(type(item) is not ProductionFoldGateSource for item in fold_sources)
    ):
        raise GateAuditValidationError("production audit requires exactly eight folds")
    if tuple(item.fold for item in fold_sources) != exact_h4_folds():
        raise GateAuditValidationError(
            "production folds differ from exact H4 authority"
        )

    schema = S3_GATE_SCHEMA if scope.family == "S3" else S4_GATE_SCHEMA
    roster = SYMBOLS if scope.family == "S3" else s4.PAIR_ORDER
    batches: list[GateAuditBatch] = []
    for source in fold_sources:
        window = _phase_window(source.fold, scope.phase)
        expected = tuple(
            (decision_ts, member)
            for decision_ts in s3.expected_decision_closes(window)
            for member in roster
        )
        actual = tuple(_source_identity(unit) for unit in source.units)
        if actual != expected:
            raise GateAuditValidationError(
                f"{source.fold.fold_id}: incomplete, duplicate, or reordered source grid"
            )
        context_failures = 0
        audited: list[ContextValidDecisionUnit] = []
        for unit in source.units:
            decision_ts, member = _source_identity(unit)
            if scope.family == "S3":
                if type(unit) is not s3.S3FormulaUnit:
                    raise TypeError("S3 production source requires exact S3FormulaUnit")
                if unit.metrics is None:
                    context_failures += 1
                    continue
                if unit.metrics.config_id != config.config_id:
                    raise GateAuditValidationError("S3 source/config identity mismatch")
                if type(config) is not R3S3Config:
                    raise TypeError("S3 scope requires exact R3S3Config")
                atomic = evaluate_r3_s3_atoms(unit.metrics, config)
                gate_results = atomic.gate_results
                market_direction = atomic.side
            else:
                if type(unit) is not R3S4ObservationOutcome:
                    raise TypeError(
                        "S4 production source requires exact observation outcome"
                    )
                if unit.observation is None:
                    context_failures += 1
                    continue
                if unit.observation.config_id != config.config_id:
                    raise GateAuditValidationError("S4 source/config identity mismatch")
                if type(config) is not R3S4Config:
                    raise TypeError("S4 scope requires exact R3S4Config")
                gate_results = evaluate_r3_s4_atoms(
                    unit.observation, config
                ).gate_results
                market_direction = None
            audited.append(
                ContextValidDecisionUnit(
                    canonical_gate_unit_id(
                        family=scope.family,
                        decision_ts=decision_ts,
                        symbol_or_pair=member,
                    ),
                    gate_results,
                    market_direction,
                )
            )
        batches.append(
            GateAuditBatch(
                scope=scope,
                fold_id=source.fold.fold_id,
                gate_schema=schema,
                evaluated_decision_units=len(source.units),
                context_valid_denominator=len(audited),
                required_context_failures=context_failures,
                units=tuple(audited),
            )
        )
    return tuple(batches)


def build_production_gate_audit(
    *,
    evidence_context: object,
    phase: str,
    config: R3S3Config | R3S4Config,
    fold_sources: tuple[ProductionFoldGateSource, ...],
) -> GateAuditReport:
    scope = production_gate_audit_scope(
        evidence_context=evidence_context,
        config=config,
        phase=phase,
    )
    batches = build_production_gate_batches(
        evidence_context=evidence_context,
        phase=phase,
        config=config,
        fold_sources=fold_sources,
    )
    return build_gate_audit(expected_scope=scope, batches=batches)


@dataclass(frozen=True, slots=True)
class ProductionGateCampaignEvidence:
    schema_version: str
    campaign_identity_sha256: str
    campaign_run_id: str
    exact_12_mapping_hash: str
    ordered_mapping: tuple[tuple[str, str], ...]
    reports: tuple[GateAuditReport, ...]
    evidence_cell_order: tuple[tuple[str, str, str], ...]
    coverage_complete: bool
    operational_status: str
    incomplete_reason: str | None
    evidence_promoted: bool
    _evidence_context: R3ProductionEvidenceContext = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_production_gate_campaign_evidence(self)


def _validate_production_gate_campaign_evidence(
    envelope: ProductionGateCampaignEvidence,
) -> None:
    if envelope._seal is not _PRODUCTION_EVIDENCE_ENVELOPE_SEAL:
        raise ProductionGateEvidenceError(
            "production gate campaign evidence was not code-issued"
        )
    context = require_r3_production_evidence_context(envelope._evidence_context)
    if envelope.schema_version != "rob974-r3-gate-campaign-evidence-v1":
        raise ProductionGateEvidenceError("campaign evidence schema version drifted")
    if type(envelope.reports) is not tuple or any(
        type(report) is not GateAuditReport for report in envelope.reports
    ):
        raise ProductionGateEvidenceError(
            "campaign evidence reports must be an exact GateAuditReport tuple"
        )
    expected_headers = tuple(
        (config.config_id, phase)
        for config in FROZEN_R3_ROSTER
        for phase in context.phases
    )
    actual_headers = tuple(
        (report.scope.config_id, report.scope.phase) for report in envelope.reports
    )
    if actual_headers != expected_headers:
        raise ProductionGateEvidenceError(
            "campaign evidence requires canonical 12x2 report order"
        )
    expected_fold_ids = tuple(fold.fold_id for fold in context.folds)
    for report, config, phase in zip(
        envelope.reports,
        (config for config in FROZEN_R3_ROSTER for _phase in context.phases),
        (phase for _config in FROZEN_R3_ROSTER for phase in context.phases),
        strict=True,
    ):
        expected_scope = _scope_from_checked_context(
            evidence_context=context,
            config=config,
            phase=phase,
        )
        if report.scope != expected_scope:
            raise ProductionGateEvidenceError(
                f"{config.config_id}/{phase}: report differs from its issued scope"
            )
        if tuple(fold.fold_id for fold in report.folds) != expected_fold_ids:
            raise ProductionGateEvidenceError(
                f"{config.config_id}/{phase}: report lacks exact eight-fold evidence"
            )
    expected_cells = tuple(
        (config.config_id, phase, fold.fold_id)
        for config in FROZEN_R3_ROSTER
        for phase in context.phases
        for fold in context.folds
    )
    if envelope.evidence_cell_order != expected_cells:
        raise ProductionGateEvidenceError("campaign evidence 192-cell order drifted")
    identity_fields = (
        envelope.campaign_identity_sha256 == context.campaign_identity_sha256,
        envelope.campaign_run_id == context.campaign_run_id,
        envelope.exact_12_mapping_hash == context.exact_12_mapping_hash,
        envelope.ordered_mapping == context.ordered_mapping,
    )
    if not all(identity_fields):
        raise ProductionGateEvidenceError(
            "campaign evidence identity differs from the issued plan"
        )
    promoted = context.operational_status == "COMPLETE"
    expected_reason = None if promoted else context.operational_blocker_reason
    if (
        envelope.coverage_complete is not True
        or envelope.operational_status != context.operational_status
        or envelope.incomplete_reason != expected_reason
        or envelope.evidence_promoted is not promoted
    ):
        raise ProductionGateEvidenceError(
            "campaign evidence promotion differs from plan operational status"
        )


def build_production_gate_campaign_evidence(
    *,
    evidence_context: object,
    reports: tuple[GateAuditReport, ...],
) -> ProductionGateCampaignEvidence:
    """Validate exact config-major 12x2x8 coverage without lifting CP3."""

    context = require_r3_production_evidence_context(evidence_context)
    promoted = context.operational_status == "COMPLETE"
    return ProductionGateCampaignEvidence(
        schema_version="rob974-r3-gate-campaign-evidence-v1",
        campaign_identity_sha256=context.campaign_identity_sha256,
        campaign_run_id=context.campaign_run_id,
        exact_12_mapping_hash=context.exact_12_mapping_hash,
        ordered_mapping=context.ordered_mapping,
        reports=reports,
        evidence_cell_order=tuple(
            (config.config_id, phase, fold.fold_id)
            for config in FROZEN_R3_ROSTER
            for phase in context.phases
            for fold in context.folds
        ),
        coverage_complete=True,
        operational_status=context.operational_status,
        incomplete_reason=(None if promoted else context.operational_blocker_reason),
        evidence_promoted=promoted,
        _evidence_context=context,
        _seal=_PRODUCTION_EVIDENCE_ENVELOPE_SEAL,
    )
