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
    accident wearing a new hat -- so every claim carries a lease. An
    expired claim is reclaimable, so the event resurfaces on a later tick
    instead of disappearing, and :meth:`ClaimStore.release` returns it
    immediately on an orderly spawn failure.

The lease is what makes the safe ordering safe. Do not remove it.

Persistence
-----------
:class:`InMemoryClaimStore` is process-local: it is the dry-path and test
implementation, and it is **not** sufficient for production, where the
poller runs as repeated Prefect flow runs in separate processes. A durable
implementation needs its own table with a UNIQUE constraint on
``event_uuid`` (an insert conflict *is* the mutual exclusion), which is a
migration -- deliberately not created here. See the ROB-1286 report for the
exact proposed DDL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from app.services.watch_trigger_repricing.consumption import (
    ConsumptionState,
    project_claim_state,
)

__all__ = [
    "Claim",
    "ClaimStore",
    "ClaimStoreUnavailable",
    "DEFAULT_LEASE",
    "InMemoryClaimStore",
]

# One lease must comfortably outlive a symbol-scoped re-judgement session
# (analysis + policy check + order_proposal_create). Too short and a live
# session's event gets re-spawned underneath it; too long and a crashed tick
# hides the fire for that long. 30 minutes is well inside the 09:00-15:30
# window while still leaving several retries in a session.
DEFAULT_LEASE = timedelta(minutes=30)


class ClaimStoreUnavailable(RuntimeError):
    """The store could not be consulted. Never means 'unclaimed'."""


@dataclass(frozen=True)
class Claim:
    """One consumer's lease on one watch event."""

    event_uuid: str
    symbol: str
    claimed_by: str
    claimed_at: datetime
    expires_at: datetime

    def is_active(self, *, now: datetime) -> bool:
        return now < self.expires_at


class ClaimStore(Protocol):
    """Port for the claim store.

    Implementations must make :meth:`try_claim` atomic against concurrent
    callers -- a durable one by relying on a UNIQUE constraint rather than
    a read-then-write, which would reintroduce the race it exists to close.
    """

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

    def release(self, event_uuid: str, *, reason: str) -> None: ...

    def active_symbols(self, *, now: datetime) -> frozenset[str]: ...


@dataclass
class InMemoryClaimStore:
    """Process-local :class:`ClaimStore`. Dry path and tests only."""

    _claims: dict[str, Claim] = field(default_factory=dict)
    _released: list[tuple[str, str]] = field(default_factory=list)
    available: bool = True

    # -- reads ---------------------------------------------------------
    def state_for(self, event_uuid: str, *, now: datetime) -> ConsumptionState:
        if not self.available:
            return project_claim_state(claim_found=False, store_available=False)
        claim = self._claims.get(event_uuid)
        return project_claim_state(
            claim_found=claim is not None and claim.is_active(now=now),
            store_available=True,
        )

    def active_symbols(self, *, now: datetime) -> frozenset[str]:
        if not self.available:
            raise ClaimStoreUnavailable("claim store unavailable")
        return frozenset(
            claim.symbol for claim in self._claims.values() if claim.is_active(now=now)
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
        """Take the lease, or return ``None`` if someone else holds it.

        An expired lease is reclaimable -- that is the self-heal for a tick
        that died between claiming and spawning.
        """
        if not self.available:
            raise ClaimStoreUnavailable("claim store unavailable")
        existing = self._claims.get(event_uuid)
        if existing is not None and existing.is_active(now=now):
            return None
        claim = Claim(
            event_uuid=event_uuid,
            symbol=symbol,
            claimed_by=claimed_by,
            claimed_at=now,
            expires_at=now + lease,
        )
        self._claims[event_uuid] = claim
        return claim

    def release(self, event_uuid: str, *, reason: str) -> None:
        """Hand an event back after an orderly failure, with the reason."""
        if not self.available:
            raise ClaimStoreUnavailable("claim store unavailable")
        self._claims.pop(event_uuid, None)
        self._released.append((event_uuid, reason))

    # -- test/audit introspection --------------------------------------
    @property
    def released(self) -> list[tuple[str, str]]:
        return list(self._released)
