"""ROB-1286 §101차 ① — the durable, fenced claim store.

This is the store the r2 verification found missing. The in-memory one
(:mod:`.claims`) is a rehearsal fixture: its dedup dies with the process,
so two Prefect flow runs both saw an unclaimed fire. Here the claim is a
row, and the exclusion is a database constraint rather than a snapshot
read, which is what makes it hold across processes.

How each r2 finding is closed
-----------------------------
``NEW BLOCKER 1 -- stale claimant fencing``
    Every write is fenced on ``(event_uuid, generation, owner_token)``.
    A claimant whose lease rolled over matches no row, so its ``UPDATE``
    touches zero rows and :meth:`finalise` raises
    :class:`~.claims.ClaimNotHeld` instead of silently terminating the new
    claimant's work. The handle is a required argument, so there is no
    call shape that forgets to fence.
``NEW BLOCKER 2 -- same-symbol, different-event race``
    ``UNIQUE (symbol) WHERE state = 'started'`` in the database. Two ticks
    that both read an empty snapshot still cannot both insert; the loser
    gets an ``IntegrityError`` and returns ``None``.
``NEW BLOCKER 3 -- self-attested arming``
    Not this module's job, but note :meth:`is_durable` is a property of
    the class, not something a caller passes in.

TTL rollover
------------
:meth:`sweep_expired` writes the ``expired_unprocessed`` terminal on leases
that ran out. That is what frees the symbol's partial-unique slot, so the
re-claim at ``generation + 1`` is only possible after the expiry has been
recorded -- the rollover leaves an audit row rather than quietly reusing
the symbol.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.models.watch_event_repricing_claims import WatchEventRepricingClaim
from app.services.watch_trigger_repricing.claims import (
    DEFAULT_LEASE,
    ClaimHandle,
    ClaimNotHeld,
)
from app.services.watch_trigger_repricing.consumption import (
    ConsumptionState,
    project_claim_state,
)
from app.services.watch_trigger_repricing.lifecycle import (
    NON_RECLAIMABLE_STATES,
    TERMINAL_LIFECYCLE_STATES,
    ClaimLifecycle,
    SessionOutcome,
)

# Spelled as raw values once, for the SQL-facing comparisons below.
_NON_RECLAIMABLE_VALUES = tuple(state.value for state in NON_RECLAIMABLE_STATES)

__all__ = ["DatabaseClaimStore"]

_TABLE = WatchEventRepricingClaim


@dataclass
class DatabaseClaimStore:
    """A :class:`~.claims.ClaimStore` backed by ``review`` rows.

    Takes a session factory so one operation owns one short transaction and
    no connection is held between ticks.
    """

    session_factory: object

    @property
    def is_durable(self) -> bool:
        """True by construction -- rows outlive the process."""
        return True

    # -- reads ---------------------------------------------------------
    async def state_for(self, event_uuid: str, *, now: datetime) -> ConsumptionState:
        async with self.session_factory() as session:  # type: ignore[operator]
            row = await self._current_row(session, event_uuid)
        if row is None:
            return project_claim_state(claim_found=False, store_available=True)
        if row.state in TERMINAL_LIFECYCLE_STATES:
            # A terminal claim is consumed regardless of its lease, except
            # the TTL terminal, which explicitly means "nobody judged it".
            if row.state == ClaimLifecycle.EXPIRED_UNPROCESSED:
                return project_claim_state(claim_found=False, store_available=True)
            if row.state == ClaimLifecycle.AWAITING_RECONCILE:
                # Unknown, not done. Blocks every consumer and is reported
                # for operator reconciliation (r2 / BLOCKER 2).
                return ConsumptionState.QUARANTINED
            return ConsumptionState.CONSUMED
        return project_claim_state(
            claim_found=row.lease_expires_at > now, store_available=True
        )

    async def _current_row(self, session, event_uuid: str):
        return await session.scalar(
            sa.select(_TABLE)
            .where(_TABLE.event_uuid == uuid.UUID(str(event_uuid)))
            .order_by(_TABLE.generation.desc())
            .limit(1)
        )

    async def active_symbols(self, *, now: datetime) -> frozenset[str]:
        """Symbols with a live claim.

        Reporting and early-skip only. It is deliberately **not** what makes
        per-symbol concurrency safe -- r2 showed a snapshot read cannot be,
        because two ticks can both observe it empty. The guarantee is the
        partial unique index; this just lets the tick name the reason before
        it bothers trying.
        """
        async with self.session_factory() as session:  # type: ignore[operator]
            rows = (
                await session.scalars(
                    sa.select(_TABLE.symbol).where(
                        _TABLE.state == ClaimLifecycle.STARTED.value,
                        _TABLE.lease_expires_at > now,
                    )
                )
            ).all()
        return frozenset(rows)

    # -- TTL -----------------------------------------------------------
    async def sweep_expired(self, *, now: datetime) -> list[str]:
        """Write the TTL terminal on every lease that ran out.

        Returns the event uuids that expired so the caller can report them:
        an expiry means a fire went unjudged, which is exactly the outcome
        this feature exists to make visible.
        """
        async with self.session_factory() as session:  # type: ignore[operator]
            result = await session.execute(
                sa.update(_TABLE)
                .where(
                    _TABLE.state == ClaimLifecycle.STARTED.value,
                    _TABLE.lease_expires_at <= now,
                )
                .values(
                    state=ClaimLifecycle.EXPIRED_UNPROCESSED.value,
                    finalised_at=now,
                )
                .returning(_TABLE.event_uuid)
            )
            expired = [str(row[0]) for row in result.all()]
            await session.commit()
        return expired

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
        """Insert a ``started`` row, or return ``None`` if someone else holds it.

        The exclusion is the insert itself. Deliberately no "check then
        write": that shape is what let two ticks pass the same snapshot.
        """
        event_id = uuid.UUID(str(event_uuid))
        async with self.session_factory() as session:  # type: ignore[operator]
            latest = await self._current_row(session, event_uuid)
            if latest is not None and latest.state == ClaimLifecycle.STARTED.value:
                if latest.lease_expires_at > now:
                    return None
                # Lease ran out: record the TTL terminal first so the
                # symbol slot is free and the expiry is audited.
                await session.execute(
                    sa.update(_TABLE)
                    .where(
                        _TABLE.id == latest.id,
                        _TABLE.state == ClaimLifecycle.STARTED.value,
                    )
                    .values(
                        state=ClaimLifecycle.EXPIRED_UNPROCESSED.value,
                        finalised_at=now,
                    )
                )
            if latest is not None and latest.state in _NON_RECLAIMABLE_VALUES:
                # Already resolved, or terminally ambiguous. Never re-judge
                # a fire that produced an outcome, or one that *may* have
                # produced a proposal nobody could confirm -- both are the
                # double-proposal direction. r2 / BLOCKER 2: this branch is
                # reached before any lease arithmetic, so TTL expiry cannot
                # undo it.
                return None

            generation = (latest.generation + 1) if latest is not None else 1
            owner_token = uuid.uuid4()
            session.add(
                _TABLE(
                    event_uuid=event_id,
                    symbol=symbol,
                    market=market,
                    generation=generation,
                    owner_token=owner_token,
                    claimed_by=claimed_by,
                    state=ClaimLifecycle.STARTED.value,
                    claimed_at=now,
                    lease_expires_at=now + lease,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                # Either (event_uuid, generation) or the per-symbol partial
                # unique fired. Both mean another claimant won.
                await session.rollback()
                return None
        return ClaimHandle(
            event_uuid=str(event_uuid),
            symbol=symbol,
            generation=generation,
            owner_token=str(owner_token),
        )

    # -- finalise ------------------------------------------------------
    async def finalise(self, handle: ClaimHandle, outcome: SessionOutcome) -> None:
        """Write a terminal, fenced on the handle.

        ``handle`` is required, not optional: r2 found that finalisation
        keyed only on ``event_uuid`` let a rolled-over claimant terminate
        the current one's row.
        """
        async with self.session_factory() as session:  # type: ignore[operator]
            result = await session.execute(
                sa.update(_TABLE)
                .where(
                    _TABLE.event_uuid == uuid.UUID(handle.event_uuid),
                    _TABLE.generation == handle.generation,
                    _TABLE.owner_token == uuid.UUID(handle.owner_token),
                    _TABLE.state == ClaimLifecycle.STARTED.value,
                )
                .values(
                    state=outcome.state.value,
                    proposal_id=outcome.proposal_id,
                    rejection_reason=outcome.rejection_reason,
                    finalised_at=sa.func.now(),
                )
            )
            if result.rowcount == 0:
                await session.rollback()
                raise ClaimNotHeld(
                    f"claim {handle.event_uuid}#{handle.generation} is not held by "
                    f"this owner (lease rolled over, or already terminal); "
                    "refusing to write over the current claimant"
                )
            await session.commit()

    async def release(self, handle: ClaimHandle, *, reason: str) -> None:
        """Hand a claim back after a *proven* clean failure to start.

        ROB-1290: this method was on the :class:`~.claims.ClaimStore`
        protocol and implemented only by the in-memory rehearsal store. No
        shipped spawner could return ``NOT_STARTED`` -- the dry one always
        answers ``DRY`` -- so the orchestrator's release branch had never
        run against the durable store, and the first spawner that could
        prove a clean failure would have hit ``AttributeError`` mid-tick
        and left the claim held until its lease expired.

        Deletes the row rather than writing a terminal, exactly as the
        in-memory store does: nothing was judged, so the fire must look
        untouched to the next tick, and the row must stop occupying the
        per-symbol partial-unique slot. The release is not silent -- the
        orchestrator logs it and reports the event with its reason -- and
        the evidence that matters (that no proposal exists) is the absence
        of a proposal row, not the absence of this one.

        Fenced like :meth:`finalise`: a rolled-over owner matches no row
        and raises rather than deleting the current claimant's work, or an
        already-written ``expired_unprocessed`` terminal.
        """
        async with self.session_factory() as session:  # type: ignore[operator]
            result = await session.execute(
                sa.delete(_TABLE).where(
                    _TABLE.event_uuid == uuid.UUID(handle.event_uuid),
                    _TABLE.generation == handle.generation,
                    _TABLE.owner_token == uuid.UUID(handle.owner_token),
                    _TABLE.state == ClaimLifecycle.STARTED.value,
                )
            )
            if result.rowcount == 0:
                await session.rollback()
                raise ClaimNotHeld(
                    f"claim {handle.event_uuid}#{handle.generation} is not held by "
                    f"this owner (lease rolled over, or already terminal); "
                    f"refusing to release it (reason={reason!r})"
                )
            await session.commit()

    async def outcomes_for(self, event_uuids: list[str]) -> dict[str, SessionOutcome]:
        """Read back terminals so the completion mapping is a stored fact."""
        if not event_uuids:
            return {}
        ids = [uuid.UUID(str(value)) for value in event_uuids]
        async with self.session_factory() as session:  # type: ignore[operator]
            rows = (
                await session.scalars(
                    sa.select(_TABLE)
                    .where(
                        _TABLE.event_uuid.in_(ids),
                        _TABLE.state != ClaimLifecycle.STARTED.value,
                    )
                    .order_by(_TABLE.generation.asc())
                )
            ).all()
        resolved: dict[str, SessionOutcome] = {}
        for row in rows:
            resolved[str(row.event_uuid)] = SessionOutcome(
                state=ClaimLifecycle(row.state),
                proposal_id=row.proposal_id,
                rejection_reason=row.rejection_reason,
            )
        return resolved
