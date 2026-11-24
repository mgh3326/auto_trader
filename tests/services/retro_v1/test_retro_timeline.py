"""retro_timeline: closed per-symbol state over the evidence sources (hk 6171)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from app.services.retro_v1 import RetroReadError, read_retro_timeline
from app.services.retro_v1.envelope import make_ref
from app.services.retro_v1.links import decision_ref
from app.services.retro_v1.retro_timeline import Event, assemble
from tests.services.retro_v1.support import (
    FILL_AT,
    FakeSession,
    forecast,
    kr_day_session,
    ns,
    operational_error,
    pick,
    proposal,
    validate,
)

pytestmark = pytest.mark.unit

DAY = date(2026, 1, 5)
T0 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def ev(
    minutes: int, source: str, kind: str, reason: str | None = None, ref: str = "r"
) -> Event:
    return Event(at(minutes), source, kind, reason, f"{source}-{ref}{minutes}")


def view(events: list[Event], unavailable: frozenset[str] = frozenset()):
    timeline = assemble(events, unavailable)
    return timeline.state, timeline.reason, [e.source for e in timeline.events]


# --------------------------------------------------------------------------
# acceptance 1: one pure fixture per state, plus the source_unavailable cases
# --------------------------------------------------------------------------
def test_not_observed_is_a_requested_symbol_in_no_source() -> None:
    assert view([]) == ("not_observed", "no_record_in_sources", [])


def test_observed_only_is_a_first_capture_and_nothing_else() -> None:
    events = [ev(20, "observation_log", "up")]
    assert view(events) == (
        "observed_only",
        "observation_log_only",
        ["observation_log"],
    )


def test_candidate_not_admitted_carries_the_recorded_reason_of_the_latest_pick() -> (
    None
):
    events = [
        ev(5, "observation_log", "priceTop"),
        ev(30, "pick_log", "dropped_preselection", "preselection_cap"),
        ev(10, "pick_log", "not_admitted", "family_slot_taken"),
    ]
    assert view(events) == (
        "candidate_not_admitted",
        "preselection_cap",
        ["observation_log", "pick_log", "pick_log"],
    )


def test_admitted_no_record_never_infers_a_hold() -> None:
    events = [
        ev(5, "observation_log", "up"),
        ev(10, "pick_log", "admitted", "round_robin"),
    ]
    assert view(events) == (
        "admitted_no_record",
        "no_decision_record",
        ["observation_log", "pick_log"],
    )


def test_admitted_held_needs_an_admitted_pick_and_a_declined_record() -> None:
    declined = [
        ev(10, "pick_log", "admitted", "round_robin"),
        ev(20, "decision", "declined"),
    ]
    assert view(declined) == (
        "admitted_held",
        "declined_decision",
        ["pick_log", "decision"],
    )
    forecast_only = [
        ev(10, "pick_log", "admitted", "round_robin"),
        ev(20, "decision", "deferred_no_action"),
    ]
    assert view(forecast_only)[:2] == (
        "admitted_held",
        "deferred_no_action_forecast",
    )


def test_a_declined_record_with_no_pick_row_is_reported_as_such() -> None:
    state, reason, _ = view([ev(20, "decision", "declined")])
    assert (state, reason) == ("admitted_held", "declined_without_pick_record")
    # a not-admitted pick is the stronger statement about the candidate
    state, reason, _ = view(
        [ev(10, "pick_log", "not_admitted", "x"), ev(20, "decision", "declined")]
    )
    assert state == "candidate_not_admitted"


def test_proposed_unfilled_names_the_furthest_stage_reached() -> None:
    working = [
        ev(10, "pick_log", "admitted"),
        ev(20, "proposal", "filled"),
        ev(30, "order", "working"),
    ]
    assert view(working) == (
        "proposed_unfilled",
        "order_working",
        ["pick_log", "proposal", "order"],
    )
    waiting = [
        ev(20, "proposal", "proposed", "awaiting_human_approval"),
    ]
    assert view(waiting)[:2] == ("proposed_unfilled", "awaiting_human_approval")
    assert view([ev(20, "proposal", "expired")])[:2] == (
        "proposed_unfilled",
        "proposal_expired",
    )
    assert view([ev(20, "decision", "direct_order")])[:2] == (
        "proposed_unfilled",
        "decision_without_order",
    )


def test_proposed_filled_uses_the_chains_order_and_fill_evidence() -> None:
    events = [
        ev(40, "fill", "buy_filled"),
        ev(40, "order", "filled"),
        ev(20, "proposal", "filled"),
        ev(10, "pick_log", "admitted"),
        ev(15, "decision", "proposal"),
    ]
    assert view(events) == (
        "proposed_filled",
        "filled",
        ["pick_log", "decision", "proposal", "order", "fill"],
    )
    partial = [ev(30, "order", "partially_filled"), ev(31, "fill", "buy_filled")]
    assert view(partial)[:2] == ("proposed_filled", "partially_filled")
    assert view([ev(31, "fill", "buy_filled")])[:2] == (
        "proposed_filled",
        "fill_event_only",
    )


def test_precedence_runs_proposed_over_held_over_not_admitted_over_observed() -> None:
    mixed = [
        ev(1, "observation_log", "up"),
        ev(2, "pick_log", "not_admitted", "a"),
        ev(3, "pick_log", "admitted", "b"),
        ev(4, "decision", "declined"),
    ]
    assert view(mixed)[0] == "admitted_held"
    assert view([*mixed, ev(5, "proposal", "proposed")])[0] == "proposed_unfilled"
    assert view(mixed[:3])[0] == "admitted_no_record"
    assert view(mixed[:2])[0] == "candidate_not_admitted"
    assert view(mixed[:1])[0] == "observed_only"


def test_pick_rows_without_a_recorded_admission_are_not_an_admission() -> None:
    assert view([ev(10, "pick_log", "unrecorded")])[:2] == (
        "observed_only",
        "pick_admission_unrecorded",
    )


def test_events_are_ordered_by_time_then_source_then_ref() -> None:
    same = [
        Event(at(5), "fill", "buy_filled", None, "fill-b"),
        Event(at(5), "order", "filled", None, "order-z"),
        Event(at(5), "order", "filled", None, "order-a"),
        Event(at(1), "pick_log", "admitted", None, "candidate-1"),
    ]
    refs = [e.ref for e in assemble(same).events]
    assert refs == ["candidate-1", "order-a", "order-z", "fill-b"]


@pytest.mark.parametrize(
    ("events", "gone"),
    [
        # a hidden proposal/order could outrank everything below proposed_*
        ([ev(10, "pick_log", "admitted"), ev(20, "decision", "declined")], "orders"),
        ([ev(10, "pick_log", "admitted")], "orders"),
        ([ev(10, "pick_log", "not_admitted", "x")], "orders"),
        ([ev(20, "proposal", "proposed")], "orders"),  # fill state unknown
        # a hidden decision turns admitted_no_record into held (or proposed)
        ([ev(10, "pick_log", "admitted")], "decisions"),
        ([ev(1, "observation_log", "up")], "decisions"),
        # a hidden pick can turn anything below it into admitted/not-admitted
        ([ev(1, "observation_log", "up")], "pick_log"),
        ([ev(20, "decision", "declined")], "pick_log"),
        ([], "pick_log"),
        # absence is only proven with the observation log read
        ([], "observation_log"),
    ],
)
def test_a_failed_group_makes_the_state_null_never_a_weaker_state(
    events: list[Event], gone: str
) -> None:
    timeline = assemble(events, frozenset({gone}))
    assert (timeline.state, timeline.reason) == (None, "source_unavailable")
    # the events that were read are still reported, in order
    assert [e.ref for e in timeline.events] == [
        e.ref for e in sorted(events, key=Event.sort_key)
    ]


def test_positive_evidence_survives_an_unrelated_failed_group() -> None:
    held = [ev(10, "pick_log", "admitted"), ev(20, "decision", "declined")]
    assert view(held, frozenset({"observation_log"}))[0] == "admitted_held"
    assert view(held, frozenset({"pick_log"}))[0] == "admitted_held"
    filled = [ev(20, "proposal", "filled"), ev(30, "fill", "buy_filled")]
    assert view(filled, frozenset({"orders", "pick_log", "decisions"}))[0] == (
        "proposed_filled"
    )
    observed = [ev(1, "observation_log", "up")]
    assert view(observed, frozenset({"observation_log"}))[0] == "observed_only"


# --------------------------------------------------------------------------
# acceptance 2/3: the reader over a fake session
# --------------------------------------------------------------------------
OBSERVED_AT = datetime(2026, 1, 5, 0, 20, tzinfo=UTC)  # 09:20 KST
PICK_AT = datetime(2026, 1, 5, 0, 30, tzinfo=UTC)
DECLINED_AT = datetime(2026, 1, 5, 0, 40, tzinfo=UTC)


def observation(symbol: str, order_type: str = "up") -> Any:
    return ns(symbol=symbol, snapshot_at=OBSERVED_AT, order_type=order_type)


def minute_row(symbol: str, venue: str, count: int) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "venue": venue,
        "bar_count": count,
        "first_bar_at": datetime(2026, 1, 5, 0, 0, tzinfo=UTC),
        "last_bar_at": datetime(2026, 1, 5, 6, 29, tzinfo=UTC),
    }


def timeline_session(**extra: Any) -> FakeSession:
    """KR 2026-01-05: five symbols, one per outcome below.

    051910 first capture only · 000100 not_admitted pick · 000660 admitted pick +
    deferred_no_action forecast · 035720 admitted pick, no decision ·
    005930 admitted pick + proposal -> rung -> order -> one linked fill.
    """
    picks = [
        pick(
            1,
            "000100",
            PICK_AT,
            admission="not_admitted",
            admission_reason="family_slot_taken",
        ),
        pick(2, "000660", PICK_AT),
        pick(3, "035720", PICK_AT),
        pick(4, "005930", PICK_AT),
    ]
    base = kr_day_session()
    routes: dict[str, Any] = dict(base.routes)
    routes.update(
        {
            "FROM review.screener_pick_log": lambda sql: (
                [2] if "count(" in sql.lower() else picks
            ),
            "FROM review.trade_forecasts": [forecast(7, "000660", DECLINED_AT)],
            "FROM invest_momentum_event_snapshots": [observation("051910")],
            "FROM invest_momentum_event_runs": [("ok", 2), ("empty", 1)],
            "FROM research.kr_candles_1m": [
                minute_row("005930", "KRX", 390),
                minute_row("005930", "NTX", 60),
                minute_row("051910", "KRX", 12),
            ],
        }
    )
    routes.update(extra)
    return FakeSession(routes)


@pytest.mark.asyncio
async def test_a_kr_day_yields_one_row_per_symbol_with_the_expected_states() -> None:
    session = timeline_session()
    response = await read_retro_timeline(session, date=DAY, market="kr")
    validate(response, "RetroTimelineResponse")

    rows = {r["symbol"]: r for r in response["data"]}
    assert list(rows) == sorted(rows)  # stable symbol order
    assert {s: (r["state"], r["reason"]) for s, r in rows.items()} == {
        "000100": ("candidate_not_admitted", "family_slot_taken"),
        "000660": ("admitted_held", "deferred_no_action_forecast"),
        "005930": ("proposed_filled", "filled"),
        "035720": ("admitted_no_record", "no_decision_record"),
        "051910": ("observed_only", "observation_log_only"),
    }
    assert response["total"] == 5 and response["truncated"] is False

    chain = [(e["source"], e["kind"]) for e in rows["005930"]["events"]]
    assert chain == [
        ("pick_log", "admitted"),
        ("proposal", "filled"),
        ("order", "filled"),
        ("fill", "buy_filled"),
    ]
    refs = {e["source"]: e["ref"] for e in rows["005930"]["events"]}
    assert refs["pick_log"] == make_ref("candidate", 4)
    assert refs["proposal"] == make_ref("proposal", 10)
    assert refs["fill"] == make_ref("fill", 1000)  # the reconciled fill, not the dup
    forecast_event = rows["000660"]["events"][-1]
    assert forecast_event["source"] == "decision"
    assert forecast_event["kind"] == "deferred_no_action"
    assert forecast_event["ref"] == make_ref("forecast", 7)
    first_capture = rows["051910"]["events"][0]
    assert first_capture == {
        "at": "2026-01-05T00:20:00Z",
        "source": "observation_log",
        "kind": "up",
        "reason": None,
        "ref": None,
    }
    assert response["data_as_of"] == "2026-01-05T02:00:00Z"


@pytest.mark.asyncio
async def test_minute_bars_are_counts_per_venue_and_empty_when_none_stored() -> None:
    response = await read_retro_timeline(timeline_session(), date=DAY, market="kr")
    rows = {r["symbol"]: r for r in response["data"]}
    assert rows["005930"]["minute_bars"] == [
        {
            "venue": "KRX",
            "rows": 390,
            "first_bar_at": "2026-01-05T00:00:00Z",
            "last_bar_at": "2026-01-05T06:29:00Z",
        },
        {
            "venue": "NTX",
            "rows": 60,
            "first_bar_at": "2026-01-05T00:00:00Z",
            "last_bar_at": "2026-01-05T06:29:00Z",
        },
    ]
    assert rows["035720"]["minute_bars"] == []
    assert "minute_bars_reason" not in rows["035720"]
    assert response["coverage"]["minute_bars"] == {"status": "available"}
    # a minute row carries counts and times only: no prices
    assert set(rows["005930"]["minute_bars"][0]) == {
        "venue",
        "rows",
        "first_bar_at",
        "last_bar_at",
    }


@pytest.mark.asyncio
async def test_coverage_counts_run_rows_by_status_and_pick_calls() -> None:
    response = await read_retro_timeline(timeline_session(), date=DAY, market="kr")
    assert response["coverage"] == {
        "observation_log": {
            "status": "runs_recorded",
            "runs": {"ok": 2, "empty": 1, "error": 0},
        },
        "pick_log": {"status": "calls_recorded", "calls": 2},
        "minute_bars": {"status": "available"},
    }


@pytest.mark.asyncio
async def test_no_runs_rows_means_observation_log_not_running() -> None:
    session = timeline_session(**{"FROM invest_momentum_event_runs": []})
    response = await read_retro_timeline(session, date=DAY, market="kr")
    validate(response, "RetroTimelineResponse")
    assert response["coverage"]["observation_log"] == {
        "status": "observation_log_not_running",
        "runs": {"ok": 0, "empty": 0, "error": 0},
    }
    # no runs rows is a coverage statement, not a read failure: rows are intact
    assert response["total"] == 5
    no_calls = timeline_session(
        **{
            "FROM review.screener_pick_log": lambda sql: (
                [0] if "count(" in sql.lower() else []
            )
        }
    )
    empty = await read_retro_timeline(no_calls, date=DAY, market="kr")
    assert empty["coverage"]["pick_log"] == {"status": "no_calls", "calls": 0}


@pytest.mark.asyncio
async def test_a_requested_symbol_in_no_source_is_a_not_observed_row() -> None:
    session = FakeSession(
        {
            "FROM review.screener_pick_log": lambda sql: (
                [0] if "count(" in sql.lower() else []
            ),
            "FROM invest_momentum_event_runs": [],
        }
    )
    response = await read_retro_timeline(
        session, date=DAY, market="kr", symbol="005930"
    )
    validate(response, "RetroTimelineResponse")
    assert response["total"] == 1
    (row,) = response["data"]
    assert (row["symbol"], row["state"], row["reason"]) == (
        "005930",
        "not_observed",
        "no_record_in_sources",
    )
    assert row["events"] == [] and row["minute_bars"] == []
    assert response["data_as_of"] is None


@pytest.mark.asyncio
async def test_a_recorded_decision_evidence_row_is_a_decision_event() -> None:
    decision_id = UUID(int=0xD1)
    session = timeline_session(
        **{
            "FROM review.decision_evidence": [
                ns(
                    decision_id=decision_id,
                    decision_kind="declined",
                    decided_at=DECLINED_AT,
                    symbol="035720",
                )
            ]
        }
    )
    response = await read_retro_timeline(session, date=DAY, market="kr")
    rows = {r["symbol"]: r for r in response["data"]}
    assert (rows["035720"]["state"], rows["035720"]["reason"]) == (
        "admitted_held",
        "declined_decision",
    )
    assert rows["035720"]["events"][-1]["ref"] == decision_ref(decision_id)


# --------------------------------------------------------------------------
# a failed or capped source: null state + unknowns, never a weaker state
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_failed_pick_log_read_nulls_only_the_rows_it_could_change() -> None:
    session = timeline_session(**{"FROM review.screener_pick_log": operational_error()})
    response = await read_retro_timeline(session, date=DAY, market="kr")
    validate(response, "RetroTimelineResponse")
    rows = {r["symbol"]: r for r in response["data"]}
    assert sorted(rows) == ["000660", "005930", "051910"]  # pick-only symbols unseen
    assert (rows["005930"]["state"], rows["005930"]["reason"]) == (
        "proposed_filled",
        "filled",
    )
    for symbol in ("000660", "051910"):
        assert (rows[symbol]["state"], rows[symbol]["reason"]) == (
            None,
            "source_unavailable",
        )
    assert response["total"] is None
    fields = {u["field"] for u in response["unknowns"]}
    assert {"source.candidates", "total"} <= fields
    assert response["coverage"]["pick_log"] == {
        "status": "source_unavailable",
        "calls": None,
    }
    assert "raw driver text" not in str(response)


@pytest.mark.asyncio
async def test_a_failed_observation_read_hides_nothing_it_did_not_decide() -> None:
    session = timeline_session(
        **{"FROM invest_momentum_event_snapshots": operational_error()}
    )
    response = await read_retro_timeline(session, date=DAY, market="kr")
    validate(response, "RetroTimelineResponse")
    rows = {r["symbol"]: r for r in response["data"]}
    # the pick-log states do not depend on the observation log
    assert rows["000100"]["state"] == "candidate_not_admitted"
    assert rows["035720"]["state"] == "admitted_no_record"
    assert response["total"] is None
    assert "051910" not in rows
    assert {"source.observation_log", "total"} <= {
        u["field"] for u in response["unknowns"]
    }


@pytest.mark.asyncio
async def test_a_failed_order_read_nulls_rows_that_have_no_positive_evidence() -> None:
    session = timeline_session(
        **{"FROM review.kis_live_order_ledger": operational_error()}
    )
    response = await read_retro_timeline(session, date=DAY, market="kr")
    validate(response, "RetroTimelineResponse")
    rows = {r["symbol"]: r for r in response["data"]}
    assert rows["005930"]["state"] == "proposed_filled"  # a fill row exists
    assert rows["035720"]["state"] is None
    assert rows["035720"]["reason"] == "source_unavailable"
    assert rows["000100"]["state"] is None


@pytest.mark.asyncio
async def test_a_failed_decision_read_is_a_null_state_not_a_hold_inference() -> None:
    session = timeline_session(**{"FROM review.decision_evidence": operational_error()})
    response = await read_retro_timeline(session, date=DAY, market="kr")
    validate(response, "RetroTimelineResponse")
    rows = {r["symbol"]: r for r in response["data"]}
    assert rows["035720"]["state"] is None  # may have a decision we could not read
    assert rows["005930"]["state"] == "proposed_filled"
    assert "source.decisions" in {u["field"] for u in response["unknowns"]}


@pytest.mark.asyncio
async def test_a_capped_pick_log_read_is_incomplete_not_complete() -> None:
    from app.services.retro_v1.envelope import MAX_SOURCE_ROWS

    many = [
        pick(i, "000100", PICK_AT, admission="not_admitted", admission_reason="x")
        for i in range(1, MAX_SOURCE_ROWS + 1)
    ]
    session = timeline_session(
        **{
            "FROM review.screener_pick_log": lambda sql: (
                [2] if "count(" in sql.lower() else many
            )
        }
    )
    response = await read_retro_timeline(session, date=DAY, market="kr")
    rows = {r["symbol"]: r for r in response["data"]}
    assert rows["000100"]["state"] is None
    assert rows["000100"]["reason"] == "source_unavailable"
    assert response["total"] is None
    assert ("source.candidates", "incomplete_evidence") in {
        (u["field"], u["reason"]) for u in response["unknowns"]
    }


@pytest.mark.asyncio
async def test_a_failed_minute_read_keeps_states_and_marks_the_bars_unread() -> None:
    session = timeline_session(**{"FROM research.kr_candles_1m": operational_error()})
    response = await read_retro_timeline(session, date=DAY, market="kr")
    validate(response, "RetroTimelineResponse")
    assert response["total"] == 5
    for row in response["data"]:
        assert row["state"] is not None
        assert row["minute_bars"] is None
        assert row["minute_bars_reason"] == "source_unavailable"
    assert response["coverage"]["minute_bars"] == {"status": "source_unavailable"}


# --------------------------------------------------------------------------
# other markets, parameters and paging
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_us_has_no_observation_events_and_reads_minutes_by_session_date() -> None:
    session = FakeSession(
        {
            "FROM review.screener_pick_log": lambda sql: (
                [1]
                if "count(" in sql.lower()
                else [pick(1, "AAPL", PICK_AT, market="us")]
            ),
            "FROM research.us_candles_1m": [minute_row("AAPL", "NASD", 390)],
        }
    )
    response = await read_retro_timeline(session, date=DAY, market="us")
    validate(response, "RetroTimelineResponse")
    (row,) = response["data"]
    assert (row["state"], row["reason"]) == ("admitted_no_record", "no_decision_record")
    assert row["minute_bars"][0]["venue"] == "NASD"
    assert response["coverage"]["observation_log"] == {
        "status": "unsupported_market",
        "runs": None,
    }
    assert not any("invest_momentum_event" in s for s in session.statements)
    minute_sql = next(s for s in session.statements if "us_candles_1m" in s)
    assert "session_date" in minute_sql


@pytest.mark.asyncio
async def test_crypto_has_no_minute_or_observation_coverage() -> None:
    session = FakeSession(
        {
            "FROM review.screener_pick_log": lambda sql: (
                [0] if "count(" in sql.lower() else []
            )
        }
    )
    response = await read_retro_timeline(
        session, date=DAY, market="crypto", symbol="KRW-BTC"
    )
    validate(response, "RetroTimelineResponse")
    (row,) = response["data"]
    assert row["state"] == "not_observed"
    assert row["minute_bars"] is None
    assert row["minute_bars_reason"] == "unsupported_market"
    assert response["coverage"]["minute_bars"] == {"status": "unsupported_market"}
    assert response["coverage"]["observation_log"]["status"] == "unsupported_market"


@pytest.mark.asyncio
async def test_paging_slices_the_symbol_population() -> None:
    session = timeline_session()
    first = await read_retro_timeline(session, date=DAY, market="kr", limit=2)
    assert [r["symbol"] for r in first["data"]] == ["000100", "000660"]
    assert first["truncated"] is True and first["total"] == 5
    second = await read_retro_timeline(
        timeline_session(),
        date=DAY,
        market="kr",
        limit=2,
        cursor=first["next_cursor"],
    )
    assert [r["symbol"] for r in second["data"]] == ["005930", "035720"]
    # a cursor from other parameters is refused
    with pytest.raises(RetroReadError) as err:
        await read_retro_timeline(
            timeline_session(),
            date=DAY,
            market="kr",
            symbol="005930",
            limit=2,
            cursor=first["next_cursor"],
        )
    assert err.value.code == "validation_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"date": "2026-13-01", "market": "kr"},
        {"date": DAY, "market": "mars"},
        {"date": DAY, "market": "kr", "limit": 0},
        {"date": DAY, "market": "kr", "symbol": "bad symbol"},
        {"date": DAY, "market": "kr", "symbol": "KRW-BTC"},
    ],
)
async def test_invalid_parameters_are_refused_before_any_statement(
    kwargs: dict[str, Any],
) -> None:
    session = FakeSession()
    with pytest.raises(RetroReadError) as err:
        await read_retro_timeline(session, **kwargs)
    assert err.value.code == "validation_error"
    assert session.statements == []


def test_the_fixture_fill_instant_is_the_one_the_chain_reports() -> None:
    assert FILL_AT == datetime(2026, 1, 5, 2, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_a_prose_void_reason_falls_back_to_the_proposal_lifecycle_code() -> None:
    prose = proposal(
        10,
        "005930",
        datetime(2026, 1, 5, 1, 0, tzinfo=UTC),
        lifecycle_state="cancelled",
        void_reason="operator said: no thanks!",
    )
    session = timeline_session(
        **{
            "FROM review.order_proposals ": [prose],
            "FROM review.order_proposal_rungs": [],
            "FROM review.execution_ledger": [],
            "FROM review.kis_live_order_ledger": [],
        }
    )
    response = await read_retro_timeline(session, date=DAY, market="kr")
    row = next(r for r in response["data"] if r["symbol"] == "005930")
    assert (row["state"], row["reason"]) == ("proposed_unfilled", "proposal_cancelled")
    assert row["events"][-1]["reason"] is None
