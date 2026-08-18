"""ROB-1286 — one repricing tick: gate -> poll -> claim -> spawn.

Ordering is the whole design (see :mod:`.claims` for why claim precedes
spawn). The sequence per selected event is:

1. ``try_claim`` -- atomic. Losing here means a concurrent tick or the rep
   session got there first, and this tick simply drops the event.
2. ``spawn`` -- the dry spawner in this PR.
3. On spawn failure, ``release`` with a reason, so an orderly failure hands
   the event straight back instead of parking it for a lease.

The residual window is a hard crash between 1 and 2. The lease closes it:
the claim expires and the fire resurfaces. It is bounded latency, not a
lost event -- and B안 (the rep session's end-of-session re-check) covers
the same window independently.

Every non-spawn is returned with a reason. A tick that spawns nothing
still says what it saw and why it passed, because a fire vanishing
quietly is the accident this issue exists to fix.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.core.config import settings
from app.services.watch_trigger_repricing.claims import (
    DEFAULT_LEASE,
    ClaimStore,
    ClaimStoreUnavailable,
    InMemoryClaimStore,
)
from app.services.watch_trigger_repricing.gate import GateDecision, evaluate_gate
from app.services.watch_trigger_repricing.selection import (
    DEFAULT_ROUND_CAP,
    CandidateEvent,
    SkippedEvent,
    select_candidates,
)
from app.services.watch_trigger_repricing.spawn import (
    DrySessionSpawner,
    SessionSpawner,
    SpawnOutcome,
    SpawnRequest,
    session_label,
)

logger = logging.getLogger(__name__)

__all__ = ["TickResult", "run_gated_tick", "run_repricing_tick"]


@dataclass(frozen=True)
class TickResult:
    gate: GateDecision
    spawned: tuple[SpawnOutcome, ...]
    overflow: tuple[SkippedEvent, ...]
    skipped: tuple[SkippedEvent, ...]

    @property
    def spawn_count(self) -> int:
        return len(self.spawned)

    @property
    def overflow_count(self) -> int:
        return len(self.overflow)

    def as_dict(self) -> dict:
        return {
            "shouldRun": self.gate.should_run,
            "gateReason": self.gate.reason,
            "sessionStatus": self.gate.session_status,
            "kstDate": self.gate.kst_date,
            "spawned": [
                {
                    "eventUuid": o.request.event_uuid,
                    "symbol": o.request.symbol,
                    "label": o.request.label,
                    "executionBoundary": o.request.execution_boundary,
                    "started": o.started,
                    "detail": o.detail,
                }
                for o in self.spawned
            ],
            "overflow": [
                {"eventUuid": s.event_uuid, "symbol": s.symbol, "reason": s.reason}
                for s in self.overflow
            ],
            "skipped": [
                {"eventUuid": s.event_uuid, "symbol": s.symbol, "reason": s.reason}
                for s in self.skipped
            ],
        }


def run_repricing_tick(
    events: list[CandidateEvent],
    *,
    store: ClaimStore,
    now: datetime,
    spawner: SessionSpawner | None = None,
    round_cap: int = DEFAULT_ROUND_CAP,
    lease: timedelta = DEFAULT_LEASE,
    claimed_by: str = "rob1286-watch-trigger-repricing",
    market: str = "kr",
) -> TickResult:
    """Run one tick. Starts no session unless a live spawner is injected."""
    spawner = spawner if spawner is not None else DrySessionSpawner()

    gate = evaluate_gate(now=now)
    if not gate.should_run:
        logger.info(
            "watch_trigger_repricing: tick skipped (reason=%s, session=%s, date=%s)",
            gate.reason,
            gate.session_status,
            gate.kst_date,
        )
        return TickResult(gate=gate, spawned=(), overflow=(), skipped=())

    selection = select_candidates(
        events, store=store, now=now, round_cap=round_cap, market=market
    )

    # §4: the cap must never be a silent truncation.
    for dropped in selection.overflow:
        logger.warning(
            "watch_trigger_repricing: round cap %d exceeded, deferring event "
            "%s (symbol=%s, reason=%s) to a later tick",
            round_cap,
            dropped.event_uuid,
            dropped.symbol,
            dropped.reason,
        )
    for dropped in selection.skipped:
        logger.info(
            "watch_trigger_repricing: skipped event %s (symbol=%s, reason=%s)",
            dropped.event_uuid,
            dropped.symbol,
            dropped.reason,
        )

    spawned: list[SpawnOutcome] = []
    lost: list[SkippedEvent] = []

    for event in selection.selected:
        try:
            claim = store.try_claim(
                event_uuid=event.event_uuid,
                symbol=event.symbol,
                claimed_by=claimed_by,
                now=now,
                lease=lease,
            )
        except ClaimStoreUnavailable:
            lost.append(
                SkippedEvent(
                    event.event_uuid, event.symbol, "claim_store_unavailable_at_claim"
                )
            )
            continue
        if claim is None:
            # Lost the race between selection and claim. Correct outcome:
            # the other holder owns it.
            lost.append(SkippedEvent(event.event_uuid, event.symbol, "claim_lost_race"))
            continue

        request = SpawnRequest(
            event_uuid=event.event_uuid,
            symbol=event.symbol,
            market=event.market,
            kst_date=gate.kst_date,
            label=session_label(event.symbol, now=now),
        )
        try:
            outcome = spawner.spawn(request)
        except Exception:  # noqa: BLE001 - a failed spawn must not hold the lease
            logger.exception(
                "watch_trigger_repricing: spawn failed for event %s (symbol=%s); "
                "releasing claim",
                event.event_uuid,
                event.symbol,
            )
            store.release(event.event_uuid, reason="spawn_failed")
            lost.append(SkippedEvent(event.event_uuid, event.symbol, "spawn_failed"))
            continue
        spawned.append(outcome)

    return TickResult(
        gate=gate,
        spawned=tuple(spawned),
        overflow=selection.overflow,
        skipped=selection.skipped + tuple(lost),
    )


def run_gated_tick(
    *,
    events: list[CandidateEvent],
    store: ClaimStore | None = None,
    spawner: SessionSpawner | None = None,
    now: dt.datetime | None = None,
    round_cap: int = DEFAULT_ROUND_CAP,
) -> dict[str, Any]:
    """Env-gated tick entrypoint. The Prefect wrapper is a thin shell over it.

    Lives here rather than in ``app/flows`` so the gate is importable and
    testable without ``prefect``, which is not a project dependency.
    """
    if not getattr(settings, "WATCH_TRIGGER_REPRICING_ENABLED", False):
        return {"status": "disabled", "spawned": [], "overflow": [], "skipped": []}

    result = run_repricing_tick(
        events,
        store=store if store is not None else InMemoryClaimStore(),
        now=now or dt.datetime.now(dt.UTC),
        spawner=spawner,
        round_cap=round_cap,
    )
    return {"status": "ok", **result.as_dict()}
