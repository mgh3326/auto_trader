"""Dedupe, quota at request and run time, and zero writes on refusal (hk 6409)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.services.market_events.backfill.policy import (
    BackfillRefused,
    producer_argv,
)
from app.services.market_events.backfill.service import run_request, submit_request
from tests.services.market_events.backfill_support import NOW, FakeStore, make_request

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class Runner:
    def __init__(self, result: int | BaseException = 0) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str]) -> int:
        self.calls.append(argv)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


async def refused(coro) -> str:
    with pytest.raises(BackfillRefused) as err:
        await coro
    return err.value.code


async def test_the_same_request_again_adds_no_row() -> None:
    store = FakeStore()
    request = make_request()
    first = await submit_request(store, request)
    again = await submit_request(store, make_request(reason="other words"))
    assert (first.status, again.status) == ("recorded", "duplicate")
    assert first.request_ref == again.request_ref
    assert store.states() == ["requested"]
    assert store.locks == ["dart", "dart"]


async def test_one_request_per_source_per_utc_day() -> None:
    store = FakeStore()
    await submit_request(store, make_request())
    other = make_request(reason_code="verification")
    assert await refused(submit_request(store, other)) == "request_quota_exceeded"
    assert store.states() == ["requested"] and store.pending == []
    # another source has its own quota
    await submit_request(
        store, make_request(source="finnhub", market="us", reason_code="verification")
    )
    store.clock = NOW + timedelta(days=1)
    await submit_request(store, other)
    assert store.states() == ["requested"] * 3


async def test_a_window_that_is_future_by_the_database_clock_writes_nothing() -> None:
    store = FakeStore(NOW - timedelta(days=2))
    assert await refused(submit_request(store, make_request())) == "future_date"
    assert store.rows == [] and store.pending == []


async def test_a_run_appends_a_claim_and_an_outcome_once() -> None:
    store, runner = FakeStore(), Runner(0)
    request = make_request()
    submitted: list[str] = []
    outcome = await run_request(
        store, request, runner, on_submitted=lambda r: submitted.append(r.status)
    )
    assert (outcome.state, outcome.exit_code) == ("done", 0)
    assert submitted == ["recorded"]
    assert runner.calls == [producer_argv(request)]
    assert store.states() == ["requested", "started", "done"]
    assert await refused(run_request(store, request, runner)) == "already_run"
    assert len(runner.calls) == 1
    assert store.states() == ["requested", "started", "done"]


async def test_the_claim_is_committed_before_the_collector_starts() -> None:
    store = FakeStore()
    seen: list[list[str]] = []

    async def runner(argv: list[str]) -> int:
        seen.append(store.states())
        assert store.pending == []
        return 0

    await run_request(store, make_request(), runner)
    assert seen == [["requested", "started"]]


@pytest.mark.parametrize(
    ("result", "exit_code", "code"),
    [
        (2, 2, "exit_nonzero"),
        (TimeoutError(), None, "timeout"),
        (FileNotFoundError(), None, "spawn_failed"),
    ],
)
async def test_a_failed_run_is_recorded_with_a_closed_code(
    result: int | BaseException, exit_code: int | None, code: str
) -> None:
    store = FakeStore()
    outcome = await run_request(store, make_request(), Runner(result))
    assert (outcome.state, outcome.exit_code, outcome.failure_code) == (
        "failed",
        exit_code,
        code,
    )
    last = store.rows[-1]
    assert (last.state, last.exit_code, last.failure_code) == (
        "failed",
        exit_code,
        code,
    )


async def test_an_interrupted_run_stays_unresolved_and_is_not_rerun() -> None:
    store = FakeStore()
    request = make_request()
    with pytest.raises(asyncio.CancelledError):
        await run_request(store, request, Runner(asyncio.CancelledError()))
    assert store.states() == ["requested", "started"]
    runner = Runner()
    assert await refused(run_request(store, request, runner)) == "run_unresolved"
    assert runner.calls == []
    assert store.states() == ["requested", "started"]


async def test_the_run_quota_is_checked_again_at_run_time() -> None:
    store = FakeStore()
    earlier = make_request()
    await submit_request(store, earlier)
    store.clock = NOW + timedelta(days=1)
    later = make_request(reason_code="verification")
    await run_request(store, later, Runner())
    runner = Runner()
    assert await refused(run_request(store, earlier, runner)) == "run_quota_exceeded"
    assert runner.calls == []
    assert store.states() == ["requested", "requested", "started", "done"]


async def test_a_refused_commit_and_run_records_nothing() -> None:
    """The request row and its claim are one decision: no half-written request."""
    store = FakeStore()
    earlier = make_request()
    await submit_request(store, earlier)
    store.clock = NOW + timedelta(days=1)
    await run_request(store, earlier, Runner())
    # today's request quota is free, today's run quota is used
    before = store.states()
    fresh = make_request(reason_code="verification")
    assert await refused(run_request(store, fresh, Runner())) == "run_quota_exceeded"
    assert store.states() == before and store.pending == []


async def test_a_failed_claim_write_leaves_no_half_written_request() -> None:
    class Broken(FakeStore):
        async def insert(self, request, *, state: str, **kwargs) -> None:
            if state == "started":
                raise RuntimeError("write failed")
            await super().insert(request, state=state, **kwargs)

    store, runner = Broken(), Runner()
    with pytest.raises(RuntimeError, match="write failed"):
        await run_request(store, make_request(), runner)
    assert store.rows == [] and store.pending == []
    assert runner.calls == []
