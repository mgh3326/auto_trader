import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.order_proposals.kis_leftover_inference import (
    EXPIRED_INFERENCE_VOID_REASON,
)
from app.services.retro_v1 import read_retro_day, read_retro_funnel
from app.services.retro_v1.chain import _INFERENCE_MARKER, reject_reason
from tests.services.retro_v1.support import (
    FILL_AT,
    kr_day_session,
    ledger,
    live_order,
    rung,
)


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ("too many requests", "rate_limited"),
        ("price limit", "price_limit"),
        ("insufficient balance", "insufficient_balance"),
        ("market closed", "market_closed"),
        ("invalid order", "invalid_order"),
        ("unrecognized", "broker_other"),
    ],
)
def test_rejection_table(evidence, expected):
    assert reject_reason(evidence) == expected


def test_inference_marker_matches_writer():
    assert _INFERENCE_MARKER == EXPIRED_INFERENCE_VOID_REASON


@pytest.mark.asyncio
async def test_order_evidence_and_counts():
    current = datetime.now(UTC)
    day = current.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Seoul")).date()
    order = live_order(
        at=current,
        response_message="too many requests SECRET",
        response_code="SECRET",
        status="rejected",
    )
    session = kr_day_session(
        **{
            "FROM review.kis_live_order_ledger": [order],
            "FROM review.execution_ledger": [],
        }
    )
    response = await read_retro_day(session, market="kr", date=day)
    row = next(r for r in response["data"] if r["stage"] == "order")
    assert (row["status"], row["basis"], row["reason"]) == (
        "rejected",
        "broker",
        "rate_limited",
    )
    assert "SECRET" not in json.dumps(response)
    assert "0000123456" not in json.dumps(response)


@pytest.mark.asyncio
async def test_marker_and_stale_and_direct_orders():
    marked = rung(100, 10, void_reason=_INFERENCE_MARKER)
    session = kr_day_session(
        **{
            "FROM review.order_proposal_rungs": [marked],
            "FROM review.execution_ledger": [],
        }
    )
    response = await read_retro_day(session, market="kr", date=FILL_AT.date())
    row = next(r for r in response["data"] if r["stage"] == "order")
    assert (row["status"], row["basis"], row["reason"]) == (
        "expired",
        "inference",
        "expired_inference_no_broker_original",
    )
    session = kr_day_session(
        **{"FROM review.order_proposals ": [], "FROM review.execution_ledger": []}
    )
    response = await read_retro_day(session, market="kr", date=FILL_AT.date())
    row = next(r for r in response["data"] if r["stage"] == "order")
    assert (row["status"], row["basis"], row["reason"], row["linked_ref"]) == (
        "unresolved",
        "unknown",
        "no_terminal_evidence",
        None,
    )


@pytest.mark.asyncio
async def test_fill_overrides_accepted_and_funnel_counts_rows():
    fills = [
        ledger(1100, FILL_AT, filled_qty=Decimal("1")),
        ledger(1101, FILL_AT + timedelta(seconds=1), filled_qty=Decimal("2")),
    ]
    routes = {"FROM review.execution_ledger": fills}
    day = await read_retro_day(
        kr_day_session(**routes), market="kr", date=FILL_AT.date()
    )
    order = next(r for r in day["data"] if r["stage"] == "order")
    assert (order["status"], order["basis"]) == ("filled", "broker")
    assert all(
        r["linked_ref"] == order["event_ref"]
        for r in day["data"]
        if r["stage"] == "fill"
    )
    funnel = await read_retro_funnel(
        kr_day_session(**routes), market="kr", from_=FILL_AT.date(), to=FILL_AT.date()
    )
    counts = {r["stage"]: r["count"] for r in funnel["data"]}
    assert counts["order"] == sum(r["stage"] == "order" for r in day["data"])
    assert counts["fill"] == sum(r["stage"] == "fill" for r in day["data"])


