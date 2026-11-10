"""retro_day, retro_symbol and retro_funnel against the contract schemas."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.retro_v1 import (
    RetroReadError,
    read_retro_day,
    read_retro_funnel,
    read_retro_symbol,
)
from app.services.retro_v1.envelope import make_ref
from tests.services.retro_v1.support import (
    FILL_AT,
    FakeSession,
    kr_day_session,
    operational_error,
    pick,
    rung,
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
async def test_retro_symbol_explicit_market_and_inference(monkeypatch) -> None:
    from app.services.retro_v1 import retro_symbol as module
    from app.services.retro_v1.chain import ChainData

    seen = []

    async def load(_session, _unknowns, info, **_kwargs):
        seen.append((info.key, _kwargs["symbols"]))
        return ChainData()

    monkeypatch.setattr(module, "load_chain", load)
    explicit = FakeSession()
    response = await read_retro_symbol(
        explicit, symbol="KRW-BTC", market="crypto", from_=DAY, to=DAY
    )
    validate(response, "RetroSymbolResponse")
    assert seen == [("crypto", ["KRW-BTC"])]
    inferred = FakeSession()
    await read_retro_symbol(inferred, symbol="BTC", from_=DAY, to=DAY)
    assert seen == [("crypto", ["KRW-BTC"]), ("us", ["BTC"])]
    with pytest.raises(RetroReadError) as err:
        await read_retro_symbol(
            FakeSession(), symbol="KRW-BTC", market="us", from_=DAY, to=DAY
        )
    assert "market" in err.value.safe_message.lower()
    await read_retro_symbol(
        FakeSession(), symbol="AGM-PA", market="us", from_=DAY, to=DAY
    )
    assert seen[-1] == ("us", ["AGM.PA"])
    for symbol in ("ETH-USD", "SOL-USDT", "KRW/BTC", "KRW.BTC"):
        with pytest.raises(RetroReadError) as err:
            await read_retro_symbol(
                FakeSession(), symbol=symbol, market="us", from_=DAY, to=DAY
            )
        assert "market" in err.value.safe_message.lower()


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


def _seed_row(id_: int):
    from tests.services.retro_v1.support import FILL_AT, ledger

    return ledger(
        id_,
        FILL_AT.replace(hour=0, minute=0),
        source="manual_import",
        broker_order_id="SEED-20260105-kis-krx-005930",
        raw_payload_json={"seed_kind": "opening_lot"},
        filled_qty=Decimal("10"),
        filled_price=Decimal("65000"),
    )


@pytest.mark.asyncio
async def test_opening_seed_is_not_a_fill_or_markout_but_still_sizes_the_lot() -> None:
    from tests.services.retro_v1.support import ledger  # noqa: F401

    rows = [_seed_row(500), *kr_day_session().routes["FROM review.execution_ledger"]]
    session = kr_day_session(**{"FROM review.execution_ledger": rows})
    response = await read_retro_day(session, market="kr", date=DAY)
    validate(response, "RetroDayResponse")
    blob = json.dumps(response)
    assert make_ref("fill", 500) not in blob and make_ref("markout", 500) not in blob
    assert len([r for r in response["data"] if r["stage"] == "fill"]) == 1
    # FIFO history still carries the seed: 10 seeded + 3 bought = 13 at the lot
    lot = next(r for r in response["data"] if r["stage"] == "lot")
    assert (lot["status"], lot["quantity"]) == ("open", "13")
    funnel = await read_retro_funnel(
        kr_day_session(**{"FROM review.execution_ledger": rows}),
        market="kr",
        from_=DAY,
        to=DAY,
    )
    assert {r["stage"]: r["count"] for r in funnel["data"]}["fill"] == 1


@pytest.mark.asyncio
async def test_reused_order_number_on_another_day_does_not_link_or_count() -> None:
    from tests.services.retro_v1.support import FILL_AT, ledger

    # same symbol and KIS order number, two trading days; only the rung of the
    # second day exists. The day-1 fill must not link to it (nor count in funnel).
    day1 = ledger(900, FILL_AT - timedelta(days=1), broker_order_id="0000123456")
    day2 = ledger(901, FILL_AT, broker_order_id="0000123456")
    earlier_rung = rung(
        101,
        10,
        validated_at=FILL_AT - timedelta(days=1, minutes=5),
        broker_order_id="0000123456",
    )
    session = kr_day_session(
        **{
            "FROM review.execution_ledger": [day1, day2],
            "FROM review.order_proposal_rungs": [rung(100, 10), earlier_rung],
            "FROM review.order_send_intents": [
                ("idem-100", FILL_AT - timedelta(hours=1)),
                ("idem-101", FILL_AT - timedelta(days=1, minutes=10)),
            ],
        }
    )
    response = await read_retro_symbol(
        session, symbol="005930", from_=date(2026, 1, 1), to=DAY
    )
    validate(response, "RetroSymbolResponse")
    fills = {r["event_ref"]: r for r in response["data"] if r["stage"] == "fill"}
    orders = {r["event_ref"] for r in response["data"] if r["stage"] == "order"}
    assert len(fills) == 2 and len(orders) == 2
    # each fill links to the order of its own trading day
    assert fills[make_ref("fill", 901)]["linked_ref"] == make_ref("order", 100)
    assert fills[make_ref("fill", 900)]["linked_ref"] == make_ref("order", 101)
    # two rungs sharing one identity are ambiguous: link unknown, never overwritten
    same_day = rung(102, 10, validated_at=FILL_AT, broker_order_id="0000123456")
    intents = [
        ("idem-100", FILL_AT - timedelta(hours=1)),
        ("idem-102", FILL_AT - timedelta(hours=1)),
    ]
    ambiguous = kr_day_session(
        **{
            "FROM review.execution_ledger": [day2],
            "FROM review.order_proposal_rungs": [rung(100, 10), same_day],
            "FROM review.order_send_intents": intents,
        }
    )
    response = await read_retro_day(ambiguous, market="kr", date=DAY)
    validate(response, "RetroDayResponse")
    fill = next(r for r in response["data"] if r["stage"] == "fill")
    assert fill["linked_ref"] is None and fill["reason"] == "no_linked_order"
    funnel = await read_retro_funnel(
        kr_day_session(
            **{
                "FROM review.execution_ledger": [day2],
                "FROM review.order_proposal_rungs": [rung(100, 10), same_day],
                "FROM review.order_send_intents": intents,
            }
        ),
        market="kr",
        from_=DAY,
        to=DAY,
    )
    validate(funnel, "RetroFunnelResponse")
    by_stage = {r["stage"]: r for r in funnel["data"]}
    assert by_stage["fill"]["count"] is None  # identities not unique: unknown
    assert by_stage["fill"]["missing_reason"] == "incomplete_evidence"


@pytest.mark.asyncio
async def test_markout_counts_sessions_on_one_venue_and_marks_mixed_venues_unknown() -> (
    None
):
    from tests.services.retro_v1.support import bar_row

    krx = [
        bar_row(
            "005930",
            datetime(2026, 1, 6, tzinfo=UTC) + timedelta(days=i),
            str(72000 + i),
        )
        for i in range(25)
    ]
    ntx = [
        bar_row(
            "005930",
            datetime(2026, 1, 6, tzinfo=UTC) + timedelta(days=i),
            "1",
            venue="NTX",
        )
        for i in range(25)
    ]
    session = kr_day_session(**{"FROM public.kr_candles_1d": [*ntx, *krx]})
    response = await read_retro_day(session, market="kr", date=DAY)
    markout = next(r for r in response["data"] if r["stage"] == "markout")
    # 20th KRX session, not the 10th date counted twice, and never an NTX price
    assert markout["price"] == "72019"
    us = kr_day_session(
        **{
            "FROM public.kr_candles_1d": [
                bar_row("005930", datetime(2026, 1, 6, tzinfo=UTC), "1", venue="NTX"),
                bar_row("005930", datetime(2026, 1, 6, tzinfo=UTC), "2", venue="OTHER"),
            ]
        }
    )
    mixed = await read_retro_day(us, market="kr", date=DAY)
    validate(mixed, "RetroDayResponse")
    row = next(r for r in mixed["data"] if r["stage"] == "markout")
    assert (row["price"], row["reason"]) == (None, "source_mixed")


@pytest.mark.asyncio
async def test_order_day_is_the_send_time_not_the_overwritten_validated_at() -> None:
    from tests.services.retro_v1.support import FILL_AT, ledger

    # a day-1 order partially filled day 1 and cancelled today: cancel/fill
    # transitions overwrite rung.validated_at with today. The identity must still
    # use the send reservation (day 1) so the day-1 fill keeps its link, and a
    # different order reusing the number today is not linked to it.
    day1_fill = ledger(910, FILL_AT - timedelta(days=1), broker_order_id="0000123456")
    today_fill = ledger(911, FILL_AT, broker_order_id="0000123456")
    cancelled = rung(
        100, 10, state="cancelled", validated_at=FILL_AT + timedelta(hours=3)
    )
    session = kr_day_session(
        **{
            "FROM review.execution_ledger": [day1_fill, today_fill],
            "FROM review.order_proposal_rungs": [cancelled],
            "FROM review.order_send_intents": [
                ("idem-100", FILL_AT - timedelta(days=1, minutes=30))
            ],
        }
    )
    response = await read_retro_symbol(
        session, symbol="005930", from_=date(2026, 1, 1), to=DAY
    )
    validate(response, "RetroSymbolResponse")
    fills = {r["event_ref"]: r for r in response["data"] if r["stage"] == "fill"}
    assert fills[make_ref("fill", 910)]["linked_ref"] == make_ref("order", 100)
    assert fills[make_ref("fill", 911)]["linked_ref"] is None


@pytest.mark.asyncio
async def test_kis_order_without_send_evidence_has_unknown_identity_and_fill_count() -> (
    None
):
    session = kr_day_session(**{"FROM review.order_send_intents": []})
    response = await read_retro_day(session, market="kr", date=DAY)
    validate(response, "RetroDayResponse")
    fill = next(r for r in response["data"] if r["stage"] == "fill")
    assert fill["linked_ref"] is None
    funnel = await read_retro_funnel(
        kr_day_session(**{"FROM review.order_send_intents": []}),
        market="kr",
        from_=DAY,
        to=DAY,
    )
    by_stage = {r["stage"]: r for r in funnel["data"]}
    assert by_stage["order"]["count"] == 1
    assert by_stage["fill"]["count"] is None


@pytest.mark.asyncio
async def test_send_evidence_is_read_from_the_kis_live_scope_only() -> None:
    session = kr_day_session()
    await read_retro_day(session, market="kr", date=DAY)
    reads = [s for s in session.statements if "FROM review.order_send_intents" in s]
    assert len(reads) == 1
    assert "account_scope =" in reads[0]
    # a same-key kis_mock row is outside the query, so it neither makes the live
    # key ambiguous nor stands in for a missing live reservation
    live_only = [("idem-100", FILL_AT - timedelta(hours=1))]
    response = await read_retro_day(
        kr_day_session(**{"FROM review.order_send_intents": live_only}),
        market="kr",
        date=DAY,
    )
    fill = next(r for r in response["data"] if r["stage"] == "fill")
    assert fill["linked_ref"] == make_ref("order", 100)
