"""ROB-1286 — the session spawner port.

The only implementation this PR ships is :class:`DrySessionSpawner`, which
records the spawn request and starts nothing. That is what makes AC1's
"dry path" dry: the polling, gating, dedup, claiming and cap logic all run
for real, and the single step that would start a session is a recorder.

A live spawner is out of scope here and is gated three times over: the flow
is default-off, no live implementation exists in the repo to select, and
:func:`.orchestrator.run_gated_tick` refuses a non-dry spawner unless the
claim store is durable.

Execution boundary (ROB-1286 불변)
---------------------------------
A spawned session's boundary is ``order_proposal_create`` -- and in r2 that
is a **capability**, not a sentence. Every :class:`SpawnRequest` carries a
:class:`~.capability.CapabilityProfile` that is validated at construction
against the proposal-only allowlist, so a request granting a broker
mutation tool cannot be built at all, let alone handed to a spawner. See
:mod:`.capability` for why a declaration was not enough.

Three-valued outcomes (r2 / BLOCKER-2)
--------------------------------------
``started: bool`` could not express the case that matters. A spawner that
raises after the remote session is already up is not "not started", and
treating it as such made the next tick spawn a duplicate. A spawner that
returns cleanly having started nothing is not "started" either, and
treating it as such buried the fire for a lease. :class:`SpawnDisposition`
names all four real answers, and each gets its own claim handling in
:mod:`.orchestrator`:

``STARTED``      proven up -- terminal CONSUMED.
``NOT_STARTED``  proven not up -- claim released, retried next tick.
``AMBIGUOUS``    unknown -- reconciled if the spawner can, quarantined if not.
``DRY``          the rehearsal path -- nothing started, and nothing will be.

A spawner proves "nothing started" by raising :class:`SpawnNotStarted` (or
returning ``NOT_STARTED``). **Any other exception is AMBIGUOUS**, because a
generic failure cannot distinguish "the request never left" from "the
session is up and the acknowledgement timed out".

``spawn_key`` exists for the ambiguous branch: it is derived from
``event_uuid`` alone, so it is identical across ticks, retries and
processes, and a live spawner can use it to ask its backend "is a session
with this key already running?" -- which is what turns AMBIGUOUS back into
a decidable answer via :meth:`ReconcilableSpawner.reconcile`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.services.watch_trigger_repricing.capability import (
    EXECUTION_BOUNDARY,
    PROPOSAL_ONLY_PROFILE,
    CapabilityProfile,
    assert_proposal_only,
)
from app.services.watch_trigger_repricing.gate import KST

__all__ = [
    "EXECUTION_BOUNDARY",
    "DrySessionSpawner",
    "ReconcilableSpawner",
    "SessionSpawner",
    "SpawnDisposition",
    "SpawnNotStarted",
    "SpawnOutcome",
    "SpawnRequest",
    "session_label",
    "spawn_key_for",
]

# Stable prefix for the deterministic spawn identity. Changing it orphans
# in-flight sessions from their reconcile lookups.
SPAWN_KEY_PREFIX = "rob1286-watch-repricing"


class SpawnDisposition(StrEnum):
    """What is actually known about the spawn attempt."""

    STARTED = "started"
    NOT_STARTED = "not_started"
    AMBIGUOUS = "ambiguous"
    DRY = "dry"


class SpawnNotStarted(RuntimeError):
    """Raised by a spawner that can prove no session was started.

    The only exception type the orchestrator may treat as a clean failure.
    Everything else is ambiguous, so a spawner must not raise this
    speculatively -- raising it when a session might be up converts a
    quarantine into a duplicate spawn.
    """


def spawn_key_for(event_uuid: str) -> str:
    """Deterministic spawn identity for one watch event.

    Derived from ``event_uuid`` and nothing else -- no clock, no attempt
    counter, no host -- so every retry of the same fire, in any process,
    computes the same key. That is the precondition for reconciling an
    ambiguous spawn by readback.
    """
    return f"{SPAWN_KEY_PREFIX}:{event_uuid}"


def session_label(symbol: str, *, now: datetime) -> str:
    """ROB-1286 설계 2항 label shape: ``opa-watch-<symbol>-<HHMM>``.

    ``HHMM`` is **KST**, per the label convention the operator lane reads.
    r1 formatted whatever zone ``now`` happened to carry, and the flow
    entrypoint defaults to UTC, so a 09:06 KST fire was labelled ``0006``
    (r2 / SHOULD-1). Requires a timezone-aware ``now``: a naive value can
    only be converted by guessing, and guessing is what produced the bug.
    """
    if now.tzinfo is None:
        raise ValueError("session_label requires a timezone-aware 'now'")
    return f"opa-watch-{symbol}-{now.astimezone(KST).strftime('%H%M')}"


@dataclass(frozen=True)
class SpawnRequest:
    """Everything the re-judgement session is told, and nothing more.

    The capability profile is validated on construction, so the boundary is
    enforced here rather than trusted downstream.
    """

    event_uuid: str
    symbol: str
    market: str
    kst_date: str
    label: str
    spawn_key: str = ""
    execution_boundary: str = EXECUTION_BOUNDARY
    capability_profile: CapabilityProfile = PROPOSAL_ONLY_PROFILE

    def __post_init__(self) -> None:
        # frozen dataclass: default the key through object.__setattr__ so
        # callers cannot forget it and cannot vary it.
        if not self.spawn_key:
            object.__setattr__(self, "spawn_key", spawn_key_for(self.event_uuid))
        assert_proposal_only(self.capability_profile)
        if self.execution_boundary != EXECUTION_BOUNDARY:
            raise ValueError(
                f"execution_boundary must be {EXECUTION_BOUNDARY!r}, "
                f"got {self.execution_boundary!r}"
            )


@dataclass(frozen=True)
class SpawnOutcome:
    request: SpawnRequest
    disposition: SpawnDisposition
    detail: str

    @property
    def started(self) -> bool:
        """True only for a proven-started session. Dry runs are False."""
        return self.disposition is SpawnDisposition.STARTED


class SessionSpawner(Protocol):
    @property
    def is_dry(self) -> bool:
        """True only if this spawner structurally cannot start a session.

        Read fail-closed by the orchestrator: an implementation that does
        not answer is assumed live.
        """
        ...

    def spawn(self, request: SpawnRequest) -> SpawnOutcome: ...


@runtime_checkable
class ReconcilableSpawner(Protocol):
    """A spawner that can answer "did the session with this key start?".

    Optional, and checked at runtime. Without it an ambiguous spawn can
    only be quarantined; with it, most ambiguity resolves into a decidable
    STARTED/NOT_STARTED on the same tick.
    """

    def reconcile(self, request: SpawnRequest) -> SpawnDisposition: ...


@dataclass
class DrySessionSpawner:
    """Records what would have been spawned. Starts no session, ever."""

    requests: list[SpawnRequest] = field(default_factory=list)

    @property
    def is_dry(self) -> bool:
        return True

    def spawn(self, request: SpawnRequest) -> SpawnOutcome:
        self.requests.append(request)
        return SpawnOutcome(
            request=request,
            disposition=SpawnDisposition.DRY,
            detail="dry_run",
        )
