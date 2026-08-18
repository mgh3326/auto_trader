"""ROB-1286 §4 — dedup, per-symbol concurrency, per-round cap.

These three are safety devices, not tidiness. Each spawned session ends at
``order_proposal_create``, and a proposal can reach the §40/51차
auto-approve lane, so "how many sessions may this tick start" is directly
"how many orders may this tick put in front of the approval machinery".

The three limits answer three different failure modes:

``event dedup`` (``event_uuid``)
    One fire must not become two re-judgements. Enforced by the claim
    store, whose claim *is* the dedup; this module only asks it.
``per-symbol concurrency`` (max 1 in flight)
    Two thresholds on one symbol can fire minutes apart (the 08-18
    Samsung ladder fired 1단 and 2단 together). Two concurrent sessions on
    one symbol would each size a sell against the same position and
    double-count the sellable quantity.
``per-round cap``
    A scanner misconfiguration or a market-wide gap can fire dozens of
    watches at once. The cap bounds one tick's blast radius.

Overflow is returned, never dropped. An event the cap pushed out is
reported in :attr:`SelectionResult.overflow` with its symbol and event
uuid so the caller logs it. Silence here would be the original ROB-1286
accident -- a fire disappearing without a trace -- reintroduced by its own
fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.services.watch_trigger_repricing.claims import (
    ClaimStore,
    ClaimStoreUnavailable,
)
from app.services.watch_trigger_repricing.consumption import (
    ConsumptionState,
    is_consumable_outcome,
    may_consume,
)

__all__ = [
    "DEFAULT_ROUND_CAP",
    "CandidateEvent",
    "SelectionResult",
    "SkippedEvent",
    "select_candidates",
]

# ROB-1286 설계 3항 ("예: 회차당 3종목").
DEFAULT_ROUND_CAP = 3

# Why an in-flight lease and a terminal consumption share one skip reason:
# from the *caller's* side both answer the same question -- "somebody else
# has this fire" -- and the operator-facing distinction is carried in the
# log line and in ``store.state_for``, which is the surface B안 reads. The
# two are kept separate in :class:`ConsumptionState`, not here.
_SKIP_REASONS: dict[ConsumptionState, str] = {
    ConsumptionState.CLAIMED: "already_consumed",
    ConsumptionState.CONSUMED: "already_consumed",
    ConsumptionState.QUARANTINED: "awaiting_spawn_reconcile",
    ConsumptionState.UNKNOWN: "consumption_state_unknown",
}


def _skip_reason(state: ConsumptionState) -> str:
    return _SKIP_REASONS.get(state, "consumption_state_unknown")


@dataclass(frozen=True)
class CandidateEvent:
    """The subset of a watch event this flow needs. Read-only."""

    event_uuid: str
    symbol: str
    market: str
    outcome: str
    delivery_status: str
    delivered_at: datetime | None


@dataclass(frozen=True)
class SkippedEvent:
    """An event that will not be spawned, and the reason it will not be."""

    event_uuid: str
    symbol: str
    reason: str


@dataclass(frozen=True)
class SelectionResult:
    selected: tuple[CandidateEvent, ...]
    overflow: tuple[SkippedEvent, ...]
    skipped: tuple[SkippedEvent, ...]

    @property
    def overflow_count(self) -> int:
        return len(self.overflow)


def select_candidates(
    events: list[CandidateEvent],
    *,
    store: ClaimStore,
    now: datetime,
    round_cap: int = DEFAULT_ROUND_CAP,
    market: str = "kr",
) -> SelectionResult:
    """Choose which fires this tick may spawn.

    Pure with respect to the store: it reads claim state but takes no
    claim. The caller claims each selection immediately afterwards, so a
    losing race there simply drops that one event rather than corrupting
    the selection.
    """
    selected: list[CandidateEvent] = []
    overflow: list[SkippedEvent] = []
    skipped: list[SkippedEvent] = []

    try:
        in_flight = set(store.active_symbols(now=now))
        store_up = True
    except ClaimStoreUnavailable:
        # Cannot prove anything is free. Fail closed for every event and say
        # so, rather than spawning blind.
        in_flight = set()
        store_up = False

    for event in sorted(
        events,
        key=lambda e: (e.delivered_at is None, e.delivered_at, e.event_uuid),
    ):
        if not store_up:
            skipped.append(
                SkippedEvent(event.event_uuid, event.symbol, "claim_store_unavailable")
            )
            continue
        if event.market != market:
            skipped.append(
                SkippedEvent(event.event_uuid, event.symbol, "market_out_of_scope")
            )
            continue
        if event.delivery_status != "delivered":
            skipped.append(
                SkippedEvent(event.event_uuid, event.symbol, "not_delivered")
            )
            continue
        if not is_consumable_outcome(event.outcome):
            skipped.append(
                SkippedEvent(event.event_uuid, event.symbol, "outcome_not_consumable")
            )
            continue

        state = store.state_for(event.event_uuid, now=now)
        if not may_consume(state):
            skipped.append(
                SkippedEvent(event.event_uuid, event.symbol, _skip_reason(state))
            )
            continue

        if event.symbol in in_flight:
            skipped.append(
                SkippedEvent(event.event_uuid, event.symbol, "symbol_already_in_flight")
            )
            continue

        # Cap last, so an event the cap pushes out is a real candidate that
        # deserves the loud overflow record -- not something that would have
        # been skipped anyway.
        if len(selected) >= round_cap:
            overflow.append(
                SkippedEvent(event.event_uuid, event.symbol, "round_cap_exceeded")
            )
            continue

        selected.append(event)
        in_flight.add(event.symbol)

    return SelectionResult(
        selected=tuple(selected),
        overflow=tuple(overflow),
        skipped=tuple(skipped),
    )
