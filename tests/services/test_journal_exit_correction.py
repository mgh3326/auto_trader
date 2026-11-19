"""hk 5357 — correction of exit fields on lots closed by a late reconcile.

Fake-session tests: the database reads are replaced by in-memory rows so the
rules, the preview/commit contract and the no-write guarantees run without a
database. The SQL reader is covered by ``test_journal_exit_correction_db.py``.
"""

from __future__ import annotations

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
        "instrument_type": InstrumentType.equity_kr,
        "symbol": "041510",
        "side": "sell",
        "source": "reconciler",
        "quarantined_at": None,
        "filled_qty": Decimal("2"),
        "filled_price": Decimal("91000"),
        "filled_at": FILLED,
        "filled_at_basis": "broker_order_time",
    }
    data.update(over)
    return SimpleNamespace(**data)


def three() -> tuple[dict[int, TradeJournal], dict[int, list[SimpleNamespace]]]:
    """Journals shaped like 243 (Toss 1), 244 (KIS 30) and 245 (KIS 2)."""
    journals = {
        243: journal(
            243,
            account="toss",
            quantity=Decimal("1"),
            exit_date=datetime(2026, 11, 19, 0, 52, tzinfo=UTC),
        ),
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
        243: [
            ledger(
                501,
                broker="toss",
                filled_qty=Decimal("1"),
                filled_at=datetime(2026, 11, 18, 7, 14, 14, tzinfo=UTC),
            )
        ],
        244: [
            ledger(
                271,
                symbol="012030",
                filled_qty=Decimal("30"),
                filled_price=Decimal("1620"),
            )
        ],
        245: [ledger(272)],
    }
    return journals, rows


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
    journals, rows = three()

    async def load_journals(session, ids, *, lock):
        session.calls.append(f"load_journals:lock={lock}")
        return {i: journals[i] for i in ids if i in journals}

    async def load_candidates(session, j):
        return list(rows.get(int(j.id), []))

    monkeypatch.setattr(jec, "load_journals", load_journals)
    monkeypatch.setattr(jec, "load_candidates", load_candidates)
    return SimpleNamespace(journals=journals, rows=rows)


def snapshot(journals: dict[int, TradeJournal]) -> dict[int, tuple]:
    return {
        i: (j.exit_date, j.total_pnl_krw, j.exit_price, j.pnl_pct, j.notes)
        for i, j in journals.items()
    }


# 1. preview shows new values and writes nothing -----------------------------


@pytest.mark.asyncio
async def test_preview_prints_new_values_and_writes_nothing(world) -> None:
    before = snapshot(world.journals)
    session = FakeSession()
    result = await jec.preview(session, [243, 244, 245])

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


# 2. refusals leave the whole batch untouched --------------------------------


def _refused(rows: list[SimpleNamespace], **journal_over: Any) -> jec.JournalPlan:
    return jec.plan_journal(journal(1, **journal_over), rows)


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([], "no_matching_ledger_row"),
        ([ledger(1), ledger(2)], "ambiguous_ledger_rows"),
        ([ledger(1, quarantined_at=BOOKED)], "no_matching_ledger_row"),
        ([ledger(1, source="websocket")], "no_matching_ledger_row"),
        ([ledger(1, filled_qty=Decimal("3"))], "no_matching_ledger_row"),
        ([ledger(1, filled_price=Decimal("90000"))], "no_matching_ledger_row"),
        ([ledger(1, side="buy")], "no_matching_ledger_row"),
        ([ledger(1, broker="toss")], "no_matching_ledger_row"),
        ([ledger(1, account_mode="mock")], "no_matching_ledger_row"),
        (
            [ledger(1, filled_at=BOOKED + timedelta(minutes=1))],
            "no_matching_ledger_row",
        ),
        ([ledger(1, filled_at=BOOKED - timedelta(days=4))], "no_matching_ledger_row"),
        ([ledger(1, filled_at=CREATED - timedelta(days=1))], "no_matching_ledger_row"),
    ],
)
def test_candidate_rules(rows, reason) -> None:
    plan = _refused(rows)
    assert (plan.status, plan.reason) == ("ineligible", reason)


def test_a_bigger_fill_in_the_window_makes_the_attribution_unprovable() -> None:
    """A 10-share sell can close a 2-share lot; an unrelated 2-share sell must not win."""
    unrelated = ledger(1, filled_at=FILLED - timedelta(hours=20))
    bigger = ledger(2, filled_qty=Decimal("10"))
    plan = _refused([unrelated, bigger])
    assert (plan.status, plan.reason) == ("ineligible", "other_sells_in_window")
    # the same unrelated row alone is still the unique candidate
    assert _refused([unrelated]).status == "eligible"
    # a sell of another account or symbol is not in the way
    assert _refused([ledger(1), ledger(2, symbol="005930")]).status == "eligible"
    assert _refused([ledger(1), ledger(2, broker="toss")]).status == "eligible"


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
    plan = _refused([ledger(1)], **over)
    assert (plan.status, plan.reason) == ("ineligible", reason)


