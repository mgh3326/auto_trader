from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal, engine
from app.models.review import TradeRetrospective, TradeRetrospectiveAction
from app.services.trade_journal.retrospective_action_types import (
    EVIDENCE_MAX_BYTES,
    EVIDENCE_MAX_DEPTH,
    EVIDENCE_STRING_MAX_LENGTH,
    ActionControlModeError,
    ActionNotFoundError,
    ActionTransitionConflict,
    ActionTransitionInvalid,
    TransitionActor,
    _max_depth,
    validate_operator_attestation,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
    pytest.mark.usefixtures("retrospective_action_control_lock"),
]


def _evidence(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "kind": "operator_attestation",
        "source": "postmortem review",
        "reference": "ROB-881:test",
        "observed_at": "2026-07-15T12:00:00+09:00",
        "summary": "authoritative operator observation",
    }
    value.update(overrides)
    return value


def _terminal_payload(status: str) -> tuple[str | None, dict[str, object] | None]:
    if status == "done":
        return "completed", None
    if status == "obsolete":
        return "superseded", None
    if status == "expired":
        return "validity condition ended", _evidence()
    return None, None


async def _set_mode(db: AsyncSession, mode: str) -> None:
    await db.execute(
        text(
            "INSERT INTO review.trade_retrospective_action_control (id, mode) "
            "VALUES (1, :mode) ON CONFLICT (id) DO UPDATE SET mode = :mode, "
            "cutover_at = CASE WHEN :mode = 'canonical' THEN now() ELSE NULL END, "
            "cutover_action_count = CASE WHEN :mode = 'canonical' THEN "
            "(SELECT count(*) FROM review.trade_retrospective_actions) ELSE NULL END"
        ),
        {"mode": mode},
    )
    await db.commit()


async def _seed_action(
    db: AsyncSession,
    *,
    status: str = "open",
    version: int = 1,
    position: int = 0,
    parent_id: int | None = None,
    due_kst_date: date | None = None,
    reason: str | None = None,
    evidence: dict[str, object] | None = None,
    legacy_payload: dict[str, object] | None = None,
) -> tuple[int, uuid.UUID]:
    retrospective_id = parent_id or (2_000_000_000 + uuid.uuid4().int % 1_000_000_000)
    parent = await db.get(TradeRetrospective, retrospective_id)
    if parent is None:
        parent = TradeRetrospective(
            id=retrospective_id,
            correlation_id=f"rob881-{uuid.uuid4()}",
            symbol="005930",
            instrument_type="equity_kr",
            account_mode="kis_mock",
            market="kr",
            outcome="filled",
        )
        db.add(parent)
        await db.flush()

    is_terminal = status in {"done", "obsolete", "expired"}
    status_reason, status_evidence = _terminal_payload(status)
    row = TradeRetrospectiveAction(
        id=uuid.uuid4(),
        retrospective_id=retrospective_id,
        position=position,
        action=f"action-{position}",
        status=status,
        due_kst_date=due_kst_date,
        version=version,
        status_changed_at=datetime.now(UTC) - timedelta(days=1),
        resolved_at=datetime.now(UTC) - timedelta(hours=1) if is_terminal else None,
        status_actor="user:original" if is_terminal else "migration:rob-878",
        status_source="web" if is_terminal else "migration",
        status_reason=reason if reason is not None else status_reason,
        status_evidence=evidence if evidence is not None else status_evidence,
        legacy_payload=legacy_payload
        or {"action": f"action-{position}", "custom": f"preserve-{position}"},
    )
    db.add(row)
    await db.commit()
    return retrospective_id, row.id


async def _load_action(
    db: AsyncSession, action_id: uuid.UUID
) -> TradeRetrospectiveAction:
    db.expire_all()
    row = await db.get(TradeRetrospectiveAction, action_id)
    assert row is not None
    return row


async def _projection(db: AsyncSession, parent_id: int) -> list[dict[str, object]]:
    db.expire_all()
    parent = await db.get(TradeRetrospective, parent_id)
    assert parent is not None
    return list(parent.next_actions or [])


