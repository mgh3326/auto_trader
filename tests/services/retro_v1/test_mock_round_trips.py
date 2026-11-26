"""mock_round_trips: paper/demo episodes as mock-marked chains, against the contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.mock_strategy import MockStrategyOrder
from app.services.retro_v1 import RetroReadError, read_mock_round_trips
from app.services.retro_v1.envelope import MAX_SOURCE_ROWS
from tests.services.retro_v1.support import FakeSession, operational_error, validate

pytestmark = pytest.mark.unit

ROUTE = "FROM review.mock_strategy_orders"
OPENED = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
CLOSED = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def order(
    id_: int,
    cid: str,
    episode: str,
    *,
    side: str = "buy",
    status: str = "closed",
    broker: str | None = None,
    **kw: object,
) -> MockStrategyOrder:
    values: dict[str, object] = {
        "id": id_,
        "strategy_id": "SYN_STRATEGY",
        "venue": "alpaca_paper",
        "account_ref": "synthetic-account-ref",
        "episode_id": episode,
        "symbol": "SPY",
        "side": side,
        "qty": Decimal("1"),
        "notional": None,
        "client_order_id": cid,
        "broker_order_id": broker,
        "status": status,
        "opened_at": OPENED,
        "closed_at": None,
        "entry_price": None,
        "exit_price": None,
        "realized_pnl": None,
        "meta": {},
    }
    values.update(kw)
    return MockStrategyOrder(**values)


def closed_episode() -> list[MockStrategyOrder]:
    """Entry bought at 100, exited at 103 (synthetic values), both legs bound."""
    return [
        order(
            1,
            "ep1",
            "ep1",
            broker="BROKER-ENTRY-901",
            entry_price=Decimal("100"),
            exit_price=Decimal("103"),
            realized_pnl=Decimal("3"),
            closed_at=CLOSED,
        ),
        order(
            2,
            "ep1-x1",
            "ep1",
            side="sell",
            broker="BROKER-EXIT-902",
            closed_at=CLOSED,
        ),
    ]


@pytest.mark.asyncio
async def test_a_closed_episode_is_one_mock_chain_with_pnl() -> None:
    session = FakeSession({ROUTE: closed_episode()})
    response = await read_mock_round_trips(session)
    validate(response, "MockRoundTripsResponse")
    assert response["total"] == 1
    (row,) = response["data"]
    assert row["mode"] == "mock"
    assert row["state"] == "closed" and row["reason"] is None
    assert row["entry_price"] == "100"
    assert row["exit_price"] == "103"
    assert row["realized_pnl_amount"] == "3"
    assert row["currency"] == "USD" and row["unit"] == "currency"
    assert row["basis"] == "inference"
    assert [leg["role"] for leg in row["legs"]] == ["entry", "exit"]
    assert all(leg["broker_order_bound"] is True for leg in row["legs"])
    # the chain carries opaque refs only: no broker order number, no raw key
    text = json.dumps(response)
    for private in ("BROKER-ENTRY-901", "BROKER-EXIT-902", "ep1", "ep1-x1"):
        assert private not in text
    assert all(
        s.lstrip().upper().startswith(("SELECT", "SET")) for s in session.statements
    )


@pytest.mark.asyncio
async def test_closed_episode_reports_broker_exit_fill_time_and_broker_side() -> None:
    filled_at = datetime(2026, 1, 5, 14, 40, tzinfo=UTC)
    observed_at = datetime(2026, 1, 5, 14, 49, tzinfo=UTC)
    episode = closed_episode()
    episode[0].closed_at = observed_at
    episode[1].meta = {
        "filled_at": filled_at.isoformat(),
        "fill_time_basis": "broker_filled_at",
    }
    response = await read_mock_round_trips(FakeSession({ROUTE: episode}))
    validate(response, "MockRoundTripsResponse")
    (row,) = response["data"]
    assert row["state"] == "closed"
    assert row["fill_time"] == filled_at.isoformat()
    assert row["fill_time_basis"] == "broker_filled_at"
    assert row["closed_at"] == "2026-01-05T14:49:00Z"
    assert [leg["broker_side"] for leg in row["legs"]] == ["buy", "sell"]


@pytest.mark.asyncio
async def test_closed_episode_without_exit_time_does_not_reuse_entry_time() -> None:
    episode = closed_episode()
    episode[0].meta = {
        "filled_at": OPENED.isoformat(),
        "fill_time_basis": "broker_filled_at",
    }
    episode[1].meta = {}
    response = await read_mock_round_trips(FakeSession({ROUTE: episode}))
    assert response["data"][0]["fill_time"] is None
    assert response["data"][0]["fill_time_basis"] is None


@pytest.mark.asyncio
async def test_an_uncertain_leg_shows_a_reason_not_a_zero() -> None:
    episode = [
        order(
            3,
            "ep2",
            "ep2",
            status="filled",
            broker="BROKER-ENTRY-903",
            entry_price=Decimal("50"),
        ),
        order(4, "ep2-x1", "ep2", side="sell", status="uncertain"),
    ]
    response = await read_mock_round_trips(FakeSession({ROUTE: episode}))
    validate(response, "MockRoundTripsResponse")
    (row,) = response["data"]
    assert row["state"] == "unresolved"
    assert row["reason"] == "uncertain_leg"
    assert row["realized_pnl_amount"] is None
    assert row["exit_price"] is None and row["closed_at"] is None
    assert {leg["status"] for leg in row["legs"]} == {"filled", "uncertain"}


@pytest.mark.asyncio
async def test_unfilled_and_open_episodes_are_not_closed_or_zero() -> None:
    episodes = [
        order(10, "planned", "planned", status="planned", opened_at=None),
        order(11, "rejected", "rejected", status="rejected"),
        order(12, "accepted", "accepted", status="accepted", broker="B-12"),
        order(13, "filled", "filled", status="filled", broker="B-13"),
    ]
    response = await read_mock_round_trips(FakeSession({ROUTE: episodes}))
    validate(response, "MockRoundTripsResponse")
    by_ref = {row["round_trip_ref"]: row for row in response["data"]}
    reasons = sorted((row["state"], row["reason"]) for row in by_ref.values())
    assert reasons == sorted(
        [
            ("not_filled", "not_sent"),
            ("not_filled", "rejected"),
            ("open", "awaiting_fill"),
            ("open", "awaiting_close_evidence"),
        ]
    )
    for row in response["data"]:
        assert row["realized_pnl_amount"] is None


@pytest.mark.asyncio
async def test_a_closed_episode_without_realized_pnl_names_the_gap() -> None:
    episode = [
        order(20, "ep3", "ep3", closed_at=CLOSED, entry_price=Decimal("10")),
        order(21, "ep3-x1", "ep3", side="sell", closed_at=CLOSED),
    ]
    response = await read_mock_round_trips(FakeSession({ROUTE: episode}))
    validate(response, "MockRoundTripsResponse")
    (row,) = response["data"]
    assert row["state"] == "closed"
    assert row["reason"] == "pnl_unrecorded"
    assert row["realized_pnl_amount"] is None


@pytest.mark.asyncio
async def test_exit_legs_without_an_entry_are_incomplete_evidence() -> None:
    orphan = [order(30, "ep4-x1", "ep4", side="sell", status="closed")]
    response = await read_mock_round_trips(FakeSession({ROUTE: orphan}))
    validate(response, "MockRoundTripsResponse")
    (row,) = response["data"]
    assert row["side"] is None
    assert row["state"] == "unresolved"
    assert row["reason"] == "incomplete_evidence"


@pytest.mark.asyncio
async def test_filters_reach_the_statement_and_a_foreign_venue_is_refused() -> None:
    session = FakeSession({ROUTE: closed_episode()})
    await read_mock_round_trips(
        session, strategy_id="SYN_STRATEGY", venue="alpaca_paper"
    )
    assert any("strategy_id =" in s and "venue =" in s for s in session.statements)
    # a live venue is not a mock venue: refused before any statement
    with pytest.raises(RetroReadError) as err:
        await read_mock_round_trips(session, venue="kis_live")
    assert err.value.code == "validation_error"
    with pytest.raises(RetroReadError):
        await read_mock_round_trips(session, strategy_id="bad id; drop")


@pytest.mark.asyncio
async def test_a_read_at_the_source_cap_is_unknown_not_a_partial_chain() -> None:
    rows = [closed_episode()[0]] * (MAX_SOURCE_ROWS + 1)
    response = await read_mock_round_trips(FakeSession({ROUTE: rows}))
    validate(response, "MockRoundTripsResponse")
    assert response["total"] is None
    assert response["data"] == []
    assert {
        "field": "source.mock_strategy_orders",
        "reason": "unavailable",
    } in response["unknowns"]
    assert {"field": "total", "reason": "count_unavailable"} in response["unknowns"]


@pytest.mark.asyncio
async def test_a_failed_read_is_an_upstream_unknown_with_no_data() -> None:
    session = FakeSession({ROUTE: operational_error()})
    response = await read_mock_round_trips(session)
    validate(response, "MockRoundTripsResponse")
    assert response["total"] is None and response["data"] == []
    assert {
        "field": "source.mock_strategy_orders",
        "reason": "upstream_error",
    } in response["unknowns"]


@pytest.mark.asyncio
async def test_pages_follow_the_open_order_by_entry_time() -> None:
    later = closed_episode()
    earlier = [
        order(
            40,
            "early",
            "early",
            broker="B-early",
            opened_at=datetime(2026, 1, 1, tzinfo=UTC),
            closed_at=CLOSED,
            realized_pnl=Decimal("1"),
            entry_price=Decimal("1"),
            exit_price=Decimal("2"),
        ),
        order(41, "early-x1", "early", side="sell", closed_at=CLOSED),
    ]
    session = FakeSession({ROUTE: [*later, *earlier]})
    first = await read_mock_round_trips(session, limit=1)
    validate(first, "MockRoundTripsResponse")
    assert first["truncated"] is True and first["total"] == 2
    assert first["data"][0]["opened_at"] == "2026-01-01T00:00:00Z"
    second = await read_mock_round_trips(
        FakeSession({ROUTE: [*later, *earlier]}),
        limit=1,
        cursor=first["next_cursor"],
    )
    validate(second, "MockRoundTripsResponse")
    assert second["data"][0]["opened_at"] == "2026-01-02T14:30:00Z"
    assert second["truncated"] is False
