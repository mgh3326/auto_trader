"""Order of the writes and of the one collector run (hk 6409).

:func:`submit_request` takes the source lock, applies every rule, writes the new
rows and commits once; any refusal rolls back, so a range or quota violation
writes nothing — including ``--commit --run``, whose request row and ``started``
claim are one transaction. The claim is committed **before** the collector
starts: a run whose outcome is unknown (process killed) stays ``started`` and is
never run again.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.services.market_events.backfill.policy import (
    REQUESTS_PER_SOURCE_PER_UTC_DAY,
    RUNS_PER_SOURCE_PER_UTC_DAY,
    BackfillRefused,
    BackfillRequest,
    check_window,
    producer_argv,
    utc_day_bounds,
)

Runner = Callable[[list[str]], Awaitable[int]]
"""Runs ``python -m <argv>`` and returns its exit code."""


class Store(Protocol):
    async def lock_source(self, source: str) -> datetime: ...
    async def states_for(self, request_hash: str) -> set[str]: ...
    async def count(
        self, *, source: str, state: str, since: datetime, until: datetime
    ) -> int: ...
    async def insert(
        self,
        request: BackfillRequest,
        *,
        state: str,
        created_at: datetime,
        exit_code: int | None = None,
        failure_code: str | None = None,
    ) -> None: ...
    async def now(self) -> datetime: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...


@dataclass(frozen=True)
class SubmitResult:
    #: ``recorded`` (a new request row) or ``duplicate`` (it already existed).
    status: str
    request_ref: str
    #: Rows this request has after the call.
    states: tuple[str, ...]


@dataclass(frozen=True)
class RunResult:
    #: ``done`` or ``failed``.
    state: str
    request_ref: str
    exit_code: int | None
    failure_code: str | None


async def _quota(
    store: Store,
    request: BackfillRequest,
    now: datetime,
    *,
    state: str,
    limit: int,
    code: str,
) -> None:
    since, until = utc_day_bounds(now)
    used = await store.count(
        source=request.source, state=state, since=since, until=until
    )
    if used >= limit:
        raise BackfillRefused(
            code, f"{request.source} already has {used} {state} row(s) this UTC day"
        )


async def submit_request(
    store: Store, request: BackfillRequest, *, claim: bool = False
) -> SubmitResult:
    """Record the request once and, with ``claim``, claim its single run.

    The same request again adds no row. With ``claim`` a request that already
    has a claim or an outcome is refused.
    """
    try:
        now = await store.lock_source(request.source)
        states = await store.states_for(request.request_hash)
        new = "requested" not in states
        if claim and states & {"done", "failed"}:
            raise BackfillRefused(
                "already_run",
                f"{request.request_ref} already has an outcome; file a new request",
            )
        if claim and "started" in states:
            raise BackfillRefused(
                "run_unresolved",
                f"{request.request_ref} was started and has no outcome; not re-run",
            )
        if new or claim:
            check_window(
                source=request.source,
                category=request.category,
                market=request.market,
                from_date=request.from_date,
                to_date=request.to_date,
                now=now,
            )
        if new:
            await _quota(
                store,
                request,
                now,
                state="requested",
                limit=REQUESTS_PER_SOURCE_PER_UTC_DAY,
                code="request_quota_exceeded",
            )
        if claim:
            await _quota(
                store,
                request,
                now,
                state="started",
                limit=RUNS_PER_SOURCE_PER_UTC_DAY,
                code="run_quota_exceeded",
            )
        added: list[str] = []
        if new:
            await store.insert(request, state="requested", created_at=now)
            added.append("requested")
        if claim:
            await store.insert(request, state="started", created_at=now)
            added.append("started")
        if added:
            await store.commit()
        else:
            await store.rollback()
    except BaseException:
        await store.rollback()
        raise
    return SubmitResult(
        "recorded" if new else "duplicate",
        request.request_ref,
        tuple(sorted(states | set(added))),
    )


async def run_request(
    store: Store,
    request: BackfillRequest,
    runner: Runner,
    *,
    on_submitted: Callable[[SubmitResult], None] | None = None,
) -> RunResult:
    """Claim, run the existing collector CLI once, and append its outcome."""
    argv = producer_argv(request)
    submitted = await submit_request(store, request, claim=True)
    if on_submitted is not None:
        on_submitted(submitted)
    exit_code: int | None = None
    failure: str | None = None
    try:
        exit_code = await runner(argv)
        if exit_code != 0:
            failure = "exit_nonzero"
    except TimeoutError:
        exit_code, failure = None, "timeout"
    except OSError:
        exit_code, failure = None, "spawn_failed"
    state = "done" if failure is None else "failed"
    try:
        await store.insert(
            request,
            state=state,
            created_at=await store.now(),
            exit_code=exit_code,
            failure_code=failure,
        )
        await store.commit()
    except BaseException:
        await store.rollback()
        raise
    return RunResult(state, request.request_ref, exit_code, failure)