@pytest.mark.parametrize(
    "bad_rows",
    [
        {245: []},  # zero ledger rows
        {245: [ledger(272), ledger(273)]},  # two rows
        {245: [ledger(272, quarantined_at=BOOKED)]},  # quarantined only
    ],
)
@pytest.mark.asyncio
async def test_one_bad_journal_blocks_the_whole_batch(world, bad_rows) -> None:
    world.rows.update(bad_rows)
    before = snapshot(world.journals)
    session = FakeSession()
    prev = await jec.preview(FakeSession(), [243, 244, 245])
    assert prev.status == "refused"
    assert {p.journal_id: p.status for p in prev.plan.plans} == {
        243: "eligible",
        244: "eligible",
        245: "ineligible",
    }

    result = await jec.commit(
        session,
        [243, 244, 245],
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert result.status == "refused"
    assert result.written == ()
    assert snapshot(world.journals) == before
    assert "commit" not in session.calls


@pytest.mark.asyncio
async def test_unknown_journal_id_refuses_the_batch(world) -> None:
    prev = await jec.preview(FakeSession(), [243, 999])
    assert prev.status == "refused"
    assert prev.plan.plans[1].reason == "journal_not_found"


@pytest.mark.asyncio
async def test_a_ledger_row_cannot_close_two_journals(world) -> None:
    world.journals[246] = journal(246)
    world.rows[246] = [ledger(272)]
    prev = await jec.preview(FakeSession(), [245, 246])
    assert prev.status == "refused"
    assert {p.reason for p in prev.plan.plans} == {"ledger_row_shared_by_journals"}


# 3. facts changed between preview and commit --------------------------------


@pytest.mark.asyncio
async def test_commit_rolls_back_when_the_facts_changed_after_the_preview(
    world,
) -> None:
    prev = await jec.preview(FakeSession(), [243, 244, 245])
    world.rows[244][0].quarantined_at = BOOKED  # quarantined after the preview
    # a second eligible-looking row appears for 245
    world.rows[245].append(ledger(999, filled_qty=Decimal("5")))
    before = snapshot(world.journals)
    session = FakeSession()

    result = await jec.commit(
        session,
        [243, 244, 245],
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert result.status in {"refused", "plan_changed"}
    assert snapshot(world.journals) == before
    assert "commit" not in session.calls and "rollback" in session.calls


@pytest.mark.asyncio
async def test_commit_rolls_back_when_the_journal_changed_after_the_preview(
    world,
) -> None:
    prev = await jec.preview(FakeSession(), [243, 244, 245])
    world.journals[245].notes = "edited by someone else"
    before = snapshot(world.journals)
    session = FakeSession()
    result = await jec.commit(
        session,
        [243, 244, 245],
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert result.status == "plan_changed"
    assert snapshot(world.journals) == before
    assert "commit" not in session.calls and "rollback" in session.calls


@pytest.mark.asyncio
async def test_a_wrong_digest_writes_nothing(world) -> None:
    before = snapshot(world.journals)
    session = FakeSession()
    result = await jec.commit(
        session, [245], expected_digest="0" * 64, reason=REASON, actor=ACTOR
    )
    assert result.status == "plan_changed"
    assert snapshot(world.journals) == before


# commit writes ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_writes_the_corrected_fields_and_an_audit_note(world) -> None:
    prev = await jec.preview(FakeSession(), [243, 244, 245])
    session = FakeSession()
    result = await jec.commit(
        session,
        [243, 244, 245],
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
        "exit_correction:hk5357 ledger_id=272",
        BOOKED.isoformat(),
        FILLED.isoformat(),
        "total_pnl_krw None -> 17600.0000",
        f"reason={REASON}",
        f"actor={ACTOR}",
        "decision=hk 5357",
    ):
        assert part in j.notes


@pytest.mark.asyncio
async def test_existing_notes_are_kept(world) -> None:
    world.journals[245].notes = "thesis note"
    prev = await jec.preview(FakeSession(), [245])
    await jec.commit(
        FakeSession(),
        [245],
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert world.journals[245].notes.startswith(
        "thesis note\n\n[exit_correction:hk5357"
    )


# 4. second commit is a no-op ---------------------------------------------------


@pytest.mark.asyncio
async def test_second_commit_changes_nothing(world) -> None:
    first = await jec.preview(FakeSession(), [243, 244, 245])
    await jec.commit(
        FakeSession(),
        [243, 244, 245],
        expected_digest=first.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    after_first = snapshot(world.journals)

    again = await jec.preview(FakeSession(), [243, 244, 245])
    assert again.status == "noop"
    assert all(p.status == "already_corrected" for p in again.plan.plans)

    session = FakeSession()
    second = await jec.commit(
        session,
        [243, 244, 245],
        expected_digest=again.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert second.status == "noop"
    assert second.written == ()
    assert snapshot(world.journals) == after_first
    assert "commit" not in session.calls


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_the_identical_retry_after_a_lost_response_is_a_noop(world) -> None:
    prev = await jec.preview(FakeSession(), [243, 244, 245])
    first = await jec.commit(
        FakeSession(),
        [243, 244, 245],
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert first.status == "committed"
    after_first = snapshot(world.journals)

    session = FakeSession()
    retry = await jec.commit(
        session,
        [243, 244, 245],
        expected_digest=prev.plan.digest,  # the original preview digest, no new preview
        reason=REASON,
        actor=ACTOR,
    )
    assert retry.status == "noop"
    assert retry.written == ()
    assert snapshot(world.journals) == after_first
    assert "commit" not in session.calls


@pytest.mark.asyncio
async def test_same_values_without_the_audit_note_still_get_the_note(world) -> None:
    world.journals[245].exit_date = FILLED
    world.journals[245].total_pnl_krw = Decimal("17600")
    plan = jec.plan_journal(world.journals[245], world.rows[245])
    assert plan.status == "eligible"


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
