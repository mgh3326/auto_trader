"""ROB-1286 — one repricing tick: gate -> poll -> claim -> spawn.

Ordering is the whole design (see :mod:`.claims` for why claim precedes
spawn). The sequence per selected event is:

1. ``try_claim`` -- atomic. Losing here means a concurrent tick or the rep
   session got there first, and this tick simply drops the event.
2. ``spawn`` -- the dry spawner in this PR.
3. Resolve the claim against what the spawn actually proved.

Step 3 is the r2 rewrite (BLOCKER-2). r1 had a boolean and two bugs in
opposite directions: every exception released the claim (so a spawner that
raised *after* starting a session got the event spawned twice), and a
clean return with ``started=False`` was counted as a success and kept the
claim (so an event nothing had started was buried for a lease). The four
dispositions each get the handling their evidence supports:

``STARTED``
    Terminal ``CONSUMED``. Never re-spawned, and no lease expiry walks it
    back -- that was the other half of BLOCKER-1.
``NOT_STARTED``
    Proven clean failure (returned, or raised as
    :class:`~.spawn.SpawnNotStarted`). Claim released with a reason, so the
    fire is available again on the very next tick. Not counted as spawned.
``AMBIGUOUS``
    Anything else -- a generic exception, an acknowledgement timeout, a
    spawner that says so. **Unknown is not "no".**
``DRY``
    The rehearsal path. Terminal, so dedup is exercised for real, but
    ``started`` is False and the detail says ``dry_run``.

The ambiguous branch, and what it costs
---------------------------------------
Ambiguity has no free answer, and the brief is right that fail-closed is
not automatic here:

* Release the claim and the event re-spawns. If the first session *was*
  up, one fire becomes two sessions, each independently reaching
  ``order_proposal_create`` on the same symbol -- and a proposal can reach
  the §40/51차 auto-approve lane. This is the failure this issue must not
  create.
* Hold the claim and, if the session was *not* up, the fire is stuck: A안
  will not retake it and B안 reads it as owned, so it goes unhandled --
  which is the original ROB-1286 accident.

So the tick does not guess. It first tries to *decide*: if the spawner
implements :class:`~.spawn.ReconcilableSpawner`, the tick asks it whether a
session with this event's deterministic ``spawn_key`` exists, and a
definite answer resolves to CONSUMED or released exactly as above. Only an
undecidable result is quarantined, and quarantine is chosen with its cost
stated: **one event's latency, in exchange for never double-spawning into
the approval lane**. It is not silent -- a quarantined event is logged at
ERROR and returned in ``TickResult.needs_reconcile``, so it surfaces as an
operator task rather than as an event that quietly stopped existing.

r3 (§101차 ③) closes that last sentence: a live spawner without
``reconcile`` is no longer "under pressure" to implement it -- it cannot be
armed at all. See :mod:`.arming`.

r3 also replaces the CONSUMED/QUARANTINED pair with the lifecycle terminals
(:mod:`.lifecycle`): a started session resolves to ``PROPOSAL_CREATED`` or
``REJECTED_WITH_REASON``, and the TTL path writes ``EXPIRED_UNPROCESSED``.
There is no "analysed" terminal, by operator decision -- a session that
judged nothing and gave no reason has not finished.
"""

from __future__ import annotations

import inspect
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

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
    ReconcilableSpawner,
    SessionSpawner,
    SpawnDisposition,
    SpawnNotStarted,
    SpawnOutcome,
    SpawnRequest,
    session_label,
)

logger = logging.getLogger(__name__)

__all__ = [
    "TickResult",
    "process_claim_store",
    "reset_process_claim_store",
    "run_repricing_tick",
]


# ---------------------------------------------------------------------------
# Process-level claim store (r2 / BLOCKER-1, partial)
# ---------------------------------------------------------------------------
# r1 built a fresh ``InMemoryClaimStore()`` on every ``run_gated_tick``
# call, so dedup could not survive even two ticks in the same process: the
# "across ticks" tests only passed because a fixture handed both calls the
# same store. A module-level singleton closes that.
#
# It closes *only* the in-process case. Prefect flow runs are separate
# processes and this singleton is not shared between them -- which is why
# ``run_gated_tick`` refuses a live spawner unless the store reports
# ``is_durable``. See :mod:`.claims` for what a durable store requires
# (a migration, deliberately not in this PR).
_PROCESS_STORE: InMemoryClaimStore | None = None
_PROCESS_STORE_LOCK = threading.Lock()


def process_claim_store() -> InMemoryClaimStore:
    """The process-wide claim store, created once."""
    global _PROCESS_STORE
    with _PROCESS_STORE_LOCK:
        if _PROCESS_STORE is None:
            _PROCESS_STORE = InMemoryClaimStore()
        return _PROCESS_STORE


def reset_process_claim_store() -> None:
    """Drop the process store. Tests only -- never called in a tick."""
    global _PROCESS_STORE
    with _PROCESS_STORE_LOCK:
        _PROCESS_STORE = None


@dataclass(frozen=True)
class TickResult:
    gate: GateDecision
    spawned: tuple[SpawnOutcome, ...]
    overflow: tuple[SkippedEvent, ...]
    skipped: tuple[SkippedEvent, ...]
    needs_reconcile: tuple[SkippedEvent, ...] = ()
    # event_uuid -> ClaimHandle for every session this tick started. The
    # caller finalises through these so the write is fenced.
    handles: dict[str, Any] = field(default_factory=dict)

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
                    "spawnKey": o.request.spawn_key,
                    "executionBoundary": o.request.execution_boundary,
                    "capabilityProfile": o.request.capability_profile.name,
                    "disposition": str(o.disposition),
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
            "needsReconcile": [
                {"eventUuid": s.event_uuid, "symbol": s.symbol, "reason": s.reason}
                for s in self.needs_reconcile
            ],
        }


