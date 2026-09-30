"""#1175 — DB-backed tests for the execution-ledger quarantine (test DB only).

Every row uses a per-test random ``Q``-prefixed symbol and order number and is
deleted afterwards. Audit rows are append-only by design and are left behind
(no foreign key to the ledger, so they never block ledger cleanup).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models.execution_ledger import (
    ExecutionLedger,
    ExecutionLedgerQuarantineEvent,
)
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services.execution_ledger import quarantine as q
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from tests.services.execution_ledger._quarantine_fixtures import row_kwargs

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

REASON = "hk 1172: KIS accept notice recorded as a fill; broker shows expired"
ACTOR = "desk-operator"


def _tag() -> str:
    return uuid.uuid4().hex[:5].upper()


class _Rows:
    """Insert ledger rows for one test and delete them afterwards."""

    def __init__(self, db) -> None:
        self.db = db
        self.symbols: set[str] = set()

    async def add(self, **overrides: Any) -> int:
        tag = _tag()
        symbol = overrides.pop("symbol", f"Q{tag}")
        order_no = overrides.pop("order_no", f"Q{tag}0001")
        self.symbols.add(symbol)
        upsert = ExecutionLedgerUpsert(
            **row_kwargs(symbol=symbol, order_no=order_no, **overrides)
        )
        row = ExecutionLedger(**upsert.model_dump())
        self.db.add(row)
        await self.db.flush()
        row_id = int(row.id)
        await self.db.commit()
        return row_id

    async def cleanup(self) -> None:
        await self.db.rollback()
        await self.db.execute(
            delete(ExecutionLedger).where(ExecutionLedger.symbol.in_(self.symbols))
        )
        await self.db.commit()


@pytest_asyncio.fixture
async def rows(db_session):
    helper = _Rows(db_session)
    try:
        yield helper
    finally:
        await helper.cleanup()


async def _snapshot(db, ids) -> list[tuple]:
    await db.rollback()
    result = await db.execute(
        select(
            ExecutionLedger.id,
            ExecutionLedger.quarantined_at,
            ExecutionLedger.quarantine_reason,
            ExecutionLedger.quarantined_by,
            ExecutionLedger.updated_at,
            ExecutionLedger.filled_qty,
            ExecutionLedger.source,
        )
        .where(ExecutionLedger.id.in_(list(ids)))
        .order_by(ExecutionLedger.id)
    )
    return [tuple(r) for r in result.all()]


async def _snapshot_rows(db, ids) -> list[Any]:
    await db.rollback()
    result = await db.execute(
        select(
            ExecutionLedger.broker,
            ExecutionLedger.account_mode,
            ExecutionLedger.venue,
            ExecutionLedger.broker_order_id,
            ExecutionLedger.fill_seq,
        )
        .where(ExecutionLedger.id.in_(list(ids)))
        .order_by(ExecutionLedger.id)
    )
    return list(result.all())


async def _audit(db, ids) -> list[ExecutionLedgerQuarantineEvent]:
    await db.rollback()
    result = await db.execute(
        select(ExecutionLedgerQuarantineEvent)
        .where(ExecutionLedgerQuarantineEvent.ledger_id.in_(list(ids)))
        .order_by(ExecutionLedgerQuarantineEvent.ledger_id)
    )
    return list(result.scalars().all())


async def _ledger_count(db) -> int:
    await db.rollback()
    return int((await db.execute(select(func.count(ExecutionLedger.id)))).scalar_one())


# ------------------------------------------------------------------ A1


async def test_a1_preview_changes_nothing_commit_quarantines_exactly_and_repeat_is_noop(
    db_session, rows
) -> None:
    targets = [await rows.add() for _ in range(3)]
    bystander = await rows.add()
    before = await _snapshot(db_session, [*targets, bystander])

    preview = await q.preview_quarantine(db_session, targets)
    assert preview.status == "eligible"
    assert [r.verdict for r in preview.rows] == ["accept_notice"] * 3
    assert await _snapshot(db_session, [*targets, bystander]) == before
    assert await _audit(db_session, targets) == []

    count_before = await _ledger_count(db_session)
    committed = await q.commit_quarantine(
        db_session, targets, reason=REASON, actor=ACTOR
    )
    assert committed.status == "committed"
    assert committed.changed == 3
    after = {row[0]: row for row in await _snapshot(db_session, [*targets, bystander])}
    for ledger_id in targets:
        _, quarantined_at, reason, actor, *_rest = after[ledger_id]
        assert quarantined_at is not None
        assert reason == REASON
        assert actor == ACTOR
    assert after[bystander][1:4] == (None, None, None)
    # never deletes: the row count is unchanged and every row is still there
    assert await _ledger_count(db_session) == count_before

    audit_rows = await _audit(db_session, targets)
    assert [a.ledger_id for a in audit_rows] == sorted(targets)
    assert {a.batch_id for a in audit_rows} == {committed.batch_id}

    first_state = await _snapshot(db_session, targets)
    again = await q.commit_quarantine(
        db_session, targets, reason="different reason", actor="someone else"
    )
    assert again.status == "noop"
    assert again.changed == 0
    assert again.batch_id is None
    assert await _snapshot(db_session, targets) == first_state
    assert len(await _audit(db_session, targets)) == 3


# ------------------------------------------------------------------ A2

INELIGIBLE: dict[str, dict[str, Any]] = {
    "not_websocket": {"source": "reconciler"},
    "fill_notice_cntg_yn_2": {"cntg_yn": "2"},
    "raw_payload_missing": {"raw_payload_json": None},
    "raw_payload_not_domestic_execution_notice": {
        "raw_payload_json": {"tr": "H0STCNI0-ish", "fields": []}
    },
    "not_live": {"account_mode": "mock"},
    "not_kis": {"broker": "upbit", "venue": "upbit_krw", "instrument_type": "crypto"},
}


@pytest.mark.parametrize(
    "case", [*sorted(INELIGIBLE), "not_found", "already_quarantined"]
)
async def test_a2_one_ineligible_id_refuses_the_whole_batch_and_changes_nothing(
    db_session, rows, case: str
) -> None:
    good = [await rows.add() for _ in range(2)]
    if case == "not_found":
        bad = (
            int(
                (
                    await db_session.execute(select(func.max(ExecutionLedger.id)))
                ).scalar_one()
            )
            + 10_000
        )
    elif case == "already_quarantined":
        bad = await rows.add()
        first = await q.commit_quarantine(db_session, [bad], reason=REASON, actor=ACTOR)
        assert first.status == "committed"
    else:
        bad = await rows.add(**INELIGIBLE[case])
    batch = [good[0], bad, good[1]]
    before = await _snapshot(db_session, batch)
    audit_before = len(await _audit(db_session, batch))

    preview = await q.preview_quarantine(db_session, batch)
    assert preview.status == "refused"
    assert preview.refused_ids == [bad]
    assert preview.rows[1].verdict == case

    result = await q.commit_quarantine(db_session, batch, reason=REASON, actor=ACTOR)
    assert result.status == "refused"
    assert result.refused_ids == [bad]
    assert result.rows[1].verdict == case
    assert result.changed == 0
    assert await _snapshot(db_session, batch) == before
    assert len(await _audit(db_session, batch)) == audit_before


async def test_a2_refusal_leaves_the_session_usable_and_unlocked(
    db_session, rows
) -> None:
    good = await rows.add()
    real_fill = await rows.add(cntg_yn="2")
    result = await q.commit_quarantine(
        db_session, [good, real_fill], reason=REASON, actor=ACTOR
    )
    assert result.status == "refused"
    # the row locks were released by the rollback: a second transaction can
    # quarantine the eligible row alone.
    alone = await q.commit_quarantine(db_session, [good], reason=REASON, actor=ACTOR)
    assert alone.status == "committed"


# ------------------------------------------------------------------ A4


async def test_a4_audit_record_carries_reason_actor_and_evidence(
    db_session, rows
) -> None:
    ledger_id = await rows.add()
    result = await q.commit_quarantine(
        db_session, [ledger_id], reason=REASON, actor=ACTOR
    )
    [row] = await _snapshot_rows(db_session, [ledger_id])
    [audit] = await _audit(db_session, [ledger_id])
    assert audit.action == "quarantine"
    assert audit.reason == REASON
    assert audit.actor == ACTOR
    assert audit.batch_id == result.batch_id
    # the audit row doubles as the idempotency-key tombstone
    assert (
        audit.broker,
        audit.account_mode,
        audit.venue,
        audit.broker_order_id,
        audit.fill_seq,
    ) == (row.broker, row.account_mode, row.venue, row.broker_order_id, row.fill_seq)
    assert audit.evidence["raw_cntg_yn"] == "1"
    assert audit.evidence["raw_tr"] == "H0STCNI0"
    assert audit.evidence["source"] == "websocket"
    assert "FAKECUST" not in repr(audit.evidence)
    assert "ACCTFAKE01" not in repr(audit.evidence)


async def test_a4_audit_table_is_append_only(db_session, rows) -> None:
    ledger_id = await rows.add()
    await q.commit_quarantine(db_session, [ledger_id], reason=REASON, actor=ACTOR)
    for statement in (
        update(ExecutionLedgerQuarantineEvent)
        .where(ExecutionLedgerQuarantineEvent.ledger_id == ledger_id)
        .values(reason="rewritten"),
        delete(ExecutionLedgerQuarantineEvent).where(
            ExecutionLedgerQuarantineEvent.ledger_id == ledger_id
        ),
    ):
        await db_session.rollback()
        with pytest.raises(DBAPIError):
            await db_session.execute(statement)
        await db_session.rollback()
    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text("TRUNCATE review.execution_ledger_quarantine_events")
        )
    await db_session.rollback()
    assert len(await _audit(db_session, [ledger_id])) == 1


# --------------------------------------------------------- DB invariants


async def test_db_refuses_to_clear_or_rewrite_a_quarantine(db_session, rows) -> None:
    ledger_id = await rows.add()
    await q.commit_quarantine(db_session, [ledger_id], reason=REASON, actor=ACTOR)
    for values in (
        {"quarantined_at": None, "quarantine_reason": None, "quarantined_by": None},
        {"quarantine_reason": "other"},
        {"quarantined_by": "other"},
        {"quarantined_at": sa.func.now()},
    ):
        await db_session.rollback()
        with pytest.raises(DBAPIError):
            await db_session.execute(
                update(ExecutionLedger)
                .where(ExecutionLedger.id == ledger_id)
                .values(**values)
            )
        await db_session.rollback()
    state = await _snapshot(db_session, [ledger_id])
    assert state[0][2] == REASON


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": "reconciler"},
        {"source": "manual_import"},
        {"broker": "upbit", "venue": "upbit_krw", "instrument_type": "crypto"},
        {"broker": "toss", "venue": "toss_krx"},
    ],
)
async def test_db_refuses_to_quarantine_any_non_kis_websocket_row(
    db_session, rows, overrides
) -> None:
    ledger_id = await rows.add(**overrides)
    with pytest.raises(IntegrityError):
        await db_session.execute(
            update(ExecutionLedger)
            .where(ExecutionLedger.id == ledger_id)
            .values(
                quarantined_at=sa.func.now(),
                quarantine_reason=REASON,
                quarantined_by=ACTOR,
            )
        )
    await db_session.rollback()


@pytest.mark.parametrize(
    "values",
    [
        {"quarantined_at": sa.func.now()},
        {"quarantined_at": sa.func.now(), "quarantine_reason": REASON},
        {
            "quarantined_at": sa.func.now(),
            "quarantine_reason": " ",
            "quarantined_by": ACTOR,
        },
        {"quarantine_reason": REASON, "quarantined_by": ACTOR},
    ],
)
async def test_db_quarantine_is_all_or_nothing(db_session, rows, values) -> None:
    ledger_id = await rows.add()
    with pytest.raises(IntegrityError):
        await db_session.execute(
            update(ExecutionLedger)
            .where(ExecutionLedger.id == ledger_id)
            .values(**values)
        )
    await db_session.rollback()


async def test_replayed_phantom_frame_stays_quarantined(db_session, rows) -> None:
    tag = _tag()
    symbol, order_no = f"Q{tag}", f"Q{tag}0001"
    ledger_id = await rows.add(symbol=symbol, order_no=order_no)
    await q.commit_quarantine(db_session, [ledger_id], reason=REASON, actor=ACTOR)

    replay = ExecutionLedgerUpsert(**row_kwargs(symbol=symbol, order_no=order_no))
    status, row_id = await ExecutionLedgerRepository(db_session).upsert_fill(replay)
    await db_session.commit()
    assert (status, row_id) == ("unchanged", ledger_id)

    # a changed replay of the same key updates the row but never un-quarantines
    changed = ExecutionLedgerUpsert(
        **row_kwargs(symbol=symbol, order_no=order_no, filled_price="5100")
    )
    status, row_id = await ExecutionLedgerRepository(db_session).upsert_fill(changed)
    await db_session.commit()
    assert (status, row_id) == ("updated", ledger_id)
    state = await _snapshot(db_session, [ledger_id])
    assert state[0][1] is not None
    assert state[0][2] == REASON


async def test_concurrent_commit_of_the_same_batch_quarantines_once(
    db_session, rows
) -> None:
    import asyncio

    from app.core.db import AsyncSessionLocal

    targets = [await rows.add() for _ in range(2)]

    async def attempt():
        async with AsyncSessionLocal() as session:
            return await q.commit_quarantine(
                session, targets, reason=REASON, actor=ACTOR
            )

    first, second = await asyncio.gather(attempt(), attempt())
    assert sorted([first.status, second.status]) == ["committed", "noop"]
    assert len(await _audit(db_session, targets)) == 2


# ------------------------------------------------ tombstone (tester r1 B1)


async def _quarantined_then_deleted(db_session, rows) -> tuple[str, str, int]:
    tag = _tag()
    symbol, order_no = f"Q{tag}", f"Q{tag}0001"
    ledger_id = await rows.add(symbol=symbol, order_no=order_no)
    await q.commit_quarantine(db_session, [ledger_id], reason=REASON, actor=ACTOR)
    # an ordinary maintenance DELETE of the quarantined row is not blocked
    await db_session.execute(
        delete(ExecutionLedger).where(ExecutionLedger.id == ledger_id)
    )
    await db_session.commit()
    return symbol, order_no, ledger_id


async def test_delete_then_replay_is_born_quarantined_and_not_a_fill(
    db_session, rows
) -> None:
    symbol, order_no, old_id = await _quarantined_then_deleted(db_session, rows)
    repo = ExecutionLedgerRepository(db_session)
    replay = ExecutionLedgerUpsert(**row_kwargs(symbol=symbol, order_no=order_no))
    status, new_id = await repo.upsert_fill(replay)
    await db_session.commit()

    # reported as a duplicate, so downstream notification stays suppressed
    assert status == "unchanged"
    assert new_id != old_id
    [state] = await _snapshot(db_session, [new_id])
    assert state[1] is not None
    assert (state[2], state[3]) == (REASON, ACTOR)
    assert not await repo.has_fill_for_order(
        broker="kis", account_mode="live", venue="krx", broker_order_id=order_no
    )
    # the original audit record stands; the re-insert is re-quarantined by key
    assert [a.ledger_id for a in await _audit(db_session, [old_id, new_id])] == [old_id]
    preview = await q.preview_quarantine(db_session, [new_id])
    assert preview.status == "noop"


async def test_trigger_mutant_would_restore_the_phantom(db_session, rows) -> None:
    symbol, order_no, _ = await _quarantined_then_deleted(db_session, rows)
    repo = ExecutionLedgerRepository(db_session)
    await db_session.execute(
        sa.text(
            "ALTER TABLE review.execution_ledger "
            "DISABLE TRIGGER trg_execution_ledger_requarantine_insert"
        )
    )
    try:
        replay = ExecutionLedgerUpsert(**row_kwargs(symbol=symbol, order_no=order_no))
        status, _row_id = await repo.upsert_fill(replay)
        assert status == "inserted"
        assert await repo.has_fill_for_order(
            broker="kis", account_mode="live", venue="krx", broker_order_id=order_no
        )
    finally:
        await db_session.rollback()  # restores the trigger and drops the row


async def test_tombstone_never_hides_an_authoritative_or_different_key_row(
    db_session, rows
) -> None:
    symbol, order_no, _ = await _quarantined_then_deleted(db_session, rows)
    repo = ExecutionLedgerRepository(db_session)
    original_seq = row_kwargs(symbol=symbol, order_no=order_no)["fill_seq"]

    reconciler = ExecutionLedgerUpsert(
        **row_kwargs(symbol=symbol, order_no=order_no, source="reconciler")
    )
    assert reconciler.fill_seq == original_seq
    status, reconciler_id = await repo.upsert_fill(reconciler)
    await db_session.commit()
    assert status == "inserted"
    [state] = await _snapshot(db_session, [reconciler_id])
    assert state[1:4] == (None, None, None)

    other_seq = ExecutionLedgerUpsert(
        **row_kwargs(symbol=symbol, order_no=order_no, fill_seq=original_seq ^ 1)
    )
    status, other_id = await repo.upsert_fill(other_seq)
    await db_session.commit()
    assert status == "inserted"
    [state] = await _snapshot(db_session, [other_id])
    assert state[1:4] == (None, None, None)
