"""ROB-1286 — the session spawner port.

The only implementation this PR ships is :class:`DrySessionSpawner`, which
records the spawn request and starts nothing. That is what makes AC1's
"dry path" dry: the polling, gating, dedup, claiming and cap logic all run
for real, and the single step that would start a session is a recorder.

A live spawner is out of scope here and is gated twice over: the flow is
default-off, and no live implementation exists in the repo to select.

Execution boundary (ROB-1286 불변)
---------------------------------
A spawned session's boundary is ``order_proposal_create``. The request
built here carries no broker credential, no account mutation surface and
no approval token; it names a symbol, the fire that triggered it, and the
existing ``kr-open-trade`` sell adapter / pre-execution checks to apply.
Nothing in this module can approve, submit or cancel an order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

__all__ = [
    "EXECUTION_BOUNDARY",
    "DrySessionSpawner",
    "SessionSpawner",
    "SpawnOutcome",
    "SpawnRequest",
    "session_label",
]

# Named here so a test can assert it, and so widening it is a visible diff.
EXECUTION_BOUNDARY = "order_proposal_create"


def session_label(symbol: str, *, now: datetime) -> str:
    """ROB-1286 설계 2항 label shape: ``opa-watch-<symbol>-<HHMM>``."""
    return f"opa-watch-{symbol}-{now.strftime('%H%M')}"


@dataclass(frozen=True)
class SpawnRequest:
    """Everything the re-judgement session is told, and nothing more."""

    event_uuid: str
    symbol: str
    market: str
    kst_date: str
    label: str
    execution_boundary: str = EXECUTION_BOUNDARY


@dataclass(frozen=True)
class SpawnOutcome:
    request: SpawnRequest
    started: bool
    detail: str


class SessionSpawner(Protocol):
    def spawn(self, request: SpawnRequest) -> SpawnOutcome: ...


@dataclass
class DrySessionSpawner:
    """Records what would have been spawned. Starts no session, ever."""

    requests: list[SpawnRequest] = field(default_factory=list)

    def spawn(self, request: SpawnRequest) -> SpawnOutcome:
        self.requests.append(request)
        return SpawnOutcome(request=request, started=False, detail="dry_run")
