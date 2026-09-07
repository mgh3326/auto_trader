"""ROB-1351 v2 symmetric A/B buy-gate evaluation.

The v2 population is independently pre-registered.  In particular, its
three shared review bits are fail-closed: a missing or non-boolean value is
evaluated as ``False`` and keeps the record out of the experiment sample.
This module is pure and does not import the sealed v1 evaluator.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.services.buy_gate_ab_shadow.spec_v2 import PRE_REGISTRATION_V2

Market = Literal["kr", "us"]
Cohort = Literal["a_and_b", "b_only", "neither"]
VariantACohortLabel = Literal["strong", "moderate_only"]


class EvaluationError(ValueError):
    """Caller input cannot be evaluated fail-closed."""


@dataclass(frozen=True, slots=True)
class _EvaluationConfig:
    """The v2 decision surface read directly from its registration."""

    rsi_max: Decimal
    support_within_pct: Decimal
    upside_min_pct: Decimal
    other_gate_keys: tuple[str, ...]
    strength_rank: dict[str, int]
    a_support_min: str
    b_support_min: str
    allowed_markets: frozenset[str]
    a_role: str
    a_executes: bool
    b_role: str
    b_executes: bool
    b_register_as: str
    strong_label: str
    moderate_only_label: str


def _config() -> _EvaluationConfig:
    """Build the evaluator configuration from the v2 registration only.

    This intentionally happens at evaluation time.  It keeps the evidence
    parser and the verdict calculation on the same reviewed v2 payload, and
    makes a deliberate test copy of that payload observably affect behavior.
    """

    registration = PRE_REGISTRATION_V2
    shared = registration["shared_gates"]
    variant_a = registration["variant_a"]
    variant_b = registration["variant_b"]
    labels = variant_a["cohort_labels"]
    return _EvaluationConfig(
        rsi_max=Decimal(str(shared["rsi_max"])),
        support_within_pct=Decimal(str(shared["support_within_pct"])),
        upside_min_pct=Decimal(str(shared["upside_min_pct"])),
        other_gate_keys=tuple(str(key) for key in shared["other_gate_bit_keys"]),
        strength_rank={
            str(strength): index
            for index, strength in enumerate(registration["support_strength_order"])
        },
        a_support_min=str(variant_a["support_strength_min"]),
        b_support_min=str(variant_b["support_strength_min"]),
        allowed_markets=frozenset(str(market) for market in registration["markets"]),
        a_role=str(variant_a["role"]),
        a_executes=bool(variant_a["executes"]),
        b_role=str(variant_b["role"]),
        b_executes=bool(variant_b["executes"]),
        b_register_as=str(variant_b["register_as"]),
        strong_label=str(labels[0]),
        moderate_only_label=str(labels[1]),
    )


def _as_decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, Decimal):
        number = value
    else:
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise EvaluationError(f"{field} is not a finite number") from exc
    if not number.is_finite():
        raise EvaluationError(f"{field} is not a finite number")
    return number


def _optional_decimal(value: object, *, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    return _as_decimal(value, field=field)


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    """One v2 review snapshot consumed identically by variants A and B."""

    symbol: str
    market: Market
    current_price: Decimal
    support_strength: str
    support_distance_pct: Decimal | None
    rsi: Decimal | None
    honest_upside_pct: Decimal | None
    other_gate_bits: Mapping[str, bool]
    shared_gate_bits_supplied: bool

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> CandidateEvidence:
        config = _config()
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol:
            raise EvaluationError("symbol is required")
        market = str(raw.get("market") or "").strip().lower()
        if market not in config.allowed_markets:
            raise EvaluationError("market must be kr or us")
        strength = str(raw.get("support_strength") or "").strip().lower()
        bits_raw = raw.get("other_gate_bits")
        supplied = isinstance(bits_raw, Mapping)
        raw_bits = bits_raw if isinstance(bits_raw, Mapping) else {}
        bits: dict[str, bool] = {}
        for key in config.other_gate_keys:
            value = raw_bits.get(key)
            if type(value) is bool:
                bits[key] = value
            else:
                # v2's registration requires False for missing/non-boolean
                # bits.  Retain the provenance separately so tagging cannot
                # mistake a coerced reject for a supplied experiment sample.
                bits[key] = False
                supplied = False
        price = _as_decimal(raw.get("current_price"), field="current_price")
        if price <= 0:
            raise EvaluationError("current_price must be positive")
        return cls(
            symbol=symbol,
            market=market,  # type: ignore[arg-type]
            current_price=price,
            support_strength=strength,
            support_distance_pct=_optional_decimal(
                raw.get("support_distance_pct"), field="support_distance_pct"
            ),
            rsi=_optional_decimal(raw.get("rsi"), field="rsi"),
            honest_upside_pct=_optional_decimal(
                raw.get("honest_upside_pct"), field="honest_upside_pct"
            ),
            other_gate_bits=bits,
            shared_gate_bits_supplied=supplied,
        )

    def input_snapshot(self) -> dict[str, Any]:
        """Return the normalized v2 gate input shared by both variants."""

        config = _config()
        return {
            "symbol": self.symbol,
            "market": self.market,
            "current_price": str(self.current_price),
            "support_strength": self.support_strength,
            "support_distance_pct": (
                None
                if self.support_distance_pct is None
                else str(self.support_distance_pct)
            ),
            "rsi": None if self.rsi is None else str(self.rsi),
            "honest_upside_pct": (
                None if self.honest_upside_pct is None else str(self.honest_upside_pct)
            ),
            "other_gate_bits": {
                key: self.other_gate_bits[key] for key in config.other_gate_keys
            },
            "shared_gate_bits_supplied": self.shared_gate_bits_supplied,
        }

    def input_snapshot_sha256(self) -> str:
        payload = json.dumps(
            self.input_snapshot(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class VariantVerdict:
    variant: Literal["A", "B"]
    role: str
    executes: bool
    register_as: str | None
    passed: bool
    reject_reasons: tuple[str, ...]
    support_strength_min: str


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    symbol: str
    market: Market
    evaluation_as_of: datetime
    entry_price: Decimal
    input_snapshot: Mapping[str, Any]
    input_snapshot_sha256: str
    support_strength: str
    variant_a_cohort_label: VariantACohortLabel
    shared_gate_bits_supplied: bool
    shared_reject_reasons: tuple[str, ...]
    variant_a: VariantVerdict
    variant_b: VariantVerdict
    cohort: Cohort
    shadow_buy: bool

    @property
    def a_cohort_label(self) -> VariantACohortLabel:
        """Short alias for the v2 A-arm observational cohort label."""

        return self.variant_a_cohort_label

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market": self.market,
            "evaluation_as_of": self.evaluation_as_of.isoformat(),
            "entry_price": str(self.entry_price),
            "input_snapshot": dict(self.input_snapshot),
            "input_snapshot_sha256": self.input_snapshot_sha256,
            "support_strength": self.support_strength,
            "variant_a_cohort_label": self.variant_a_cohort_label,
            "shared_gate_bits_supplied": self.shared_gate_bits_supplied,
            "shared_reject_reasons": list(self.shared_reject_reasons),
            "variant_a": {
                "variant": self.variant_a.variant,
                "role": self.variant_a.role,
                "executes": self.variant_a.executes,
                "passed": self.variant_a.passed,
                "reject_reasons": list(self.variant_a.reject_reasons),
                "support_strength_min": self.variant_a.support_strength_min,
            },
            "variant_b": {
                "variant": self.variant_b.variant,
                "role": self.variant_b.role,
                "executes": self.variant_b.executes,
                "register_as": self.variant_b.register_as,
                "passed": self.variant_b.passed,
                "reject_reasons": list(self.variant_b.reject_reasons),
                "support_strength_min": self.variant_b.support_strength_min,
            },
            "cohort": self.cohort,
            "shadow_buy": self.shadow_buy,
            "promote": False,
            "live_gate_impact": False,
        }


def _shared_reject_reasons(
    evidence: CandidateEvidence, config: _EvaluationConfig
) -> list[str]:
    reasons: list[str] = []
    if evidence.rsi is None or evidence.rsi >= config.rsi_max:
        reasons.append("rsi_not_below_max")
    if (
        evidence.support_distance_pct is None
        or evidence.support_distance_pct < 0
        or evidence.support_distance_pct > config.support_within_pct
    ):
        reasons.append("support_not_within_pct")
    if (
        evidence.honest_upside_pct is None
        or evidence.honest_upside_pct < config.upside_min_pct
    ):
        reasons.append("honest_upside_below_min")
    for key in config.other_gate_keys:
        if not evidence.other_gate_bits.get(key, False):
            reasons.append(f"other_gate_{key}_failed")
    return reasons


def _support_ok(strength: str, required: str, config: _EvaluationConfig) -> bool:
    have = config.strength_rank.get(strength, -1)
    need = config.strength_rank.get(required)
    if need is None:
        raise EvaluationError("v2 support strength registration is invalid")
    return have >= need


def _verdict(
    *,
    variant: Literal["A", "B"],
    role: str,
    executes: bool,
    register_as: str | None,
    required: str,
    evidence: CandidateEvidence,
    shared: Sequence[str],
    config: _EvaluationConfig,
) -> VariantVerdict:
    reasons = list(shared)
    if not _support_ok(evidence.support_strength, required, config):
        reasons.append(f"support_strength_below_{required}")
    return VariantVerdict(
        variant=variant,
        role=role,
        executes=executes,
        register_as=register_as,
        passed=not reasons,
        reject_reasons=tuple(reasons),
        support_strength_min=required,
    )


def evaluate_candidate(
    evidence: CandidateEvidence,
    *,
    evaluation_as_of: datetime,
) -> CandidateEvaluation:
    """Apply the v2 sealed gate to one reviewed candidate snapshot."""

    if evaluation_as_of.tzinfo is None:
        raise EvaluationError("evaluation_as_of must be timezone-aware")
    config = _config()
    shared = _shared_reject_reasons(evidence, config)
    variant_a = _verdict(
        variant="A",
        role=config.a_role,
        executes=config.a_executes,
        register_as=None,
        required=config.a_support_min,
        evidence=evidence,
        shared=shared,
        config=config,
    )
    variant_b = _verdict(
        variant="B",
        role=config.b_role,
        executes=config.b_executes,
        register_as=config.b_register_as,
        required=config.b_support_min,
        evidence=evidence,
        shared=shared,
        config=config,
    )
    if variant_a.passed and not variant_b.passed:
        raise EvaluationError("variant A cannot pass when variant B fails")
    if variant_a.passed:
        cohort: Cohort = "a_and_b"
    elif variant_b.passed:
        cohort = "b_only"
    else:
        cohort = "neither"
    a_label: VariantACohortLabel = (
        "strong" if evidence.support_strength == "strong" else "moderate_only"
    )
    if a_label == "strong" and config.strong_label != a_label:
        raise EvaluationError("v2 strong cohort label registration is invalid")
    if a_label == "moderate_only" and config.moderate_only_label != a_label:
        raise EvaluationError("v2 moderate cohort label registration is invalid")
    return CandidateEvaluation(
        symbol=evidence.symbol,
        market=evidence.market,
        evaluation_as_of=evaluation_as_of,
        entry_price=evidence.current_price,
        input_snapshot=evidence.input_snapshot(),
        input_snapshot_sha256=evidence.input_snapshot_sha256(),
        support_strength=evidence.support_strength,
        variant_a_cohort_label=a_label,
        shared_gate_bits_supplied=evidence.shared_gate_bits_supplied,
        shared_reject_reasons=tuple(shared),
        variant_a=variant_a,
        variant_b=variant_b,
        cohort=cohort,
        shadow_buy=cohort == "b_only",
    )


def evaluate_candidates(
    rows: Sequence[Mapping[str, Any]],
    *,
    evaluation_as_of: datetime,
) -> list[CandidateEvaluation]:
    return [
        evaluate_candidate(
            CandidateEvidence.from_mapping(row),
            evaluation_as_of=evaluation_as_of,
        )
        for row in rows
    ]


__all__ = [
    "CandidateEvaluation",
    "CandidateEvidence",
    "Cohort",
    "EvaluationError",
    "Market",
    "VariantACohortLabel",
    "VariantVerdict",
    "evaluate_candidate",
    "evaluate_candidates",
]
