"""Symmetric A/B buy-gate evaluation. Support strength is the only fork.

A and B consume one CandidateEvidence object and one evaluation_as_of.
Shared gates are applied with the frozen pre-registration thresholds, not
the live policy file. This module has no broker, proposal, watch, DB, or
network import.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.services.buy_gate_ab_shadow.spec import PRE_REGISTRATION

Market = Literal["kr", "us"]
Cohort = Literal["a_and_b", "b_only", "neither"]

_STRENGTH_RANK: dict[str, int] = {"weak": 0, "moderate": 1, "strong": 2}
_SHARED = PRE_REGISTRATION["shared_gates"]
RSI_MAX = Decimal(str(_SHARED["rsi_max"]))
SUPPORT_WITHIN_PCT = Decimal(str(_SHARED["support_within_pct"]))
UPSIDE_MIN_PCT = Decimal(str(_SHARED["upside_min_pct"]))
OTHER_GATE_KEYS: tuple[str, ...] = tuple(_SHARED["other_gate_bit_keys"])
A_SUPPORT_MIN = str(PRE_REGISTRATION["variant_a"]["support_strength_min"])
B_SUPPORT_MIN = str(PRE_REGISTRATION["variant_b"]["support_strength_min"])
ALLOWED_MARKETS: frozenset[str] = frozenset(PRE_REGISTRATION["markets"])


class EvaluationError(ValueError):
    """Caller input cannot be evaluated fail-closed."""


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
    """One screening snapshot. A and B both see this exact record."""

    symbol: str
    market: Market
    current_price: Decimal
    support_strength: str
    support_distance_pct: Decimal | None
    rsi: Decimal | None
    honest_upside_pct: Decimal | None
    other_gate_bits: Mapping[str, bool]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> CandidateEvidence:
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol:
            raise EvaluationError("symbol is required")
        market = str(raw.get("market") or "").strip().lower()
        if market not in ALLOWED_MARKETS:
            raise EvaluationError("market must be kr or us")
        strength = str(raw.get("support_strength") or "").strip().lower()
        bits_raw = raw.get("other_gate_bits") or {}
        if not isinstance(bits_raw, Mapping):
            raise EvaluationError("other_gate_bits must be an object")
        bits: dict[str, bool] = {}
        for key in OTHER_GATE_KEYS:
            if key not in bits_raw:
                bits[key] = False
                continue
            value = bits_raw[key]
            if not isinstance(value, bool):
                raise EvaluationError(f"other_gate_bits.{key} must be a boolean")
            bits[key] = value
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
        )

    def input_snapshot(self) -> dict[str, Any]:
        """Return the exact, normalized gate input shared by A and B."""

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
                key: self.other_gate_bits[key] for key in OTHER_GATE_KEYS
            },
        }

    def input_snapshot_sha256(self) -> str:
        payload = json.dumps(
            self.input_snapshot(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class VariantVerdict:
    variant: Literal["A", "B"]
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
    shared_reject_reasons: tuple[str, ...]
    variant_a: VariantVerdict
    variant_b: VariantVerdict
    cohort: Cohort
    shadow_buy: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market": self.market,
            "evaluation_as_of": self.evaluation_as_of.isoformat(),
            "entry_price": str(self.entry_price),
            "input_snapshot": dict(self.input_snapshot),
            "input_snapshot_sha256": self.input_snapshot_sha256,
            "support_strength": self.support_strength,
            "shared_reject_reasons": list(self.shared_reject_reasons),
            "variant_a": {
                "variant": self.variant_a.variant,
                "passed": self.variant_a.passed,
                "reject_reasons": list(self.variant_a.reject_reasons),
                "support_strength_min": self.variant_a.support_strength_min,
            },
            "variant_b": {
                "variant": self.variant_b.variant,
                "passed": self.variant_b.passed,
                "reject_reasons": list(self.variant_b.reject_reasons),
                "support_strength_min": self.variant_b.support_strength_min,
            },
            "cohort": self.cohort,
            "shadow_buy": self.shadow_buy,
            "promote": False,
            "live_gate_impact": False,
        }


def _shared_reject_reasons(evidence: CandidateEvidence) -> list[str]:
    reasons: list[str] = []
    if evidence.rsi is None or evidence.rsi >= RSI_MAX:
        reasons.append("rsi_not_below_max")
    if (
        evidence.support_distance_pct is None
        or evidence.support_distance_pct < 0
        or evidence.support_distance_pct > SUPPORT_WITHIN_PCT
    ):
        reasons.append("support_not_within_pct")
    if (
        evidence.honest_upside_pct is None
        or evidence.honest_upside_pct < UPSIDE_MIN_PCT
    ):
        reasons.append("honest_upside_below_min")
    for key in OTHER_GATE_KEYS:
        if not evidence.other_gate_bits.get(key, False):
            reasons.append(f"other_gate_{key}_failed")
    return reasons


def _support_ok(strength: str, required: str) -> bool:
    have = _STRENGTH_RANK.get(strength, -1)
    need = _STRENGTH_RANK[required]
    return have >= need


def _verdict(
    *,
    variant: Literal["A", "B"],
    required: str,
    evidence: CandidateEvidence,
    shared: Sequence[str],
) -> VariantVerdict:
    reasons = list(shared)
    if not _support_ok(evidence.support_strength, required):
        reasons.append(f"support_strength_below_{required}")
    return VariantVerdict(
        variant=variant,
        passed=not reasons,
        reject_reasons=tuple(reasons),
        support_strength_min=required,
    )


def evaluate_candidate(
    evidence: CandidateEvidence,
    *,
    evaluation_as_of: datetime,
) -> CandidateEvaluation:
    if evaluation_as_of.tzinfo is None:
        raise EvaluationError("evaluation_as_of must be timezone-aware")
    shared = _shared_reject_reasons(evidence)
    variant_a = _verdict(
        variant="A", required=A_SUPPORT_MIN, evidence=evidence, shared=shared
    )
    variant_b = _verdict(
        variant="B", required=B_SUPPORT_MIN, evidence=evidence, shared=shared
    )
    if variant_a.passed and not variant_b.passed:
        raise EvaluationError("variant A cannot pass when variant B fails")
    if variant_a.passed:
        cohort: Cohort = "a_and_b"
    elif variant_b.passed:
        cohort = "b_only"
    else:
        cohort = "neither"
    return CandidateEvaluation(
        symbol=evidence.symbol,
        market=evidence.market,
        evaluation_as_of=evaluation_as_of,
        entry_price=evidence.current_price,
        input_snapshot=evidence.input_snapshot(),
        input_snapshot_sha256=evidence.input_snapshot_sha256(),
        support_strength=evidence.support_strength,
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
