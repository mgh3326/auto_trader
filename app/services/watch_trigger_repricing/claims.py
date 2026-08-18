"""ROB-1286 §3④ — the claim store: atomic, leased, claimed-before-spawn.

Ordering
--------
The claim is taken **before** the spawn, never after. The two orderings
fail in opposite directions:

``spawn -> claim``
    A crash between the two leaves an event that was already spawned but
    still reads as unclaimed. The next tick spawns it again, and two
    sessions independently reach ``order_proposal_create`` for the same
    symbol. That is the T3 failure this issue is trying not to create.

``claim -> spawn`` (chosen)
    A crash between the two leaves a claimed event that nobody is working.
    Left alone that would bury the fire -- which is the *original* ROB-1286
    accident wearing a new hat -- so every **non-terminal** claim carries a
    lease. An expired lease is reclaimable, so the event resurfaces on a
    later tick instead of disappearing, and :meth:`ClaimStore.release`
    returns it immediately on a spawn that proved it started nothing.

Two clocks, not one (r2 / BLOCKER-1)
------------------------------------
r1 had a single lease and therefore a single clock, so an event whose
session had genuinely run became reclaimable 30 minutes later and was
re-spawned. The store now keeps the two apart:

* **Event lifecycle** -- ``UNCLAIMED -> CLAIMED -> CONSUMED|QUARANTINED``.
  The two terminal states are permanent. :meth:`try_claim` refuses them
  regardless of any expiry, and :meth:`release` refuses to walk them back.
  Only ``CLAIMED`` -- an in-progress lease whose holder may have died --
  expires.
* **Symbol occupancy** -- lease-bounded for every state including terminal
  ones. A session that just started should block a second session on the
  same symbol, but only for as long as it could plausibly be running.
  Blocking a symbol permanently because one of its fires was consumed
  would mute every later fire on that symbol for the rest of the day.

So :meth:`state_for` reads the event lifecycle and :meth:`active_symbols`
reads the lease, and they legitimately disagree about a consumed claim
whose lease has lapsed: that event is done forever, that symbol is free.

Atomicity
---------
:meth:`try_claim` is the mutual-exclusion primitive, so its check and its
write must not be separable. :class:`InMemoryClaimStore` holds a lock
across both; a read-then-write without one loses the race it exists to
close (``test_atomicity_concurrency.py`` proves this with real threads and
a forced interleave, not sequential calls).

Persistence
-----------
:class:`InMemoryClaimStore` is process-local -- ``is_durable`` is ``False``
and that flag is load-bearing: :func:`.orchestrator.run_gated_tick` refuses
to run a non-dry spawner against a non-durable store. Within one process it
now *does* hold across flow runs, because ``run_gated_tick`` resolves a
process-level singleton instead of constructing a fresh store per call
(that was the other half of BLOCKER-1). Across processes it holds nothing,
and Prefect flow runs are separate processes.

**What cannot be closed without a migration**: durable cross-process dedup.
It needs its own table with a UNIQUE constraint on ``event_uuid`` (an
insert conflict *is* the mutual exclusion) plus terminal-state and lease
columns mirroring :class:`ConsumptionState`. That is approval-gated and is
deliberately not created here; see the ROB-1286 report for the proposed
DDL. Until it lands, arming this flow in production is blocked by the
``is_durable`` gate rather than by a comment.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from app.services.watch_trigger_repricing.consumption import (
    ConsumptionState,
    project_claim_state,
)

__all__ = [
    "Claim",
    "ClaimNotHeld",
    "ClaimStore",
    "ClaimStoreUnavailable",
    "DEFAULT_LEASE",
    "InMemoryClaimStore",
    "TerminalClaimNotReleasable",
]

# One lease must comfortably outlive a symbol-scoped re-judgement session
# (analysis + policy check + order_proposal_create). Too short and a live
# session's event gets re-spawned underneath it; too long and a crashed tick
# hides the fire for that long. 30 minutes is well inside the 09:00-15:30
# window while still leaving several retries in a session.
DEFAULT_LEASE = timedelta(minutes=30)


class ClaimStoreUnavailable(RuntimeError):
    """The store could not be consulted. Never means 'unclaimed'."""


class ClaimNotHeld(RuntimeError):
    """Tried to finalise a claim this caller does not hold."""


class TerminalClaimNotReleasable(RuntimeError):
    """Tried to walk back a CONSUMED/QUARANTINED claim.

    Raised rather than ignored: a caller that thinks it can release a
    terminal claim has a bug whose symptom is a duplicate sell proposal,
    and silently no-oping would hide it.
    """


@dataclass(frozen=True)
class Claim:
    """One consumer's lease on one watch event."""

    event_uuid: str
    symbol: str
    claimed_by: str
    claimed_at: datetime
    expires_at: datetime
    terminal_state: ConsumptionState | None = None
    terminal_reason: str | None = None

    def is_active(self, *, now: datetime) -> bool:
        """Is the *lease* still running? Says nothing about terminality."""
        return now < self.expires_at

    @property
    def is_terminal(self) -> bool:
        return self.terminal_state is not None


class ClaimStore(Protocol):
    """Port for the claim store.

    Implementations must make :meth:`try_claim` atomic against concurrent
    callers -- a durable one by relying on a UNIQUE constraint rather than
    a read-then-write, which would reintroduce the race it exists to close.
    """

    @property
    def is_durable(self) -> bool:
        """True only if claims survive across processes.

        Gates arming: a live spawner against a non-durable store is
        refused, because dedup that evaporates with the process is not
        dedup at all.
        """
        ...

    def state_for(self, event_uuid: str, *, now: datetime) -> ConsumptionState: ...

    def try_claim(
        self,
        *,
        event_uuid: str,
        symbol: str,
        claimed_by: str,
        now: datetime,
        lease: timedelta = DEFAULT_LEASE,
    ) -> Claim | None: ...

    def mark_consumed(self, event_uuid: str, *, reason: str) -> None: ...

    def quarantine(self, event_uuid: str, *, reason: str) -> None: ...

    def release(self, event_uuid: str, *, reason: str) -> None: ...

    def active_symbols(self, *, now: datetime) -> frozenset[str]: ...


