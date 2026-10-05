"""#1250 — DB-backed AC tests for the Q-46 kis_mock expired[inference] close.

Runs only against the pytest-owned throwaway database. The four decision ids
are inserted explicitly per test and purged afterwards (fixtures module).

A1 preview writes nothing; commit closes exactly the four rows; second commit
   is a no-op.
A2 an id outside the four, a live (non-kis_mock) row, a terminal row, a row
   with any recorded fill, or a wrong decision ref refuses the whole batch and
   changes nothing.
A3 closed rows carry the #1112 marker + caveat + decision ref and drop out of
   every open-order reader exactly like any terminal row.
A4 the audit table is append-only and confined to the four ids.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models.execution_ledger import ExecutionLedger
from app.models.review import (
    KISLiveOrderLedger,
    KISMockInferenceExpiryEvent,
    KISMockOrderLedger,
)
from app.services import kis_mock_inference_expiry_service as service
from app.services import kis_mock_lifecycle_service as lifecycle_module
from app.services.kis_mock_inference_expiry import InferenceInputError
from app.services.kis_mock_lifecycle_service import (
    ExpiredLifecycleConflict,
    KISMockLifecycleService,
)
from app.services.order_proposals.kis_leftover_inference import (
    is_expired_inference_reason,
)
from tests.services._kis_mock_inference_fixtures import (
    IDS,
    KST,
    NOW,
    exec_row,
    live_row,
    mock_row,
    purge,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

REF = "Q-46"
REASON = "hk 706 c1092: stale July/August shadow pending buys, operator Q-46"
ACTOR = "operator-desk"


class _World:
    def __init__(self, db) -> None:
        self.db = db
        self.mock_ids: set[int] = set()
        self.exec_ids: set[int] = set()
        self.live_ids: set[int] = set()

    async def seed(self, **per_row: dict[str, Any]) -> None:
        for ledger_id in IDS:
            changes = per_row.get(f"r{ledger_id}", {})
            if changes is None:
                continue
            self.db.add(mock_row(ledger_id, **changes))
            self.mock_ids.add(ledger_id)
        await self.db.commit()

    async def add_mock(self, ledger_id: int, row: KISMockOrderLedger) -> None:
        row.id = ledger_id
        self.db.add(row)
        self.mock_ids.add(ledger_id)
        await self.db.commit()

    async def add_exec(self, **changes: Any) -> int:
        row = exec_row(**changes)
        self.db.add(row)
        await self.db.flush()
        self.exec_ids.add(int(row.id))
        await self.db.commit()
        return int(row.id)

    async def add_live(self, ledger_id: int) -> None:
        self.db.add(live_row(ledger_id))
        self.live_ids.add(ledger_id)
        await self.db.commit()

    async def cleanup(self) -> None:
        await purge(
            self.db,
            mock_ids=sorted(self.mock_ids),
            exec_ids=sorted(self.exec_ids),
            live_ids=sorted(self.live_ids),
        )


@pytest_asyncio.fixture
async def world(db_session, monkeypatch):
    monkeypatch.setattr(service, "_utcnow", lambda: NOW.astimezone(datetime.UTC))
    taken = (
        (
            await db_session.execute(
                sa.select(KISMockOrderLedger.id).where(KISMockOrderLedger.id.in_(IDS))
            )
        )
        .scalars()
        .all()
    )
    if taken:
        pytest.fail(f"test DB already holds kis_mock rows {sorted(taken)}")
    helper = _World(db_session)
    try:
        yield helper
    finally:
        await helper.cleanup()


async def _snapshot(db) -> dict[str, Any]:
    await db.rollback()
    rows = (
        await db.execute(
            sa.select(
                KISMockOrderLedger.id,
                KISMockOrderLedger.lifecycle_state,
                KISMockOrderLedger.status,
                KISMockOrderLedger.reconcile_attempts,
                KISMockOrderLedger.last_reconcile_detail,
                KISMockOrderLedger.reconciled_at,
            )
            .where(KISMockOrderLedger.id.in_(IDS))
            .order_by(KISMockOrderLedger.id)
        )
    ).all()
    audit = (
        await db.execute(
            sa.select(sa.func.count()).select_from(KISMockInferenceExpiryEvent)
        )
    ).scalar_one()
    live = (
        await db.execute(
            sa.select(KISLiveOrderLedger.id, KISLiveOrderLedger.lifecycle_state).where(
                KISLiveOrderLedger.id.in_(IDS)
            )
        )
    ).all()
    await db.rollback()
    return {"rows": [tuple(r) for r in rows], "audit": audit, "live": sorted(live)}


async def _preview(db):
    return await service.preview_inference_expiry(db, IDS, decision_ref=REF)


async def _commit(db, ids=IDS, ref=REF):
    return await service.commit_inference_expiry(
        db, ids, decision_ref=ref, reason=REASON, actor=ACTOR
    )


# ----------------------------------------------------------------------- A1


async def test_a1_preview_writes_nothing(world) -> None:
    await world.seed()
    before = await _snapshot(world.db)
    result = await _preview(world.db)
    assert result.status == "eligible", [r.as_dict() for r in result.rows]
    assert all(r.failed_conditions == () for r in result.rows)
    printed = result.as_dict()
    assert printed["waived_conditions"] == ["strategy_match", "reconcile_coverage"]
    assert all(
        row["waived_conditions"] == ["strategy_match", "reconcile_coverage"]
        for row in printed["rows"]
    )
    assert await _snapshot(world.db) == before


async def test_a1_commit_closes_exactly_the_four_and_second_commit_is_noop(
    world,
) -> None:
    await world.seed()
    # A bystander kis_mock row of the same symbol and an open state must stay.
    bystander = mock_row(63, order_no="0000099999", lifecycle_state="pending")
    bystander.trade_date = datetime.datetime(2026, 7, 1, 10, 0, tzinfo=KST)
    bystander.raw_response = {**bystander.raw_response, "odno": "0000099999"}
    await world.add_mock(950_001, bystander)

    result = await _commit(world.db)
    assert result.status == "committed"
    assert result.changed == 4

    after = await _snapshot(world.db)
    assert [r[1] for r in after["rows"]] == ["expired"] * 4
    assert [r[3] for r in after["rows"]] == [1] * 4
    assert after["audit"] == 4
    await world.db.rollback()
    untouched = await world.db.get(KISMockOrderLedger, 950_001)
    assert untouched is not None and untouched.lifecycle_state == "pending"

    events = (
        (
            await world.db.execute(
                sa.select(KISMockInferenceExpiryEvent).order_by(
                    KISMockInferenceExpiryEvent.ledger_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [e.ledger_id for e in events] == [63, 64, 66, 80]
    assert {e.batch_id for e in events} == {result.batch_id}
    for event in events:
        assert event.operator_decision_ref == "Q-46"
        assert event.reason == REASON and event.actor == ACTOR
        assert event.before_state == "accepted" and event.after_state == "expired"
        assert event.evidence["row"]["lifecycle_state"] == "accepted"
        assert event.evidence["closed_detail"]["operator_decision_ref"] == "Q-46"
        assert event.evidence["waived_conditions"] == [
            "strategy_match",
            "reconcile_coverage",
        ]
        assert event.evidence["closed_detail"]["expiry_caveat"] == "no_broker_original"
        assert event.evidence["closed_detail"]["waived_conditions"] == [
            "strategy_match",
            "reconcile_coverage",
        ]

    second = await _commit(world.db)
    assert second.status == "noop"
    assert second.changed == 0
    assert await _snapshot(world.db) == after
    assert (await _preview(world.db)).status == "noop"


# ----------------------------------------------------------------------- A2


async def _assert_refused_unchanged(world, *, failing: int, condition: str) -> None:
    before = await _snapshot(world.db)
    preview = await _preview(world.db)
    assert preview.status == "refused"
    assert preview.refused_ids == [failing]
    [row] = [r for r in preview.rows if r.ledger_id == failing]
    assert condition in row.failed_conditions, row.failed_conditions
    result = await _commit(world.db)
    assert result.status == "refused"
    assert result.changed == 0
    assert await _snapshot(world.db) == before


@pytest.mark.parametrize(
    "ids",
    [(80, 66, 64, 63, 81), (80, 66, 64), (80, 66, 64, 62), (80, 66, 64, 63, 63)],
)
async def test_a2_ids_outside_or_short_of_the_four_change_nothing(world, ids) -> None:
    await world.seed()
    before = await _snapshot(world.db)
    with pytest.raises(InferenceInputError):
        await _commit(world.db, ids=ids)
    with pytest.raises(InferenceInputError):
        await service.preview_inference_expiry(world.db, ids, decision_ref=REF)
    assert await _snapshot(world.db) == before


@pytest.mark.parametrize("ref", ["Q-47", "hk:task/706 Q-46", "q-46", " Q-46", ""])
async def test_a2_wrong_decision_ref_changes_nothing(world, ref) -> None:
    await world.seed()
    before = await _snapshot(world.db)
    with pytest.raises(InferenceInputError):
        await _commit(world.db, ref=ref)
    assert await _snapshot(world.db) == before


async def test_a2_a_live_ledger_row_is_unreachable(world) -> None:
    # Id 80 exists only in the live ledger: the tool never reads that table, so
    # the batch is refused for a missing kis_mock row and the live row is intact.
    await world.seed(r80=None)
    await world.add_live(80)
    await _assert_refused_unchanged(world, failing=80, condition="row_missing")


@pytest.mark.parametrize("state", ["cancelled", "expired", "reconciled", "stale"])
async def test_a2_a_terminal_row_refuses_the_batch(world, state) -> None:
    await world.seed(r66={"lifecycle_state": state})
    await _assert_refused_unchanged(world, failing=66, condition="kis_mock_row_open")


async def test_a2_a_row_already_expired_by_the_q46_tool_refuses(world) -> None:
    await world.seed(
        r63={
            "lifecycle_state": "expired",
            "last_reconcile_detail": {
                "reason_code": "operator_legacy_day_expired",
                "operator_decision_ref": "hk:task/706 Q-46",
            },
        }
    )
    await _assert_refused_unchanged(world, failing=63, condition="kis_mock_row_open")


@pytest.mark.parametrize(
    "exec_changes",
    [
        {"source": "websocket"},
        {"source": "reconciler", "broker_order_id": "26063"},
        {"source": "manual_import"},
    ],
)
async def test_a2_an_execution_ledger_fill_for_the_order_refuses(
    world, exec_changes
) -> None:
    await world.seed()
    await world.add_exec(**exec_changes)
    await _assert_refused_unchanged(
        world, failing=63, condition="no_fill_recorded_for_order"
    )


async def test_a2_a_quarantined_mock_fill_still_refuses(world) -> None:
    await world.seed()
    await world.add_exec(
        quarantined_at=NOW,
        quarantine_reason="test phantom",
        quarantined_by="test",
    )
    await _assert_refused_unchanged(
        world, failing=63, condition="no_fill_recorded_for_order"
    )


async def test_a2_a_live_execution_ledger_fill_does_not_count_as_mock(world) -> None:
    # Sanity of scope: a live fill with the same number is a different account.
    await world.seed()
    await world.add_exec(account_mode="live")
    assert (await _preview(world.db)).status == "eligible"


@pytest.mark.parametrize(
    "detail",
    [
        {"reason_code": "fill_detected", "attributed_fill_qty": "3"},
        {"reason_code": "pending_unconfirmed", "attributed_fill_qty": "1"},
        {"reason_code": "attribution_unconfirmed"},
    ],
)
async def test_a2_own_fill_evidence_refuses(world, detail) -> None:
    await world.seed(r80={"last_reconcile_detail": detail})
    await _assert_refused_unchanged(
        world, failing=80, condition="no_fill_recorded_for_order"
    )


async def test_a2_a_filled_sibling_on_the_correlation_refuses(world) -> None:
    await world.seed()
    sibling = mock_row(64, order_no="0000077777", lifecycle_state="fill")
    sibling.symbol = "999999"
    sibling.raw_response = {**sibling.raw_response, "odno": "0000077777"}
    await world.add_mock(950_002, sibling)
    await _assert_refused_unchanged(
        world, failing=64, condition="no_fill_recorded_for_order"
    )


async def test_a2_a_later_fill_of_the_symbol_refuses(world) -> None:
    await world.seed()
    later = mock_row(
        66,
        order_no="0000088888",
        lifecycle_state="reconciled",
        correlation_id="unrelated",
        last_reconcile_detail={"reason_code": "position_reconciled"},
        reconciled_at=datetime.datetime(2026, 7, 23, 9, 0, tzinfo=KST),
    )
    later.trade_date = datetime.datetime(2026, 7, 23, 9, 30, tzinfo=KST)
    later.raw_response = {**later.raw_response, "odno": "0000088888"}
    await world.add_mock(950_003, later)
    await _assert_refused_unchanged(
        world, failing=66, condition="holding_quantity_unchanged"
    )


async def test_a2_a_later_mock_execution_of_the_symbol_refuses(world) -> None:
    await world.seed()
    await world.add_exec(
        symbol="000100",
        raw_symbol="000100",
        broker_order_id="0000055555",
        side="sell",
        filled_at=datetime.datetime(2026, 9, 1, 10, 0, tzinfo=KST),
    )
    await _assert_refused_unchanged(
        world, failing=80, condition="holding_quantity_unchanged"
    )


async def test_a2_unknown_holding_at_send_refuses(world) -> None:
    await world.seed(r64={"holdings_baseline_qty": None})
    await _assert_refused_unchanged(
        world, failing=64, condition="holding_quantity_unchanged"
    )


async def test_a2_a_row_outside_the_regular_session_refuses(world) -> None:
    await world.seed(
        r66={
            "trade_date": datetime.datetime(2026, 7, 22, 8, 40, 1, tzinfo=KST),
            "order_time": "084000",
            "raw_response": {
                "rt_cd": "0",
                "msg_cd": "40600000",
                "msg": "ok",
                "odno": "0000027766",
                "ord_tmd": "084000",
            },
        }
    )
    await _assert_refused_unchanged(
        world, failing=66, condition="regular_session_accept"
    )


async def test_a2_a_broker_unaccepted_row_refuses(world) -> None:
    await world.seed(r63={"status": "unknown"})
    await _assert_refused_unchanged(
        world, failing=63, condition="kis_mock_accepted_buy_row"
    )


async def test_a2_strategy_free_text_is_not_a_refusal(world) -> None:
    # The decision skips the strategy match; row 64's free text and any other
    # strategy value must not change the verdict (and nothing else is skipped).
    await world.seed(r80={"strategy": None}, r63={"strategy": "anything at all"})
    assert (await _preview(world.db)).status == "eligible"


async def test_a2_row_changed_between_preview_and_commit_is_rechecked(
    world,
) -> None:
    await world.seed()
    assert (await _preview(world.db)).status == "eligible"
    await world.db.execute(
        sa.update(KISMockOrderLedger)
        .where(KISMockOrderLedger.id == 66)
        .values(lifecycle_state="cancelled")
    )
    await world.db.commit()
    before = await _snapshot(world.db)
    result = await _commit(world.db)
    assert result.status == "refused" and result.refused_ids == [66]
    assert await _snapshot(world.db) == before


async def test_a2_guarded_write_mismatch_rolls_back_everything(
    world, monkeypatch
) -> None:
    await world.seed()
    before = await _snapshot(world.db)
    original = KISMockLifecycleService.close_rows_by_q46_inference

    async def _short(self, *, details, closed_at):
        await original(self, details=details, closed_at=closed_at)
        return 3

    monkeypatch.setattr(KISMockLifecycleService, "close_rows_by_q46_inference", _short)
    with pytest.raises(service.InferenceConflictError):
        await _commit(world.db)
    assert await _snapshot(world.db) == before


async def test_writer_refuses_ids_outside_the_allowlist(world) -> None:
    await world.seed()
    svc = KISMockLifecycleService(world.db)
    for details in (
        {81: {}},
        {63: {}, 64: {}, 66: {}},
        {63: {}, 64: {}, 66: {}, 81: {}},
    ):
        with pytest.raises(ValueError):
            await svc.close_rows_by_q46_inference(details=details, closed_at=NOW)
    await world.db.rollback()


async def test_waiver_never_reaches_a_row_outside_the_allowlist(world) -> None:
    """Director-1 option A: the waiver exists only inside the four-id allowlist.

    Row 81 has exactly the shape that passes every strict condition, and the
    live ledger holds a row with id 63; neither is read or written.
    """
    await world.seed()
    twin = mock_row(63, order_no="0000026081", correlation_id="twin-81")
    twin.raw_response = {**twin.raw_response, "odno": "0000026081"}
    twin.symbol = "000660"
    await world.add_mock(81, twin)
    await world.add_live(63)

    for ids in ((80, 66, 64, 81), (80, 66, 64, 63, 81), (81,)):
        with pytest.raises(InferenceInputError):
            await _commit(world.db, ids=ids)
    assert (await _commit(world.db)).status == "committed"

    await world.db.rollback()
    outside = await world.db.get(KISMockOrderLedger, 81)
    assert outside is not None
    assert (outside.lifecycle_state, outside.reconcile_attempts) == ("accepted", 0)
    assert outside.last_reconcile_detail is None
    live = await world.db.get(KISLiveOrderLedger, 63)
    assert live is not None and live.lifecycle_state == "accepted"
    audited = (
        (await world.db.execute(sa.select(KISMockInferenceExpiryEvent.ledger_id)))
        .scalars()
        .all()
    )
    assert sorted(audited) == [63, 64, 66, 80]
    with pytest.raises(IntegrityError):
        world.db.add(
            KISMockInferenceExpiryEvent(
                batch_id=__import__("uuid").uuid4(),
                ledger_id=81,
                action="expire_inference",
                operator_decision_ref="Q-46",
                rule_version="v",
                reason="r",
                actor="a",
                before_state="accepted",
                after_state="expired",
                evidence={},
            )
        )
        await world.db.flush()
    await world.db.rollback()


# ----------------------------------------------------------------------- A3


async def test_a3_closed_rows_carry_marker_and_leave_every_open_reader(
    world, monkeypatch
) -> None:
    await world.seed()
    svc = KISMockLifecycleService(world.db)
    open_before = {r.id for r in await svc.list_open_orders(ledger_ids=list(IDS))}
    assert open_before == set(IDS)
    await world.db.rollback()

    assert (await _commit(world.db)).status == "committed"

    rows = (
        (
            await world.db.execute(
                sa.select(KISMockOrderLedger).where(KISMockOrderLedger.id.in_(IDS))
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        detail = row.last_reconcile_detail
        assert row.lifecycle_state == "expired"
        assert is_expired_inference_reason(detail["reason_code"])
        assert detail["expiry_basis"] == "inference"
        assert detail["expiry_caveat"] == "no_broker_original"
        assert detail["operator_decision_ref"] == "Q-46"
        assert detail["waived_conditions"] == ["strategy_match", "reconcile_coverage"]
        assert row.reconciled_at is not None
    await world.db.rollback()

    # Open-order / shadow-reservation / reconcile reader.
    assert await svc.list_open_orders(ledger_ids=list(IDS)) == []
    await world.db.rollback()

    # The generic transition API refuses to touch an expired row.
    with pytest.raises(ExpiredLifecycleConflict):
        await svc.apply_lifecycle_transition(
            ledger_id=63,
            next_state="fill",
            reason_code="fill_detected",
            detail={},
            dry_run=False,
        )
    await world.db.rollback()

    # The #881 Q-46 tool sees a terminal row and writes nothing.
    monkeypatch.setattr(
        lifecycle_module, "_today_kst", lambda: datetime.date(2026, 10, 5)
    )
    results = await svc.expire_legacy_day_orders(
        ledger_ids=[63],
        operator_decision_ref="hk:task/706 Q-46",
        expected_strategy="b0xk",
        min_sessions=2,
        dry_run=True,
    )
    assert results[0]["reason_code"] == "already_terminal"


# ----------------------------------------------------------------------- A4


async def test_a4_audit_rows_are_append_only(world) -> None:
    await world.seed()
    assert (await _commit(world.db)).status == "committed"
    for statement in (
        sa.update(KISMockInferenceExpiryEvent).values(reason="rewritten"),
        sa.delete(KISMockInferenceExpiryEvent),
        sa.text("TRUNCATE review.kis_mock_inference_expiry_events"),
    ):
        with pytest.raises(DBAPIError) as excinfo:
            await world.db.execute(statement)
        assert "append-only" in str(excinfo.value)
        await world.db.rollback()
    assert (await _snapshot(world.db))["audit"] == 4


@pytest.mark.parametrize(
    "changes",
    [
        {"ledger_id": 81},
        {"operator_decision_ref": "Q-47"},
        {"action": "delete"},
        {"after_state": "cancelled"},
        {"before_state": "fill"},
        {"reason": "  "},
        {"actor": ""},
    ],
)
async def test_a4_audit_checks_refuse_out_of_contract_rows(world, changes) -> None:
    values: dict[str, Any] = {
        "batch_id": __import__("uuid").uuid4(),
        "ledger_id": 63,
        "action": "expire_inference",
        "operator_decision_ref": "Q-46",
        "rule_version": "v",
        "reason": "r",
        "actor": "a",
        "before_state": "accepted",
        "after_state": "expired",
        "evidence": {},
    }
    values.update(changes)
    world.db.add(KISMockInferenceExpiryEvent(**values))
    with pytest.raises(IntegrityError):
        await world.db.flush()
    await world.db.rollback()


async def test_a4_unique_ledger_id_means_one_close_per_row(world) -> None:
    await world.seed()
    assert (await _commit(world.db)).status == "committed"
    world.db.add(
        KISMockInferenceExpiryEvent(
            batch_id=__import__("uuid").uuid4(),
            ledger_id=63,
            action="expire_inference",
            operator_decision_ref="Q-46",
            rule_version="v",
            reason="r",
            actor="a",
            before_state="accepted",
            after_state="expired",
            evidence={},
        )
    )
    with pytest.raises(IntegrityError):
        await world.db.flush()
    await world.db.rollback()


async def test_execution_ledger_is_never_written(world) -> None:
    await world.seed()
    count = sa.select(sa.func.count()).select_from(ExecutionLedger)
    before = (await world.db.execute(count)).scalar_one()
    await world.db.rollback()
    assert (await _commit(world.db)).status == "committed"
    assert (await world.db.execute(count)).scalar_one() == before
    await world.db.rollback()


async def test_decimal_baseline_zero_is_a_known_holding(world) -> None:
    await world.seed(r63={"holdings_baseline_qty": Decimal("0")})
    assert (await _preview(world.db)).status == "eligible"
