"""ROB-1286 — the claim store port, and an in-memory rehearsal implementation.

A claim is one attempt, by one owner, to re-judge one watch fire. Holding
it means "I am responsible for this event"; releasing it without a terminal
means nobody is, which is the accident this feature exists to remove.

Ordering: claim, then spawn
---------------------------
``spawn -> claim`` loses a crash as a double spawn: the event still reads
unclaimed, the next tick starts a second session, and two sessions
independently reach ``order_proposal_create`` for one symbol.

``claim -> spawn`` loses a crash as an *unjudged* event, which the lease
converts into bounded latency: the TTL sweep writes
``EXPIRED_UNPROCESSED`` and the flow re-claims at ``generation + 1``.

Fencing (r2 NEW BLOCKER 1)
--------------------------
Every terminal write takes a :class:`ClaimHandle` carrying the generation
and owner token. A claimant whose lease already rolled over matches no row
and is refused with :class:`ClaimNotHeld`, so it cannot terminate the
current claimant's work. The handle is a positional requirement of
:meth:`ClaimStore.finalise`; there is no unfenced call to write.

Async on purpose
----------------
The production store is
:class:`~.db_claim_store.DatabaseClaimStore`, which is async. Making the
port async means the rehearsal store and the real one have the *same*
shape, so a test that passes against the in-memory store is exercising the
call sequence the DB store will see. r2 found the previous split let the
tested path and the shipped path drift.

:class:`InMemoryClaimStore` is a rehearsal fixture and reports
``is_durable = False``. The arming contract refuses to run a live spawner
against it, so it cannot be what production ends up using by omission.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from app.services.watch_trigger_repricing.consumption import (
    ConsumptionState,
    project_claim_state,
)
from app.services.watch_trigger_repricing.lifecycle import (
    NON_RECLAIMABLE_STATES,
    RESOLVED_LIFECYCLE_STATES,
    ClaimLifecycle,
    SessionOutcome,
)

__all__ = [
    "DEFAULT_LEASE",
    "Claim",
    "ClaimHandle",
    "ClaimNotHeld",
    "ClaimStore",
    "ClaimStoreUnavailable",
    "InMemoryClaimStore",
]

# One lease must outlive a symbol-scoped re-judgement session (analysis,
# policy check, proposal create). Too short re-spawns underneath a live
# session; too long hides a crashed tick for that long.
DEFAULT_LEASE = timedelta(minutes=30)


class ClaimStoreUnavailable(RuntimeError):
    """The store could not be consulted. Never means 'unclaimed'."""


class ClaimNotHeld(RuntimeError):
    """A finalise arrived from someone who no longer owns the claim."""


@dataclass(frozen=True)
class ClaimHandle:
    """Proof that *this* caller holds *this* generation of a claim.

    Carrying ``generation`` and ``owner_token`` puts the fence in the call
    signature, so an unfenced finalise is not expressible.
    """

    event_uuid: str
    symbol: str
    generation: int
    owner_token: str


@dataclass
class Claim:
    """One claim record."""

    event_uuid: str
    symbol: str
    market: str
    generation: int
    owner_token: str
    claimed_by: str
    state: ClaimLifecycle
    lease_expires_at: datetime
    proposal_id: str | None = None
    rejection_reason: str | None = None

    def is_live(self, *, now: datetime) -> bool:
        return self.state is ClaimLifecycle.STARTED and now < self.lease_expires_at


class ClaimStore(Protocol):
    """Port. Implementations must make :meth:`try_claim` atomic."""

    @property
    def is_durable(self) -> bool: ...

    async def state_for(
        self, event_uuid: str, *, now: datetime
    ) -> ConsumptionState: ...

    async def active_symbols(self, *, now: datetime) -> frozenset[str]: ...

    async def try_claim(
        self,
        *,
        event_uuid: str,
        symbol: str,
        market: str,
        claimed_by: str,
        now: datetime,
        lease: timedelta = DEFAULT_LEASE,
    ) -> ClaimHandle | None: ...

    async def finalise(self, handle: ClaimHandle, outcome: SessionOutcome) -> None: ...

    async def release(self, handle: ClaimHandle, *, reason: str) -> None: ...


@dataclass
class InMemoryClaimStore:
    """Process-local rehearsal store. Not durable; see the module docstring."""

    _claims: dict[tuple[str, int], Claim] = field(default_factory=dict)
    _released: list[tuple[str, str]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    available: bool = True

    @property
    def is_durable(self) -> bool:
        return False

    # -- internals -----------------------------------------------------
    def _latest(self, event_uuid: str) -> Claim | None:
        matches = [c for (uid, _g), c in self._claims.items() if uid == event_uuid]
        return max(matches, key=lambda c: c.generation) if matches else None

    def _expire_locked(self, *, now: datetime) -> None:
        """TTL sweep. Writes the terminal so the symbol slot frees."""
        for claim in self._claims.values():
            if claim.state is ClaimLifecycle.STARTED and claim.lease_expires_at <= now:
                claim.state = ClaimLifecycle.EXPIRED_UNPROCESSED

    # -- reads ---------------------------------------------------------
    async def state_for(self, event_uuid: str, *, now: datetime) -> ConsumptionState:
        if not self.available:
            return project_claim_state(claim_found=False, store_available=False)
        with self._lock:
            self._expire_locked(now=now)
            latest = self._latest(event_uuid)
            if latest is None:
                return project_claim_state(claim_found=False, store_available=True)
            if latest.state in RESOLVED_LIFECYCLE_STATES:
                return ConsumptionState.CONSUMED
            if latest.state is ClaimLifecycle.AWAITING_RECONCILE:
                # Terminal and a fault: nobody knows whether a proposal
                # exists, so no consumer may take it (r2 / BLOCKER 2).
                return ConsumptionState.QUARANTINED
            return project_claim_state(
                claim_found=latest.is_live(now=now), store_available=True
            )

    async def active_symbols(self, *, now: datetime) -> frozenset[str]:
        if not self.available:
            raise ClaimStoreUnavailable("claim store unavailable")
        with self._lock:
            self._expire_locked(now=now)
            return frozenset(
                c.symbol for c in self._claims.values() if c.is_live(now=now)
            )

    # -- claim ---------------------------------------------------------
    async def try_claim(
        self,
        *,
        event_uuid: str,
        symbol: str,
        market: str = "kr",
        claimed_by: str,
        now: datetime,
        lease: timedelta = DEFAULT_LEASE,
    ) -> ClaimHandle | None:
        """Atomic check-and-insert. Mirrors the DB store's two unique keys."""
        if not self.available:
            raise ClaimStoreUnavailable("claim store unavailable")
        with self._lock:
            self._expire_locked(now=now)
            latest = self._latest(event_uuid)
            if latest is not None:
                if latest.is_live(now=now):
                    return None
                if latest.state in NON_RECLAIMABLE_STATES:
                    # Already resolved, or terminally ambiguous. Re-judging
                    # either is the double-proposal direction: the first
                    # produced a proposal, or *may* have and nobody knows.
                    # Note this is checked after ``is_live``, which is a
                    # lease question -- a terminal outlives its lease by
                    # design, so no clock can walk this refusal back.
                    return None
            # Stands in for UNIQUE (symbol) WHERE state = 'started'. r2
            # NEW BLOCKER 2: without this, two ticks holding the same empty
            # snapshot both spawned on one symbol.
            if any(
                c.symbol == symbol and c.is_live(now=now) for c in self._claims.values()
            ):
                return None
            generation = (latest.generation + 1) if latest is not None else 1
            owner_token = str(uuid.uuid4())
            self._claims[(event_uuid, generation)] = Claim(
                event_uuid=event_uuid,
                symbol=symbol,
                market=market,
                generation=generation,
                owner_token=owner_token,
                claimed_by=claimed_by,
                state=ClaimLifecycle.STARTED,
                lease_expires_at=now + lease,
            )
        return ClaimHandle(
            event_uuid=event_uuid,
            symbol=symbol,
            generation=generation,
            owner_token=owner_token,
        )

    # -- terminal ------------------------------------------------------
    async def finalise(self, handle: ClaimHandle, outcome: SessionOutcome) -> None:
        if not self.available:
            raise ClaimStoreUnavailable("claim store unavailable")
        with self._lock:
            claim = self._claims.get((handle.event_uuid, handle.generation))
            if (
                claim is None
                or claim.owner_token != handle.owner_token
                or claim.state is not ClaimLifecycle.STARTED
            ):
                raise ClaimNotHeld(
                    f"claim {handle.event_uuid}#{handle.generation} is not held by "
                    "this owner (lease rolled over, or already terminal)"
                )
            claim.state = outcome.state
            claim.proposal_id = outcome.proposal_id
            claim.rejection_reason = outcome.rejection_reason

    async def release(self, handle: ClaimHandle, *, reason: str) -> None:
        """Hand a claim back after a *proven* clean failure to start.

        Drops the row entirely rather than writing a terminal: nothing was
        judged, so the fire must look untouched to the next tick.
        """
        if not self.available:
            raise ClaimStoreUnavailable("claim store unavailable")
        with self._lock:
            claim = self._claims.get((handle.event_uuid, handle.generation))
            if (
                claim is None
                or claim.owner_token != handle.owner_token
                # Must still be live. A rolled-over owner whose row is
                # already EXPIRED_UNPROCESSED would otherwise delete that
                # terminal, erasing the evidence that a fire went unjudged.
                or claim.state is not ClaimLifecycle.STARTED
            ):
                raise ClaimNotHeld(
                    f"claim {handle.event_uuid}#{handle.generation} is not held "
                    "by this owner (lease rolled over, or already terminal)"
                )
            del self._claims[(handle.event_uuid, handle.generation)]
            self._released.append((handle.event_uuid, reason))

    # -- test/audit introspection --------------------------------------
    @property
    def released(self) -> list[tuple[str, str]]:
        return list(self._released)

    def snapshot(self) -> list[Claim]:
        with self._lock:
            return list(self._claims.values())
