"""retro_day, retro_symbol and retro_funnel against the contract schemas."""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.services.retro_v1 import (
    RetroReadError,
    read_retro_day,
    read_retro_funnel,
    read_retro_symbol,
)
from app.services.retro_v1.envelope import make_ref
from tests.services.retro_v1.support import (
    FakeSession,
    kr_day_session,
    operational_error,
    pick,
    validate,
)

pytestmark = pytest.mark.unit
DAY = date(2026, 1, 5)


def stages(response: dict) -> list[str]:
    return [row["stage"] for row in response["data"]]


@pytest.mark.asyncio
async def test_retro_day_matches_contract_and_links_the_chain() -> None:
    response = await read_retro_day(kr_day_session(), market="kr", date=DAY)
    validate(response, "RetroDayResponse")
    assert response["total"] == 8
    assert response["truncated"] is False
    assert response["timezone"] == "KST"
    assert sorted(stages(response)) == sorted(
        [
            "candidate",
            "candidate",
            "proposal",
            "rung",
            "order",
            "fill",
            "markout",
            "lot",
        ]
    )
    by_stage = {
        row["stage"]: row for row in response["data"] if row["stage"] != "candidate"
    }
    assert by_stage["proposal"]["linked_ref"] == make_ref("candidate", 1)
    assert by_stage["rung"]["linked_ref"] == by_stage["proposal"]["event_ref"]
    assert by_stage["order"]["linked_ref"] == by_stage["rung"]["event_ref"]
    assert by_stage["fill"]["linked_ref"] == by_stage["order"]["event_ref"]
    assert by_stage["markout"]["linked_ref"] == by_stage["fill"]["event_ref"]
    assert by_stage["lot"]["linked_ref"] == by_stage["fill"]["event_ref"]
    assert by_stage["fill"]["basis"] == "broker"


@pytest.mark.asyncio
async def test_quarantined_row_and_superseded_websocket_duplicate_are_not_fills() -> (
    None
):
    session = kr_day_session()
    response = await read_retro_day(session, market="kr", date=DAY)
    fills = [row for row in response["data"] if row["stage"] == "fill"]
    assert [row["event_ref"] for row in fills] == [make_ref("fill", 1000)]
    blob = json.dumps(response)
    # neither the duplicate (1001) nor the quarantined phantom (1002) appears
    # anywhere: not as a fill, a markout, or a lot link.
    assert make_ref("fill", 1001) not in blob
    assert make_ref("fill", 1002) not in blob
    # the in-effect predicate is part of every execution_ledger read (#1175)
    ledger_reads = [
        s for s in session.statements if "FROM review.execution_ledger" in s
    ]
    assert ledger_reads
    assert all("quarantined_at IS NULL" in s for s in ledger_reads)


@pytest.mark.asyncio
async def test_markout_uses_the_twentieth_later_bar_and_never_a_float() -> None:
    response = await read_retro_day(kr_day_session(), market="kr", date=DAY)
    markout = next(row for row in response["data"] if row["stage"] == "markout")
    assert markout["price"] == "72019"  # 20th bar strictly after the fill
    assert markout["quantity"] == "3"
    assert isinstance(markout["price"], str)


@pytest.mark.asyncio
async def test_missing_markout_bar_is_an_unknown_row_not_a_skipped_row() -> None:
    session = kr_day_session(**{"FROM public.kr_candles_1d": []})
    response = await read_retro_day(session, market="kr", date=DAY)
    validate(response, "RetroDayResponse")
    markout = next(row for row in response["data"] if row["stage"] == "markout")
    assert markout["price"] is None
    assert markout["event_at"] is None
    assert markout["reason"] == "missing_data"
    assert response["total"] == 8


@pytest.mark.asyncio
async def test_no_broker_order_number_or_internal_id_reaches_the_response() -> None:
    blob = json.dumps(await read_retro_day(kr_day_session(), market="kr", date=DAY))
    assert "123456" not in blob and "999999" not in blob
    for key in ("account", "odno", "order_no", "raw_payload"):
        assert key not in blob


@pytest.mark.asyncio
async def test_failed_source_read_is_an_unknown_and_total_is_not_claimed() -> None:
    session = kr_day_session(**{"FROM review.screener_pick_log": operational_error()})
    response = await read_retro_day(session, market="kr", date=DAY)
    validate(response, "RetroDayResponse")
    assert response["total"] is None
    fields = {(u["field"], u["reason"]) for u in response["unknowns"]}
    assert ("source.candidates", "upstream_error") in fields
    assert ("total", "count_unavailable") in fields
    assert "raw driver text" not in json.dumps(response)
    assert "candidate" not in {
        row["stage"] for row in response["data"] if row["status"] == "admitted"
    }