@dataclass
class InMemoryClaimStore:
    """Process-local :class:`ClaimStore`. Dry path and tests only."""

    _claims: dict[str, Claim] = field(default_factory=dict)
    _released: list[tuple[str, str]] = field(default_factory=list)
    _finalised: list[tuple[str, ConsumptionState, str]] = field(default_factory=list)
    available: bool = True

    # Re-entrant so a hook (below) may call back in without deadlocking.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # Test seam only: invoked inside :meth:`try_claim` between the check and
    # the write, so a concurrency test can force the interleave that a
    # read-then-write implementation loses. Production never sets it.
    _race_hook: Callable[[], None] | None = field(default=None, repr=False)

    @property
    def is_durable(self) -> bool:
        return False

    # -- reads ---------------------------------------------------------
    def state_for(self, event_uuid: str, *, now: datetime) -> ConsumptionState:
        if not self.available:
            return project_claim_state(claim_found=False, store_available=False)
        with self._lock:
            claim = self._claims.get(event_uuid)
            if claim is None:
                return project_claim_state(claim_found=False, store_available=True)
            # Terminality is read first and without consulting the lease:
            # a consumed event stays consumed after its lease lapses.
            return project_claim_state(
                claim_found=claim.is_active(now=now),
                store_available=True,
                terminal_state=claim.terminal_state,
            )

    def active_symbols(self, *, now: datetime) -> frozenset[str]:
        """Symbols with a live lease -- terminal or not.

        Deliberately lease-bounded even for terminal claims: see the module
        docstring on the two clocks.
        """
        if not self.available:
            raise ClaimStoreUnavailable("claim store unavailable")
        with self._lock:
            return frozenset(
                claim.symbol
                for claim in self._claims.values()
                if claim.is_active(now=now)
            )

    # -- writes --------------------------------------------------------
    def try_claim(
        self,
        *,
        event_uuid: str,
        symbol: str,
        claimed_by: str,
        now: datetime,
        lease: timedelta = DEFAULT_LEASE,
    ) -> Claim | None:
        """Take the lease, or return ``None`` if the event is not takeable.

        Not takeable means either an active lease someone else holds, or a
        terminal record. An expired **non-terminal** lease is reclaimable --
        that is the self-heal for a tick that died between claiming and
        spawning, and it is the only case that reclaims.
        """
        if not self.available:
            raise ClaimStoreUnavailable("claim store unavailable")
        with self._lock:
            existing = self._claims.get(event_uuid)
            if existing is not None:
                if existing.is_terminal:
                    # Permanent. No expiry walks this back.
                    return None
                if existing.is_active(now=now):
                    return None
            if self._race_hook is not None:
                self._race_hook()
            claim = Claim(
                event_uuid=event_uuid,
                symbol=symbol,
                claimed_by=claimed_by,
                claimed_at=now,
                expires_at=now + lease,
            )
            self._claims[event_uuid] = claim
            return claim

    def mark_consumed(self, event_uuid: str, *, reason: str) -> None:
        """Finalise a held claim as CONSUMED. Permanent."""
        self._finalise(event_uuid, ConsumptionState.CONSUMED, reason)

    def quarantine(self, event_uuid: str, *, reason: str) -> None:
        """Finalise a held claim as QUARANTINED. Permanent, and a fault."""
        self._finalise(event_uuid, ConsumptionState.QUARANTINED, reason)

    def _finalise(self, event_uuid: str, state: ConsumptionState, reason: str) -> None:
        if not self.available:
            raise ClaimStoreUnavailable("claim store unavailable")
        with self._lock:
            existing = self._claims.get(event_uuid)
            if existing is None:
                raise ClaimNotHeld(
                    f"cannot finalise {event_uuid!r} as {state}: no claim held"
                )
            if existing.is_terminal:
                # Idempotent for the same state; a genuine conflict is a bug.
                if existing.terminal_state is state:
                    return
                raise TerminalClaimNotReleasable(
                    f"{event_uuid!r} is already {existing.terminal_state}; "
                    f"refusing to re-finalise as {state}"
                )
            self._claims[event_uuid] = Claim(
                event_uuid=existing.event_uuid,
                symbol=existing.symbol,
                claimed_by=existing.claimed_by,
                claimed_at=existing.claimed_at,
                expires_at=existing.expires_at,
                terminal_state=state,
                terminal_reason=reason,
            )
            self._finalised.append((event_uuid, state, reason))

    def release(self, event_uuid: str, *, reason: str) -> None:
        """Hand an event back after a failure that proved nothing started.

        Refuses terminal claims: releasing one would re-open an event a
        session has already acted on.
        """
        if not self.available:
            raise ClaimStoreUnavailable("claim store unavailable")
        with self._lock:
            existing = self._claims.get(event_uuid)
            if existing is not None and existing.is_terminal:
                raise TerminalClaimNotReleasable(
                    f"refusing to release {event_uuid!r}: "
                    f"already {existing.terminal_state}"
                )
            self._claims.pop(event_uuid, None)
            self._released.append((event_uuid, reason))

    # -- test/audit introspection --------------------------------------
    @property
    def released(self) -> list[tuple[str, str]]:
        return list(self._released)

    @property
    def finalised(self) -> list[tuple[str, ConsumptionState, str]]:
        return list(self._finalised)
