"""ROB-1061 H3 (AC20, AC23) — the signal-engine output record.

    (decision_ts, strategy, config_id, symbol, action, target_notional,
     reason_code, evidence_hash)

NO ``pnl``, ``return``, ``forward-*``, or ``exit-price`` field exists on this
dataclass, anywhere in this module, or anywhere else in this package — H5's
PnL-blind dry-count gate depends on that property, and
``tests/test_no_forbidden_imports_and_pnl_surface.py`` statically enforces it
(scans every field name in this whole package, not just this file).

Canonical output order is ``(decision_ts, strategy, config_id, symbol)``
(AC23) — container/file-traversal order can never change the byte output;
``canonical_sort`` is the single, sole ordering authority.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import canonical_hash

import reason_codes as rc

__all__ = [
    "ActionReasonMismatchError",
    "SignalRecord",
    "canonical_sort",
    "evidence_hash",
]

StrategyLiteral = Literal["AP-A1", "AP-A2"]


class ActionReasonMismatchError(ValueError):
    """A ``SignalRecord``'s ``action`` does not match its ``reason_code``'s
    single mapped action (``reason_codes.ACTION_FOR_REASON``) — this can
    never construct silently, closing off a whole class of "ships an ENTER
    record under a NO_ENTRY_SIGNAL reason" mutation."""


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


def evidence_hash(evidence: dict) -> str:
    """The canonical SHA-256 identity of one record's supporting evidence
    dict (D/R/Score/rank/sigma20/vol_scale/... — whatever the caller
    supplies), via the SAME typed canonical AST authority H1/H2 use. Never
    includes a pnl/return/forward-*/exit-price key (the caller-supplied
    ``evidence`` dict is itself scanned by the no-PnL-surface guard's
    lexical token check, belt-and-suspenders alongside the schema check)."""
    return canonical_hash.canonical_sha256(evidence)


@dataclass(frozen=True)
class SignalRecord:
    decision_ts_ms: int
    strategy: StrategyLiteral
    config_id: str
    symbol: str
    action: str  # reason_codes.ActionLiteral
    target_notional: float
    reason_code: str  # reason_codes.ReasonCode
    evidence_hash: str

    def __post_init__(self) -> None:
        _int(self.decision_ts_ms, "decision_ts_ms")
        _float(self.target_notional, "target_notional")
        if self.target_notional < 0.0:
            raise ValueError("target_notional must be non-negative")
        if self.strategy not in ("AP-A1", "AP-A2"):
            raise ValueError(f"unknown strategy {self.strategy!r}")
        if self.reason_code not in rc.ALL_REASON_CODES:
            raise ValueError(f"unknown reason_code {self.reason_code!r}")
        expected_action = rc.ACTION_FOR_REASON[self.reason_code]
        if self.action != expected_action:
            raise ActionReasonMismatchError(
                f"reason_code {self.reason_code!r} requires action "
                f"{expected_action!r}, got {self.action!r}"
            )
        # A non-ENTER record carries no committed notional (nothing sized
        # was actually accepted) -- this closes off a mutation where a
        # rejected candidate's would-be target_notional leaks into the
        # output as if it were real committed capital.
        if self.action != "ENTER" and self.target_notional != 0.0:
            raise ValueError(
                f"action {self.action!r} (reason {self.reason_code!r}) must "
                f"carry target_notional == 0.0, got {self.target_notional!r}"
            )

    def to_dict(self) -> dict:
        return {
            "decision_ts_ms": self.decision_ts_ms,
            "strategy": self.strategy,
            "config_id": self.config_id,
            "symbol": self.symbol,
            "action": self.action,
            "target_notional": self.target_notional,
            "reason_code": self.reason_code,
            "evidence_hash": self.evidence_hash,
        }


def canonical_sort(records: Sequence[SignalRecord]) -> tuple[SignalRecord, ...]:
    """The sole canonical-order authority (AC23):
    ``(decision_ts, strategy, config_id, symbol)`` ascending. Any container
    permutation of the SAME records sorts to the SAME output — file
    traversal order, dict iteration order, or multiprocessing completion
    order can never change the result."""
    return tuple(
        sorted(
            records,
            key=lambda r: (r.decision_ts_ms, r.strategy, r.config_id, r.symbol),
        )
    )