@pytest_asyncio.fixture(autouse=True)
async def _isolate_tables(
    db_session: AsyncSession,
    investment_reports_cleanup_lock: AsyncSession,
    retrospective_action_control_lock: None,
) -> AsyncIterator[None]:
    await db_session.rollback()
    await db_session.execute(delete(TradeRetrospectiveAction))
    await db_session.execute(delete(TradeRetrospective))
    await db_session.commit()
    await _set_mode(db_session, "canonical")
    yield
    await db_session.rollback()
    await db_session.execute(delete(TradeRetrospectiveAction))
    await db_session.execute(delete(TradeRetrospective))
    await db_session.commit()
    await _set_mode(db_session, "shadow")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source", ["open", "in_progress", "done", "obsolete", "expired"]
)
@pytest.mark.parametrize(
    "target", ["open", "in_progress", "done", "obsolete", "expired"]
)
async def test_complete_transition_matrix(
    db_session: AsyncSession, source: str, target: str
) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    reason, evidence = _terminal_payload(source if source == target else target)
    _, action_id = await _seed_action(db_session, status=source, version=4)

    if source in {"done", "obsolete", "expired"} and source != target:
        with pytest.raises(ActionTransitionConflict):
            await transition_retrospective_action(
                db_session,
                action_id=action_id,
                target_status=target,
                expected_version=4,
                actor=TransitionActor.web(7),
                reason=reason,
                evidence=evidence,
            )
        return

    result = await transition_retrospective_action(
        db_session,
        action_id=action_id,
        target_status=target,
        expected_version=1
        if source == target and source not in {"open", "in_progress"}
        else 4,
        actor=TransitionActor.web(7),
        reason=reason,
        evidence=evidence,
    )
    assert result.changed is (source != target)
    assert result.idempotent is (source == target)
    assert result.action["status"] == target
    assert result.action["version"] == (5 if source != target else 4)


@pytest.mark.asyncio
async def test_active_same_state_requires_current_version(
    db_session: AsyncSession,
) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    _, action_id = await _seed_action(db_session, status="open", version=3)
    current = await transition_retrospective_action(
        db_session,
        action_id=action_id,
        target_status="open",
        expected_version=3,
        actor=TransitionActor.web(1),
        reason=None,
        evidence=None,
    )
    assert current.idempotent and not current.changed
    with pytest.raises(ActionTransitionConflict) as exc_info:
        await transition_retrospective_action(
            db_session,
            action_id=action_id,
            target_status="open",
            expected_version=2,
            actor=TransitionActor.web(1),
            reason=None,
            evidence=None,
        )
    assert (
        exc_info.value.action_id,
        exc_info.value.status,
        exc_info.value.version,
    ) == (
        action_id,
        "open",
        3,
    )


@pytest.mark.asyncio
async def test_real_transition_increments_version_exactly_once(
    db_session: AsyncSession,
) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    _, action_id = await _seed_action(db_session, version=8)
    result = await transition_retrospective_action(
        db_session,
        action_id=action_id,
        target_status="done",
        expected_version=8,
        actor=TransitionActor.web(9),
        reason=" completed ",
        evidence=None,
    )
    assert result.action["version"] == 9
    row = await _load_action(db_session, action_id)
    assert row.version == 9
    assert row.status_reason == "completed"