@pytest.mark.asyncio
async def test_sgov_us_reconciler_fill_overrides_accepted():
    from app.services.retro_v1 import read_retro_symbol
    from tests.services.retro_v1.support import FakeSession, proposal

    at = datetime(2026, 9, 28, 14, tzinfo=UTC)
    order = live_order(
        55,
        at,
        symbol="SGOV",
        currency="USD",
        quantity=Decimal("31"),
        idempotency_key="idem-100",
    )
    fill = ledger(
        90,
        at,
        instrument_type="equity_us",
        symbol="SGOV",
        raw_symbol="SGOV",
        currency="USD",
        filled_qty=Decimal("31"),
    )
    p = proposal(10, "SGOV", at, market="equity_us")
    r = rung(100, 10, quantity=Decimal("31"), state="resting", created_at=at)
    session = FakeSession(
        {
            "FROM review.live_order_ledger": [order],
            "FROM review.execution_ledger": [fill],
            "FROM review.order_proposals ": [p],
            "FROM review.order_proposal_rungs": [r],
            "FROM review.order_send_intents": [("idem-100", at)],
        }
    )
    response = await read_retro_symbol(
        session, symbol="SGOV", from_=at.date(), to=at.date()
    )
    o = next(r for r in response["data"] if r["stage"] == "order")
    f = next(r for r in response["data"] if r["stage"] == "fill")
    assert (o["status"], o["basis"], f["linked_ref"]) == (
        "filled",
        "broker",
        o["event_ref"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("expired", "day_order_expired"),
        ("rejected", "broker_other"),
        ("cancelled", "cancelled"),
    ],
)
async def test_toss_terminal(status, reason):
    from tests.services.retro_v1.support import ns

    toss = ns(
        id=42,
        trade_date=FILL_AT,
        broker="toss",
        symbol="005930",
        side="buy",
        currency="KRW",
        broker_order_id="x",
        client_order_id="direct",
        quantity=Decimal("3"),
        price=Decimal("10"),
        status=status,
        response_code=None,
        response_message=None,
        broker_status="REJECTED",
        correlation_id=None,
    )
    response = await read_retro_day(
        kr_day_session(
            **{
                "FROM review.kis_live_order_ledger": [],
                "FROM review.toss_live_order_ledger": [toss],
                "FROM review.execution_ledger": [],
            }
        ),
        market="kr",
        date=FILL_AT.date(),
    )
    order = next(r for r in response["data"] if r["stage"] == "order")
    assert (order["status"], order["basis"], order["reason"]) == (
        status,
        "broker",
        reason,
    )


@pytest.mark.asyncio
async def test_current_order_is_working_and_kis_expiry_is_broker_evidence():
    from zoneinfo import ZoneInfo

    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    routes = {
        "FROM review.kis_live_order_ledger": [live_order(at=now)],
        "FROM review.execution_ledger": [],
        "FROM review.order_proposals ": [],
    }
    response = await read_retro_day(kr_day_session(**routes), market="kr", date=today)
    order = next(r for r in response["data"] if r["stage"] == "order")
    assert order["status"] == "working"
    routes["FROM review.kis_live_order_ledger"] = [live_order(status="expired")]
    response = await read_retro_day(
        kr_day_session(**routes), market="kr", date=FILL_AT.date()
    )
    order = next(r for r in response["data"] if r["stage"] == "order")
    assert (order["status"], order["basis"], order["reason"]) == (
        "expired",
        "broker",
        "day_order_expired",
    )


@pytest.mark.asyncio
async def test_prior_proposal_links_order_and_marker():
    from tests.services.retro_v1.support import FakeSession, proposal

    p = proposal(10, "005930", FILL_AT - timedelta(days=1))
    r = rung(100, 10, void_reason=_INFERENCE_MARKER)

    def proposals(sql):
        return [p] if "order_proposals.id IN" in sql else []

    session = FakeSession(
        {
            "FROM review.order_proposals ": proposals,
            "FROM review.order_proposal_rungs": [r],
            "FROM review.order_send_intents": [("idem-100", FILL_AT)],
            "FROM review.kis_live_order_ledger": [live_order()],
        }
    )
    response = await read_retro_day(session, market="kr", date=FILL_AT.date())
    order = next(r for r in response["data"] if r["stage"] == "order")
    assert order["status"] == "expired"
    assert order["linked_ref"] is not None


@pytest.mark.asyncio
async def test_us_fill_links_order_from_previous_kst_day():
    from tests.services.retro_v1.support import FakeSession

    sent = datetime(2026, 9, 28, 14, 50, tzinfo=UTC)
    filled = sent + timedelta(minutes=20)
    order = live_order(55, sent, symbol="SGOV", currency="USD", quantity=Decimal("31"))
    fill = ledger(
        90,
        filled,
        instrument_type="equity_us",
        symbol="SGOV",
        raw_symbol="SGOV",
        currency="USD",
        filled_qty=Decimal("31"),
    )
    response = await read_retro_day(
        FakeSession(
            {
                "FROM review.live_order_ledger": [order],
                "FROM review.execution_ledger": [fill],
            }
        ),
        market="us",
        date=filled.date() + timedelta(days=1),
    )
    row = next(r for r in response["data"] if r["stage"] == "fill")
    assert row["linked_ref"] is not None
    assert not any(r["stage"] == "order" for r in response["data"])


@pytest.mark.asyncio
async def test_rung_requires_matching_key_even_when_order_number_matches():
    other = rung(101, 10)
    routes = {
        "FROM review.order_proposal_rungs": [rung(100, 10), other],
        "FROM review.order_send_intents": [
            ("idem-100", FILL_AT),
            ("idem-101", FILL_AT),
        ],
        "FROM review.execution_ledger": [],
    }
    response = await read_retro_day(
        kr_day_session(**routes), market="kr", date=FILL_AT.date()
    )
    from app.services.retro_v1.envelope import make_ref

    order = next(r for r in response["data"] if r["stage"] == "order")
    assert order["linked_ref"] == make_ref("rung", 100)
