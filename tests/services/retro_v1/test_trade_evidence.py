"""hk 4188: decision_ref, fill_time_basis and costs on retro_trades rows (#4154)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from app.models.trading import InstrumentType
from app.services.retro_v1 import read_retro_trades, trade_evidence
from app.services.retro_v1.links import decision_ref
from app.services.retro_v1.market import MARKETS
from app.services.retro_v1.trade_evidence import (
    costs_body,
    fill_time_basis,
    trade_costs,
    weakest_time_basis,
)
from tests.services.retro_v1.support import (
    FILL_AT,
    FakeSession,
    gate_trade_journal,
    gate_trade_review,
    ledger,
    live_order,
    ns,
    operational_error,
    validate,
)
from tests.services.retro_v1.test_retro_trades import journal, review

pytestmark = pytest.mark.unit

GOLDEN = Path(__file__).parent / "golden" / "legacy_main_4188.json"
NEW_FIELDS = {
    "decision_ref",
    "decision_link_basis",
    "link_reason",
    "fill_time_basis",
    "costs",
}
DECISION_A, DECISION_B = UUID(int=0xDA), UUID(int=0xDB)
KR = MARKETS["kr"]
KEY = "idem-9"
ORDER_NO = "0000123456"


def attempt(n: int, decision: UUID, key: str = KEY):
    return ns(
        attempt_id=UUID(int=n),
        decision_id=decision,
        account_scope="kis_live",
        send_key=key,
        proposal_id=None,
        rung_index=None,
    )


def order(**values):
    base = {
        "symbol": "SAMPLE",
        "correlation_id": "corr-1",
        "idempotency_key": KEY,
        "order_no": ORDER_NO,
        "status": "filled",
        "filled_qty": Decimal("3"),
    }
    base.update(values)
    return live_order(**base)


def fill(id_: int = 1, **values):
    base = {
        "symbol": "SAMPLE",
        "raw_symbol": "SAMPLE",
        "filled_notional": Decimal("210000"),
        "filled_at_basis": "broker_order_time",
        "fee_basis": "broker_reported",
        "fee_amount": Decimal("12"),
        "fee_currency": "KRW",
        "tax_amount": Decimal("3"),
    }
    base.update(values)
    return ledger(id_, FILL_AT, **base)


def session(journals, reviews, **routes):
    return FakeSession(
        {
            "FROM review.trade_journals": journals,
            "FROM review.trade_retrospectives": reviews,
            **routes,
        }
    )


async def read(source: FakeSession) -> dict:
    result = await read_retro_trades(source, from_="2026-01-01", to="2026-01-31")
    validate(result, "RetroTradesResponse")
    return result


@pytest.fixture(autouse=True)
def fixed_policy(monkeypatch):
    monkeypatch.setattr(
        trade_evidence,
        "_round_trip_bps",
        lambda market: (Decimal("50"), "policy-v1", "abc123def456"),
    )


# --------------------------------------------------------------------------
# rows without new data differ from main only by the optional fields
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_trade_without_evidence_differs_from_main_only_by_optional_fields() -> (
    None
):
    golden = json.loads(GOLDEN.read_text())["retro_trades"]
    result = await read(session([gate_trade_journal()], [gate_trade_review()]))
    rows = [{k: v for k, v in r.items() if k not in NEW_FIELDS} for r in result["data"]]
    assert rows == golden["data"]
    assert result["unknowns"] == golden["unknowns"]
    assert result["data"][0]["link_reason"] == "no_decision_link"
    assert not ((NEW_FIELDS - {"link_reason"}) & result["data"][0].keys())


# --------------------------------------------------------------------------
# §3 row 7: recorded versus derived
# --------------------------------------------------------------------------
def kr_journal(**values):
    return journal(101, InstrumentType.equity_kr, **values)


def kr_review(id_: int = 301, **values):
    return review(id_, 101, **values)


@pytest.mark.asyncio
async def test_stored_decision_id_is_recorded() -> None:
    result = await read(session([kr_journal()], [kr_review(decision_id=DECISION_A)]))
    row = result["data"][0]
    assert row["decision_ref"] == decision_ref(DECISION_A)
    assert row["decision_link_basis"] == "recorded"
    assert "link_reason" not in row
    assert str(DECISION_A) not in json.dumps(result)


@pytest.mark.asyncio
async def test_derived_decision_is_an_inference_through_the_order_attempts() -> None:
    routes = {
        "FROM review.kis_live_order_ledger": [order()],
        "FROM review.decision_order_links": [attempt(1, DECISION_A)],
        "FROM review.decision_order_link_outcomes": [
            ns(attempt_id=UUID(int=1), outcome="sent")
        ],
    }
    result = await read(
        session([kr_journal()], [kr_review(correlation_id="corr-1")], **routes)
    )
    row = result["data"][0]
    assert row["decision_ref"] == decision_ref(DECISION_A)
    assert row["decision_link_basis"] == "inference"
    assert "link_reason" not in row


@pytest.mark.asyncio
async def test_unconfirmed_attempt_is_marked() -> None:
    routes = {
        "FROM review.kis_live_order_ledger": [order()],
        "FROM review.decision_order_links": [attempt(1, DECISION_A)],
    }
    result = await read(
        session([kr_journal()], [kr_review(correlation_id="corr-1")], **routes)
    )
    row = result["data"][0]
    assert row["decision_link_basis"] == "inference"
    assert row["link_reason"] == "attempt_unconfirmed"


@pytest.mark.asyncio
async def test_ambiguity_gives_no_link() -> None:
    cases = {
        "ambiguous_attempts": {
            "FROM review.kis_live_order_ledger": [order()],
            "FROM review.decision_order_links": [
                attempt(1, DECISION_A),
                attempt(2, DECISION_B),
            ],
        },
        "ambiguous_decision": {
            "FROM review.kis_live_order_ledger": [
                order(id_=100),
                order(id_=101, idempotency_key="idem-10"),
            ],
        },
        "no_decision_link": {
            "FROM review.kis_live_order_ledger": [order()],
        },
    }
    for reason, routes in cases.items():
        result = await read(
            session([kr_journal()], [kr_review(correlation_id="corr-1")], **routes)
        )
        row = result["data"][0]
        assert row["link_reason"] == reason, reason
        assert "decision_ref" not in row and "decision_link_basis" not in row


@pytest.mark.asyncio
async def test_two_stored_decisions_are_ambiguous_and_stored_beats_derived() -> None:
    two = await read(
        session(
            [kr_journal()],
            [
                kr_review(301, decision_id=DECISION_A),
                kr_review(302, decision_id=DECISION_B),
            ],
        )
    )
    row = two["data"][0]
    assert row["link_reason"] == "ambiguous_decision" and "decision_ref" not in row
    routes = {
        "FROM review.kis_live_order_ledger": [order()],
        "FROM review.decision_order_links": [attempt(1, DECISION_B)],
        "FROM review.decision_order_link_outcomes": [
            ns(attempt_id=UUID(int=1), outcome="sent")
        ],
    }
    both = await read(
        session(
            [kr_journal()],
            [kr_review(decision_id=DECISION_A, correlation_id="corr-1")],
            **routes,
        )
    )
    assert both["data"][0]["decision_ref"] == decision_ref(DECISION_A)
    assert both["data"][0]["decision_link_basis"] == "recorded"


@pytest.mark.asyncio
async def test_failed_order_read_does_not_claim_there_is_no_link() -> None:
    routes = {"FROM review.kis_live_order_ledger": operational_error()}
    result = await read(
        session([kr_journal()], [kr_review(correlation_id="corr-1")], **routes)
    )
    row = result["data"][0]
    assert "link_reason" not in row and "decision_ref" not in row
    assert {"field": "source.trade_kis_orders", "reason": "upstream_error"} in result[
        "unknowns"
    ]


# --------------------------------------------------------------------------
# fill time basis and costs, end to end
# --------------------------------------------------------------------------
def fills_session(*fills, **values):
    return session(
        [kr_journal(correlation_id="corr-1")],
        [kr_review()],
        **{
            "FROM review.kis_live_order_ledger": [order()],
            "FROM review.execution_ledger": list(fills),
            **values,
        },
    )


@pytest.mark.asyncio
async def test_recorded_and_estimated_costs_are_kept_apart() -> None:
    result = await read(fills_session(fill()))
    row = result["data"][0]
    assert row["fill_time_basis"] == "broker_order_time"
    assert row["costs"] == {
        "recorded": {
            "fee": "12",
            "tax": "3",
            "currency": "KRW",
            "fee_includes_tax": False,
        },
        "estimated": {
            "amount": "1050",
            "rate_source": "trading_policy.order_proposals.auto_approve.round_trip_cost_bps",
            "policy_version": "policy-v1",
            "policy_content_hash": "abc123def456",
            "basis": "inference",
        },
    }


@pytest.mark.asyncio
async def test_a_fee_that_was_not_provided_is_not_a_recorded_zero() -> None:
    for values in (
        {"fee_basis": "not_provided", "fee_amount": None, "tax_amount": None},
        # a pre-L5 KIS reconciler row: the missing fee was stored as 0.0
        {"fee_basis": None, "fee_amount": Decimal("0"), "tax_amount": None},
    ):
        result = await read(fills_session(fill(**values)))
        costs = result["data"][0]["costs"]
        assert costs["recorded"] is None
        assert costs["recorded_reason"] == "not_provided"
        assert costs["estimated"]["amount"] == "1050"
        assert {"field": "data.0.costs.recorded", "reason": "missing_data"} in result[
            "unknowns"
        ]


@pytest.mark.asyncio
async def test_missing_rate_leaves_the_estimate_null_with_a_reason(monkeypatch) -> None:
    monkeypatch.setattr(
        trade_evidence, "_round_trip_bps", lambda market: (None, None, None)
    )
    result = await read(fills_session(fill()))
    costs = result["data"][0]["costs"]
    assert (
        costs["estimated"] is None and costs["estimated_reason"] == "rate_unavailable"
    )
    assert costs["recorded"]["fee"] == "12"


@pytest.mark.asyncio
async def test_policy_estimate_comes_from_the_policy_file(monkeypatch) -> None:
    monkeypatch.undo()
    from app.services.trading_policy_service import policy_version_stamp

    result = await read(fills_session(fill()))
    estimated = result["data"][0]["costs"]["estimated"]
    stamp = policy_version_stamp()
    assert estimated["policy_version"] == stamp["version"]
    assert estimated["policy_content_hash"] == stamp["content_hash"]
    assert Decimal(estimated["amount"]) > 0


@pytest.mark.asyncio
async def test_fills_of_other_orders_and_quarantined_rows_are_not_counted() -> None:
    result = await read(
        fills_session(
            fill(1),
            fill(2, broker_order_id="0000999999", fee_amount=Decimal("500")),
            fill(
                3, fee_amount=Decimal("700"), quarantined_at=FILL_AT, source="websocket"
            ),
        )
    )
    assert result["data"][0]["costs"]["recorded"]["fee"] == "12"


@pytest.mark.asyncio
async def test_trade_without_a_found_fill_has_no_time_basis_or_costs() -> None:
    result = await read(fills_session())
    row = result["data"][0]
    assert "costs" not in row and "fill_time_basis" not in row


@pytest.mark.asyncio
async def test_failed_fill_read_is_unknown_not_empty() -> None:
    result = await read(
        fills_session(**{"FROM review.execution_ledger": operational_error()})
    )
    row = result["data"][0]
    assert "costs" not in row and "fill_time_basis" not in row
    assert {"field": "source.trade_fills", "reason": "upstream_error"} in result[
        "unknowns"
    ]


# --------------------------------------------------------------------------
# pure rules
# --------------------------------------------------------------------------
def ledger_fill(**values):
    base = {
        "broker": "kis",
        "account_mode": "live",
        "instrument_type": InstrumentType.equity_kr,
        "symbol": "SAMPLE",
        "broker_order_id": "ord-1",
        "filled_at": FILL_AT,
        "filled_qty": Decimal("1"),
        "source": "reconciler",
        "side": "buy",
        "currency": "KRW",
        "filled_notional": Decimal("1000"),
        "fee_basis": "broker_reported",
        "fee_amount": Decimal("1"),
        "fee_currency": "KRW",
        "tax_amount": None,
        "filled_at_basis": None,
        "raw_payload_json": None,
    }
    base.update(values)
    return ns(**base)


def test_time_basis_is_the_weakest_and_legacy_rows_are_read_by_source() -> None:
    precise = ledger_fill(filled_at_basis="broker_fill_time")
    order_time = ledger_fill(filled_at_basis="broker_order_time")
    assert weakest_time_basis([precise]) == "broker_fill_time"
    assert weakest_time_basis([precise, order_time]) == "broker_order_time"
    assert (
        weakest_time_basis([order_time, ledger_fill(filled_at_basis="observed_at")])
        == "observed_at"
    )
    assert fill_time_basis(ledger_fill()) == "broker_order_time"  # KIS reconciler
    websocket = ledger_fill(source="websocket")
    assert fill_time_basis(websocket) is None
    assert weakest_time_basis([precise, websocket]) is None
    assert weakest_time_basis([]) is None


def costs_of(*fills):
    costs, includes_tax = trade_costs(fills, KR)
    return costs_body(costs, fee_includes_tax=includes_tax)


def test_costs_never_report_a_partial_sum() -> None:
    known = ledger_fill(fee_amount=Decimal("2"), tax_amount=Decimal("1"))
    unknown = ledger_fill(fee_basis="not_provided", fee_amount=None)
    both = costs_of(known, known)
    assert both["recorded"]["fee"] == "4" and both["recorded"]["tax"] == "2"
    mixed = costs_of(known, unknown)
    assert mixed["recorded"] is None and mixed["recorded_reason"] == "not_provided"


def test_costs_flag_pre_split_toss_fees_and_estimate_on_the_entry_leg() -> None:
    legacy_toss = ledger_fill(broker="toss", fee_basis=None, fee_amount=Decimal("8"))
    body = costs_of(legacy_toss)
    assert body["recorded"]["fee"] == "8"
    assert body["recorded"]["fee_includes_tax"] is True
    buy = ledger_fill(filled_notional=Decimal("1000"))
    sell = ledger_fill(side="sell", filled_notional=Decimal("1100"))
    with_both = costs_of(buy, sell)
    only_sell = costs_of(sell)
    # a round trip is estimated once, on the entry notional
    assert with_both["estimated"]["amount"] == "5"  # 1000 * 50 bps
    # no recorded entry fill: the exit proceeds are not an entry notional
    assert only_sell["estimated"] is None
    assert only_sell["estimated_reason"] == "missing_notional"


def test_a_missing_fee_leaves_no_recorded_cost_even_when_the_tax_is_known() -> None:
    body = costs_of(
        ledger_fill(fee_basis="not_provided", fee_amount=None, tax_amount=Decimal("1"))
    )
    assert body["recorded"] is None and body["recorded_reason"] == "not_provided"
    assert body["estimated"]["amount"] == "5"


def test_costs_with_unknown_or_mixed_fee_currencies_are_withheld() -> None:
    # a fill whose fee_currency was never recorded: the fee is not a KRW fee
    missing = costs_of(ledger_fill(fee_currency=None))
    assert missing["recorded"] is None
    assert missing["recorded_reason"] == "missing_currency"
    # two recorded fees in different currencies cannot be added together
    mixed = costs_of(ledger_fill(), ledger_fill(fee_currency="USD"))
    assert mixed["recorded"] is None
    assert mixed["recorded_reason"] == "missing_currency"
    # the trade currency is no stand-in for a missing fee currency either
    foreign = costs_of(ledger_fill(currency="USD"))
    assert foreign["recorded"]["currency"] == "KRW"


# --------------------------------------------------------------------------
# review hardening: incomplete evidence, brokers without a KIS day, time basis
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_incomplete_order_evidence_withholds_fills_derived_fields() -> None:
    # one order ledger fails while another still returns the order and its fill
    source = fills_session(
        fill(),
        **{
            "FROM review.kis_live_order_ledger": operational_error(),
            "FROM review.live_order_ledger": [order()],
        },
    )
    result = await read(source)
    row = result["data"][0]
    assert "costs" not in row and "fill_time_basis" not in row


@pytest.mark.asyncio
async def test_capped_order_read_withholds_fills_derived_fields(monkeypatch) -> None:
    monkeypatch.setattr(trade_evidence, "MAX_SOURCE_ROWS", 2)
    orders = [order(id_=100), order(id_=101, idempotency_key="idem-10")]
    # two orders hit the cap of 2, one fill does not
    result = await read(
        fills_session(fill(), **{"FROM review.kis_live_order_ledger": orders})
    )
    row = result["data"][0]
    assert "costs" not in row and "fill_time_basis" not in row
    assert {
        "field": "source.trade_kis_orders",
        "reason": "incomplete_evidence",
    } in result["unknowns"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["failed", "capped"])
async def test_incomplete_reviews_withhold_every_new_field(
    failure, monkeypatch
) -> None:
    reviews = [
        kr_review(301, decision_id=DECISION_A),
        kr_review(302, decision_id=DECISION_B),
    ]
    source = fills_session(fill())
    if failure == "failed":
        source.routes["FROM review.trade_retrospectives"] = operational_error()
    else:
        source.routes["FROM review.trade_retrospectives"] = reviews
        monkeypatch.setattr("app.services.retro_v1.retro_trades.MAX_SOURCE_ROWS", 2)
    result = await read(source)
    row = result["data"][0]
    assert not NEW_FIELDS & row.keys()
    assert result["summary"]["pnl_coverage"] is None


@pytest.mark.asyncio
async def test_unavailable_attempts_give_no_derived_decision() -> None:
    routes = {
        "FROM review.kis_live_order_ledger": [order()],
        "FROM review.decision_order_links": operational_error(),
    }
    result = await read(
        session([kr_journal()], [kr_review(correlation_id="corr-1")], **routes)
    )
    row = result["data"][0]
    assert row["link_reason"] == "attempts_unavailable"
    assert "decision_ref" not in row


@pytest.mark.asyncio
async def test_upbit_fills_after_kst_midnight_still_belong_to_the_order() -> None:
    placed = FILL_AT.replace(hour=14, minute=50)  # 23:50 KST
    after_midnight = FILL_AT.replace(hour=15, minute=10)  # 00:10 KST next day
    common = {
        "broker": "upbit",
        "instrument_type": "crypto",
        "broker_order_id": "uuid-1",
        "fee_amount": Decimal("5"),
        "fee_currency": "KRW",
        "filled_notional": Decimal("1000"),
    }
    fills = [
        ledger(
            1,
            placed,
            **{
                **common,
                "symbol": "SAMPLE",
                "raw_symbol": "SAMPLE",
                "fill_seq": 1,
                "fee_basis": "broker_reported",
                "tax_amount": None,
                "filled_at_basis": "broker_fill_time",
            },
        ),
        ledger(
            2,
            after_midnight,
            **{
                **common,
                "symbol": "SAMPLE",
                "raw_symbol": "SAMPLE",
                "fill_seq": 2,
                "fee_basis": "broker_reported",
                "tax_amount": None,
                "filled_at_basis": "broker_fill_time",
            },
        ),
    ]
    upbit_order = live_order(
        id_=100,
        at=placed,
        broker="upbit",
        symbol="SAMPLE",
        order_no="uuid-1",
        correlation_id="corr-1",
        idempotency_key="oprop-1",
        status="filled",
        filled_qty=Decimal("6"),
    )
    source = session(
        [journal(101, InstrumentType.crypto, correlation_id="corr-1")],
        [kr_review()],
        **{
            "FROM review.live_order_ledger": [upbit_order],
            "FROM review.execution_ledger": fills,
        },
    )
    row = (await read(source))["data"][0]
    assert row["costs"]["recorded"]["fee"] == "10"
    assert row["fill_time_basis"] == "broker_fill_time"


@pytest.mark.asyncio
async def test_websocket_fill_times_are_used_only_when_they_cover_the_order() -> None:
    tap = {
        "source": "websocket",
        "fill_seq": 7,
        "filled_at_basis": "broker_fill_time",
        "fee_basis": None,
        "fee_amount": None,
        "tax_amount": None,
    }
    complete = await read(
        fills_session(fill(1), fill(2, **tap, filled_qty=Decimal("3")))
    )
    assert complete["data"][0]["fill_time_basis"] == "broker_fill_time"
    short = await read(fills_session(fill(1), fill(2, **tap, filled_qty=Decimal("2"))))
    assert short["data"][0]["fill_time_basis"] == "broker_order_time"
    unlabelled = await read(
        fills_session(
            fill(1),
            fill(2, **{**tap, "filled_at_basis": None}, filled_qty=Decimal("3")),
        )
    )
    assert unlabelled["data"][0]["fill_time_basis"] == "broker_order_time"
    # the costs still come from the authoritative row only
    assert complete["data"][0]["costs"]["recorded"]["fee"] == "12"


@pytest.mark.asyncio
async def test_a_duplicated_websocket_fill_is_not_coverage_of_another_fill() -> None:
    # ROB-1379: the same fill can be written under two fill_seq values; two
    # copies of a 3-unit fill do not cover a 6-unit authoritative fill.
    tap = {
        "source": "websocket",
        "filled_at_basis": "broker_fill_time",
        "fee_basis": None,
        "fee_amount": None,
        "tax_amount": None,
        "filled_qty": Decimal("3"),
    }
    routes = {
        "FROM review.kis_live_order_ledger": [order(filled_qty=Decimal("6"))],
        "FROM review.execution_ledger": [
            fill(1, filled_qty=Decimal("6")),
            fill(2, **tap, fill_seq=7),
            fill(3, **{**tap, "fill_seq": 8}),
        ],
    }
    result = await read(
        session(
            [kr_journal(correlation_id="corr-1")],
            [kr_review()],
            **routes,
        )
    )
    assert result["data"][0]["fill_time_basis"] == "broker_order_time"
    # one distinct websocket row covering the order still wins
    routes["FROM review.execution_ledger"] = [
        fill(1, filled_qty=Decimal("6")),
        fill(2, **{**tap, "filled_qty": Decimal("6")}, fill_seq=7),
    ]
    result = await read(
        session(
            [kr_journal(correlation_id="corr-1")],
            [kr_review()],
            **routes,
        )
    )
    assert result["data"][0]["fill_time_basis"] == "broker_fill_time"


@pytest.mark.asyncio
async def test_an_order_without_fills_withholds_trade_costs_and_time_basis() -> None:
    # a closed round trip whose exit order's fills were never written: the
    # entry leg alone is not the trade's recorded cost.
    buy = order(order_no="0000123456", side="buy")
    sell = order(
        id_=101,
        order_no="0000654321",
        side="sell",
        correlation_id="corr-sell",
        filled_qty=Decimal("3"),
    )
    source = session(
        [kr_journal(correlation_id="corr-1")],
        [kr_review(correlation_id="corr-sell")],
        **{
            "FROM review.kis_live_order_ledger": [buy, sell],
            "FROM review.execution_ledger": [fill()],
        },
    )
    row = (await read(source))["data"][0]
    assert "costs" not in row and "fill_time_basis" not in row
    # an order whose recorded fill total differs from the ledger's also fails
    buy = order(order_no="0000123456", side="buy", filled_qty=Decimal("6"))
    source.routes["FROM review.kis_live_order_ledger"] = [buy]
    row = (await read(source))["data"][0]
    assert "costs" not in row and "fill_time_basis" not in row


def test_toss_cumulative_notional_is_reconstructed_or_unavailable() -> None:
    def toss_fill(prev: Decimal, cum: Decimal, avg: Decimal):
        return ledger_fill(
            broker="toss",
            filled_qty=cum - prev,
            filled_price=avg,
            filled_notional=(cum - prev) * avg,
            raw_payload_json={
                "previous_filled_qty": str(prev),
                "broker_cumulative_filled_qty": str(cum),
            },
        )

    rows = [
        toss_fill(Decimal(0), Decimal(1), Decimal(100)),
        toss_fill(Decimal(1), Decimal(2), Decimal(150)),
    ]
    body = costs_of(*rows)
    # the entry notional is 2 * 150 = 300, not the ledger rows' 100 + 150
    assert body["estimated"]["amount"] == "1.5"
    # a gap in the cumulative chain means some fills were missed
    broken = costs_of(toss_fill(Decimal(1), Decimal(2), Decimal(150)))
    assert broken["estimated"] is None
    assert broken["estimated_reason"] == "missing_notional"
    # a malformed payload is unvalidated data, not a crash
    for bad in ({"nested": "bad"}, None):
        malformed = ledger_fill(
            broker="toss",
            raw_payload_json={
                "previous_filled_qty": "0",
                "broker_cumulative_filled_qty": bad,
            },
        )
        body = costs_of(malformed)
        assert body["estimated"] is None
        assert body["estimated_reason"] == "missing_notional"


@pytest.mark.asyncio
async def test_websocket_only_duplicates_do_not_satisfy_coverage() -> None:
    # no reconciler row yet: fillwire and the monitor can record the same
    # fill under two fill_seq values, and the doubled quantity must not
    # pass for the order's real filled total
    tap = {
        "source": "websocket",
        "broker_order_id": ORDER_NO,
        "filled_at_basis": "broker_fill_time",
        "fee_basis": None,
        "fee_amount": None,
        "tax_amount": None,
        "filled_qty": Decimal("3"),
        "filled_notional": Decimal("210000"),
        "fee_currency": None,
    }
    routes = {
        "FROM review.kis_live_order_ledger": [order(filled_qty=Decimal("6"))],
        "FROM review.execution_ledger": [
            fill(1, **tap, fill_seq=7),
            fill(2, **{**tap, "fill_seq": 8}),
        ],
    }
    row = (
        await read(
            session([kr_journal(correlation_id="corr-1")], [kr_review()], **routes)
        )
    )["data"][0]
    assert "costs" not in row and "fill_time_basis" not in row