@pytest.mark.asyncio
async def test_same_terminal_stale_retry_returns_stored_audit(
    db_session: AsyncSession,
) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    _, action_id = await _seed_action(
        db_session,
        status="expired",
        version=6,
        reason="window ended",
        evidence=_evidence(),
    )
    before = await _load_action(db_session, action_id)
    immutable = (
        before.status_actor,
        before.status_source,
        before.status_reason,
        before.status_evidence,
        before.resolved_at,
        before.status_changed_at,
    )
    result = await transition_retrospective_action(
        db_session,
        action_id=action_id,
        target_status="expired",
        expected_version=1,
        actor=TransitionActor.web(999),
        reason=" window ended ",
        evidence=_evidence(source=" postmortem review "),
    )
    assert result.idempotent and not result.changed
    after = await _load_action(db_session, action_id)
    assert after.version == 6
    assert (
        after.status_actor,
        after.status_source,
        after.status_reason,
        after.status_evidence,
        after.resolved_at,
        after.status_changed_at,
    ) == immutable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "reason", "evidence"),
    [
        ("done", "window ended", _evidence()),
        ("expired", "different", _evidence()),
        ("expired", "window ended", _evidence(summary="different")),
        ("open", None, None),
    ],
)
async def test_terminal_reopen_or_audit_edit_conflicts_without_mutation(
    db_session: AsyncSession,
    target: str,
    reason: str | None,
    evidence: dict[str, object] | None,
) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    _, action_id = await _seed_action(
        db_session,
        status="expired",
        version=2,
        reason="window ended",
        evidence=_evidence(),
    )
    before = await _load_action(db_session, action_id)
    immutable = (
        before.version,
        before.status_actor,
        before.status_reason,
        before.status_evidence,
        before.resolved_at,
    )
    with pytest.raises(ActionTransitionConflict):
        await transition_retrospective_action(
            db_session,
            action_id=action_id,
            target_status=target,
            expected_version=2,
            actor=TransitionActor.web(2),
            reason=reason,
            evidence=evidence,
        )
    after = await _load_action(db_session, action_id)
    assert (
        after.version,
        after.status_actor,
        after.status_reason,
        after.status_evidence,
        after.resolved_at,
    ) == immutable


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "", "   "])
async def test_obsolete_requires_nonblank_reason(
    db_session: AsyncSession, reason: str | None
) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    _, action_id = await _seed_action(db_session)
    with pytest.raises(ActionTransitionInvalid):
        await transition_retrospective_action(
            db_session,
            action_id=action_id,
            target_status="obsolete",
            expected_version=1,
            actor=TransitionActor.web(1),
            reason=reason,
            evidence=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "evidence"),
    [(None, _evidence()), ("", _evidence()), ("ended", None), ("ended", {})],
)
async def test_expired_requires_reason_and_complete_evidence(
    db_session: AsyncSession,
    reason: str | None,
    evidence: dict[str, object] | None,
) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    _, action_id = await _seed_action(db_session)
    with pytest.raises(ActionTransitionInvalid):
        await transition_retrospective_action(
            db_session,
            action_id=action_id,
            target_status="expired",
            expected_version=1,
            actor=TransitionActor.web(1),
            reason=reason,
            evidence=evidence,
        )


@pytest.mark.asyncio
async def test_past_due_date_alone_never_expires_action(
    db_session: AsyncSession,
) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    _, action_id = await _seed_action(
        db_session, status="open", due_kst_date=date.today() - timedelta(days=30)
    )
    with pytest.raises(ActionTransitionInvalid):
        await transition_retrospective_action(
            db_session,
            action_id=action_id,
            target_status="expired",
            expected_version=1,
            actor=TransitionActor.web(1),
            reason="past due",
            evidence=None,
        )
    row = await _load_action(db_session, action_id)
    assert row.status == "open" and row.version == 1


@pytest.mark.parametrize(
    "bad_evidence",
    [
        _evidence(extra="forbidden"),
        _evidence(observed_at="2026-07-15T12:00:00"),
        _evidence(source=" "),
        _evidence(reference=""),
        _evidence(summary="x" * (EVIDENCE_STRING_MAX_LENGTH + 1)),
        _evidence(source="한" * 2000, reference="한" * 2000, summary="한" * 2000),
        _evidence(**{"Api_Token": "do-not-log-this-secret"}),
        _evidence(summary={"nested": {"PASSWORD": "do-not-log-this-secret"}}),
    ],
)
def test_operator_attestation_rejects_schema_time_bounds_and_secret_keys(
    bad_evidence: dict[str, object],
) -> None:
    with pytest.raises(ActionTransitionInvalid) as exc_info:
        validate_operator_attestation(bad_evidence)
    assert "do-not-log-this-secret" not in str(exc_info.value)


def test_operator_attestation_accepts_exact_boundary_and_offset() -> None:
    valid = validate_operator_attestation(
        _evidence(
            summary="x" * EVIDENCE_STRING_MAX_LENGTH, observed_at="2026-07-15T03:00:00Z"
        )
    )
    assert len(valid["summary"]) == EVIDENCE_STRING_MAX_LENGTH
    assert EVIDENCE_MAX_BYTES == 16 * 1024


def test_operator_attestation_rejects_key_over_64_characters() -> None:
    with pytest.raises(ActionTransitionInvalid):
        validate_operator_attestation(_evidence(**{"k" * 65: "value"}))


def test_evidence_depth_boundary_is_five() -> None:
    at_limit: object = {"a": {"b": {"c": {"d": {"e": "value"}}}}}
    too_deep: object = {"a": {"b": {"c": {"d": {"e": {"f": "value"}}}}}}
    assert _max_depth(at_limit) == EVIDENCE_MAX_DEPTH
    assert _max_depth(too_deep) == EVIDENCE_MAX_DEPTH + 1