@pytest.mark.asyncio
async def test_pagination_total_is_the_population_not_the_page() -> None:
    picks = [pick(i, "005930", pick_at(i)) for i in range(1, 4)]
    session = FakeSession({"FROM review.screener_pick_log": picks})
    first = await read_retro_day(session, market="kr", date=DAY, limit=2)
    validate(first, "RetroDayResponse")
    assert (first["total"], len(first["data"]), first["truncated"]) == (3, 2, True)
    second = await read_retro_day(
        session, market="kr", date=DAY, limit=2, cursor=first["next_cursor"]
    )
    validate(second, "RetroDayResponse")
    assert (second["total"], len(second["data"]), second["truncated"]) == (3, 1, False)
    assert second["next_cursor"] is None
    refs = [r["event_ref"] for r in first["data"] + second["data"]]
    assert len(set(refs)) == 3
    with pytest.raises(RetroReadError) as err:
        await read_retro_day(
            session, market="us", date=DAY, limit=2, cursor=first["next_cursor"]
        )
    assert err.value.code == "validation_error"


def pick_at(i: int):
    from datetime import UTC, datetime

    return datetime(2026, 1, 5, 0, i, tzinfo=UTC)


@pytest.mark.asyncio
async def test_retro_symbol_matches_contract_and_accepts_the_from_key() -> None:
    session = kr_day_session()
    response = await read_retro_symbol(
        session, symbol="005930", to=DAY, **{"from": "2026-01-01"}
    )
    validate(response, "RetroSymbolResponse")
    assert "lot" in stages(response) and "fill" in stages(response)
    via_keyword = await read_retro_symbol(
        kr_day_session(), symbol="005930", from_=date(2026, 1, 1), to=DAY
    )
    assert via_keyword["total"] == response["total"]


@pytest.mark.asyncio
async def test_lot_is_unknown_not_zero_when_history_has_unverified_provisional_rows() -> (
    None
):
    from tests.services.retro_v1.support import FILL_AT, ledger

    only_websocket = [ledger(2000, FILL_AT, source="websocket")]
    session = kr_day_session(**{"FROM review.execution_ledger": only_websocket})
    response = await read_retro_symbol(session, symbol="005930", from_=DAY, to=DAY)
    validate(response, "RetroSymbolResponse")
    lot = next(row for row in response["data"] if row["stage"] == "lot")
    assert (lot["status"], lot["quantity"], lot["price"]) == ("unknown", None, None)
    assert lot["basis"] == "inference"


@pytest.mark.asyncio
async def test_retro_funnel_counts_and_rates() -> None:
    response = await read_retro_funnel(kr_day_session(), market="kr", from_=DAY, to=DAY)
    validate(response, "RetroFunnelResponse")
    rows = {row["stage"]: row for row in response["data"]}
    assert [
        rows[s]["count"] for s in ("candidate", "proposal", "rung", "order", "fill")
    ] == [1] * 5
    assert rows["candidate"]["denominator"] is None
    assert rows["candidate"]["fill_rate"] is None
    assert rows["fill"]["denominator"] == 1
    assert rows["fill"]["fill_rate"] == "1.0000"
    assert rows["rejected_forecast"]["count"] == 1
    assert response["total"] == len(response["data"]) == 6


@pytest.mark.asyncio
async def test_funnel_rate_needs_a_known_denominator() -> None:
    session = kr_day_session(**{"FROM review.order_proposals ": operational_error()})
    response = await read_retro_funnel(session, market="kr", from_=DAY, to=DAY)
    validate(response, "RetroFunnelResponse")
    rows = {row["stage"]: row for row in response["data"]}
    assert rows["proposal"]["count"] is None
    assert rows["proposal"]["fill_rate"] is None
    assert rows["proposal"]["missing_reason"] == "incomplete_evidence"
    assert rows["fill"]["fill_rate"] is None  # denominator (order) not known


@pytest.mark.asyncio
async def test_request_validation_is_a_closed_error() -> None:
    session = kr_day_session()
    cases = [
        read_retro_day(session, market="xx", date=DAY),
        read_retro_day(session, market="kr", date="2026-13-40"),
        read_retro_day(session, market="kr", date=DAY, limit=501),
        read_retro_symbol(session, symbol="005930", from_=date(2025, 1, 1), to=DAY),
        read_retro_symbol(session, symbol="005930", to=DAY),
        read_retro_funnel(session, market="kr", from_=DAY, to=date(2026, 1, 1)),
    ]
    for case in cases:
        with pytest.raises(RetroReadError) as err:
            await case
        assert err.value.code == "validation_error"
        assert err.value.safe_message == "Request invalid."
    assert session.statements == []  # rejected before any statement


@pytest.mark.asyncio
async def test_symbol_in_any_broker_spelling_is_normalised_by_the_symbol_module() -> (
    None
):
    session = FakeSession()
    for spelling in ("BRK/B", "BRK-B", "brk.b"):
        response = await read_retro_symbol(session, symbol=spelling, from_=DAY, to=DAY)
        validate(response, "RetroSymbolResponse")
    uppercase = [s for s in session.statements if "upper(" in s.lower()]
    assert uppercase  # symbol filter is applied case-insensitively
    with pytest.raises(RetroReadError):
        await read_retro_symbol(session, symbol="005930; DROP", from_=DAY, to=DAY)
