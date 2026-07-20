"""ROB-982 H4 post-arbitration, pre-H2-open funding authority.

This seam deliberately cannot receive a ranked candidate list: H3 has already
made one global winner.  A failed exact-entry or funding gate therefore has no
fallback/rerank route before the actual H2 opening adapter is called.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass


def _int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


def _float(value: object, name: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{name} must be built-in float")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


@dataclass(frozen=True, slots=True)
class PITFundingObservation:
    timestamp_ms: int
    signed_bps: float

    def __post_init__(self) -> None:
        _int(self.timestamp_ms, "timestamp_ms")
        _float(self.signed_bps, "signed_bps")


@dataclass(frozen=True, slots=True)
class FundingGateResult:
    accepted: bool
    reason: str | None
    expected_signed_bps: float | None

    def __post_init__(self) -> None:
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be built-in bool")
        if self.reason not in (
            None,
            "funding_evidence_unavailable",
            "expected_funding_cost_above_3bps",
        ):
            raise ValueError("funding reason is outside the closed taxonomy")
        if self.expected_signed_bps is not None:
            _float(self.expected_signed_bps, "expected_signed_bps")
        if self.accepted != (self.reason is None):
            raise ValueError("funding result acceptance/reason mismatch")


def last_known_pit_funding(*, observations: object, entry_ts: object) -> float | None:
    """Return one last-known signed value at/before entry, fail closed otherwise."""
    entry = _int(entry_ts, "entry_ts")
    if type(observations) is not tuple:
        raise TypeError("observations must be a built-in tuple")
    latest: PITFundingObservation | None = None
    for observation in observations:
        if type(observation) is not PITFundingObservation:
            raise TypeError("observations must be exact PITFundingObservation values")
        if observation.timestamp_ms <= entry and (
            latest is None or observation.timestamp_ms > latest.timestamp_ms
        ):
            latest = observation
    return None if latest is None else latest.signed_bps


def _gate(expected_signed_bps: object) -> FundingGateResult:
    if expected_signed_bps is None:
        return FundingGateResult(False, "funding_evidence_unavailable", None)
    expected = _float(expected_signed_bps, "expected_signed_bps")
    if expected > 3.0:
        return FundingGateResult(False, "expected_funding_cost_above_3bps", expected)
    return FundingGateResult(True, None, expected)


def s3_funding_gate(expected_signed_bps: object) -> FundingGateResult:
    """S3's signed debit limit is strict: exactly 3bp and credits pass."""
    return _gate(expected_signed_bps)


def s4_funding_gate(
    *,
    leg_a_signed_bps: object,
    leg_b_signed_bps: object,
    weight_a: object,
    weight_b: object,
) -> FundingGateResult:
    """Both leg PIT evidence is mandatory; entry-frozen basket cost is once."""
    if leg_a_signed_bps is None or leg_b_signed_bps is None:
        return FundingGateResult(False, "funding_evidence_unavailable", None)
    a = _float(leg_a_signed_bps, "leg_a_signed_bps")
    b = _float(leg_b_signed_bps, "leg_b_signed_bps")
    wa = _float(weight_a, "weight_a")
    wb = _float(weight_b, "weight_b")
    if wa <= 0.0 or wb <= 0.0 or abs((wa + wb) - 1.0) > 1e-12:
        raise ValueError("entry-frozen S4 weights must be positive and sum to one")
    return _gate(wa * a + wb * b)


def invoke_after_arbitration[Winner, Entry, Opened](
    *,
    winner: Winner,
    resolve_exact_entry: Callable[[Winner], Entry | None],
    funding_gate: Callable[[Winner, Entry], FundingGateResult],
    h2_open: Callable[[Winner, Entry], Opened],
) -> tuple[str, FundingGateResult | None, Opened | None]:
    """Enforce H3 winner → exact entry → funding → actual H2 open ordering."""
    entry = resolve_exact_entry(winner)
    if entry is None:
        return ("no_trade_missing_exact_entry", None, None)
    gate = funding_gate(winner, entry)
    if type(gate) is not FundingGateResult:
        raise TypeError("funding_gate must return exact FundingGateResult")
    if not gate.accepted:
        return (gate.reason or "funding_gate_rejected", gate, None)
    return ("opened", gate, h2_open(winner, entry))


__all__ = [
    "FundingGateResult",
    "PITFundingObservation",
    "invoke_after_arbitration",
    "last_known_pit_funding",
    "s3_funding_gate",
    "s4_funding_gate",
]
