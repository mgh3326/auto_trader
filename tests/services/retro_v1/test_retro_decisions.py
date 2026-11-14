"""read_retro_decisions: one row per recorded decision, under the contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from app.services.retro_v1 import RetroReadError, read_retro_decisions
from app.services.retro_v1.envelope import make_ref
from tests.services.retro_v1.support import (
    FakeSession,
    WithParams,
    bar_row,
    ledger,
    live_order,
    ns,
    operational_error,
    proposal,
    rung,
    validate,
)

pytestmark = pytest.mark.unit
DECIDED = datetime(2026, 1, 5, 0, 30, tzinfo=UTC)  # 09:30 KST
FILL_AT = datetime(2026, 1, 5, 2, 0, tzinfo=UTC)  # 11:00 KST
DECISION_A = UUID(int=1)
DECISION_B = UUID(int=2)
PROPOSAL_A = UUID(int=10)
SEND_KEY = "send-1"


def decision(id_: int, did: UUID = DECISION_A, **kw):
    values = {
        "id": id_,
        "decision_id": did,
        "decision_kind": "proposal",
        "decided_at": DECIDED,
        "decided_by": "order_proposals",
        "market": "equity_kr",
        "account_mode": "kis_live",
        "symbol": "005930",
        "side": "buy",
        "strategy": "swing-etf",
        "sleeve_id": None,
        "candidate_call_id": None,
        "candidate_symbol": None,
        "candidate_link_basis": None,
        "currency": "KRW",
        "stop_price": Decimal("69000"),
        "stop_basis": "absolute",
        "stop_source": "policy",
        "target_price": None,
        "horizon_sessions": 20,
        "forecast_ids": None,
        "report_item_uuid": None,
        "policy_version": None,
        "policy_content_hash": None,
    }
    values.update(kw)
    return ns(**values)


def decision_rung(did: UUID = DECISION_A, index: int = 0, **kw):
    values = {
        "decision_id": did,
        "rung_index": index,
        "side": "buy",
        "planned_quantity": Decimal("3"),
        "planned_notional": Decimal("210000"),
        "limit_price": Decimal("70000"),
    }
    values.update(kw)
    return ns(**values)


def ref_price(did: UUID = DECISION_A, **kw):
    values = {
        "id": 1,
        "decision_id": did,
        "rung_index": 0,
        "kind": "limit",
        "price": Decimal("70000"),
        "asof": DECIDED,
    }
    values.update(kw)
    return ns(**values)


def attempt(n: int, did: UUID, proposal_id: UUID | None, rung_index=None):
    return ns(
        attempt_id=UUID(int=n),
        decision_id=did,
        account_scope="kis_live",
        send_key=SEND_KEY,
        proposal_id=proposal_id,
        rung_index=rung_index if proposal_id is not None else None,
    )


def session(decisions, **extra):
    routes = {
        # the longer fragment first: decision_evidence is a prefix of the
        # rungs table name, so ordering decides which route matches.
        "FROM review.decision_evidence_rungs": [],
        "FROM review.decision_reference_prices": [],
        "FROM review.decision_evidence": decisions,
        "FROM review.order_proposals ": [],
        "FROM review.order_proposal_rungs": [],
        "FROM review.decision_order_links": [],
        "FROM review.decision_order_link_outcomes": [],
        "FROM review.kis_live_order_ledger": [],
        "FROM review.live_order_ledger": [],
        "FROM review.toss_live_order_ledger": [],
        "FROM review.strategy_sleeve_versions": [],
        "FROM review.lot_exit_plans": [],
        "FROM review.screener_pick_log": [],
        "FROM review.trade_retrospectives": [],
        "FROM review.execution_ledger": [],
        "FROM public.kr_candles_1d": [],
        "FROM public.us_candles_1d": [],
        "FROM public.crypto_candles_1d": [],
    }
    routes.update(extra)
    return FakeSession(routes)


def linked_session(decisions=None, **extra):
    """One recorded decision → proposal → sent order → filled order."""
    base = {
        "FROM review.decision_evidence_rungs": [decision_rung()],
        "FROM review.decision_reference_prices": [ref_price()],
        "FROM review.order_proposals ": [
            proposal(
                10,
                "005930",
                datetime(2026, 1, 5, 1, 0, tzinfo=UTC),
                proposal_id=PROPOSAL_A,
                decision_id=DECISION_A,
            )
        ],
        "FROM review.order_proposal_rungs": [rung(100, 10)],
        "FROM review.decision_order_links": [attempt(1, DECISION_A, PROPOSAL_A, 0)],
        "FROM review.decision_order_link_outcomes": [
            ns(attempt_id=UUID(int=1), outcome="sent")
        ],
        "FROM review.kis_live_order_ledger": [
            live_order(100, FILL_AT, idempotency_key=SEND_KEY, status="filled")
        ],
        "FROM review.execution_ledger": [ledger(1000, FILL_AT)],
        "FROM public.kr_candles_1d": [
            bar_row("005930", datetime(2026, 1, 5, tzinfo=UTC), "71000"),
            *[
                bar_row(
                    "005930",
                    datetime(2026, 1, 6, tzinfo=UTC) + timedelta(days=i),
                    str(72000 + i * 100),
                )
                for i in range(25)
            ],
        ],
    }
    base.update(extra)
    return session(decisions or [decision(1)], **base)


@pytest.mark.asyncio
async def test_linked_decision_row_is_exact_and_opaque() -> None:
    response = await read_retro_decisions(
        linked_session(), from_="2026-01-01", to="2026-01-31"
    )
    validate(response, "RetroDecisionsResponse")
    assert response["total"] == 1 and response["truncated"] is False
    row = response["data"][0]
    assert row["decision_ref"] == make_ref("decision", DECISION_A)
    assert row["proposal_ref"] == make_ref("proposal", 10)
    assert row["decision_kind"] == "proposal" and row["market"] == "kr"
    # risk = (reference - stop) * planned quantity = (70000-69000)*3, exact text
    assert row["planned_risk_amount"] == "3000"
    assert row["risk_basis"] == "recorded" and row["risk_reason"] is None
    assert row["reference_price"] == "70000"
    assert row["reference_kind"] == "limit"
    assert row["order_count"] == 1 and row["order_states"] == ["filled"]
    assert row["entry_avg_price"] == "70000" and row["filled_quantity"] == "3"
    assert row["fill_time_basis"] == "broker_order_time"
    assert row["candidate_reason"] == "no_candidate_ref"
    blob = json.dumps(response)
    assert "0000123456" not in blob and SEND_KEY not in blob


@pytest.mark.asyncio
async def test_mfe_mae_come_from_closed_sessions_only() -> None:
    response = await read_retro_decisions(
        linked_session(), from_="2026-01-01", to="2026-01-31"
    )
    row = response["data"][0]
    # risk per unit = 3000/3 = 1000; first closed session's bar is 72000
    one = row["mfe"]["1"]
    assert one["status"] == "complete"
    assert one["amount"] == "2000" and one["r"] == "2"
    assert row["mae"]["1"]["amount"] == "0"  # never below entry → clamped
    assert row["excursion_basis"] == "inference"


@pytest.mark.asyncio
async def test_declined_decision_has_no_risk_and_no_orders() -> None:
    declined = decision(
        9,
        DECISION_B,
        decision_kind="declined",
        side=None,
        stop_price=None,
        decided_at=DECIDED + timedelta(hours=1),
    )
    response = await read_retro_decisions(
        linked_session(
            decisions=[decision(1), declined],
            **{"FROM review.decision_evidence_rungs": [decision_rung()]},
        ),
        from_="2026-01-01",
        to="2026-01-31",
    )
    validate(response, "RetroDecisionsResponse")
    row = response["data"][1]
    assert row["decision_kind"] == "declined"
    assert row["planned_risk_amount"] is None
    assert row["risk_reason"] == "not_applicable"
    assert row["order_count"] == 0 and row["order_states"] == []
    assert row["retrospective_reason"] == "no_decision_link"


@pytest.mark.asyncio
async def test_shared_send_key_between_two_decisions_resolves_neither() -> None:
    other = decision(2, DECISION_B, decided_at=DECIDED + timedelta(hours=1))
    src = linked_session(
        decisions=[decision(1), other],
        **{
            "FROM review.decision_evidence_rungs": [
                decision_rung(),
                decision_rung(DECISION_B),
            ],
            "FROM review.decision_order_links": [
                attempt(1, DECISION_A, PROPOSAL_A, 0),
                attempt(2, DECISION_B, PROPOSAL_A, 0),
            ],
            "FROM review.decision_order_link_outcomes": [
                ns(attempt_id=UUID(int=1), outcome="sent")
            ],
        },
    )
    response = await read_retro_decisions(src, from_="2026-01-01", to="2026-01-31")
    validate(response, "RetroDecisionsResponse")
    for row in response["data"]:
        # the key is ambiguous: no positive order attribution for either side
        assert row["order_count"] == 0 and row["order_states"] == []
        assert row["filled_quantity"] is None
        assert row["fill_reason"] == "no_linked_order"


@pytest.mark.asyncio
async def test_failed_link_read_is_unavailable_not_silent() -> None:
    src = linked_session(**{"FROM review.decision_order_links": operational_error()})
    response = await read_retro_decisions(src, from_="2026-01-01", to="2026-01-31")
    validate(response, "RetroDecisionsResponse")
    row = response["data"][0]
    assert row["order_count"] is None
    assert row["order_count_reason"] == "attempts_unavailable"
    assert row["order_states"] is None
    assert row["filled_quantity"] is None
    assert any(
        item["field"] == "source.decision_links" for item in response["unknowns"]
    )
    assert {"field": "data.0.order_count", "reason": "count_unavailable"} in response[
        "unknowns"
    ]


@pytest.mark.asyncio
async def test_inferred_retrospective_link_via_one_correlation() -> None:
    order = live_order(
        100, FILL_AT, idempotency_key=SEND_KEY, status="filled", correlation_id="c-1"
    )
    review = ns(
        id=77,
        decision_id=None,
        correlation_id="c-1",
        created_at=FILL_AT + timedelta(days=9),
    )
    response = await read_retro_decisions(
        linked_session(
            **{
                "FROM review.kis_live_order_ledger": [order],
                "FROM review.trade_retrospectives": [review],
            }
        ),
        from_="2026-01-01",
        to="2026-01-31",
    )
    validate(response, "RetroDecisionsResponse")
    row = response["data"][0]
    assert row["retrospective_ref"] == make_ref("review", 77)
    assert row["retrospective_link_basis"] == "inference"


@pytest.mark.asyncio
async def test_date_symbol_and_pagination() -> None:
    rows = [decision(1), decision(2, DECISION_B)]
    src = linked_session(decisions=rows)
    src.routes["FROM review.decision_evidence"] = WithParams(
        lambda sql, params: [] if "999999" in params.values() else list(rows)
    )
    first = await read_retro_decisions(
        src, from_="2026-01-01", to="2026-01-31", limit=1
    )
    validate(first, "RetroDecisionsResponse")
    assert first["total"] == 2 and first["truncated"] is True
    second = await read_retro_decisions(
        src,
        from_="2026-01-01",
        to="2026-01-31",
        limit=1,
        cursor=first["next_cursor"],
    )
    assert [r["decision_ref"] for r in second["data"]] != [
        r["decision_ref"] for r in first["data"]
    ]
    filtered = await read_retro_decisions(
        src, from_="2026-01-01", to="2026-01-31", symbol="999999"
    )
    assert filtered["data"] == [] and filtered["total"] == 0
    sql = "\n".join(src.statements)
    assert "decision_evidence.symbol" in sql


@pytest.mark.asyncio
async def test_queries_are_select_only_and_snapshot_is_read_only() -> None:
    src = linked_session()
    await read_retro_decisions(src, from_="2026-01-01", to="2026-01-31")
    assert any("REPEATABLE READ" in s and "READ ONLY" in s for s in src.statements)
    writes = [
        s
        for s in src.statements
        if s.lstrip().split(" ", 1)[0] not in {"SELECT", "SET", "SAVEPOINT", "RELEASE"}
    ]
    assert writes == []


@pytest.mark.asyncio
async def test_us_symbol_filter_is_normalized_to_db_form() -> None:
    rows = [decision(1, market="equity_us", symbol="BRK.B")]
    seen: list[dict] = []
    src = session(rows)
    src.routes["FROM review.decision_evidence"] = WithParams(
        lambda sql, params: (seen.append(dict(params)), list(rows))[1]
    )
    result = await read_retro_decisions(
        src, from_="2026-01-01", to="2026-01-31", market="us", symbol="BRK-B"
    )
    validate(result, "RetroDecisionsResponse")
    assert result["total"] == 1
    assert "BRK.B" in seen[0].values() and "BRK-B" not in seen[0].values()


@pytest.mark.asyncio
async def test_failed_order_ledger_read_withholds_the_count() -> None:
    src = linked_session(**{"FROM review.kis_live_order_ledger": operational_error()})
    response = await read_retro_decisions(src, from_="2026-01-01", to="2026-01-31")
    validate(response, "RetroDecisionsResponse")
    row = response["data"][0]
    assert row["order_count"] is None
    assert row["order_states"] is None
    assert row["filled_quantity"] is None
    assert row["fill_reason"] == "attempts_unavailable"


@pytest.mark.asyncio
async def test_invalid_market_rejected() -> None:
    with pytest.raises(RetroReadError):
        await read_retro_decisions(
            FakeSession(), from_="2026-01-01", to="2026-01-31", market="moon"
        )
