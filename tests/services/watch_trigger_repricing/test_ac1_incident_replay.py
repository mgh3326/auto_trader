"""ROB-1286 AC1 — replay the 08-18 Samsung fire through the dry path.

What "dry" means here, precisely:

* **Real**: the trading-session gate (against the actual XKRX calendar --
  2026-08-18 is a genuine KR session, so this replay is not vacuous), the
  intraday window, event filtering, the consumption criterion, claiming,
  per-symbol concurrency, the round cap, and overflow reporting.
* **Stand-in**: two things only -- the event rows are constructed in the
  test instead of read from ``review.investment_watch_events`` (no DB
  write of any kind, including no consumption marking), and the spawner is
  :class:`DrySessionSpawner`, which records a request and starts nothing.

So: no session is started, no proposal is created, no broker is touched,
and no row is written anywhere.

The incident: 005930 watch rungs at 276,000 and 282,000 both fired at
09:05 KST with ``outcome='review_required'`` and ``delivery_status=
'delivered'``. Nothing consumed them, the 0905 rep session had already
started, the 11:30 consumption rolled back, and the sell window up to a
288,000 high closed with zero sell proposals.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.watch_trigger_repricing.claims import InMemoryClaimStore
from app.services.watch_trigger_repricing.consumption import (
    ConsumptionState,
    may_consume,
)
from app.services.watch_trigger_repricing.orchestrator import (
    run_gated_tick,
    run_repricing_tick,
)
from app.services.watch_trigger_repricing.spawn import (
    EXECUTION_BOUNDARY,
    DrySessionSpawner,
    session_label,
)

from .conftest import INCIDENT_FIRE, INCIDENT_TICK, make_event

pytestmark = pytest.mark.unit

# The two rungs, exactly as the scanner recorded them.
RUNG_1 = make_event(event_uuid="evt-005930-276000", symbol="005930")
RUNG_2 = make_event(event_uuid="evt-005930-282000", symbol="005930")


def test_fire_is_picked_up_on_the_next_tick_not_the_next_rep_cycle() -> None:
    """The headline: 09:05 fire -> 09:06 spawn request."""
    store = InMemoryClaimStore()
    spawner = DrySessionSpawner()

    result = run_repricing_tick(
        [RUNG_1, RUNG_2], store=store, now=INCIDENT_TICK, spawner=spawner
    )

    assert result.gate.should_run is True
    assert len(spawner.requests) == 1
    request = spawner.requests[0]
    assert request.symbol == "005930"
    assert request.kst_date == "2026-08-18"
    assert request.label == "opa-watch-005930-0906"
    # The whole point: acted on within a minute of the fire, not at 11:30.
    assert INCIDENT_TICK - INCIDENT_FIRE == dt.timedelta(minutes=1)


def test_dry_path_starts_no_session_and_creates_no_proposal() -> None:
    store = InMemoryClaimStore()
    spawner = DrySessionSpawner()

    result = run_repricing_tick(
        [RUNG_1, RUNG_2], store=store, now=INCIDENT_TICK, spawner=spawner
    )

    assert all(outcome.started is False for outcome in result.spawned)
    assert all(outcome.detail == "dry_run" for outcome in result.spawned)
    # The request carries a boundary, not an execution capability.
    assert all(r.execution_boundary == EXECUTION_BOUNDARY for r in spawner.requests)
    assert not hasattr(spawner.requests[0], "approval_token")


def test_the_second_rung_is_deferred_not_dropped() -> None:
    """One symbol, one in-flight session -- but rung 2 must stay visible."""
    store = InMemoryClaimStore()

    result = run_repricing_tick([RUNG_1, RUNG_2], store=store, now=INCIDENT_TICK)

    deferred = [s for s in result.skipped if s.event_uuid == RUNG_2.event_uuid]
    assert deferred, "rung 2 vanished from the tick report"
    assert deferred[0].reason == "symbol_already_in_flight"
    # And it is genuinely still available for a later consumer.
    assert (
        store.state_for(RUNG_2.event_uuid, now=INCIDENT_TICK)
        is ConsumptionState.UNCLAIMED
    )


def test_the_race_that_caused_the_incident_does_not_recur() -> None:
    """The 0905 rep session starting concurrently must not orphan the fire.

    In the incident the fire fell between two consumers and neither took
    it. Here, whichever consumer arrives first takes it and the other is
    told; the fire is never left owned by nobody.
    """
    store = InMemoryClaimStore()

    # The rep session (B안) starts at 09:05, the same minute as the fire.
    rep_claim = store.try_claim(
        event_uuid=RUNG_1.event_uuid,
        symbol="005930",
        claimed_by="kr-open-trade-0905",
        now=INCIDENT_FIRE,
    )
    assert rep_claim is not None

    # A안's tick a minute later sees it owned and stands down -- explicitly.
    result = run_repricing_tick([RUNG_1], store=store, now=INCIDENT_TICK)
    assert result.spawned == ()
    assert [s.reason for s in result.skipped] == ["already_consumed"]

    # And the reverse ordering: A안 first, B안 sees it is taken. r2: the
    # tick finalises its own spawn, so what B안 sees is the terminal
    # CONSUMED rather than a lease that would lapse in 30 minutes.
    fresh = InMemoryClaimStore()
    run_repricing_tick([RUNG_1], store=fresh, now=INCIDENT_TICK)
    assert (
        fresh.state_for(RUNG_1.event_uuid, now=INCIDENT_TICK)
        is ConsumptionState.CONSUMED
    )
    assert may_consume(fresh.state_for(RUNG_1.event_uuid, now=INCIDENT_TICK)) is False


def test_no_consumer_means_the_fire_is_still_there() -> None:
    """The 11:30 rollback shape: an unconsumed fire must stay claimable."""
    store = InMemoryClaimStore()
    late = dt.datetime(2026, 8, 18, 11, 30, tzinfo=INCIDENT_TICK.tzinfo)

    assert store.state_for(RUNG_1.event_uuid, now=late) is ConsumptionState.UNCLAIMED
    result = run_repricing_tick([RUNG_1], store=store, now=late)
    assert len(result.spawned) == 1


def test_flow_entrypoint_is_off_by_default() -> None:
    """AC: no scheduling, no arming. The default answer is 'disabled'."""
    out = run_gated_tick(events=[RUNG_1, RUNG_2])

    assert out["status"] == "disabled"
    assert out["spawned"] == []


@pytest.mark.usefixtures("enabled")
def test_flow_entrypoint_reports_the_full_tick_when_enabled() -> None:
    store = InMemoryClaimStore()
    spawner = DrySessionSpawner()

    out = run_gated_tick(
        events=[RUNG_1, RUNG_2],
        store=store,
        spawner=spawner,
        now=INCIDENT_TICK,
    )

    assert out["status"] == "ok"
    assert out["kstDate"] == "2026-08-18"
    assert [s["symbol"] for s in out["spawned"]] == ["005930"]
    assert out["spawned"][0]["started"] is False
    assert out["spawned"][0]["executionBoundary"] == "order_proposal_create"
    # Everything not spawned is still named in the report.
    reported = {s["eventUuid"] for s in out["spawned"]} | {
        s["eventUuid"] for s in out["skipped"] + out["overflow"]
    }
    assert reported == {RUNG_1.event_uuid, RUNG_2.event_uuid}


# ---------------------------------------------------------------------------
# r2 / SHOULD-1 — the label is KST, whatever zone the tick's clock carries
# ---------------------------------------------------------------------------
def test_label_is_kst_even_when_now_is_utc() -> None:
    """The flow entrypoint defaults to a UTC clock.

    r1 formatted ``now`` in whatever zone it arrived in, so the 09:06 KST
    fire above would have been labelled ``opa-watch-005930-0006`` in a real
    run -- the tests only passed because they handed it a KST datetime.
    """
    utc_equivalent = INCIDENT_TICK.astimezone(dt.UTC)

    assert utc_equivalent.strftime("%H%M") == "0006"  # the r1 bug, in one line
    assert session_label("005930", now=utc_equivalent) == "opa-watch-005930-0906"


def test_label_is_identical_across_equivalent_clocks() -> None:
    """Same instant, three zones, one label."""
    instants = [
        INCIDENT_TICK,
        INCIDENT_TICK.astimezone(dt.UTC),
        INCIDENT_TICK.astimezone(dt.timezone(dt.timedelta(hours=-5))),
    ]

    labels = {session_label("005930", now=instant) for instant in instants}
    assert labels == {"opa-watch-005930-0906"}


def test_label_refuses_a_naive_clock() -> None:
    """A naive datetime can only be converted by guessing, and guessing is
    what produced the bug."""
    with pytest.raises(ValueError, match="timezone-aware"):
        session_label("005930", now=dt.datetime(2026, 8, 18, 9, 6))


def test_the_tick_report_label_is_kst_from_a_utc_clock() -> None:
    """End to end: a UTC-clocked tick still emits a KST label."""
    spawner = DrySessionSpawner()
    run_repricing_tick(
        [RUNG_1],
        store=InMemoryClaimStore(),
        now=INCIDENT_TICK.astimezone(dt.UTC),
        spawner=spawner,
    )

    assert spawner.requests[0].label == "opa-watch-005930-0906"
