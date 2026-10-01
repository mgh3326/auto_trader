"""#1175 — DB-backed tests for the execution-ledger quarantine (test DB only).

Every row uses a per-test random ``Q``-prefixed symbol and order number and is
deleted afterwards. Audit rows are append-only by design and are left behind
(no foreign key to the ledger, so they never block ledger cleanup).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
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
from tests.services.execution_ledger._quarantine_fixtures import (
    purge_test_ledger_rows,
    row_kwargs,
)

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
        await purge_test_ledger_rows(self.db, ExecutionLedger.symbol.in_(self.symbols))


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
    [audit] = await _audit(db_session, [ledger_id])
    assert audit.action == "quarantine"
    assert audit.reason == REASON
    assert audit.actor == ACTOR
    assert audit.batch_id == result.batch_id
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

    before = await _snapshot(db_session, [ledger_id])
    # a changed replay would rewrite a terminal row: the DB refuses it loudly
    changed = ExecutionLedgerUpsert(
        **row_kwargs(symbol=symbol, order_no=order_no, filled_price="5100")
    )
    with pytest.raises(IntegrityError, match="quarantined and terminal"):
        await ExecutionLedgerRepository(db_session).upsert_fill(changed)
    await db_session.rollback()
    assert await _snapshot(db_session, [ledger_id]) == before


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


# --------------------------------------- terminal quarantine (round 4)


async def _quarantined(db_session, rows, **overrides) -> tuple[str, str, int]:
    tag = _tag()
    symbol, order_no = f"Q{tag}", f"Q{tag}0001"
    ledger_id = await rows.add(symbol=symbol, order_no=order_no, **overrides)
    result = await q.commit_quarantine(
        db_session, [ledger_id], reason=REASON, actor=ACTOR
    )
    assert result.status == "committed"
    return symbol, order_no, ledger_id


async def _visible(db_session, order_no: str) -> bool:
    try:
        return await ExecutionLedgerRepository(db_session).has_fill_for_order(
            broker="kis", account_mode="live", venue="krx", broker_order_id=order_no
        )
    finally:
        await db_session.rollback()


@pytest.mark.parametrize(
    "statement",
    ["delete", "update_payload", "update_updated_at", "update_source"],
)
async def test_a_quarantined_row_is_terminal(db_session, rows, statement) -> None:
    _, order_no, ledger_id = await _quarantined(db_session, rows)
    before = await _snapshot(db_session, [ledger_id])
    where = ExecutionLedger.id == ledger_id
    sql = {
        "delete": delete(ExecutionLedger).where(where),
        "update_payload": update(ExecutionLedger)
        .where(where)
        .values(filled_qty=Decimal("2")),
        "update_updated_at": update(ExecutionLedger)
        .where(where)
        .values(updated_at=sa.func.now()),
        "update_source": update(ExecutionLedger)
        .where(where)
        .values(source="reconciler"),
    }[statement]
    with pytest.raises(IntegrityError, match="quarantined and terminal"):
        await db_session.execute(sql)
    await db_session.rollback()
    assert await _snapshot(db_session, [ledger_id]) == before
    assert not await _visible(db_session, order_no)


async def test_unquarantined_rows_stay_ordinary(db_session, rows) -> None:
    ledger_id = await rows.add()
    await db_session.execute(
        update(ExecutionLedger)
        .where(ExecutionLedger.id == ledger_id)
        .values(filled_price=Decimal("5100"))
    )
    await db_session.execute(
        delete(ExecutionLedger).where(ExecutionLedger.id == ledger_id)
    )
    await db_session.commit()
    assert await _snapshot(db_session, [ledger_id]) == []


async def test_truncate_is_refused_while_a_quarantined_row_exists(
    db_session, rows
) -> None:
    await _quarantined(db_session, rows)
    with pytest.raises(IntegrityError, match="TRUNCATE rejected"):
        await db_session.execute(sa.text("TRUNCATE review.execution_ledger"))
    await db_session.rollback()


# Round-3 reproductions (tester verdict-r3 B1/B2/B3) must now fail safely.


async def test_r3_b1_whitespace_frame_cannot_be_deleted_and_replayed(
    db_session, rows
) -> None:
    from tests.services.execution_ledger._quarantine_fixtures import frame

    tag = _tag()
    symbol, order_no = f"Q{tag}", f"Q{tag}0001"
    raw = frame(order_no=order_no, symbol=symbol, cntg_yn="\t1\t")
    ledger_id = await rows.add(symbol=symbol, order_no=order_no, raw_payload_json=raw)
    assert (
        await q.commit_quarantine(db_session, [ledger_id], reason=REASON, actor=ACTOR)
    ).status == "committed"

    with pytest.raises(IntegrityError, match="quarantined and terminal"):
        await db_session.execute(
            delete(ExecutionLedger).where(ExecutionLedger.id == ledger_id)
        )
    await db_session.rollback()

    replay = ExecutionLedgerUpsert(
        **row_kwargs(symbol=symbol, order_no=order_no, raw_payload_json=raw)
    )
    status, row_id = await ExecutionLedgerRepository(db_session).upsert_fill(replay)
    await db_session.commit()
    assert (status, row_id) == ("unchanged", ledger_id)
    assert not await _visible(db_session, order_no)


def _colliding_real_fill(symbol: str, order_no: str, **overrides):
    """A CNTG_YN=2 execution forced onto the quarantined row's exact key."""
    phantom = ExecutionLedgerUpsert(**row_kwargs(symbol=symbol, order_no=order_no))
    raw = dict(phantom.raw_payload_json or {})
    fields = list(raw["fields"])
    fields[13] = "2"
    raw["fields"] = fields
    update_fields = {
        "raw_payload_json": raw,
        "filled_qty": Decimal("2"),
        "filled_notional": Decimal("10000"),
    }
    update_fields.update(overrides)
    return phantom.model_copy(update=update_fields)


