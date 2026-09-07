"""Reconnect reconcile trigger coordinator (fillwire P0).

Kernels are always mocked: this suite makes zero broker calls.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services.reconcile_trigger import (
    KERNELS,
    ReconcileTriggerCoordinator,
    count_backfilled,
    project_kernel_result,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _kernel_result(
    *, dry_run: bool = False, actions: list[str] | None = None, success: bool = True
) -> dict[str, Any]:
    return {
        "success": success,
        "dry_run": dry_run,
        "counts": {"filled": len(actions or [])},
        "reconciled": [
            {"ledger_id": index, "action": action, "verdict": "FILLED"}
            for index, action in enumerate(actions or [])
        ],
        "message": "Reconciled",
    }


def _recording_kernels(
    calls: list[tuple[str, bool]], result: dict[str, Any] | None = None
):
    def _make(market: str):
        async def _kernel(*, dry_run: bool) -> dict[str, Any]:
            calls.append((market, dry_run))
            return result if result is not None else _kernel_result()

        return _kernel

    return {market: _make(market) for market in ("kr", "us", "crypto")}


@pytest.mark.unit
def test_backfilled_counts_only_committed_bookings() -> None:
    booked = _kernel_result(
        actions=["booked", "booked_partial", "noop_pending", "marked_expired"]
    )
    assert count_backfilled(booked, dry_run=False) == 2


@pytest.mark.unit
def test_backfilled_is_zero_for_dry_run_even_with_would_book_rows() -> None:
    planned = _kernel_result(dry_run=True, actions=["would_book", "would_book_filled"])
    assert count_backfilled(planned, dry_run=True) == 0
    # And a dry-run flag from the caller wins even over booked-looking rows.
    assert count_backfilled(_kernel_result(actions=["booked"]), dry_run=True) == 0


@pytest.mark.unit
def test_backfilled_is_zero_when_the_kernel_failed() -> None:
    failed = {"success": False, "error": "boom"}
    assert count_backfilled(failed, dry_run=False) == 0


@pytest.mark.unit
def test_kernel_projection_is_bounded_and_drops_the_row_list() -> None:
    raw = _kernel_result(actions=["booked"] * 5)
    raw["message"] = "x" * 5_000
    raw["reconciled"][0]["symbol"] = "000660"

    projection = project_kernel_result(raw)

    assert projection is not None
    assert "reconciled" not in projection
    assert projection["reconciled_rows"] == 5
    assert len(projection["message"]) <= 300
    assert projection["counts"] == {"filled": 5}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["kr", "us", "crypto"])
async def test_each_market_dispatches_to_its_own_kernel(market: str) -> None:
    calls: list[tuple[str, bool]] = []
    coordinator = ReconcileTriggerCoordinator(
        window_seconds=60.0, clock=_Clock(), kernels=_recording_kernels(calls)
    )

    outcome = await coordinator.trigger(market=market, dry_run=True)

    assert calls == [(market, True)]
    assert outcome.status == "executed"
    assert outcome.deduped is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_is_dry_run() -> None:
    calls: list[tuple[str, bool]] = []
    coordinator = ReconcileTriggerCoordinator(
        window_seconds=60.0, clock=_Clock(), kernels=_recording_kernels(calls)
    )

    await coordinator.trigger(market="kr")

    assert calls == [("kr", True)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_commit_run_reports_booked_rows_as_backfilled() -> None:
    coordinator = ReconcileTriggerCoordinator(
        window_seconds=60.0,
        clock=_Clock(),
        kernels=_recording_kernels(
            [], _kernel_result(actions=["booked", "noop_pending", "booked"])
        ),
    )

    outcome = await coordinator.trigger(market="us", dry_run=False)

    assert outcome.backfilled == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_second_call_inside_the_window_is_deduped() -> None:
    calls: list[tuple[str, bool]] = []
    clock = _Clock()
    coordinator = ReconcileTriggerCoordinator(
        window_seconds=60.0, clock=clock, kernels=_recording_kernels(calls)
    )

    first = await coordinator.trigger(market="kr")
    clock.advance(59.0)
    second = await coordinator.trigger(market="kr")

    assert first.status == "executed"
    assert second.status == "deduped"
    assert second.deduped is True
    assert second.backfilled == 0
    assert calls == [("kr", True)], "the kernel must be entered exactly once"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_window_is_measured_from_the_start_of_the_first_run() -> None:
    calls: list[tuple[str, bool]] = []
    clock = _Clock()
    coordinator = ReconcileTriggerCoordinator(
        window_seconds=60.0, clock=clock, kernels=_recording_kernels(calls)
    )

    await coordinator.trigger(market="kr")
    clock.advance(60.0)
    third = await coordinator.trigger(market="kr")

    assert third.status == "executed"
    assert calls == [("kr", True), ("kr", True)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dedupe_is_per_market() -> None:
    calls: list[tuple[str, bool]] = []
    coordinator = ReconcileTriggerCoordinator(
        window_seconds=60.0, clock=_Clock(), kernels=_recording_kernels(calls)
    )

    await coordinator.trigger(market="kr")
    other = await coordinator.trigger(market="crypto")

    assert other.status == "executed"
    assert calls == [("kr", True), ("crypto", True)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_calls_enter_the_kernel_once() -> None:
    calls: list[tuple[str, bool]] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_kernel(*, dry_run: bool) -> dict[str, Any]:
        calls.append(("kr", dry_run))
        started.set()
        await release.wait()
        return _kernel_result()

    coordinator = ReconcileTriggerCoordinator(
        window_seconds=60.0, clock=_Clock(), kernels={"kr": _slow_kernel}
    )

    first = asyncio.create_task(coordinator.trigger(market="kr"))
    await started.wait()
    second = await coordinator.trigger(market="kr")
    release.set()
    first_outcome = await first

    assert len(calls) == 1
    assert first_outcome.status == "executed"
    assert second.status == "deduped"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kernel_exception_is_reported_as_a_bounded_error() -> None:
    async def _explode(*, dry_run: bool) -> dict[str, Any]:
        raise RuntimeError("dsn=postgres://user:pw@host/db leaked into the message")

    coordinator = ReconcileTriggerCoordinator(
        window_seconds=60.0, clock=_Clock(), kernels={"kr": _explode}
    )

    outcome = await coordinator.trigger(market="kr")

    assert outcome.status == "failed"
    assert outcome.backfilled == 0
    assert outcome.error == "RuntimeError"
    assert "postgres://" not in (outcome.error or "")


@pytest.mark.unit
def test_kernels_map_targets_only_the_existing_reconcile_impls() -> None:
    """No new reconcile logic: the trigger only dispatches to known kernels."""
    assert set(KERNELS) == {"kr", "us", "crypto"}