@pytest.mark.asyncio
async def test_manual_done_may_omit_evidence_but_reconciler_done_may_not(
    db_session: AsyncSession,
) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    parent_id, manual_id = await _seed_action(db_session, position=0)
    _, reconciler_id = await _seed_action(db_session, parent_id=parent_id, position=1)
    manual = await transition_retrospective_action(
        db_session,
        action_id=manual_id,
        target_status="done",
        expected_version=1,
        actor=TransitionActor.web(1),
        reason=None,
        evidence=None,
    )
    assert manual.changed
    with pytest.raises(ActionTransitionInvalid):
        await transition_retrospective_action(
            db_session,
            action_id=reconciler_id,
            target_status="done",
            expected_version=1,
            actor=TransitionActor.reconciler("position-flat"),
            reason=None,
            evidence=None,
        )


@pytest.mark.asyncio
async def test_reason_length_boundary(db_session: AsyncSession) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    parent_id, accepted_id = await _seed_action(db_session, position=0)
    _, rejected_id = await _seed_action(db_session, parent_id=parent_id, position=1)
    accepted = await transition_retrospective_action(
        db_session,
        action_id=accepted_id,
        target_status="obsolete",
        expected_version=1,
        actor=TransitionActor.web(1),
        reason="x" * 2000,
        evidence=None,
    )
    assert accepted.changed
    with pytest.raises(ActionTransitionInvalid):
        await transition_retrospective_action(
            db_session,
            action_id=rejected_id,
            target_status="obsolete",
            expected_version=1,
            actor=TransitionActor.web(1),
            reason="x" * 2001,
            evidence=None,
        )


@pytest.mark.asyncio
async def test_dry_run_returns_would_be_snapshot_without_db_or_projection_write(
    db_session: AsyncSession,
) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    parent_id, action_id = await _seed_action(db_session)
    projection_before = await _projection(db_session, parent_id)
    result = await transition_retrospective_action(
        db_session,
        action_id=action_id,
        target_status="done",
        expected_version=1,
        actor=TransitionActor.web(1),
        reason="complete",
        evidence=None,
        dry_run=True,
    )
    assert result.dry_run and result.changed and not result.idempotent
    assert result.action["status"] == "done" and result.action["version"] == 2
    row = await _load_action(db_session, action_id)
    assert row.status == "open" and row.version == 1 and row.resolved_at is None
    assert await _projection(db_session, parent_id) == projection_before
    marker = await db_session.scalar(
        text(
            "SELECT current_setting('app.retrospective_action_projection_writer', true)"
        )
    )
    assert marker != "v1"


@pytest.mark.asyncio
async def test_noop_and_conflict_do_not_write_projection_or_guc(
    db_session: AsyncSession,
) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    parent_id, action_id = await _seed_action(db_session, version=3)
    projection_before = await _projection(db_session, parent_id)
    result = await transition_retrospective_action(
        db_session,
        action_id=action_id,
        target_status="open",
        expected_version=3,
        actor=TransitionActor.web(1),
        reason=None,
        evidence=None,
    )
    assert result.idempotent
    with pytest.raises(ActionTransitionConflict):
        await transition_retrospective_action(
            db_session,
            action_id=action_id,
            target_status="done",
            expected_version=2,
            actor=TransitionActor.web(1),
            reason=None,
            evidence=None,
        )
    assert await _projection(db_session, parent_id) == projection_before
    marker = await db_session.scalar(
        text(
            "SELECT current_setting('app.retrospective_action_projection_writer', true)"
        )
    )
    assert marker != "v1"


@pytest.mark.asyncio
async def test_not_found_and_noncanonical_fail_closed(db_session: AsyncSession) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    missing_id = uuid.uuid4()
    with pytest.raises(ActionNotFoundError):
        await transition_retrospective_action(
            db_session,
            action_id=missing_id,
            target_status="done",
            expected_version=1,
            actor=TransitionActor.web(1),
            reason=None,
            evidence=None,
        )
    _, action_id = await _seed_action(db_session)
    await _set_mode(db_session, "shadow")
    with pytest.raises(ActionControlModeError):
        await transition_retrospective_action(
            db_session,
            action_id=action_id,
            target_status="done",
            expected_version=1,
            actor=TransitionActor.web(1),
            reason=None,
            evidence=None,
        )