@pytest.mark.parametrize("path", ["repository", "commit_fill"])
async def test_r3_b2_real_fill_on_a_quarantined_key_is_refused_loudly(
    db_session, rows, path: str
) -> None:
    from app.core.db import AsyncSessionLocal
    from app.services.execution_ledger.fill_ingest import commit_fill

    symbol, order_no, ledger_id = await _quarantined(db_session, rows)
    before = await _snapshot(db_session, [ledger_id])
    real = _colliding_real_fill(symbol, order_no)
    with pytest.raises(IntegrityError, match="quarantined and terminal"):
        if path == "repository":
            await ExecutionLedgerRepository(db_session).upsert_fill(real)
        else:
            await commit_fill(real, session_factory=AsyncSessionLocal)
    await db_session.rollback()
    # not overwritten, not silently hidden: the write failed and nothing changed
    assert await _snapshot(db_session, [ledger_id]) == before


async def test_r3_b2_http_ingest_reports_the_collision_as_rejected(
    db_session, rows, monkeypatch, caplog
) -> None:
    from app.routers import execution_ledger_ingest as router
    from app.schemas.execution_ledger_ingest import ExecutionLedgerFillIngestRequest

    symbol, order_no, ledger_id = await _quarantined(db_session, rows)
    before = await _snapshot(db_session, [ledger_id])
    downstream: list[dict] = []

    async def record_downstream(**kwargs):
        downstream.append(kwargs)

    monkeypatch.setattr(router, "run_post_upsert_downstream", record_downstream)
    real = _colliding_real_fill(symbol, order_no)
    caplog.set_level("WARNING", logger=router.logger.name)
    response = await router.ingest_execution_ledger_fills(
        ExecutionLedgerFillIngestRequest(
            source="fillwire", fills=[real.model_dump(mode="json")]
        ),
        db_session,
    )
    assert (response.received, response.accepted, response.rejected) == (1, 0, 1)
    assert response.results[0].status == "rejected"
    assert response.results[0].reason == "IntegrityError"
    assert downstream == []
    assert f"order_id={order_no}" in caplog.text
    assert await _snapshot(db_session, [ledger_id]) == before


@pytest.mark.parametrize("source", ["reconciler", "manual_import"])
async def test_r3_b2_b3_authoritative_write_on_a_quarantined_key_is_refused(
    db_session, rows, source: str
) -> None:
    symbol, order_no, ledger_id = await _quarantined(db_session, rows)
    before = await _snapshot(db_session, [ledger_id])
    authoritative = _colliding_real_fill(
        symbol,
        order_no,
        source=source,
        raw_payload_json={"authority": "synthetic broker fill evidence"},
        filled_qty=Decimal("10"),
    )
    repo = ExecutionLedgerRepository(db_session)
    # classify reads "updated" first; whatever happens in between, the write
    # itself can only fail: the row cannot be deleted or rewritten.
    assert await repo.classify_fill(authoritative) == "updated"
    with pytest.raises(IntegrityError, match="quarantined and terminal"):
        await repo.upsert_fill(authoritative)
    await db_session.rollback()
    assert await _snapshot(db_session, [ledger_id]) == before


async def test_r3_b3_reconcile_run_surfaces_the_collision(
    db_session, rows, monkeypatch
) -> None:
    from types import SimpleNamespace

    from app.services.execution_ledger import reconciler as reconciler_module

    symbol, order_no, ledger_id = await _quarantined(db_session, rows)
    before = await _snapshot(db_session, [ledger_id])
    authoritative = _colliding_real_fill(
        symbol,
        order_no,
        source="reconciler",
        raw_payload_json={"authority": "synthetic broker fill evidence"},
    )
    monkeypatch.setattr(
        reconciler_module,
        "settings",
        SimpleNamespace(EXECUTION_LEDGER_COMMIT_ENABLED=True),
    )
    repo = ExecutionLedgerRepository(db_session)
    recorded: list = []
    real_record_run = repo.record_run

    def capture(run):
        recorded.append(run)
        real_record_run(run)

    monkeypatch.setattr(repo, "record_run", capture)
    reconciler = reconciler_module.ExecutionLedgerReconciler(repo)

    async def fetch(*_args, **_kwargs):
        return [authoritative]

    monkeypatch.setattr(reconciler, "_fetch_normalized", fetch)
    # commit mode: the run fails loudly (the task/script then rolls back and
    # re-raises), and its run record names the refusal for operator review
    with pytest.raises(IntegrityError, match="quarantined and terminal"):
        await reconciler.run("kis", dry_run=False)
    await db_session.rollback()
    assert await _snapshot(db_session, [ledger_id]) == before
    [run] = recorded
    assert run.error_summary is not None
    assert "quarantined and terminal" in run.error_summary
