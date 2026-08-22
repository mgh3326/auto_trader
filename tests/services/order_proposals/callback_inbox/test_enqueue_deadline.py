"""R33 — bounded best-effort enqueue even when cancellation is resisted.

These deliberately exercise the ingress-private task lifecycle through its
observable contracts.  The registry has no test reset/getter: every stubborn
producer is released in ``finally`` so its real done callback owns cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid

import pytest

pytestmark = pytest.mark.unit


async def _finish_task(task: asyncio.Task[object], release: asyncio.Event) -> None:
    """Release a fake after an assertion failure without cancelling its parent."""
    release.set()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)
    # Let the producer task's done callback consume its terminal outcome.
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_exception", [None, RuntimeError("late enqueue failure")])
async def test_resistant_injected_producer_returns_at_the_ack_deadline_and_consumes_late_outcome(
    late_exception: Exception | None,
) -> None:
    """A producer may resist cancellation, but it cannot extend the ACK."""
    from app.services.order_proposals.callback_inbox.ingress import _kick

    started = asyncio.Event()
    cancel_requested = asyncio.Event()
    release = asyncio.Event()
    started_at = 0.0
    loop_errors: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def _capture_loop_error(
        _loop: asyncio.AbstractEventLoop, context: dict[str, object]
    ) -> None:
        loop_errors.append(context)

    loop.set_exception_handler(_capture_loop_error)

    async def _resistant(_job_id: uuid.UUID) -> None:
        nonlocal started_at
        started_at = time.monotonic()
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancel_requested.set()
        if late_exception is not None:
            raise late_exception

    request = asyncio.create_task(
        _kick(uuid.uuid4(), enqueue_fn=_resistant, timeout_seconds=0.02)
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=0.25)
        result = await asyncio.wait_for(asyncio.shield(request), timeout=0.15)
        elapsed = time.monotonic() - started_at

        assert result is False
        assert cancel_requested.is_set()
        assert elapsed < 0.15, elapsed
    finally:
        await _finish_task(request, release)
        loop.set_exception_handler(previous_handler)

    assert not [
        context
        for context in loop_errors
        if "Task exception was never retrieved" in str(context.get("message", ""))
    ], loop_errors


@pytest.mark.asyncio
async def test_resistant_producers_are_capped_and_hold_slots_until_they_finish() -> (
    None
):
    """Timeout never discards a still-running producer from the fixed cap."""
    from app.services.order_proposals.callback_inbox.ingress import _kick

    release = asyncio.Event()
    started_sixteen = asyncio.Event()
    all_finished = asyncio.Event()
    calls = 0
    active = 0
    peak_active = 0

    async def _resistant(_job_id: uuid.UUID) -> None:
        nonlocal active, calls, peak_active
        calls += 1
        active += 1
        peak_active = max(peak_active, active)
        if calls == 16:
            started_sixteen.set()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
        finally:
            active -= 1
            if active == 0:
                all_finished.set()

    kick_tasks = [
        asyncio.create_task(
            _kick(uuid.uuid4(), enqueue_fn=_resistant, timeout_seconds=0.02)
        )
        for _ in range(17)
    ]
    try:
        await asyncio.wait_for(started_sixteen.wait(), timeout=0.5)
        # Give the overflow caller a full loop turn after the first 16 started.
        await asyncio.sleep(0)

        assert calls == 16
        assert active == 16
        assert peak_active == 16

        results = await asyncio.wait_for(asyncio.gather(*kick_tasks), timeout=0.25)
        assert results == [False] * 17

        overflow_calls = 0

        async def _overflow_must_not_run(_job_id: uuid.UUID) -> None:
            nonlocal overflow_calls
            overflow_calls += 1

        assert (
            await _kick(
                uuid.uuid4(),
                enqueue_fn=_overflow_must_not_run,
                timeout_seconds=0.02,
            )
            is False
        )
        assert overflow_calls == 0

        release.set()
        await asyncio.wait_for(all_finished.wait(), timeout=0.5)
        # Done callbacks, rather than timeout handling, free the cap slots.
        await asyncio.sleep(0)

        assert await _kick(
            uuid.uuid4(), enqueue_fn=_overflow_must_not_run, timeout_seconds=0.02
        )
        assert overflow_calls == 1
    finally:
        release.set()
        await asyncio.wait_for(
            asyncio.gather(*kick_tasks, return_exceptions=True), timeout=1.0
        )


@pytest.mark.asyncio
async def test_outer_cancellation_is_re_raised_while_the_resistant_child_keeps_its_cap_slot() -> (
    None
):
    """Cancelling a request asks its child to stop, but never loses accounting."""
    from app.services.order_proposals.callback_inbox.ingress import _kick

    release = asyncio.Event()
    parent_started = asyncio.Event()
    child_cancel_requested = asyncio.Event()
    all_started = asyncio.Event()
    active = 0

    async def _resistant(_job_id: uuid.UUID) -> None:
        nonlocal active
        active += 1
        if active == 16:
            all_started.set()
        parent_started.set()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    child_cancel_requested.set()
        finally:
            active -= 1

    parent = asyncio.create_task(
        _kick(uuid.uuid4(), enqueue_fn=_resistant, timeout_seconds=1.0)
    )
    sibling_tasks: list[asyncio.Task[bool]] = []
    try:
        await asyncio.wait_for(parent_started.wait(), timeout=0.25)
        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(parent), timeout=0.15)
        await asyncio.wait_for(child_cancel_requested.wait(), timeout=0.25)

        sibling_tasks = [
            asyncio.create_task(
                _kick(uuid.uuid4(), enqueue_fn=_resistant, timeout_seconds=0.02)
            )
            for _ in range(15)
        ]
        await asyncio.wait_for(all_started.wait(), timeout=0.5)
        assert (
            await asyncio.wait_for(asyncio.gather(*sibling_tasks), timeout=0.25)
            == [False] * 15
        )

        overflow_calls = 0

        async def _overflow_must_not_run(_job_id: uuid.UUID) -> None:
            nonlocal overflow_calls
            overflow_calls += 1

        assert (
            await _kick(
                uuid.uuid4(),
                enqueue_fn=_overflow_must_not_run,
                timeout_seconds=0.02,
            )
            is False
        )
        assert overflow_calls == 0
    finally:
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(parent, timeout=1.0)
        await asyncio.wait_for(
            asyncio.gather(*sibling_tasks, return_exceptions=True), timeout=1.0
        )


@pytest.mark.asyncio
async def test_fast_error_cooperative_timeout_and_runtime_tamper_are_fail_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Normal outcomes stay unchanged; invalid runtime values never invoke a producer."""
    from app.services.order_proposals.callback_inbox import ingress as ingress_module
    from app.services.order_proposals.callback_inbox.ingress import _kick

    async def _success(_job_id: uuid.UUID) -> None:
        return None

    async def _boom(_job_id: uuid.UUID) -> None:
        raise ConnectionError("broker unavailable")

    assert await _kick(uuid.uuid4(), enqueue_fn=_success, timeout_seconds=0.1)
    assert not await _kick(uuid.uuid4(), enqueue_fn=_boom, timeout_seconds=0.1)

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _cooperative(_job_id: uuid.UUID) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    cooperative = asyncio.create_task(
        _kick(uuid.uuid4(), enqueue_fn=_cooperative, timeout_seconds=0.02)
    )
    await asyncio.wait_for(started.wait(), timeout=0.25)
    assert await asyncio.wait_for(cooperative, timeout=0.15) is False
    assert cancelled.is_set()

    invoked = 0

    async def _must_not_run(_job_id: uuid.UUID) -> None:
        nonlocal invoked
        invoked += 1
        raise AssertionError("invalid timeout invoked the producer")

    invalid_timeouts = (
        float("nan"),
        float("inf"),
        float("-inf"),
        0.0,
        -1.0,
        10.01,
        "r33-invalid-timeout",
    )
    caplog.set_level(logging.ERROR, logger=ingress_module.__name__)
    caplog.clear()
    for invalid_timeout in invalid_timeouts:
        assert (
            await _kick(
                uuid.uuid4(),
                enqueue_fn=_must_not_run,
                timeout_seconds=invalid_timeout,
            )
            is False
        )
    assert invoked == 0

    records = [
        record for record in caplog.records if record.name == ingress_module.__name__
    ]
    assert [record.getMessage() for record in records] == [
        "order_proposals.telegram.callback_job_enqueue_timeout_invalid"
    ] * len(invalid_timeouts)
    assert [getattr(record, "enqueue_timeout_error", None) for record in records] == [
        "invalid_timeout"
    ] * len(invalid_timeouts)
    for raw_value in (
        "nan",
        "inf",
        "-inf",
        "0.0",
        "-1.0",
        "10.01",
        "r33-invalid-timeout",
    ):
        assert raw_value not in caplog.text