@pytest.mark.asyncio
async def test_missing_and_unknown_control_modes_fail_closed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    _, action_id = await _seed_action(db_session)
    await db_session.execute(
        text("DELETE FROM review.trade_retrospective_action_control WHERE id = 1")
    )
    await db_session.commit()
    with pytest.raises(ActionControlModeError):
        await transition_retrospective_action(
            db_session,
            action_id=action_id,
            target_status="done",
            expected_version=1,
            actor=TransitionActor.web(1),
            reason=None,
            evidence=None,
        )

    monkeypatch.setattr(
        RetrospectiveActionRepository,
        "get_control_mode",
        AsyncMock(return_value="unknown"),
    )
    with pytest.raises(ActionControlModeError) as exc_info:
        await transition_retrospective_action(
            db_session,
            action_id=action_id,
            target_status="done",
            expected_version=1,
            actor=TransitionActor.web(1),
            reason=None,
            evidence=None,
        )
    assert exc_info.value.mode == "unknown"


@pytest.mark.asyncio
async def test_parent_is_locked_before_all_children_in_id_order(
    db_session: AsyncSession,
) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    parent_id, first_id = await _seed_action(db_session, position=0)
    await _seed_action(db_session, parent_id=parent_id, position=1)
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        normalized = " ".join(statement.lower().split())
        if "for update" in normalized:
            statements.append(normalized)

    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        await transition_retrospective_action(
            db_session,
            action_id=first_id,
            target_status="in_progress",
            expected_version=1,
            actor=TransitionActor.web(1),
            reason=None,
            evidence=None,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)

    parent_lock = next(
        i
        for i, sql in enumerate(statements)
        if "trade_retrospectives" in sql and "trade_retrospective_actions" not in sql
    )
    child_lock = next(
        i for i, sql in enumerate(statements) if "trade_retrospective_actions" in sql
    )
    assert parent_lock < child_lock
    assert "order by" in statements[child_lock] and ".id" in statements[child_lock]


@pytest.mark.asyncio
async def test_caller_owned_commit_and_rollback(db_session: AsyncSession) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    _, committed_id = await _seed_action(db_session, position=0)
    await transition_retrospective_action(
        db_session,
        action_id=committed_id,
        target_status="done",
        expected_version=1,
        actor=TransitionActor.web(1),
        reason=None,
        evidence=None,
    )
    await db_session.commit()
    assert (await _load_action(db_session, committed_id)).status == "done"

    parent_id = (await _load_action(db_session, committed_id)).retrospective_id
    _, rolled_back_id = await _seed_action(db_session, parent_id=parent_id, position=1)
    await transition_retrospective_action(
        db_session,
        action_id=rolled_back_id,
        target_status="done",
        expected_version=1,
        actor=TransitionActor.web(1),
        reason=None,
        evidence=None,
    )
    await db_session.rollback()
    rolled_back = await _load_action(db_session, rolled_back_id)
    assert rolled_back.status == "open" and rolled_back.version == 1


@pytest.mark.asyncio
async def test_explicit_caller_transaction_commits_atomically(
    db_session: AsyncSession,
) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    _, action_id = await _seed_action(db_session)
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await transition_retrospective_action(
                session,
                action_id=action_id,
                target_status="done",
                expected_version=1,
                actor=TransitionActor.web(1),
                reason=None,
                evidence=None,
            )
            assert result.changed
    row = await _load_action(db_session, action_id)
    assert row.status == "done" and row.version == 2


