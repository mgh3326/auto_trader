"""ROB-1061 H3 (AC22) — the closed rejection/acceptance reason-code enum and
histogram reconciliation identity.

Every ``SignalRecord`` (``output_schema.py``) carries exactly one
``reason_code`` from this closed set, and every reason_code has exactly one
valid ``action`` (``ACTION_FOR_REASON``) — a record whose action does not
match its reason's mapped action is a construction-time error
(``output_schema.SignalRecord.__post_init__``), not a possibility a mutation
could silently ship.

``reconcile_histogram`` enforces AC22's identity: the sum of a closed
histogram's counts must equal the number of records it was built from, and
every key in the histogram must be a real member of this enum — an unknown
reason code (typo, drift, a new code introduced without updating this file)
fails closed rather than silently under/over-counting.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

__all__ = [
    "ACTION_FOR_REASON",
    "ALL_REASON_CODES",
    "ActionLiteral",
    "ReasonCode",
    "UnknownReasonCodeError",
    "HistogramReconciliationError",
    "reconcile_histogram",
]

ActionLiteral = Literal["ENTER", "EXIT", "HOLD", "NO_ACTION"]

ReasonCode = Literal[
    # Shared across AP-A1 and AP-A2 (common decision contract, AC1-AC5).
    "UNIVERSE_INELIGIBLE",
    "INVALID_DECISION_DAY",
    "INSUFFICIENT_PRICE_HISTORY",
    "SIGMA_INSUFFICIENT_SAMPLE",
    "MIN_TARGET_NOTIONAL",
    "INSUFFICIENT_CASH",
    # Run A §6 rule 7 (N_t >= 18): the universe-wide "restricted_exits_only"
    # mode (H1 pit_universe_alpaca.universe_state) blocks every NEW entry for
    # every symbol, regardless of that symbol's own signal -- shared across
    # both strategies for the same reason UNIVERSE_INELIGIBLE is shared.
    "UNIVERSE_RESTRICTED_NEW_ENTRY_BLOCKED",
    # AP-A1 DATS (SS11.3/SS11.5).
    "ENTRY_ACCEPTED",
    "NO_ENTRY_SIGNAL",
    "EXIT_TRIGGERED",
    "HYSTERESIS_HOLD",
    # A long position holding well outside the hysteresis band (D already at
    # or past the ENTRY threshold, simply not re-entered per AC9) is a
    # distinct diagnostic state from "sitting inside the -threshold<D<
    # threshold band" -- collapsing both into HYSTERESIS_HOLD mislabels every
    # healthy long as if it were teetering on the exit boundary.
    "TREND_INTACT_HOLD",
    # AP-A2 WCM-B (SS12.2-SS12.5).
    "RANK_BUY_ACCEPTED",
    "SCORE_NOT_POSITIVE",
    "RANK_SLOTS_FULL",
    "RANK_EXCEEDS_BUFFER_EXIT",
    "RANK_BUFFER_HOLD",
]

# The literal set materialized for runtime membership tests — kept in exact
# 1:1 sync with the ``ReasonCode`` Literal above (see
# ``tests/test_reason_codes.py``'s drift guard).
ALL_REASON_CODES: frozenset[str] = frozenset(
    {
        "UNIVERSE_INELIGIBLE",
        "INVALID_DECISION_DAY",
        "INSUFFICIENT_PRICE_HISTORY",
        "SIGMA_INSUFFICIENT_SAMPLE",
        "MIN_TARGET_NOTIONAL",
        "INSUFFICIENT_CASH",
        "UNIVERSE_RESTRICTED_NEW_ENTRY_BLOCKED",
        "ENTRY_ACCEPTED",
        "NO_ENTRY_SIGNAL",
        "EXIT_TRIGGERED",
        "HYSTERESIS_HOLD",
        "TREND_INTACT_HOLD",
        "RANK_BUY_ACCEPTED",
        "SCORE_NOT_POSITIVE",
        "RANK_SLOTS_FULL",
        "RANK_EXCEEDS_BUFFER_EXIT",
        "RANK_BUFFER_HOLD",
    }
)

# Every reason code has EXACTLY one valid action — enforced at SignalRecord
# construction, not merely documented here.
ACTION_FOR_REASON: dict[str, str] = {
    "UNIVERSE_INELIGIBLE": "NO_ACTION",
    "INVALID_DECISION_DAY": "NO_ACTION",
    "INSUFFICIENT_PRICE_HISTORY": "NO_ACTION",
    "SIGMA_INSUFFICIENT_SAMPLE": "NO_ACTION",
    "MIN_TARGET_NOTIONAL": "NO_ACTION",
    "INSUFFICIENT_CASH": "NO_ACTION",
    "UNIVERSE_RESTRICTED_NEW_ENTRY_BLOCKED": "NO_ACTION",
    "ENTRY_ACCEPTED": "ENTER",
    "NO_ENTRY_SIGNAL": "NO_ACTION",
    "EXIT_TRIGGERED": "EXIT",
    "HYSTERESIS_HOLD": "HOLD",
    "TREND_INTACT_HOLD": "HOLD",
    "RANK_BUY_ACCEPTED": "ENTER",
    "SCORE_NOT_POSITIVE": "NO_ACTION",
    "RANK_SLOTS_FULL": "NO_ACTION",
    "RANK_EXCEEDS_BUFFER_EXIT": "EXIT",
    "RANK_BUFFER_HOLD": "HOLD",
}


class UnknownReasonCodeError(ValueError):
    """A reason code outside the closed ``ALL_REASON_CODES`` set was used."""


class HistogramReconciliationError(ValueError):
    """A histogram's counts do not reconcile with the records it summarizes
    (AC22's "히스토그램 합이 상위 카운트와 정확히 일치" identity)."""


def reconcile_histogram(reason_codes: Sequence[str]) -> dict[str, int]:
    """Build a closed-enum histogram from a flat sequence of reason codes,
    failing closed on any code outside ``ALL_REASON_CODES``. The identity
    ``sum(histogram.values()) == len(reason_codes)`` holds by construction
    (every input code is counted exactly once, nothing is dropped or
    double-counted) — callers that need to PROVE this against an external
    total should use ``sum(histogram.values())`` directly.
    """
    histogram: dict[str, int] = {}
    for code in reason_codes:
        if code not in ALL_REASON_CODES:
            raise UnknownReasonCodeError(
                f"{code!r} is not a member of the closed reason-code enum "
                f"({sorted(ALL_REASON_CODES)})"
            )
        histogram[code] = histogram.get(code, 0) + 1
    if sum(histogram.values()) != len(reason_codes):
        # Structurally unreachable given the loop above, but kept as an
        # explicit, independently-checkable identity per AC22 rather than
        # merely trusting the loop's arithmetic.
        raise HistogramReconciliationError(
            "histogram total does not match input record count"
        )
    return histogram
