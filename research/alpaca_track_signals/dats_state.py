"""ROB-1061 H3 (Run A SS11.3) — the AP-A1 DATS per-asset state-transition
boundary contract, as a pure, stateless classifier.

    R[i,m,t] = C[i,t]/C[i,t-m] - 1
    D[i,f,s,t] = EMA_f(C)/EMA_s(C) - 1

    entry: flat AND D >= +threshold AND R > 0
    exit:  long AND (D <= -threshold OR R <= 0)
    hysteresis: -threshold < D < +threshold keeps the existing state (a long
        position that is neither at nor past either boundary just holds)

``threshold`` is always the SEALED, per-config ``AP_A1_FIXED_THRESHOLD``
(``alpaca_track_seal.configs``) passed in by the caller — this module never
hardcodes ``0.005`` (that would be exactly the "typing 0.005 as a literal"
anti-pattern the ROB-1061 issue calls out).

Entry fires ONLY on a per-asset flat->long (0->1) transition (AC9): this
function takes the CURRENT state and only evaluates the branch that state
permits — a "long" input can structurally never produce an ``ENTER`` outcome,
there is no code path that even asks the entry question for a long position.
No re-entry, no pyramiding, no rebalancing while held.

Pure stdlib. No app/DB/network/broker import. No PnL, return, or price field
anywhere in this module's surface (state-classification only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "PositionStateLiteral",
    "TransitionActionLiteral",
    "TransitionOutcome",
    "classify_transition",
]

PositionStateLiteral = Literal["flat", "long"]
TransitionActionLiteral = Literal["ENTER", "EXIT", "HOLD", "NO_ENTRY"]

_VALID_STATES: frozenset[str] = frozenset({"flat", "long"})


@dataclass(frozen=True)
class TransitionOutcome:
    """The classifier's verdict for one symbol at one AP-A1 decision.

    ``action``:
      - ``"ENTER"``: flat -> long (only ever returned when ``state == "flat"``)
      - ``"EXIT"``: long -> flat (only ever returned when ``state == "long"``)
      - ``"HOLD"``: long, no exit trigger fired (hysteresis band, or D still
        comfortably above the entry threshold with R > 0 — both are "the
        existing long state is maintained", per SS11.3's literal wording)
      - ``"NO_ENTRY"``: flat, entry condition not satisfied
    """

    action: TransitionActionLiteral

    def __post_init__(self) -> None:
        if self.action not in ("ENTER", "EXIT", "HOLD", "NO_ENTRY"):
            raise ValueError(f"invalid action {self.action!r}")


def classify_transition(
    *, state: PositionStateLiteral, d: float, r: float, threshold: float
) -> TransitionOutcome:
    """Classify ONE symbol's AP-A1 state transition for ONE decision.

    ``threshold`` must be positive (the sealed ``AP_A1_FIXED_THRESHOLD``,
    +-0.005). Boundaries are inclusive exactly where SS11.3/AC8 says so:
    ``D == +threshold`` allows entry, ``D == -threshold`` fires exit,
    ``R == 0`` fires exit (only ``R > 0``, strictly, allows entry).
    """
    if type(threshold) is not float or threshold <= 0.0:
        raise ValueError(f"threshold must be a positive float, got {threshold!r}")
    if state not in _VALID_STATES:
        raise ValueError(
            f"state must be one of {sorted(_VALID_STATES)}, got {state!r}"
        )
    if state == "flat":
        if d >= threshold and r > 0.0:
            return TransitionOutcome(action="ENTER")
        return TransitionOutcome(action="NO_ENTRY")
    # state == "long": entry is structurally unreachable from here (AC9).
    if d <= -threshold or r <= 0.0:
        return TransitionOutcome(action="EXIT")
    return TransitionOutcome(action="HOLD")