@pytest.mark.asyncio
async def test_projection_failure_rolls_back_child_and_parent(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    parent_id, action_id = await _seed_action(db_session)
    before_projection = await _projection(db_session, parent_id)
    monkeypatch.setattr(
        RetrospectiveActionRepository,
        "rebuild_projection",
        AsyncMock(side_effect=RuntimeError("projection failed")),
    )
    with pytest.raises(RuntimeError, match="projection failed"):
        await transition_retrospective_action(
            db_session,
            action_id=action_id,
            target_status="done",
            expected_version=1,
            actor=TransitionActor.web(1),
            reason=None,
            evidence=None,
        )
    await db_session.rollback()
    row = await _load_action(db_session, action_id)
    assert row.status == "open" and row.version == 1
    assert await _projection(db_session, parent_id) == before_projection


async def _set_timeout(session: AsyncSession) -> None:
    await session.execute(text("SET LOCAL statement_timeout = '8s'"))
    await session.execute(text("SET LOCAL lock_timeout = '7s'"))


@pytest.mark.asyncio
async def test_same_row_stale_race_serializes_without_deadlock(
    db_session: AsyncSession,
) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    _, action_id = await _seed_action(db_session)
    first_holds_lock = asyncio.Event()
    release_first = asyncio.Event()

    async def first() -> None:
        async with AsyncSessionLocal() as session:
            await _set_timeout(session)
            await transition_retrospective_action(
                session,
                action_id=action_id,
                target_status="in_progress",
                expected_version=1,
                actor=TransitionActor.web(1),
                reason=None,
                evidence=None,
            )
            first_holds_lock.set()
            await release_first.wait()
            await session.commit()

    async def stale_second() -> None:
        await first_holds_lock.wait()
        async with AsyncSessionLocal() as session:
            await _set_timeout(session)
            with pytest.raises(ActionTransitionConflict):
                await transition_retrospective_action(
                    session,
                    action_id=action_id,
                    target_status="done",
                    expected_version=1,
                    actor=TransitionActor.web(2),
                    reason=None,
                    evidence=None,
                )

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(stale_second())
    await asyncio.wait_for(first_holds_lock.wait(), timeout=3)
    await asyncio.sleep(0.1)
    release_first.set()
    await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=8)
    row = await _load_action(db_session, action_id)
    assert row.status == "in_progress" and row.version == 2


@pytest.mark.asyncio
async def test_sibling_transitions_preserve_both_projection_updates(
    db_session: AsyncSession,
) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    parent_id, first_id = await _seed_action(db_session, position=0)
    _, second_id = await _seed_action(db_session, parent_id=parent_id, position=1)

    async def transition(action_id: uuid.UUID, actor_id: int) -> None:
        async with AsyncSessionLocal() as session:
            await _set_timeout(session)
            await transition_retrospective_action(
                session,
                action_id=action_id,
                target_status="done",
                expected_version=1,
                actor=TransitionActor.web(actor_id),
                reason=None,
                evidence=None,
            )
            await session.commit()

    await asyncio.wait_for(
        asyncio.gather(transition(first_id, 1), transition(second_id, 2)), timeout=8
    )
    projection = await _projection(db_session, parent_id)
    assert [item["status"] for item in projection] == ["done", "done"]
    assert [item["version"] for item in projection] == [2, 2]
    assert [item["custom"] for item in projection] == ["preserve-0", "preserve-1"]


@pytest.mark.asyncio
async def test_save_and_transition_share_lock_order_and_keep_projection(
    db_session: AsyncSession,
) -> None:
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    parent_id, action_id = await _seed_action(db_session, position=0)

    async def save() -> None:
        async with AsyncSessionLocal() as session:
            await _set_timeout(session)
            await RetrospectiveActionRepository(session).reconcile_actions(
                parent_id,
                [
                    {
                        "action_id": str(action_id),
                        "action": "action-0",
                        "owner": None,
                        "issue_id": None,
                        "status": "open",
                        "due_kst_date": None,
                        "custom": "preserve-0",
                    }
                ],
                "user:save",
                control_mode="canonical",
            )
            await session.commit()

    async def transition() -> None:
        async with AsyncSessionLocal() as session:
            await _set_timeout(session)
            await transition_retrospective_action(
                session,
                action_id=action_id,
                target_status="in_progress",
                expected_version=1,
                actor=TransitionActor.web(2),
                reason=None,
                evidence=None,
            )
            await session.commit()

    outcomes = await asyncio.wait_for(
        asyncio.gather(save(), transition(), return_exceptions=True), timeout=8
    )
    assert not any(isinstance(outcome, BaseException) for outcome in outcomes)
    row = await _load_action(db_session, action_id)
    assert row.status == "in_progress" and row.version == 2
    projection = await _projection(db_session, parent_id)
    assert projection[0]["status"] == "in_progress"
    assert projection[0]["version"] == 2


@pytest.mark.asyncio
async def test_transition_has_no_external_side_effects(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.trade_journal.retrospective_action_transition import (
        transition_retrospective_action,
    )

    notifier = AsyncMock()
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.get_trade_notifier",
        AsyncMock(return_value=notifier),
    )
    _, action_id = await _seed_action(db_session)
    await transition_retrospective_action(
        db_session,
        action_id=action_id,
        target_status="done",
        expected_version=1,
        actor=TransitionActor.web(1),
        reason=None,
        evidence=None,
    )
    notifier.assert_not_called()
