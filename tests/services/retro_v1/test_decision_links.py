"""hk 4188: link_basis / decision_ref on chain rows (design #4154 §3 rows 1, 4, Q20)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from app.services.retro_v1 import links as links_module
from app.services.retro_v1 import read_retro_day, read_retro_funnel, read_retro_symbol
from app.services.retro_v1.envelope import make_ref
from app.services.retro_v1.links import (
    AttemptResolution,
    OrderLinkEvidence,
    decision_ref,
    resolve_attempts,
)
from tests.services.retro_v1.support import (
    FILL_AT,
    kr_day_session,
    live_order,
    ns,
    operational_error,
    pick,
    proposal,
    rung,
    validate,
)

pytestmark = pytest.mark.unit

DAY = date(2026, 1, 5)
GOLDEN = Path(__file__).parent / "golden" / "legacy_main_4188.json"
#: Everything hk 4188 added to a chain row; all optional.
NEW_FIELDS = {"link_basis", "decision_ref", "decision_link_basis", "link_reason"}

PROPOSAL_A, PROPOSAL_B = UUID(int=0xA), UUID(int=0xB)
DECISION_A, DECISION_B = UUID(int=0xDA), UUID(int=0xDB)
CALL = UUID(int=0xCA11)
SEND_KEY = "idem-100"


def without_new(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in NEW_FIELDS}


def row_of(response: dict, stage: str) -> dict:
    return next(r for r in response["data"] if r["stage"] == stage)


# --------------------------------------------------------------------------
# rows without new data differ from main only by the optional fields
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_legacy_rows_differ_from_main_only_by_optional_link_fields() -> None:
    golden = json.loads(GOLDEN.read_text())
    day = await read_retro_day(kr_day_session(), market="kr", date=DAY)
    symbol = await read_retro_symbol(
        kr_day_session(), symbol="005930", from_=date(2026, 1, 1), to=DAY
    )
    for name, response in (("retro_day", day), ("retro_symbol", symbol)):
        validate(
            response,
            "RetroDayResponse" if name == "retro_day" else "RetroSymbolResponse",
        )
        expected = golden[name]
        assert [without_new(r) for r in response["data"]] == expected["data"]
        assert response["unknowns"] == expected["unknowns"]
        assert response["total"] == expected["total"]
    # What the legacy rules now say about how each row was linked.
    assert {r["stage"]: r.get("link_basis") for r in day["data"]} == {
        "candidate": None,
        "proposal": "inference",
        "rung": "recorded",
        "order": "recorded",
        "fill": "recorded",
        "markout": "recorded",
        "lot": "inference",
    }
    assert not any(
        "decision_ref" in r or "link_reason" in r or "decision_link_basis" in r
        for r in day["data"]
    )


@pytest.mark.asyncio
async def test_funnel_does_not_read_the_decision_tables() -> None:
    session = kr_day_session()
    await read_retro_funnel(session, market="kr", from_=DAY, to=DAY)
    assert not any("review.decision_" in sql for sql in session.statements)


# --------------------------------------------------------------------------
# §3 row 4: the attempt rule, as a pure function
# --------------------------------------------------------------------------
def attempt(
    n: int, decision: UUID, proposal_id: UUID | None, rung_index: int | None = 0
):
    return ns(
        attempt_id=UUID(int=n),
        decision_id=decision,
        account_scope="kis_live",
        send_key=SEND_KEY,
        proposal_id=proposal_id,
        rung_index=rung_index if proposal_id is not None else None,
    )


A1 = attempt(1, DECISION_A, PROPOSAL_A)
A2 = attempt(2, DECISION_B, PROPOSAL_B)


def test_resolution_rule_cases() -> None:
    assert resolve_attempts([], {}).kind == "none"
    # every attempt proven not sent: as if there were no link rows
    assert resolve_attempts([A1], {A1.attempt_id: "proven_not_sent"}).kind == "none"
    # two decisions with no proof for either
    assert resolve_attempts([A1, A2], {}).kind == "ambiguous"
    assert resolve_attempts([A1, A2], {A2.attempt_id: "sent"}).kind == "ambiguous", (
        "a sent outcome of one attempt does not settle another live attempt"
    )
    # one decision: recorded only with a sent outcome
    unconfirmed = resolve_attempts([A1], {})
    assert unconfirmed == AttemptResolution("unconfirmed", DECISION_A, PROPOSAL_A, 0)
    sent = resolve_attempts([A1], {A1.attempt_id: "sent"})
    assert sent == AttemptResolution("recorded", DECISION_A, PROPOSAL_A, 0)
    # a retry of the same decision is not ambiguous
    retry = attempt(3, DECISION_A, PROPOSAL_A)
    assert resolve_attempts([A1, retry], {retry.attempt_id: "sent"}).kind == "recorded"


def test_resolution_of_attempts_without_one_rung_binding_keeps_the_decision() -> None:
    direct = attempt(4, DECISION_A, None)
    both = resolve_attempts([A1, direct], {})
    assert both.kind == "unconfirmed" and both.decision_id == DECISION_A
    assert both.proposal_id is None and both.rung_index is None


def test_order_link_evidence_resolves_by_scope_and_key() -> None:
    evidence = OrderLinkEvidence(
        {("kis_live", SEND_KEY): [A1]}, {A1.attempt_id: "sent"}
    )
    kis = live_order(idempotency_key=SEND_KEY)
    assert evidence.resolve(kis).kind == "recorded"
    other_scope = live_order(idempotency_key=SEND_KEY, broker="upbit")
    assert evidence.resolve(other_scope).kind == "none"
    assert evidence.resolve(live_order(idempotency_key=None)).kind == "none"


# --------------------------------------------------------------------------
# §3 row 4 end to end: the L4 orders (a) (b) (c)
# --------------------------------------------------------------------------
def two_proposal_session(attempts, outcomes):
    """A and B carry the same content and so the same KIS send key; only B's
    rung ever got a broker order number (A was held back before sending)."""
    return kr_day_session(
        **{
            "FROM review.order_proposals ": [
                proposal(
                    10,
                    "005930",
                    datetime(2026, 1, 5, 1, 0, tzinfo=UTC),
                    proposal_id=PROPOSAL_A,
                    decision_id=DECISION_A,
                ),
                proposal(
                    11,
                    "005930",
                    datetime(2026, 1, 5, 1, 1, tzinfo=UTC),
                    proposal_id=PROPOSAL_B,
                    decision_id=DECISION_B,
                ),
            ],
            "FROM review.order_proposal_rungs": [
                rung(100, 10, idempotency_key=None, broker_order_id=None, state="void"),
                rung(101, 11, idempotency_key=SEND_KEY),
            ],
            "FROM review.order_send_intents": [
                (SEND_KEY, FILL_AT - timedelta(hours=1))
            ],
            "FROM review.decision_order_links": attempts,
            "FROM review.decision_order_link_outcomes": [
                ns(attempt_id=a.attempt_id, outcome=o) for a, o in outcomes
            ],
        }
    )


@pytest.mark.asyncio
async def test_a_blocked_first_proposal_reads_the_sent_one_as_recorded() -> None:
    session = two_proposal_session([A1, A2], [(A1, "proven_not_sent"), (A2, "sent")])
    response = await read_retro_day(session, market="kr", date=DAY)
    validate(response, "RetroDayResponse")
    order = row_of(response, "order")
    assert order["linked_ref"] == make_ref("rung", 101)
    assert order["link_basis"] == "recorded"
    assert order["decision_ref"] == decision_ref(DECISION_B)
    assert order["decision_link_basis"] == "recorded"
    assert "link_reason" not in order


@pytest.mark.asyncio
async def test_b_a_lost_proven_not_sent_row_makes_the_order_ambiguous() -> None:
    session = two_proposal_session([A1, A2], [(A2, "sent")])
    response = await read_retro_day(session, market="kr", date=DAY)
    validate(response, "RetroDayResponse")
    order = row_of(response, "order")
    # no link at all, not even the legacy key match to B's rung
    assert order["linked_ref"] is None
    assert not {"link_basis", "decision_ref", "decision_link_basis"} & order.keys()
    assert order["link_reason"] == "ambiguous_attempts"


@pytest.mark.asyncio
async def test_c_combined_write_failure_is_an_unconfirmed_inference() -> None:
    # A's proven_not_sent and B's link row were both lost; B was sent anyway.
    session = two_proposal_session([A1], [])
    response = await read_retro_day(session, market="kr", date=DAY)
    validate(response, "RetroDayResponse")
    order = row_of(response, "order")
    assert order["linked_ref"] == make_ref("rung", 100)
    assert order["link_basis"] == "inference"
    assert order["decision_ref"] == decision_ref(DECISION_A)
    assert order["decision_link_basis"] == "inference"
    assert order["link_reason"] == "attempt_unconfirmed"


@pytest.mark.asyncio
async def test_direct_order_has_a_decision_but_no_rung() -> None:
    session = kr_day_session(
        **{
            "FROM review.order_proposals ": [],
            "FROM review.order_proposal_rungs": [],
            "FROM review.order_send_intents": [],
            "FROM review.decision_order_links": [attempt(5, DECISION_A, None)],
            "FROM review.decision_order_link_outcomes": [
                ns(attempt_id=UUID(int=5), outcome="sent")
            ],
        }
    )
    response = await read_retro_day(session, market="kr", date=DAY)
    validate(response, "RetroDayResponse")
    order = row_of(response, "order")
    assert order["linked_ref"] is None and "link_basis" not in order
    assert order["decision_ref"] == decision_ref(DECISION_A)
    assert order["decision_link_basis"] == "recorded"


@pytest.mark.asyncio
async def test_legacy_correlation_only_match_is_an_inference_with_the_proposal_decision() -> (
    None
):
    session = kr_day_session(
        **{
            "FROM review.order_proposals ": [
                proposal(
                    10,
                    "005930",
                    datetime(2026, 1, 5, 1, 0, tzinfo=UTC),
                    proposal_id=PROPOSAL_A,
                    decision_id=DECISION_A,
                )
            ],
            "FROM review.order_proposal_rungs": [
                rung(100, 10, idempotency_key="other-key", correlation_id="corr-1")
            ],
            "FROM review.order_send_intents": [
                ("other-key", FILL_AT - timedelta(hours=1))
            ],
            "FROM review.kis_live_order_ledger": [
                live_order(idempotency_key="idem-100", correlation_id="corr-1")
            ],
        }
    )
    response = await read_retro_day(session, market="kr", date=DAY)
    order = row_of(response, "order")
    assert order["linked_ref"] == make_ref("rung", 100)
    assert order["link_basis"] == "inference"
    assert order["decision_ref"] == decision_ref(DECISION_A)
    assert order["decision_link_basis"] == "inference"
    rung_row = row_of(response, "rung")
    assert rung_row["link_basis"] == "recorded"
    assert rung_row["decision_ref"] == decision_ref(DECISION_A)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["attempts_failed", "attempts_capped", "outcomes_failed"]
)
async def test_unreadable_attempts_give_no_link_and_no_legacy_fallback(
    failure, monkeypatch
) -> None:
    if failure == "attempts_failed":
        session = kr_day_session(
            **{"FROM review.decision_order_links": operational_error()}
        )
        field = "source.decision_links"
    elif failure == "attempts_capped":
        monkeypatch.setattr(links_module, "_LINK_ROWS_CAP", 2)
        # a surviving sent attempt would read as recorded; the cut-off one
        # (another decision) is not in the result
        session = two_proposal_session([A1, A2], [(A2, "sent")])
        field = "source.decision_links"
    else:
        session = two_proposal_session([A1], [])
        session.routes["FROM review.decision_order_link_outcomes"] = operational_error()
        field = "source.decision_link_outcomes"
    response = await read_retro_day(session, market="kr", date=DAY)
    validate(response, "RetroDayResponse")
    assert any(u["field"] == field for u in response["unknowns"])
    order = row_of(response, "order")
    # the legacy idempotency-key match would have linked this order to a rung
    assert order["linked_ref"] is None
    assert not {"link_basis", "decision_ref", "decision_link_basis"} & order.keys()
    assert order["link_reason"] == "attempts_unavailable"
    assert response["total"] is not None, "a link read is not part of the population"


# --------------------------------------------------------------------------
# §3 row 1 and Q20: candidate -> proposal
# --------------------------------------------------------------------------
def kst(day: int, hour: int, minute: int) -> datetime:
    """A January 2026 KST wall time as an instant."""
    return datetime(2026, 1, day, hour, minute, tzinfo=UTC) - timedelta(hours=9)


def candidate_session(picks, proposals, decisions=(), ref_picks=()):
    def pick_route(sql: str):
        return list(ref_picks) if "call_id IN" in sql else list(picks)

    return kr_day_session(
        **{
            "FROM review.screener_pick_log": pick_route,
            "FROM review.order_proposals ": proposals,
            "FROM review.order_proposal_rungs": [],
            "FROM review.order_send_intents": [],
            "FROM review.kis_live_order_ledger": [],
            "FROM review.decision_evidence": list(decisions),
        }
    )


async def proposal_row(session) -> dict:
    response = await read_retro_symbol(
        session, symbol="005930", from_=date(2026, 1, 4), to=DAY
    )
    validate(response, "RetroSymbolResponse")
    return row_of(response, "proposal")


@pytest.mark.asyncio
async def test_legacy_inference_stops_at_the_proposals_kst_day() -> None:
    proposed = [proposal(10, "005930", kst(5, 0, 10))]
    # 23:50 KST the evening before: an earlier candidate, but another day
    row = await proposal_row(
        candidate_session([pick(1, "005930", kst(4, 23, 50))], proposed)
    )
    assert row["linked_ref"] is None
    assert row["link_reason"] == "candidate_outside_trading_day"
    assert "link_basis" not in row
    # 00:05 KST the same day: inferred as before
    row = await proposal_row(
        candidate_session([pick(2, "005930", kst(5, 0, 5))], proposed)
    )
    assert row["linked_ref"] == make_ref("candidate", 2)
    assert row["link_basis"] == "inference" and "link_reason" not in row
    # the latest candidate of the same day wins over an older day's
    row = await proposal_row(
        candidate_session(
            [
                pick(3, "005930", kst(4, 9, 0)),
                pick(4, "005930", kst(5, 0, 1)),
                pick(5, "005930", kst(5, 0, 3)),
            ],
            proposed,
        )
    )
    assert row["linked_ref"] == make_ref("candidate", 5)
    # no earlier candidate at all
    row = await proposal_row(candidate_session([], proposed))
    assert row["link_reason"] == "no_candidate_ref"


@pytest.mark.asyncio
async def test_stored_candidate_ref_is_recorded_without_a_day_cap() -> None:
    decided = [proposal(10, "005930", kst(5, 12, 0), decision_id=DECISION_A)]
    older = pick(7, "005930", kst(1, 9, 0), call_id=CALL)
    other = pick(1, "005930", kst(5, 9, 0), call_id=UUID(int=1))
    decision = ns(
        decision_id=DECISION_A,
        candidate_call_id=CALL,
        candidate_symbol="005930",
        candidate_link_basis="recorded",
    )
    row = await proposal_row(
        candidate_session([other], decided, [decision], ref_picks=[older])
    )
    assert row["linked_ref"] == make_ref("candidate", 7)
    assert row["link_basis"] == "recorded"
    assert row["decision_ref"] == decision_ref(DECISION_A)
    assert row["decision_link_basis"] == "recorded"


@pytest.mark.asyncio
async def test_writer_labelled_inference_is_not_upgraded_or_redone() -> None:
    decided = [proposal(10, "005930", kst(5, 12, 0), decision_id=DECISION_A)]
    named = pick(7, "005930", kst(5, 9, 0), call_id=CALL)
    decision = ns(
        decision_id=DECISION_A,
        candidate_call_id=CALL,
        candidate_symbol="005930",
        candidate_link_basis="inference",
    )
    row = await proposal_row(candidate_session([named], decided, [decision]))
    assert row["link_basis"] == "inference"
    # a decision without a candidate reference is not re-inferred by the reader
    bare = ns(
        decision_id=DECISION_A,
        candidate_call_id=None,
        candidate_symbol=None,
        candidate_link_basis=None,
    )
    row = await proposal_row(candidate_session([named], decided, [bare]))
    assert row["linked_ref"] is None and row["link_reason"] == "no_candidate_ref"
    assert row["decision_ref"] == decision_ref(DECISION_A)


@pytest.mark.asyncio
async def test_unreadable_candidate_reference_is_not_guessed() -> None:
    decided = [proposal(10, "005930", kst(5, 12, 0), decision_id=DECISION_A)]
    same_day = pick(7, "005930", kst(5, 9, 0))
    # the decision row is missing
    row = await proposal_row(candidate_session([same_day], decided, []))
    assert row["linked_ref"] is None
    assert row["link_reason"] == "candidate_ref_unresolved"
    # the decision names a call whose pick row cannot be found
    gone = ns(
        decision_id=DECISION_A,
        candidate_call_id=CALL,
        candidate_symbol="005930",
        candidate_link_basis="recorded",
    )
    row = await proposal_row(candidate_session([same_day], decided, [gone]))
    assert row["linked_ref"] is None
    assert row["link_reason"] == "candidate_ref_unresolved"
