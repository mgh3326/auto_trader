"""ROB-1286 §5 / AC3 — holiday + intraday-window gate.

Records the indeterminate-calendar decision as an executable fact: this
flow does not run on a session it cannot confirm is open.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.watch_trigger_repricing import gate as gate_module
from app.services.watch_trigger_repricing.claims import InMemoryClaimStore
from app.services.watch_trigger_repricing.gate import KST, evaluate_gate
from app.services.watch_trigger_repricing.orchestrator import run_repricing_tick

from .conftest import INCIDENT_TICK, make_event

pytestmark = pytest.mark.unit


def _force_status(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    monkeypatch.setattr(
        gate_module, "trading_session_status", lambda market, day: status
    )


# ---------------------------------------------------------------------------
# Trading-day gate
# ---------------------------------------------------------------------------
def test_open_session_inside_window_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_status(monkeypatch, "open")

    decision = evaluate_gate(now=INCIDENT_TICK)

    assert decision.should_run is True
    assert decision.reason == "ok"
    assert decision.kst_date == "2026-08-18"


def test_holiday_does_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_status(monkeypatch, "closed")

    decision = evaluate_gate(now=INCIDENT_TICK)

    assert decision.should_run is False
    assert decision.reason == "market_closed"


def test_indeterminate_calendar_does_not_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5 decision: unknown is treated as not-open, not as open.

    Cost of the choice: on an unclassifiable date this flow adds no
    latency reduction, and B안 (the rep session's end-of-session re-check)
    remains the net. Cost of the opposite choice: proposals manufactured
    on a day we cannot confirm is a session, which can reach the §40/51차
    auto-approve lane without a human click.
    """
    _force_status(monkeypatch, "unknown")

    decision = evaluate_gate(now=INCIDENT_TICK)

    assert decision.should_run is False
    assert decision.reason == "session_status_indeterminate"
    assert decision.session_status == "unknown"


def test_holiday_and_indeterminate_are_distinguishable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both skip, but an XKRX outage must not be filed as 'holiday'."""
    _force_status(monkeypatch, "closed")
    closed = evaluate_gate(now=INCIDENT_TICK)
    _force_status(monkeypatch, "unknown")
    unknown = evaluate_gate(now=INCIDENT_TICK)

    assert closed.reason != unknown.reason


@pytest.mark.asyncio
async def test_holiday_blocks_spawning_end_to_end(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryClaimStore
) -> None:
    _force_status(monkeypatch, "closed")

    result = await run_repricing_tick(
        [make_event(event_uuid="evt-1")], store=store, now=INCIDENT_TICK
    )

    assert result.spawned == ()
    assert result.gate.reason == "market_closed"
    # A gated-off tick must not claim the fire either.
    state = await store.state_for("evt-1", now=INCIDENT_TICK)
    assert state.value == "unclaimed"


# ---------------------------------------------------------------------------
# Intraday window
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("clock", "should_run"),
    [
        (dt.time(8, 59), False),
        (dt.time(9, 0), True),
        (dt.time(12, 0), True),
        (dt.time(15, 29), True),
        (dt.time(15, 30), False),
        (dt.time(16, 0), False),
    ],
)
def test_intraday_window_bounds(
    monkeypatch: pytest.MonkeyPatch, clock: dt.time, should_run: bool
) -> None:
    _force_status(monkeypatch, "open")
    now = dt.datetime.combine(dt.date(2026, 8, 18), clock, tzinfo=KST)

    assert evaluate_gate(now=now).should_run is should_run


def test_window_is_evaluated_in_kst_not_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """01:00 UTC is 10:00 KST -- inside the session, not before it."""
    _force_status(monkeypatch, "open")
    now = dt.datetime(2026, 8, 18, 1, 0, tzinfo=dt.UTC)

    decision = evaluate_gate(now=now)

    assert decision.should_run is True
    assert decision.kst_date == "2026-08-18"


def test_naive_now_is_rejected() -> None:
    """Guessing the zone would silently shift the whole session window."""
    with pytest.raises(ValueError):
        evaluate_gate(now=dt.datetime(2026, 8, 18, 9, 5))


# ---------------------------------------------------------------------------
# Calendar provenance: no new holiday judgement was invented
# ---------------------------------------------------------------------------
def test_gate_uses_the_existing_offline_xkrx_calendar() -> None:
    from app.services.market_events import session_calendar

    assert gate_module.trading_session_status is session_calendar.trading_session_status


def test_real_calendar_classifies_a_known_kr_holiday() -> None:
    """2026-01-01 (신정) is closed on XKRX; no static list of our own."""
    new_year = dt.datetime(2026, 1, 1, 10, 0, tzinfo=KST)

    assert evaluate_gate(now=new_year).should_run is False


def test_real_calendar_classifies_a_weekend() -> None:
    saturday = dt.datetime(2026, 8, 22, 10, 0, tzinfo=KST)

    assert evaluate_gate(now=saturday).should_run is False