@pytest.mark.asyncio
async def test_shutdown_reap_is_bounded_and_does_not_discard_a_resistant_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graceful shutdown asks for cancellation before broker teardown, not a lie."""
    from app.services.order_proposals.callback_inbox import ingress as ingress_module
    from app.services.order_proposals.callback_inbox.ingress import (
        _kick,
        shutdown_callback_enqueue_tasks,
    )

    monkeypatch.setattr(ingress_module, "_ENQUEUE_SHUTDOWN_REAP_TIMEOUT_SECONDS", 0.02)
    release = asyncio.Event()
    started = asyncio.Event()
    cancellation_count = 0

    async def _resistant(_job_id: uuid.UUID) -> None:
        nonlocal cancellation_count
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellation_count += 1

    request = asyncio.create_task(
        _kick(uuid.uuid4(), enqueue_fn=_resistant, timeout_seconds=0.02)
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=0.25)
        assert await asyncio.wait_for(asyncio.shield(request), timeout=0.15) is False

        started_reap = time.monotonic()
        await shutdown_callback_enqueue_tasks()
        assert time.monotonic() - started_reap < 0.15
        # One cancellation came from the deadline; shutdown makes another request.
        assert cancellation_count >= 2
    finally:
        await _finish_task(request, release)


@pytest.mark.asyncio
async def test_shutdown_reap_keeps_all_resistant_producers_in_the_cap_until_they_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown may request cancellation, never clear still-live cap slots."""
    from app.services.order_proposals.callback_inbox import ingress as ingress_module
    from app.services.order_proposals.callback_inbox.ingress import _kick

    monkeypatch.setattr(
        ingress_module, "_ENQUEUE_SHUTDOWN_REAP_TIMEOUT_SECONDS", 0.02, raising=False
    )
    release = asyncio.Event()
    started_sixteen = asyncio.Event()
    all_finished = asyncio.Event()
    active = 0
    calls = 0

    async def _resistant(_job_id: uuid.UUID) -> None:
        nonlocal active, calls
        calls += 1
        active += 1
        if calls == 16:
            started_sixteen.set()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
        finally:
            active -= 1
            if active == 0:
                all_finished.set()

    kick_tasks = [
        asyncio.create_task(
            _kick(uuid.uuid4(), enqueue_fn=_resistant, timeout_seconds=0.02)
        )
        for _ in range(16)
    ]
    gather_kicks = asyncio.gather(*kick_tasks)
    try:
        await asyncio.wait_for(started_sixteen.wait(), timeout=0.5)
        assert (
            await asyncio.wait_for(asyncio.shield(gather_kicks), timeout=0.25)
            == [False] * 16
        )
        assert active == 16

        await ingress_module.shutdown_callback_enqueue_tasks()
        assert active == 16

        overflow_calls = 0

        async def _overflow_must_not_run(_job_id: uuid.UUID) -> None:
            nonlocal overflow_calls
            overflow_calls += 1

        assert (
            await _kick(
                uuid.uuid4(),
                enqueue_fn=_overflow_must_not_run,
                timeout_seconds=0.02,
            )
            is False
        )
        assert overflow_calls == 0

        release.set()
        await asyncio.wait_for(all_finished.wait(), timeout=0.5)
        await asyncio.sleep(0)

        assert await _kick(
            uuid.uuid4(),
            enqueue_fn=_overflow_must_not_run,
            timeout_seconds=0.02,
        )
        assert overflow_calls == 1
    finally:
        release.set()
        await asyncio.wait_for(
            asyncio.gather(*kick_tasks, return_exceptions=True), timeout=1.0
        )
