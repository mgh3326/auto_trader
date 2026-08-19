"""ROB-1286 §101차 ② ⑥ — the entrypoint the scheduler calls.

Ownership split (§101차 ⑥)
--------------------------
The **schedule** lives in ``robin-prefect-automations``, outside this repo.
The **logic** lives here, and this module is the seam between them: a
scheduler calls :func:`run_watch_repricing_tick` and gets a dict back. That
matches how ``scripts/b0x`` and the other operator lanes are driven -- the
repo owns the behaviour, something outside it owns the cadence -- and it is
why this PR registers no Prefect deployment, no cron, and no flow object.

r2's B3 finding
---------------
The r2 entrypoint took ``events`` as a required argument and never polled.
Its "E2E" test hand-assembled a fake source, called the poller itself, and
passed the result into the tick. So the one thing a real deployment could
get wrong -- which rows the tick sees -- was the one thing not exercised;
the shipped shell, run with no arguments, polled nothing and did nothing.

Here the entrypoint **builds the source and polls**. ``events`` is gone as
an input. A caller may inject a source (a test does), but the default is
:class:`~.event_source.DatabaseWatchEventSource` over the app's real
session factory, so the wiring under test is the wiring that ships.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from app.core.config import settings
from app.services.watch_trigger_repricing.claims import ClaimNotHeld, ClaimStore
from app.services.watch_trigger_repricing.event_source import (
    DatabaseWatchEventSource,
    WatchEventSource,
)
from app.services.watch_trigger_repricing.lifecycle import (
    SessionOutcome,
    build_completion_mapping,
)
from app.services.watch_trigger_repricing.orchestrator import run_repricing_tick
from app.services.watch_trigger_repricing.poller import (
    DEFAULT_POLL_LIMIT,
    poll_candidate_events,
)
from app.services.watch_trigger_repricing.selection import DEFAULT_ROUND_CAP
from app.services.watch_trigger_repricing.spawn import DrySessionSpawner, SessionSpawner

logger = logging.getLogger(__name__)

__all__ = ["DISABLED_RESULT", "run_watch_repricing_tick"]

DISABLED_RESULT: dict[str, Any] = {
    "status": "disabled",
    "polled": [],
    "spawned": [],
    "overflow": [],
    "skipped": [],
    "needsReconcile": [],
    "completion": [],
    "completionQuarantined": [],
}


def _default_source() -> WatchEventSource:
    # Imported lazily so importing this module does not construct an engine.
    from app.core.db import AsyncSessionLocal

    return DatabaseWatchEventSource(AsyncSessionLocal)


async def run_watch_repricing_tick(
    *,
    source: WatchEventSource | None = None,
    store: ClaimStore | None = None,
    spawner: SessionSpawner | None = None,
    now: dt.datetime | None = None,
    delivered_since: dt.datetime | None = None,
    round_cap: int = DEFAULT_ROUND_CAP,
    poll_limit: int = DEFAULT_POLL_LIMIT,
    market: str = "kr",
) -> dict[str, Any]:
    """Poll the watch-event table and run one repricing tick.

    Returns the tick report plus two things r2 asked for:

    ``polled``
        The event set N, fixed from what the poll actually saw, before any
        selection or capping. The completion criterion is judged against
        this, so it has to be recorded rather than reconstructed.
    ``completion``
        The event -> ``{proposal_id | rejection_reason}`` mapping. Every
        polled fire appears; one with neither is an ``unmapped`` row rather
        than an omission.
    """
    if not getattr(settings, "WATCH_TRIGGER_REPRICING_ENABLED", False):
        return dict(DISABLED_RESULT)

    resolved_now = now or dt.datetime.now(dt.UTC)
    resolved_source = source if source is not None else _default_source()

    candidates = await poll_candidate_events(
        resolved_source,
        market=market,
        delivered_since=delivered_since,
        limit=poll_limit,
    )
    polled = [(event.event_uuid, event.symbol) for event in candidates]
    logger.info("watch_trigger_repricing: polled %d delivered fire(s)", len(candidates))

    from app.services.watch_trigger_repricing.orchestrator import (
        process_claim_store,
    )

    resolved_store = store if store is not None else process_claim_store()
    resolved_spawner = spawner if spawner is not None else DrySessionSpawner()

    from app.services.watch_trigger_repricing.arming import (
        ArmingRefused,
        assert_arming_contract,
    )

    try:
        assert_arming_contract(spawner=resolved_spawner, store=resolved_store)
    except ArmingRefused as refusal:
        logger.error("watch_trigger_repricing: %s", refusal)
        blocked = dict(DISABLED_RESULT)
        blocked.update(
            {
                "status": "blocked",
                "reason": refusal.reason,
                "detail": str(refusal),
                "polled": [{"eventUuid": uid, "symbol": sym} for uid, sym in polled],
            }
        )
        return blocked

    result = await run_repricing_tick(
        candidates,
        store=resolved_store,
        now=resolved_now,
        spawner=resolved_spawner,
        round_cap=round_cap,
        market=market,
    )

    # Write each session's terminal through its fenced handle. A stale
    # claimant raises ClaimNotHeld here rather than overwriting the current
    # one (r2 NEW BLOCKER 1), and an event whose session reported nothing
    # keeps its STARTED claim until the TTL sweep names it
    # EXPIRED_UNPROCESSED -- it is never quietly marked done.
    outcomes: dict[str, SessionOutcome] = dict(
        getattr(resolved_spawner, "session_outcomes", {}) or {}
    )
    for event_uuid, outcome in outcomes.items():
        handle = result.handles.get(event_uuid)
        if handle is None:
            continue
        try:
            await resolved_store.finalise(handle, outcome)
        except ClaimNotHeld:
            logger.exception(
                "watch_trigger_repricing: refusing stale finalise for event %s "
                "(generation %s no longer holds the claim)",
                event_uuid,
                handle.generation,
            )
    # Everything the tick knowingly set aside, with the reason it gave.
    # Quarantined fires are split out: they are terminal and no later tick
    # will pick them up, so reporting them as "deferred" would promise a
    # retry that is deliberately never coming (r2 / BLOCKER 2).
    quarantined = {
        skipped.event_uuid: skipped.reason for skipped in result.needs_reconcile
    }
    deferrals = {
        skipped.event_uuid: skipped.reason
        for skipped in result.overflow + result.skipped
    }
    completion = build_completion_mapping(
        polled_event_uuids=polled,
        outcomes=outcomes,
        deferrals=deferrals,
        quarantined=quarantined,
    )
    for row in completion.unaccounted:
        logger.warning(
            "watch_trigger_repricing: event %s (symbol=%s) resolved to neither a "
            "proposal nor an attributed rejection reason (state=%s) -- this is a "
            "completion failure, not a quiet skip",
            row.event_uuid,
            row.symbol,
            row.state,
        )

    payload: dict[str, Any] = {"status": "ok", **result.as_dict()}
    payload["polled"] = [{"eventUuid": uid, "symbol": sym} for uid, sym in polled]
    payload["completion"] = completion.as_table()
    payload["completionComplete"] = completion.is_complete
    payload["completionAccounted"] = completion.is_accounted
    payload["completionDeferred"] = [row.event_uuid for row in completion.deferred]
    payload["completionQuarantined"] = [
        row.event_uuid for row in completion.quarantined
    ]
    for row in completion.quarantined:
        logger.error(
            "watch_trigger_repricing: event %s (symbol=%s) is quarantined (%s) -- "
            "whether a proposal was created is unknown, it will NOT be re-judged, "
            "and an operator must reconcile it",
            row.event_uuid,
            row.symbol,
            row.deferral_reason,
        )
    for row in completion.deferred:
        logger.info(
            "watch_trigger_repricing: event %s (symbol=%s) deferred to a later tick "
            "(%s) -- not judged yet, and not counted as complete",
            row.event_uuid,
            row.symbol,
            row.deferral_reason,
        )
    return payload
