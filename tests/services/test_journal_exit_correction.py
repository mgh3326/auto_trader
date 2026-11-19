"""hk 5357 — correction of exit fields on lots closed by a late reconcile.

Fake-session tests: the database reads are replaced by in-memory rows so the
rules, the preview/commit contract and the no-write guarantees run without a
database. The SQL reader is covered by ``test_journal_exit_correction_db.py``.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.trade_journal import TradeJournal
from app.models.trading import InstrumentType
from app.services import journal_exit_correction as jec

pytestmark = pytest.mark.unit

REASON = "hk 5357: closed by a reconcile before hk 5332"
ACTOR = "operator"
BOOKED = datetime(2026, 11, 19, 0, 52, tzinfo=UTC)  # 09:52 KST booking
FILLED = datetime(2026, 11, 18, 7, 9, 45, tzinfo=UTC)  # 16:09:45 KST fill
CREATED = datetime(2026, 11, 10, 1, 0, tzinfo=UTC)


def journal(jid: int, **over: Any) -> TradeJournal:
    data: dict[str, Any] = {
        "id": jid,
        "symbol": "041510",
        "instrument_type": InstrumentType.equity_kr,
        "side": "buy",
        "status": "closed",
        "account": "kis",
        "account_type": "live",
        "entry_price": Decimal("82200"),
        "quantity": Decimal("2"),
        "exit_price": Decimal("91000"),
        "exit_date": BOOKED,
        "total_pnl_krw": None,
        "pnl_pct": Decimal("10.7056"),
        "notes": None,
        "created_at": CREATED,
    }
    data.update(over)
    return TradeJournal(**data)


def ledger(lid: int, **over: Any) -> SimpleNamespace:
    data: dict[str, Any] = {
        "id": lid,
        "broker": "kis",
        "account_mode": "live",
        "venue": "krx",
        "broker_order_id": f"ORD{lid}",
        "fill_seq": 0,
        "instrument_type": InstrumentType.equity_kr,
        "symbol": "041510",
        "side": "sell",
        "currency": "KRW",
        "source": "reconciler",
        "quarantined_at": None,
        "filled_qty": Decimal("2"),
        "filled_price": Decimal("91000"),
        "filled_at": FILLED,
        "filled_at_basis": "broker_fill_time",
    }
    data.update(over)
    return SimpleNamespace(**data)


def evidence(jid: int, row: SimpleNamespace, **over: Any) -> jec.Evidence:
    data: dict[str, Any] = {
        "journal_id": jid,
        "evidence_ref": f"hk:doc/reconcile-result-{row.id}",
        "record_order_id": row.broker_order_id,
        "record_closed_journal_ids": (jid,),
        "venue": row.venue,
        "broker_order_id": row.broker_order_id,
        "fill_seq": row.fill_seq,
        "allocated_quantity": Decimal("2"),
    }
    data.update(over)
    return jec.Evidence(**data)


def three() -> tuple[
    dict[int, TradeJournal], dict[int, SimpleNamespace], dict[int, jec.Evidence]
]:
    """Journals shaped like 243 (Toss 1), 244 (KIS 30) and 245 (KIS 2)."""
    journals = {
        243: journal(243, account="toss", quantity=Decimal("1")),
        244: journal(
            244,
            symbol="012030",
            entry_price=Decimal("1500"),
            quantity=Decimal("30"),
            exit_price=Decimal("1620"),
        ),
        245: journal(245),
    }
    rows = {
        243: ledger(
            501,
            broker="toss",
            filled_qty=Decimal("1"),
            filled_at=datetime(2026, 11, 18, 7, 14, 14, tzinfo=UTC),
        ),
        244: ledger(
            271,
            symbol="012030",
            filled_qty=Decimal("30"),
            filled_price=Decimal("1620"),
        ),
        245: ledger(272),
    }
    ev = {
        243: evidence(243, rows[243], allocated_quantity=Decimal("1")),
        244: evidence(244, rows[244], allocated_quantity=Decimal("30")),
        245: evidence(245, rows[245]),
    }
    return journals, rows, ev


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, stmt: Any) -> None:
        self.calls.append(f"execute:{stmt}")

    async def flush(self) -> None:
        self.calls.append("flush")

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")


@pytest.fixture
def world(monkeypatch):
    journals, rows, ev = three()

    async def load_journals(session, ids, *, lock):
        session.calls.append(f"load_journals:lock={lock}")
        return {i: journals[i] for i in ids if i in journals}

    async def load_order_fills(session, j, evidence_item):
        row = rows.get(int(j.id))
        return [] if row is None else [row]

    monkeypatch.setattr(jec, "load_journals", load_journals)
    monkeypatch.setattr(jec, "load_order_fills", load_order_fills)
    return SimpleNamespace(journals=journals, rows=rows, ev=ev)


def snapshot(journals: dict[int, TradeJournal]) -> dict[int, tuple]:
    return {
        i: (j.exit_date, j.total_pnl_krw, j.exit_price, j.pnl_pct, j.notes)
        for i, j in journals.items()
    }


IDS = [243, 244, 245]

# 1. preview shows new values and writes nothing -----------------------------


@pytest.mark.asyncio
async def test_preview_prints_new_values_and_writes_nothing(world) -> None:
    before = snapshot(world.journals)
    session = FakeSession()
    result = await jec.preview(session, IDS, world.ev)

    assert result.status == "eligible"
    by_id = {p.journal_id: p for p in result.plan.plans}
    assert by_id[243].new_exit_date == datetime(2026, 11, 18, 7, 14, 14, tzinfo=UTC)
    assert by_id[243].new_total_pnl_krw == Decimal("8800.0000")
    assert by_id[244].new_exit_date == FILLED
    assert by_id[244].new_total_pnl_krw == Decimal("3600.0000")
    assert by_id[245].new_exit_date == FILLED
    assert by_id[245].new_total_pnl_krw == Decimal("17600.0000")
    assert [p.ledger_id for p in result.plan.plans] == [501, 271, 272]
    assert all(p.status == "eligible" for p in result.plan.plans)
    assert snapshot(world.journals) == before
    assert "commit" not in session.calls and "flush" not in session.calls
    assert session.calls[0] == "execute:SET TRANSACTION READ ONLY"
    assert session.calls[-1] == "rollback"
    printed = result.as_dict()["journals"][2]
    assert printed["current"]["exit_date"] == BOOKED.isoformat()
    assert printed["new"] == {
        "exit_date": FILLED.isoformat(),
        "total_pnl_krw": "17600.0000",
    }
    assert printed["evidence"]["evidence_ref"] == "hk:doc/reconcile-result-272"


# 2. refusals -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row_over", "reason"),
    [
        ({"quarantined_at": BOOKED}, "ledger_row_not_in_effect_authoritative"),
        ({"source": "websocket"}, "ledger_row_not_in_effect_authoritative"),
        ({"broker": "toss"}, "ledger_row_not_in_effect_authoritative"),
        ({"account_mode": "mock"}, "ledger_row_not_in_effect_authoritative"),
        ({"venue": "nasd"}, "ledger_row_not_in_effect_authoritative"),
        ({"fill_seq": 3}, "ledger_row_not_in_effect_authoritative"),
        ({"symbol": "005930"}, "ledger_row_is_not_this_sell"),
        ({"side": "buy"}, "ledger_row_is_not_this_sell"),
        ({"currency": "USD"}, "ledger_row_is_not_this_sell"),
        ({"filled_qty": Decimal("1")}, "allocation_exceeds_fill"),
        ({"filled_price": Decimal("90000")}, "ledger_price_mismatch"),
        ({"filled_at": BOOKED + timedelta(minutes=1)}, "fill_after_booking"),
    ],
)
def test_ledger_row_rules(row_over, reason) -> None:
    row = ledger(9, **row_over)
    ev = evidence(1, ledger(9))  # the evidence names the identity of a clean row
    plan = jec.plan_journal(journal(1), ev, [row])
    assert (plan.status, plan.reason) == ("ineligible", reason)


def test_a_clean_row_with_clean_evidence_is_eligible() -> None:
    row = ledger(9)
    assert jec.plan_journal(journal(1), evidence(1, row), [row]).status == "eligible"


def test_no_ledger_row_is_refused() -> None:
    plan = jec.plan_journal(journal(1), evidence(1, ledger(9)), [])
    assert (plan.status, plan.reason) == ("ineligible", "ledger_row_not_found")


# execution time basis ----------------------------------------------------------

VERIFIED = datetime(2026, 11, 18, 7, 9, 45, tzinfo=UTC)


@pytest.mark.parametrize("basis", ["broker_order_time", "observed_at", None])
def test_only_a_broker_fill_time_is_an_execution_time(basis) -> None:
    """KIS reconciler rows carry the order time; it must not become the exit time."""
    row = ledger(9, filled_at_basis=basis, filled_at=FILLED - timedelta(hours=5))
    plan = jec.plan_journal(journal(1), evidence(1, row), [row])
    assert (plan.status, plan.reason) == (
        "ineligible",
        "fill_time_not_a_broker_fill_time",
    )


def test_a_verified_execution_time_can_stand_in_for_an_order_time_row() -> None:
    row = ledger(
        9, filled_at_basis="broker_order_time", filled_at=FILLED - timedelta(hours=5)
    )
    ev = evidence(
        1, row, verified_fill_at=VERIFIED, verified_fill_ref="hk:doc/fill-time"
    )
    plan = jec.plan_journal(journal(1), ev, [row])
    assert plan.status == "eligible"
    assert plan.new_exit_date == VERIFIED  # not the order time
    assert plan.fill_time_source == "evidence_verified"
    note = jec.audit_note(plan, reason=REASON, actor=ACTOR)
    assert "fill_time=evidence_verified" in note


def test_a_verified_time_cannot_contradict_a_broker_fill_time() -> None:
    row = ledger(9, filled_at_basis="broker_fill_time")
    ev = evidence(
        1, row, verified_fill_at=VERIFIED + timedelta(minutes=1), verified_fill_ref="r"
    )
    plan = jec.plan_journal(journal(1), ev, [row])
    assert plan.reason == "verified_fill_time_conflicts_with_ledger"
    same = evidence(1, row, verified_fill_at=row.filled_at, verified_fill_ref="r")
    assert jec.plan_journal(journal(1), same, [row]).status == "eligible"


def test_a_verified_time_after_the_booking_is_refused() -> None:
    row = ledger(9, filled_at_basis="broker_order_time")
    ev = evidence(
        1, row, verified_fill_at=BOOKED + timedelta(minutes=1), verified_fill_ref="r"
    )
    assert jec.plan_journal(journal(1), ev, [row]).reason == "fill_after_booking"


def test_the_ledger_time_is_the_exit_time_for_a_broker_fill_time_row() -> None:
    row = ledger(9)
    plan = jec.plan_journal(journal(1), evidence(1, row), [row])
    assert plan.fill_time_source == "ledger_broker_fill_time"
    assert plan.new_exit_date == FILLED


# an order with more than one authoritative fill -----------------------------------


@pytest.mark.parametrize("cited_seq", [0, 1])
def test_an_order_with_two_authoritative_fills_is_refused_whichever_is_cited(
    cited_seq,
) -> None:
    """Same order, same price, enough quantity: the evidence cannot pick the fill."""
    first = ledger(9, broker_order_id="ORD9", fill_seq=0)
    second = ledger(
        10, broker_order_id="ORD9", fill_seq=1, filled_at=FILLED + timedelta(minutes=1)
    )
    cited = first if cited_seq == 0 else second
    plan = jec.plan_journal(journal(1), evidence(1, cited), [first, second])
    assert (plan.status, plan.reason) == ("ineligible", "ambiguous_order_fills")


def test_siblings_that_are_not_authoritative_do_not_make_an_order_ambiguous() -> None:
    row = ledger(9, broker_order_id="ORD9")
    quarantined = ledger(10, broker_order_id="ORD9", fill_seq=1, quarantined_at=BOOKED)
    websocket = ledger(11, broker_order_id="ORD9", fill_seq=2, source="websocket")
    plan = jec.plan_journal(journal(1), evidence(1, row), [row, quarantined, websocket])
    assert plan.status == "eligible"


@pytest.mark.asyncio
async def test_an_ambiguous_order_blocks_the_whole_batch(world, monkeypatch) -> None:
    sibling = ledger(273, broker_order_id=world.rows[245].broker_order_id, fill_seq=1)

    async def load_order_fills(session, j, evidence_item):
        row = world.rows.get(int(j.id))
        return [row, sibling] if int(j.id) == 245 else [row]

    monkeypatch.setattr(jec, "load_order_fills", load_order_fills)
    prev = await jec.preview(FakeSession(), IDS, world.ev)
    assert prev.status == "refused"
    assert {p.journal_id: p.reason for p in prev.plan.plans}[
        245
    ] == "ambiguous_order_fills"
    before = snapshot(world.journals)
    result = await jec.commit(
        FakeSession(),
        IDS,
        world.ev,
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert result.status == "refused"
    assert snapshot(world.journals) == before


@pytest.mark.parametrize(
    ("ev_over", "reason"),
    [
        ({"record_closed_journal_ids": (7, 8)}, "evidence_does_not_list_journal"),
        ({"record_order_id": "OTHER"}, "evidence_order_mismatch"),
        ({"allocated_quantity": Decimal("1")}, "allocation_quantity_mismatch"),
    ],
)
def test_evidence_rules(ev_over, reason) -> None:
    row = ledger(9)
    plan = jec.plan_journal(journal(1), evidence(1, row, **ev_over), [row])
    assert (plan.status, plan.reason) == ("ineligible", reason)


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"status": "active"}, "journal_not_closed"),
        ({"account_type": "paper"}, "journal_not_live"),
        ({"side": "sell"}, "unsupported_journal_side"),
        ({"instrument_type": InstrumentType.equity_us}, "unsupported_instrument"),
        ({"instrument_type": InstrumentType.crypto}, "unsupported_instrument"),
        ({"account": "upbit"}, "unsupported_account"),
        ({"account": None}, "unsupported_account"),
        ({"quantity": None}, "journal_fields_missing"),
        ({"entry_price": None}, "journal_fields_missing"),
        ({"exit_price": None}, "journal_fields_missing"),
        ({"exit_date": None}, "journal_fields_missing"),
    ],
)
def test_journal_rules(over, reason) -> None:
    row = ledger(9)
    plan = jec.plan_journal(journal(1, **over), evidence(1, row), [row])
    assert (plan.status, plan.reason) == ("ineligible", reason)


# The reviewed attribution cases ------------------------------------------------


def test_a_fill_before_the_journal_was_created_can_be_the_closer() -> None:
    """A late buy journal is closed by an earlier 10-share sell (booked later).

    No time or quantity rule may hide it; the evidence names it and the P&L uses
    the allocated quantity, not the fill's.
    """
    closer = ledger(10, filled_qty=Decimal("10"), filled_at=CREATED - timedelta(days=2))
    j = journal(1, created_at=CREATED)
    plan = jec.plan_journal(
        j, evidence(1, closer, allocated_quantity=Decimal("2")), [closer]
    )
    assert plan.status == "eligible"
    assert plan.new_exit_date == CREATED - timedelta(days=2)
    assert plan.new_total_pnl_krw == Decimal("17600.0000")  # (91000-82200) * 2


def test_an_unrelated_sell_is_never_picked_by_quantity_price_or_time() -> None:
    """The unrelated 2-share sell matches by qty, price and time but is not evidence."""
    closer = ledger(10, filled_qty=Decimal("10"), filled_at=CREATED - timedelta(days=2))
    unrelated = ledger(11)
    j = journal(1)
    # evidence naming the real closer: the unrelated row is irrelevant
    ok = jec.plan_journal(j, evidence(1, closer), [closer])
    assert (ok.ledger_id, ok.new_exit_date) == (10, CREATED - timedelta(days=2))
    # a bare ledger id has no evidence entry and cannot be expressed at all
    with pytest.raises(jec.CorrectionInputError):
        jec.parse_evidence(json.dumps([{"journal_id": 1, "ledger_id": 11}]), [1])
    assert unrelated.id == 11


def test_one_fill_may_close_several_lots_within_what_it_booked() -> None:
    row = ledger(10, filled_qty=Decimal("10"))
    journals = {1: journal(1), 2: journal(2, quantity=Decimal("8"))}
    ev = {
        1: evidence(1, row, allocated_quantity=Decimal("2")),
        2: evidence(2, row, allocated_quantity=Decimal("8")),
    }
    batch = jec.plan_batch([1, 2], journals, ev, {1: [row], 2: [row]})
    assert batch.status == "eligible"
    assert [p.new_total_pnl_krw for p in batch.plans] == [
        Decimal("17600.0000"),
        Decimal("70400.0000"),
    ]
    over = {
        1: evidence(1, row, allocated_quantity=Decimal("2")),
        2: evidence(2, row, allocated_quantity=Decimal("9")),
    }
    journals[2].quantity = Decimal("9")
    refused = jec.plan_batch([1, 2], journals, over, {1: [row], 2: [row]})
    assert refused.status == "refused"
    assert {p.reason for p in refused.plans} == {"allocations_exceed_fill"}


@pytest.mark.parametrize("member", [243, 244, 245])
@pytest.mark.asyncio
async def test_one_bad_member_blocks_the_whole_batch(world, member) -> None:
    prev = await jec.preview(FakeSession(), IDS, world.ev)
    assert prev.status == "eligible"
    world.ev[member] = evidence(
        member, world.rows[member], record_closed_journal_ids=(999,)
    )
    before = snapshot(world.journals)
    session = FakeSession()
    result = await jec.commit(
        session,
        IDS,
        world.ev,
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert result.status == "refused"
    assert {p.journal_id: p.status for p in result.plan.plans}[member] == "ineligible"
    assert result.written == ()
    assert snapshot(world.journals) == before
    assert "commit" not in session.calls


@pytest.mark.parametrize(
    "mutate",
    [
        lambda w: w.rows.pop(245),  # no ledger row at all
        lambda w: setattr(w.rows[245], "quarantined_at", BOOKED),  # quarantined only
        lambda w: setattr(w.rows[245], "source", "websocket"),
    ],
)
@pytest.mark.asyncio
async def test_a_missing_or_unusable_ledger_row_leaves_the_batch_untouched(
    world, mutate
) -> None:
    mutate(world)
    before = snapshot(world.journals)
    session = FakeSession()
    prev = await jec.preview(FakeSession(), IDS, world.ev)
    assert prev.status == "refused"
    result = await jec.commit(
        session,
        IDS,
        world.ev,
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert result.status == "refused"
    assert snapshot(world.journals) == before
    assert "commit" not in session.calls


@pytest.mark.asyncio
async def test_unknown_journal_id_refuses_the_batch(world) -> None:
    ev = {**world.ev, 999: evidence(999, ledger(9))}
    prev = await jec.preview(FakeSession(), [243, 999], ev)
    assert prev.status == "refused"
    assert prev.plan.plans[1].reason == "journal_not_found"


# 3. facts changed between preview and commit --------------------------------


@pytest.mark.asyncio
async def test_commit_rolls_back_when_the_fill_changed_after_the_preview(world) -> None:
    prev = await jec.preview(FakeSession(), IDS, world.ev)
    world.rows[244].filled_at = FILLED + timedelta(minutes=1)  # a writer moved it
    before = snapshot(world.journals)
    session = FakeSession()
    result = await jec.commit(
        session,
        IDS,
        world.ev,
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert result.status == "plan_changed"
    assert snapshot(world.journals) == before
    assert "commit" not in session.calls and "rollback" in session.calls


@pytest.mark.asyncio
async def test_commit_rolls_back_when_the_journal_changed_after_the_preview(
    world,
) -> None:
    prev = await jec.preview(FakeSession(), IDS, world.ev)
    world.journals[245].notes = "edited by someone else"
    before = snapshot(world.journals)
    session = FakeSession()
    result = await jec.commit(
        session,
        IDS,
        world.ev,
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert result.status == "plan_changed"
    assert snapshot(world.journals) == before
    assert "commit" not in session.calls and "rollback" in session.calls


@pytest.mark.asyncio
async def test_other_evidence_than_the_previewed_one_writes_nothing(world) -> None:
    prev = await jec.preview(FakeSession(), IDS, world.ev)
    world.ev[245] = evidence(245, world.rows[245], evidence_ref="hk:doc/another-record")
    before = snapshot(world.journals)
    result = await jec.commit(
        FakeSession(),
        IDS,
        world.ev,
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert result.status == "plan_changed"
    assert snapshot(world.journals) == before


@pytest.mark.asyncio
async def test_a_wrong_digest_writes_nothing(world) -> None:
    before = snapshot(world.journals)
    session = FakeSession()
    result = await jec.commit(
        session, IDS, world.ev, expected_digest="0" * 64, reason=REASON, actor=ACTOR
    )
    assert result.status == "plan_changed"
    assert snapshot(world.journals) == before


# commit writes ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_writes_the_corrected_fields_and_an_audit_note(world) -> None:
    prev = await jec.preview(FakeSession(), IDS, world.ev)
    session = FakeSession()
    result = await jec.commit(
        session,
        IDS,
        world.ev,
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert result.status == "committed"
    assert result.written == (243, 244, 245)
    assert session.calls.count("commit") == 1
    assert "load_journals:lock=True" in session.calls
    lock = next(i for i, c in enumerate(session.calls) if "LOCK TABLE" in c)
    assert "review.execution_ledger IN SHARE MODE" in session.calls[lock]
    assert "lock_timeout" in session.calls[lock - 1]
    # ledger writers are held off before the journals are locked and read
    assert lock < session.calls.index("load_journals:lock=True")

    j = world.journals[245]
    assert j.exit_date == FILLED
    assert j.total_pnl_krw == Decimal("17600.0000")
    assert j.exit_price == Decimal("91000")  # untouched
    assert j.pnl_pct == Decimal("10.7056")  # untouched
    assert j.notes is not None
    for part in (
        "exit_correction:hk5357 ledger_id=272 evidence=hk:doc/reconcile-result-272",
        BOOKED.isoformat(),
        FILLED.isoformat(),
        "total_pnl_krw None -> 17600.0000",
        "fill=krx/ORD272/0 fill_time=ledger_broker_fill_time allocated=2",
        f"reason={REASON}",
        f"actor={ACTOR}",
        "decision=hk 5357",
    ):
        assert part in j.notes


@pytest.mark.asyncio
async def test_existing_notes_are_kept(world) -> None:
    world.journals[245].notes = "thesis note"
    prev = await jec.preview(FakeSession(), [245], {245: world.ev[245]})
    await jec.commit(
        FakeSession(),
        [245],
        {245: world.ev[245]},
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert world.journals[245].notes.startswith(
        "thesis note\n\n[exit_correction:hk5357"
    )


# 4. retries are no-ops ---------------------------------------------------------


@pytest.mark.asyncio
async def test_second_commit_changes_nothing(world) -> None:
    first = await jec.preview(FakeSession(), IDS, world.ev)
    await jec.commit(
        FakeSession(),
        IDS,
        world.ev,
        expected_digest=first.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    after_first = snapshot(world.journals)

    again = await jec.preview(FakeSession(), IDS, world.ev)
    assert again.status == "noop"
    assert all(p.status == "already_corrected" for p in again.plan.plans)

    session = FakeSession()
    second = await jec.commit(
        session,
        IDS,
        world.ev,
        expected_digest=again.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert second.status == "noop"
    assert second.written == ()
    assert snapshot(world.journals) == after_first
    assert "commit" not in session.calls


@pytest.mark.asyncio
async def test_the_identical_retry_after_a_lost_response_is_a_noop(world) -> None:
    prev = await jec.preview(FakeSession(), IDS, world.ev)
    first = await jec.commit(
        FakeSession(),
        IDS,
        world.ev,
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert first.status == "committed"
    after_first = snapshot(world.journals)

    session = FakeSession()
    retry = await jec.commit(
        session,
        IDS,
        world.ev,
        expected_digest=prev.plan.digest,  # the original preview digest, no new preview
        reason=REASON,
        actor=ACTOR,
    )
    assert retry.status == "noop"
    assert retry.written == ()
    assert snapshot(world.journals) == after_first
    assert "commit" not in session.calls


@pytest.mark.asyncio
async def test_a_receipt_for_other_evidence_is_not_a_noop(world) -> None:
    prev = await jec.preview(FakeSession(), [245], {245: world.ev[245]})
    await jec.commit(
        FakeSession(),
        [245],
        {245: world.ev[245]},
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    other = {245: evidence(245, world.rows[245], evidence_ref="hk:doc/another-record")}
    again = await jec.preview(FakeSession(), [245], other)
    assert again.status == "eligible"


@pytest.mark.asyncio
async def test_same_values_without_the_audit_note_still_get_the_note(world) -> None:
    world.journals[245].exit_date = FILLED
    world.journals[245].total_pnl_krw = Decimal("17600")
    plan = jec.plan_journal(world.journals[245], world.ev[245], [world.rows[245]])
    assert plan.status == "eligible"


# evidence parsing --------------------------------------------------------------


def _entry(jid: int = 1, **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "journal_id": jid,
        "evidence_ref": "hk:doc/reconcile-result-1",
        "record_order_id": "ORD1",
        "record_closed_journal_ids": [jid],
        "ledger": {"venue": "krx", "broker_order_id": "ORD1", "fill_seq": 0},
        "allocated_quantity": "2",
    }
    data.update(over)
    return data


def test_a_verified_fill_time_parses_and_needs_an_offset() -> None:
    ok = _entry(
        1, verified_fill_time={"at": "2026-11-18T16:09:45+09:00", "ref": "hk:doc/t"}
    )
    parsed = jec.parse_evidence(json.dumps([ok]), [1])
    assert parsed[1].verified_fill_at == datetime(2026, 11, 18, 7, 9, 45, tzinfo=UTC)
    assert parsed[1].verified_fill_ref == "hk:doc/t"
    for bad in (
        {"at": "2026-11-18T16:09:45", "ref": "r"},  # no offset
        {"at": "yesterday", "ref": "r"},
        {"at": 5, "ref": "r"},
        {"at": "2026-11-18T16:09:45+09:00"},  # no ref
        {"at": "2026-11-18T16:09:45+09:00", "ref": "r", "x": 1},
        "2026-11-18T16:09:45+09:00",
    ):
        with pytest.raises(jec.CorrectionInputError):
            jec.parse_evidence(json.dumps([_entry(1, verified_fill_time=bad)]), [1])


def test_a_valid_evidence_file_parses() -> None:
    parsed = jec.parse_evidence(json.dumps([_entry(1), _entry(2)]), [1, 2])
    assert parsed[1].allocated_quantity == Decimal("2")
    assert parsed[2].fill_seq == 0 and parsed[2].venue == "krx"


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "{}",
        json.dumps([]),
        json.dumps([_entry(1)]),  # ids 1,2 requested but one entry
        json.dumps([_entry(1), _entry(2), _entry(3)]),  # an extra journal
        json.dumps([_entry(1), _entry(1)]),  # repeated journal
        json.dumps([{**_entry(1), "extra": 1}, _entry(2)]),  # unknown key
        json.dumps([{"journal_id": 1, "ledger_id": 11}, _entry(2)]),  # a bare ledger id
        json.dumps(
            [_entry(1, ledger={"venue": "krx", "broker_order_id": "O"}), _entry(2)]
        ),
        json.dumps(
            [
                _entry(
                    1, ledger={"venue": "krx", "broker_order_id": "O", "fill_seq": True}
                ),
                _entry(2),
            ]
        ),
        json.dumps([_entry(1, allocated_quantity="two"), _entry(2)]),
        json.dumps([_entry(1, allocated_quantity="0"), _entry(2)]),
        json.dumps([_entry(1, allocated_quantity=2), _entry(2)]),
        json.dumps([_entry(1, evidence_ref="has space"), _entry(2)]),
        json.dumps([_entry(1, evidence_ref=""), _entry(2)]),
        json.dumps([_entry(1, record_closed_journal_ids=[]), _entry(2)]),
        json.dumps([_entry(1, journal_id="1"), _entry(2)]),
        "[" + '{"a":' * 3 + "]",
        " " * (jec.MAX_EVIDENCE_BYTES + 1),
    ],
)
def test_bad_evidence_is_refused_before_any_database_access(raw) -> None:
    with pytest.raises(jec.CorrectionInputError):
        jec.parse_evidence(raw, [1, 2])


# 5. input validation -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "243-245",
        "24%",
        "243, 244",
        "0",
        "-1",
        "1.5",
        "abc",
        "",
        "243,243",
        " ",
        "243,,244",
    ],
)
def test_ids_are_exact(raw) -> None:
    with pytest.raises(jec.CorrectionInputError):
        jec.parse_ids(raw)


def test_ids_accept_an_explicit_list_only() -> None:
    assert jec.parse_ids("243,244,245") == [243, 244, 245]
    with pytest.raises(jec.CorrectionInputError):
        jec.parse_ids(",".join(str(i) for i in range(1, jec.MAX_IDS + 2)))


@pytest.mark.parametrize("value", [None, "", "   ", "two\nlines", "x" * 1000])
def test_reason_and_actor_are_validated(value) -> None:
    with pytest.raises(jec.CorrectionInputError):
        jec.validate_text("reason", value, max_chars=jec.MAX_REASON_CHARS)


# 6. no direct SQL write ------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[2]
_WRITE_SQL = re.compile(
    r"\b(update|insert|delete|upsert)\s*\(|\b(UPDATE|INSERT|DELETE|TRUNCATE)\b",
)


@pytest.mark.parametrize(
    "rel",
    [
        "app/services/journal_exit_correction.py",
        "scripts/correct_journal_exit_fields.py",
    ],
)
def test_no_direct_sql_write(rel) -> None:
    source = (_ROOT / rel).read_text("utf-8")
    assert _WRITE_SQL.findall(source) == []
    # The only raw statements are the read-only guard and the ledger-writer lock.
    statements = set(re.findall(r"\btext\(\s*f?\"([^\"]+)\"", source))
    assert statements <= {
        "SET TRANSACTION READ ONLY",
        "SET LOCAL lock_timeout = {int(LOCK_TIMEOUT_MS)}",
        "LOCK TABLE {LEDGER_TABLE} IN SHARE MODE",
    }
    assert "session.add(" not in source and ".delete(" not in source