async def _resolve_ambiguity(
    *,
    spawner: SessionSpawner,
    request: SpawnRequest,
    detail: str,
) -> tuple[SpawnDisposition, str]:
    """Turn an ambiguous spawn into a decided one where possible.

    Asks the spawner to look up its own backend by the deterministic
    ``spawn_key``. A reconcile that itself fails leaves the ambiguity
    standing -- it must never be read as "not started", which would be the
    double-spawn direction.
    """
    if not isinstance(spawner, ReconcilableSpawner):
        return SpawnDisposition.AMBIGUOUS, f"{detail}; spawner cannot reconcile"
    try:
        verdict = spawner.reconcile(request)
        if inspect.isawaitable(verdict):
            verdict = await verdict
    except Exception as exc:  # noqa: BLE001 - a failed readback stays ambiguous
        logger.exception(
            "watch_trigger_repricing: reconcile failed for spawn_key=%s",
            request.spawn_key,
        )
        return SpawnDisposition.AMBIGUOUS, f"{detail}; reconcile failed: {exc!r}"
    if verdict in (SpawnDisposition.STARTED, SpawnDisposition.NOT_STARTED):
        return verdict, f"{detail}; reconciled as {verdict}"
    return SpawnDisposition.AMBIGUOUS, f"{detail}; reconcile inconclusive"


async def _attempt_spawn(
    *,
    spawner: SessionSpawner,
    request: SpawnRequest,
) -> tuple[SpawnDisposition, str]:
    """Call the spawner and classify what it actually proved.

    ``spawn`` may be sync or async. The dry rehearsal spawners are sync
    because they do nothing; a spawner that actually reaches
    ``order_proposal_create`` is necessarily async, and awaiting here is
    what lets the *same* orchestrator drive both rather than growing a
    second, less-tested tick for the live path.
    """
    try:
        outcome = spawner.spawn(request)
        if inspect.isawaitable(outcome):
            outcome = await outcome
    except SpawnNotStarted as exc:
        # The only exception that proves a clean failure.
        return SpawnDisposition.NOT_STARTED, f"spawn_not_started: {exc}"
    except Exception as exc:  # noqa: BLE001 - unknown is not "no"
        logger.exception(
            "watch_trigger_repricing: spawn raised for event %s (symbol=%s); "
            "outcome is AMBIGUOUS, not a clean failure",
            request.event_uuid,
            request.symbol,
        )
        return SpawnDisposition.AMBIGUOUS, f"spawn raised: {exc!r}"
    return outcome.disposition, outcome.detail


async def run_repricing_tick(
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

    selection = await select_candidates(
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
    handles: dict[str, Any] = {}
    lost: list[SkippedEvent] = []
    needs_reconcile: list[SkippedEvent] = []

    for event in selection.selected:
        try:
            handle = await store.try_claim(
                event_uuid=event.event_uuid,
                symbol=event.symbol,
                market=event.market,
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
        if handle is None:
            # Lost the race between selection and claim, or the event went
            # terminal underneath us. Either way the other holder owns it.
            lost.append(SkippedEvent(event.event_uuid, event.symbol, "claim_lost_race"))
            continue

        request = SpawnRequest(
            event_uuid=event.event_uuid,
            symbol=event.symbol,
            market=event.market,
            kst_date=gate.kst_date,
            label=session_label(event.symbol, now=now),
        )

        disposition, detail = await _attempt_spawn(spawner=spawner, request=request)
        if disposition is SpawnDisposition.AMBIGUOUS:
            disposition, detail = await _resolve_ambiguity(
                spawner=spawner, request=request, detail=detail
            )

        if disposition in (SpawnDisposition.STARTED, SpawnDisposition.DRY):
            # The claim stays STARTED. It is the *session* that must reach a
            # terminal, by reporting a proposal id or an attributed reason;
            # a started spawn is not itself an outcome. r2 flagged the old
            # behaviour here: marking CONSUMED at spawn time meant a session
            # that died before proposing left a permanently 0-proposal event
            # that looked handled.
            handles[event.event_uuid] = handle
            spawned.append(
                SpawnOutcome(request=request, disposition=disposition, detail=detail)
            )
            continue

        if disposition is SpawnDisposition.NOT_STARTED:
            # Proven clean: hand it straight back, retried next tick.
            await store.release(handle, reason="spawn_not_started")
            lost.append(
                SkippedEvent(event.event_uuid, event.symbol, "spawn_not_started")
            )
            continue

        # AMBIGUOUS and undecidable. Hold the claim so it cannot double
        # spawn, and shout so it cannot silently vanish. The lease still
        # runs, so if nothing reconciles it the TTL sweep records
        # EXPIRED_UNPROCESSED -- an unjudged fire, named as such.
        logger.error(
            "watch_trigger_repricing: AMBIGUOUS spawn for event %s (symbol=%s, "
            "spawn_key=%s): %s -- claim quarantined, needs operator "
            "reconciliation. This event will NOT be retried automatically.",
            event.event_uuid,
            event.symbol,
            request.spawn_key,
            detail,
        )
        needs_reconcile.append(
            SkippedEvent(event.event_uuid, event.symbol, "spawn_ambiguous")
        )

    return TickResult(
        gate=gate,
        spawned=tuple(spawned),
        overflow=selection.overflow,
        skipped=selection.skipped + tuple(lost),
        needs_reconcile=tuple(needs_reconcile),
        handles=handles,
    )
