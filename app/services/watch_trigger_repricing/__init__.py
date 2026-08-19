"""ROB-1286 — watch-fire triggered repricing (§93차 A안).

Turns a delivered, unconsumed ``review_required`` watch event into a
symbol-scoped re-judgement session, so a fire no longer waits for the next
rep cycle to be noticed.

The package is deliberately split so the risky part is pure and testable:

``consumption``
    The **canonical** definition of "this watch event has been consumed".
    Both consumers (A안 = this flow, B안 = the rep session's end-of-session
    re-check) are defined against this one module.
``claims``
    The claim store port. A claim is taken *before* a spawn; an in-progress
    lease expires so a crashed tick self-heals, and a terminal claim never
    does so a finished session is not repeated.
``capability``
    The proposal-only tool allowlist a spawned session is granted. The
    execution boundary is enforced here, not merely declared.
``event_source``
    The read-only DB seam the tick polls. The one file allowed to touch
    ``InvestmentReportsRepository``, and only its one read method.
``poller``
    Rows -> candidate events, de-duplicated.
``gate``
    Trading-session / intraday-window gate. Reuses the repo's existing
    offline XKRX calendar; invents no holiday judgement.
``selection``
    Pure dedup + concurrency + per-round cap, with overflow surfaced.
``spawn``
    Session spawner port. The shipped implementation is dry: it records
    what *would* be spawned and never starts a session.
``orchestrator``
    The tick: gate -> poll -> claim -> spawn -> resolve the claim against
    what the spawn actually proved.

Safety boundary: nothing here submits an order, mutates a broker, touches a
watch alert, or relaxes an approval gate. The spawned session's execution
boundary is ``order_proposal_create``, enforced as a capability allowlist
that structurally excludes every order-mutation tool.
"""

from __future__ import annotations

__all__ = [
    "capability",
    "claims",
    "consumption",
    "event_source",
    "gate",
    "orchestrator",
    "poller",
    "selection",
    "spawn",
]
