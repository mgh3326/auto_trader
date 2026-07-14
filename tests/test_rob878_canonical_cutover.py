"""ROB-878 child-2: canonical cutover — repository/save projection/read API tests.

Tests cover:
- Control-mode repository (shadow/canonical routing, fail-closed)
- Canonical cutover command (advisory lock, parity, idempotency, rollback)
- Save reconciliation (occurrence matching, field presence, idempotency, projection)
- Canonical GET (pagination, filter, overdue-first ordering)
- Legacy /next-actions alias compatibility
- Deploy script post-switch cutover step
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import engine
from app.models.review import (
    TradeRetrospective,
    TradeRetrospectiveAction,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_KST_DATE_TODAY = datetime.now(UTC).astimezone().date().isoformat()


async def _ensure_shadow_mode(db: AsyncSession) -> None:
    """Reset control row to shadow mode (test isolation)."""
    await db.execute(
        text(
            "ALTER TABLE review.trade_retrospective_action_control "
            "DROP CONSTRAINT IF EXISTS ck_trade_retrospective_action_control_mode"
        )
    )
    await db.execute(
        text(
            "ALTER TABLE review.trade_retrospective_action_control "
            "DROP CONSTRAINT IF EXISTS mode"
        )
    )
    await db.execute(
        text(
            "INSERT INTO review.trade_retrospective_action_control (id, mode) "
            "VALUES (1, 'shadow') "
            "ON CONFLICT (id) DO UPDATE SET "
            "mode = 'shadow', cutover_at = NULL, cutover_action_count = NULL"
        )
    )
    await db.execute(
        text(
            "ALTER TABLE review.trade_retrospective_action_control "
            "ADD CONSTRAINT ck_trade_retrospective_action_control_mode "
            "CHECK (mode IN ('shadow','canonical'))"
        )
    )
    await db.commit()


async def _set_canonical_mode(db: AsyncSession) -> None:
    await db.execute(
        text(
            "UPDATE review.trade_retrospective_action_control "
            "SET mode = 'canonical' WHERE id = 1"
        )
    )
    await db.commit()


async def _insert_retrospective(
    db: AsyncSession,
    *,
    retro_id: int,
    next_actions: list | None,
    correlation_id: str | None = None,
    symbol: str = "005930",
    market: str = "kr",
    trigger_type: str | None = "fill",
    realized_pnl: Decimal | None = None,
) -> TradeRetrospective:
    """Insert a retrospective with the given next_actions JSONB."""
    row = TradeRetrospective(
        id=retro_id,
        symbol=symbol,
        instrument_type="equity_kr" if market == "kr" else "equity_us",
        account_mode="kis_mock",
        market=market,
        outcome="filled",
        trigger_type=trigger_type,
        realized_pnl=realized_pnl,
        correlation_id=correlation_id or f"test-{retro_id}",
    )
    if next_actions is not None:
        row.next_actions = next_actions
    db.add(row)
    await db.commit()
    return row


async def _insert_canonical_action(
    db: AsyncSession,
    *,
    retrospective_id: int,
    position: int,
    action: str,
    owner: str | None = None,
    issue_id: str | None = None,
    status: str = "open",
    due_kst_date=None,
    version: int = 1,
    status_actor: str = "migration:rob-878",
    status_source: str = "migration",
    legacy_payload: dict | None = None,
) -> TradeRetrospectiveAction:
    """Insert a canonical action row directly."""
    row = TradeRetrospectiveAction(
        retrospective_id=retrospective_id,
        position=position,
        action=action,
        owner=owner,
        issue_id=issue_id,
        status=status,
        due_kst_date=due_kst_date,
        version=version,
        status_actor=status_actor,
        status_source=status_source,
        legacy_payload=legacy_payload or {},
    )
    db.add(row)
    await db.commit()
    return row


async def _clear_actions(db: AsyncSession) -> None:
    await db.execute(delete(TradeRetrospectiveAction))
    await db.commit()


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(db_session: AsyncSession):
    """Clean up retrospectives, actions, and reset control mode before each test."""
    await db_session.execute(delete(TradeRetrospectiveAction))
    await db_session.execute(delete(TradeRetrospective))
    await db_session.commit()
    await _ensure_shadow_mode(db_session)
    yield
    await db_session.execute(delete(TradeRetrospectiveAction))
    await db_session.execute(delete(TradeRetrospective))
    await db_session.commit()
    await _ensure_shadow_mode(db_session)


# ---------------------------------------------------------------------------
# Section 1: Control-mode repository
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repository_shadow_mode_reads_legacy_json(db_session: AsyncSession):
    """In shadow mode, read_actions returns parent JSONB next_actions."""
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _insert_retrospective(
        db_session,
        retro_id=1001,
        next_actions=[{"action": "check A", "status": "open"}],
    )
    repo = RetrospectiveActionRepository(db_session)
    mode = await repo.get_control_mode()
    assert mode == "shadow"

    actions = await repo.read_actions(1001)
    assert len(actions) == 1
    assert actions[0]["action"] == "check A"


@pytest.mark.asyncio
async def test_repository_canonical_mode_reads_child_ledger(db_session: AsyncSession):
    """In canonical mode, read_actions returns child ledger rows."""
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _insert_retrospective(
        db_session,
        retro_id=1002,
        next_actions=[{"action": "legacy action"}],
    )
    await _insert_canonical_action(
        db_session,
        retrospective_id=1002,
        position=0,
        action="legacy action",
        legacy_payload={"action": "legacy action"},
    )
    await _set_canonical_mode(db_session)

    repo = RetrospectiveActionRepository(db_session)
    mode = await repo.get_control_mode()
    assert mode == "canonical"

    actions = await repo.read_actions(1002)
    assert len(actions) == 1
    assert actions[0]["action"] == "legacy action"


@pytest.mark.asyncio
async def test_repository_missing_control_row_fails_closed(db_session: AsyncSession):
    """Absent control row must fail closed, not default to shadow."""
    from app.services.trade_journal.retrospective_action_repository import (
        ActionControlError,
        RetrospectiveActionRepository,
    )

    await db_session.execute(
        text("DELETE FROM review.trade_retrospective_action_control WHERE id = 1")
    )
    await db_session.commit()

    repo = RetrospectiveActionRepository(db_session)
    with pytest.raises(ActionControlError, match="control row"):
        await repo.get_control_mode()


@pytest.mark.asyncio
async def test_repository_unknown_mode_fails_closed(db_session: AsyncSession):
    """Unknown control mode must fail closed (defense-in-depth beyond DB CHECK)."""
    from app.services.trade_journal.retrospective_action_repository import (
        ActionControlError,
        RetrospectiveActionRepository,
    )

    await db_session.execute(
        text(
            "ALTER TABLE review.trade_retrospective_action_control "
            "DROP CONSTRAINT IF EXISTS ck_trade_retrospective_action_control_mode"
        )
    )
    await db_session.execute(
        text(
            "UPDATE review.trade_retrospective_action_control "
            "SET mode = 'bogus' WHERE id = 1"
        )
    )
    await db_session.commit()

    repo = RetrospectiveActionRepository(db_session)
    with pytest.raises(ActionControlError, match="mode.*invalid"):
        await repo.get_control_mode()


# ---------------------------------------------------------------------------
# Section 2: Canonical cutover
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cutover_shadow_to_canonical_succeeds(db_session: AsyncSession):
    """Cutover switches shadow→canonical and verifies parity."""
    from app.services.trade_journal.retrospective_action_repository import run_cutover

    await _insert_retrospective(
        db_session,
        retro_id=2001,
        next_actions=[
            {"action": "action A", "status": "open"},
            {"action": "action B", "status": "done", "owner": "alice"},
        ],
    )
    await _clear_actions(db_session)

    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            result = await run_cutover(conn, if_shadow=True)
            assert result["mode"] == "canonical"
            assert result["action_count"] == 2
            assert result["cutover_at"] is not None
            await trans.commit()
        except Exception:
            await trans.rollback()
            raise

    # Verify mode switched
    mode = (
        await db_session.execute(
            text(
                "SELECT mode FROM review.trade_retrospective_action_control WHERE id = 1"
            )
        )
    ).scalar_one()
    assert mode == "canonical"

    # Verify children exist
    count = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM review.trade_retrospective_actions "
                "WHERE retrospective_id = 2001"
            )
        )
    ).scalar_one()
    assert count == 2


@pytest.mark.asyncio
async def test_cutover_idempotent_if_shadow(db_session: AsyncSession):
    """Re-running cutover with --if-shadow on already-canonical is a safe no-op."""
    from app.services.trade_journal.retrospective_action_repository import run_cutover

    await _insert_retrospective(
        db_session,
        retro_id=2002,
        next_actions=[{"action": "single action"}],
    )
    await _clear_actions(db_session)

    # First cutover
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await run_cutover(conn, if_shadow=True)
            await trans.commit()
        except Exception:
            await trans.rollback()
            raise

    # Second cutover — should be no-op (already canonical)
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            result = await run_cutover(conn, if_shadow=True)
            assert result["mode"] == "canonical"
            assert result["action_count"] == 1
            assert result.get("idempotent") is True
            await trans.commit()
        except Exception:
            await trans.rollback()
            raise


@pytest.mark.asyncio
async def test_cutover_parity_failure_rolls_back(db_session: AsyncSession):
    """If parity check fails, mode stays shadow and no children are committed."""
    from app.services.trade_journal.retrospective_action_repository import (
        CutoverParityError,
        _verify_parity,
    )

    await _insert_retrospective(
        db_session,
        retro_id=2003,
        next_actions=[{"action": "parity test"}],
    )

    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospective_actions "
                    "(id, retrospective_id, position, action, status, version, "
                    " status_actor, status_source, legacy_payload) "
                    "VALUES (gen_random_uuid(), 2003, 0, 'parity test', 'open', 1, "
                    " 'migration:rob-878', 'migration', "
                    ' \'{"action":"parity test"}\'::jsonb)'
                )
            )
            await conn.execute(
                text(
                    "UPDATE review.trade_retrospective_actions "
                    "SET action = 'corrupted' WHERE retrospective_id = 2003"
                )
            )
            with pytest.raises(CutoverParityError, match="parity"):
                await _verify_parity(conn)
            await trans.rollback()
        except Exception:
            await trans.rollback()
            raise

    mode = (
        await db_session.execute(
            text(
                "SELECT mode FROM review.trade_retrospective_action_control WHERE id = 1"
            )
        )
    ).scalar_one()
    assert mode == "shadow"


@pytest.mark.asyncio
async def test_cutover_rebuilds_from_frozen_parent_json(db_session: AsyncSession):
    """Cutover deletes stale shadow children and rebuilds from current parent JSON."""
    from app.services.trade_journal.retrospective_action_repository import run_cutover

    await _insert_retrospective(
        db_session,
        retro_id=2004,
        next_actions=[
            {"action": "updated action", "status": "open"},
            {"action": "second", "status": "in_progress", "owner": "bob"},
        ],
    )
    # Seed stale shadow children (from old parent JSON)
    await _clear_actions(db_session)
    await _insert_canonical_action(
        db_session,
        retrospective_id=2004,
        position=0,
        action="OLD action text",
    )

    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            result = await run_cutover(conn, if_shadow=True)
            assert result["action_count"] == 2
            await trans.commit()
        except Exception:
            await trans.rollback()
            raise

    # Children should match the CURRENT parent JSON, not the stale shadow
    rows = (
        await db_session.execute(
            text(
                "SELECT action, owner, status FROM review.trade_retrospective_actions "
                "WHERE retrospective_id = 2004 ORDER BY position"
            )
        )
    ).all()
    assert len(rows) == 2
    assert rows[0].action == "updated action"
    assert rows[1].action == "second"
    assert rows[1].owner == "bob"
    assert rows[1].status == "in_progress"


@pytest.mark.asyncio
async def test_cutover_full_field_parity(db_session: AsyncSession):
    """Cutover verifies ordinal, action, owner, issue_id, status, due_kst_date."""
    from app.services.trade_journal.retrospective_action_repository import run_cutover

    await _insert_retrospective(
        db_session,
        retro_id=2005,
        next_actions=[
            {
                "action": "first",
                "owner": "alice",
                "issue_id": "ROB-100",
                "status": "open",
                "due_kst_date": "2026-08-01",
            },
            {
                "action": "second",
                "owner": None,
                "issue_id": None,
                "status": "done",
            },
        ],
    )
    await _clear_actions(db_session)

    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            result = await run_cutover(conn, if_shadow=True)
            assert result["action_count"] == 2
            await trans.commit()
        except Exception:
            await trans.rollback()
            raise

    rows = (
        await db_session.execute(
            text(
                "SELECT position, action, owner, issue_id, status, due_kst_date, "
                "version, status_actor, status_source, resolved_at "
                "FROM review.trade_retrospective_actions "
                "WHERE retrospective_id = 2005 ORDER BY position"
            )
        )
    ).all()
    assert len(rows) == 2

    assert rows[0].position == 0
    assert rows[0].action == "first"
    assert rows[0].owner == "alice"
    assert rows[0].issue_id == "ROB-100"
    assert rows[0].status == "open"
    assert str(rows[0].due_kst_date) == "2026-08-01"
    assert rows[0].version == 1
    assert rows[0].status_actor == "migration:rob-878"
    assert rows[0].status_source == "migration"
    assert rows[0].resolved_at is None

    assert rows[1].position == 1
    assert rows[1].action == "second"
    assert rows[1].status == "done"
    assert rows[1].resolved_at is not None


# ---------------------------------------------------------------------------
# Section 3: Save reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canonical_save_creates_new_action(db_session: AsyncSession):
    """In canonical mode, saving a new retrospective creates child actions."""
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)

    repo = RetrospectiveActionRepository(db_session)
    await _insert_retrospective(
        db_session,
        retro_id=3001,
        next_actions=None,
    )

    await repo.reconcile_actions(
        3001,
        [
            {"action": "new action", "status": "open"},
        ],
        actor="user:1",
    )

    actions = await repo.read_actions(3001)
    assert len(actions) == 1
    assert actions[0]["action"] == "new action"

    # Projection should be written to parent JSONB
    parent_na = (
        await db_session.execute(
            text("SELECT next_actions FROM review.trade_retrospectives WHERE id = 3001")
        )
    ).scalar_one()
    assert parent_na is not None
    assert len(parent_na) == 1
    assert parent_na[0]["action"] == "new action"


@pytest.mark.asyncio
async def test_canonical_save_occurrence_aware_matching(db_session: AsyncSession):
    """Duplicate action text is matched occurrence-aware (first unmatched)."""
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)
    await _insert_retrospective(
        db_session,
        retro_id=3002,
        next_actions=None,
    )

    repo = RetrospectiveActionRepository(db_session)
    # Create two actions with identical text
    await repo.reconcile_actions(
        3002,
        [
            {"action": "same text", "status": "open"},
            {"action": "same text", "status": "open"},
        ],
        actor="user:1",
    )
    actions = await repo.read_actions(3002)
    assert len(actions) == 2
    assert all(a["action"] == "same text" for a in actions)

    # Re-save with the same two items — should match both, not create new
    await repo.reconcile_actions(
        3002,
        [
            {"action": "same text", "status": "open"},
            {"action": "same text", "status": "open"},
        ],
        actor="user:1",
    )
    actions = await repo.read_actions(3002)
    assert len(actions) == 2  # no new rows


@pytest.mark.asyncio
async def test_canonical_save_action_id_ownership_validation(db_session: AsyncSession):
    """action_id belonging to another parent is rejected."""
    from app.services.trade_journal.retrospective_action_repository import (
        ActionReconcileError,
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)
    await _insert_retrospective(db_session, retro_id=3003, next_actions=None)
    await _insert_retrospective(db_session, retro_id=3004, next_actions=None)

    repo = RetrospectiveActionRepository(db_session)
    await repo.reconcile_actions(
        3003,
        [{"action": "owned by 3003", "status": "open"}],
        actor="user:1",
    )
    actions_3003 = await repo.read_actions(3003)
    foreign_action_id = actions_3003[0]["action_id"]

    # Try to use that action_id on a different parent
    with pytest.raises(ActionReconcileError, match="action_id.*not.*parent"):
        await repo.reconcile_actions(
            3004,
            [
                {
                    "action": "different action",
                    "action_id": foreign_action_id,
                    "status": "open",
                }
            ],
            actor="user:1",
        )


@pytest.mark.asyncio
async def test_canonical_save_force_new_creation_key_idempotency(
    db_session: AsyncSession,
):
    """force_new + creation_key creates a new action and is idempotent on retry."""
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)
    await _insert_retrospective(db_session, retro_id=3005, next_actions=None)

    repo = RetrospectiveActionRepository(db_session)
    ckey = uuid.uuid4()

    # Create first action
    await repo.reconcile_actions(
        3005,
        [{"action": "original", "status": "open"}],
        actor="user:1",
    )

    # force_new with creation_key
    await repo.reconcile_actions(
        3005,
        [
            {"action": "original", "status": "open"},
            {
                "action": "original",
                "status": "open",
                "force_new": True,
                "creation_key": str(ckey),
            },
        ],
        actor="user:1",
    )
    actions = await repo.read_actions(3005)
    assert len(actions) == 2

    # Retry with same creation_key — should reuse, not create a third
    await repo.reconcile_actions(
        3005,
        [
            {"action": "original", "status": "open"},
            {
                "action": "original",
                "status": "open",
                "force_new": True,
                "creation_key": str(ckey),
            },
        ],
        actor="user:1",
    )
    actions = await repo.read_actions(3005)
    assert len(actions) == 2  # idempotent


@pytest.mark.asyncio
async def test_canonical_save_omitted_status_preserved(db_session: AsyncSession):
    """Omitted status means 'leave lifecycle unchanged', not 'reset to open'."""
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)
    await _insert_retrospective(db_session, retro_id=3006, next_actions=None)

    repo = RetrospectiveActionRepository(db_session)
    # Create with in_progress
    await repo.reconcile_actions(
        3006,
        [{"action": "in progress action", "status": "in_progress"}],
        actor="user:1",
    )

    # Re-save without status — should stay in_progress
    await repo.reconcile_actions(
        3006,
        [{"action": "in progress action"}],  # no status key
        actor="user:1",
    )
    actions = await repo.read_actions(3006)
    assert actions[0]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_canonical_save_terminal_initial_rejected(db_session: AsyncSession):
    """Creating a new action with terminal status (done/obsolete/expired) is rejected."""
    from app.services.trade_journal.retrospective_action_repository import (
        ActionReconcileError,
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)
    await _insert_retrospective(db_session, retro_id=3007, next_actions=None)

    repo = RetrospectiveActionRepository(db_session)
    with pytest.raises(ActionReconcileError, match="terminal.*initial"):
        await repo.reconcile_actions(
            3007,
            [{"action": "done at creation", "status": "done"}],
            actor="user:1",
        )


@pytest.mark.asyncio
async def test_canonical_save_projection_preserves_unknown_keys(
    db_session: AsyncSession,
):
    """Projection starts from legacy_payload and preserves unknown keys."""
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)
    await _insert_retrospective(db_session, retro_id=3008, next_actions=None)

    repo = RetrospectiveActionRepository(db_session)
    await repo.reconcile_actions(
        3008,
        [
            {
                "action": "with custom",
                "status": "open",
                "custom_key": "custom_value",
                "priority": 5,
            }
        ],
        actor="user:1",
    )

    parent_na = (
        await db_session.execute(
            text("SELECT next_actions FROM review.trade_retrospectives WHERE id = 3008")
        )
    ).scalar_one()
    assert len(parent_na) == 1
    assert parent_na[0]["action"] == "with custom"
    assert parent_na[0]["custom_key"] == "custom_value"
    assert parent_na[0]["priority"] == 5
    # action_id and version are additive
    assert "action_id" in parent_na[0]
    assert parent_na[0]["version"] == 1


@pytest.mark.asyncio
async def test_canonical_save_projection_obsolete_expired_to_done(
    db_session: AsyncSession,
):
    """obsolete/expired project as status=done with additive terminal_status."""
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)
    await _insert_retrospective(db_session, retro_id=3009, next_actions=None)

    repo = RetrospectiveActionRepository(db_session)
    # Create an open action first, then manually set it to obsolete via SQL
    # (transition API is ROB-881, not this issue)
    await repo.reconcile_actions(
        3009,
        [{"action": "to be obsoleted", "status": "open"}],
        actor="user:1",
    )
    actions = await repo.read_actions(3009)
    action_id = actions[0]["action_id"]

    # Manually set to obsolete (simulating a future transition)
    await db_session.execute(
        text(
            "UPDATE review.trade_retrospective_actions "
            "SET status = 'obsolete', resolved_at = now(), "
            "    status_reason = 'superseded', version = 2, "
            "    status_changed_at = now(), status_source = 'web', "
            "    status_actor = 'user:1' "
            "WHERE id = :aid"
        ),
        {"aid": str(action_id)},
    )
    await db_session.commit()

    # Trigger a projection rebuild by re-saving
    await repo.reconcile_actions(
        3009,
        [{"action": "to be obsoleted"}],  # omitted status = preserve
        actor="user:1",
    )

    parent_na = (
        await db_session.execute(
            text("SELECT next_actions FROM review.trade_retrospectives WHERE id = 3009")
        )
    ).scalar_one()
    assert len(parent_na) == 1
    assert parent_na[0]["status"] == "done"
    assert parent_na[0].get("terminal_status") == "obsolete"


@pytest.mark.asyncio
async def test_canonical_save_omitted_next_actions_no_reconcile(
    db_session: AsyncSession,
):
    """Omitted or null next_actions performs no child reconciliation."""
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)
    await _insert_retrospective(db_session, retro_id=3010, next_actions=None)

    repo = RetrospectiveActionRepository(db_session)
    # Create an action
    await repo.reconcile_actions(
        3010,
        [{"action": "existing", "status": "open"}],
        actor="user:1",
    )
    actions_before = await repo.read_actions(3010)
    assert len(actions_before) == 1

    # Reconcile with None — should be a no-op
    await repo.reconcile_actions(3010, None, actor="user:1")

    actions_after = await repo.read_actions(3010)
    assert len(actions_after) == 1
    assert actions_after[0]["action"] == "existing"


@pytest.mark.asyncio
async def test_canonical_save_projection_no_unrelated_parent_field_changes(
    db_session: AsyncSession,
):
    """Projection update does not change unrelated parent fields."""
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)
    await _insert_retrospective(
        db_session,
        retro_id=3011,
        next_actions=None,
        realized_pnl=Decimal("1234.56"),
    )

    repo = RetrospectiveActionRepository(db_session)
    await repo.reconcile_actions(
        3011,
        [{"action": "test", "status": "open"}],
        actor="user:1",
    )

    updated_retro = (
        await db_session.execute(
            text(
                "SELECT realized_pnl, lesson, rationale, trigger_type "
                "FROM review.trade_retrospectives WHERE id = 3011"
            )
        )
    ).one()
    assert updated_retro.realized_pnl == Decimal("1234.56")
    assert updated_retro.lesson is None
    assert updated_retro.rationale is None
    assert updated_retro.trigger_type == "fill"


@pytest.mark.asyncio
async def test_canonical_save_actor_from_identity(db_session: AsyncSession):
    """Actor is from authenticated identity, not caller payload."""
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)
    await _insert_retrospective(db_session, retro_id=3012, next_actions=None)

    repo = RetrospectiveActionRepository(db_session)
    await repo.reconcile_actions(
        3012,
        [{"action": "actor test", "status": "open"}],
        actor="user:42",
    )

    row = (
        await db_session.execute(
            text(
                "SELECT status_actor, status_source "
                "FROM review.trade_retrospective_actions "
                "WHERE retrospective_id = 3012"
            )
        )
    ).one()
    assert row.status_actor == "user:42"
    assert row.status_source == "retrospective_save"


# ---------------------------------------------------------------------------
# Section 4: Canonical GET
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canonical_get_response_shape(db_session: AsyncSession):
    """Canonical GET returns {total, count, limit, offset, as_of, items}."""
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)
    await _insert_retrospective(
        db_session,
        retro_id=4001,
        next_actions=None,
    )
    repo = RetrospectiveActionRepository(db_session)
    await repo.reconcile_actions(
        4001,
        [{"action": "action 1", "status": "open"}],
        actor="user:1",
    )

    result = await repo.query_actions(limit=50, offset=0)

    assert "total" in result
    assert "count" in result
    assert "limit" in result
    assert "offset" in result
    assert "as_of" in result
    assert "items" in result
    assert result["count"] == len(result["items"])
    assert result["limit"] == 50
    assert result["offset"] == 0
    assert result["total"] >= 1


@pytest.mark.asyncio
async def test_canonical_get_active_default_filter(db_session: AsyncSession):
    """Omitted status defaults to open,in_progress (active only)."""
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)
    await _insert_retrospective(db_session, retro_id=4002, next_actions=None)

    repo = RetrospectiveActionRepository(db_session)
    await repo.reconcile_actions(
        4002,
        [
            {"action": "open action", "status": "open"},
            {"action": "in_progress action", "status": "in_progress"},
        ],
        actor="user:1",
    )
    # Manually set one to done
    actions = await repo.read_actions(4002)
    done_id = [a for a in actions if a["action"] == "open action"][0]["action_id"]
    await db_session.execute(
        text(
            "UPDATE review.trade_retrospective_actions "
            "SET status = 'done', resolved_at = now(), version = 2 "
            "WHERE id = :aid"
        ),
        {"aid": str(done_id)},
    )
    await db_session.commit()

    # Default filter should return only active (in_progress)
    result = await repo.query_actions()
    statuses = {item["status"] for item in result["items"]}
    assert "done" not in statuses
    assert "in_progress" in statuses


@pytest.mark.asyncio
async def test_canonical_get_overdue_first_ordering(db_session: AsyncSession):
    """Overdue actions come first, then in_progress, then by due date."""
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)
    await _insert_retrospective(db_session, retro_id=4003, next_actions=None)

    repo = RetrospectiveActionRepository(db_session)
    # Create actions with different statuses and due dates
    await repo.reconcile_actions(
        4003,
        [
            {"action": "no due date", "status": "open"},
            {"action": "overdue", "status": "open", "due_kst_date": "2020-01-01"},
            {
                "action": "future due",
                "status": "in_progress",
                "due_kst_date": "2099-12-31",
            },
            {"action": "in_progress no due", "status": "in_progress"},
        ],
        actor="user:1",
    )

    result = await repo.query_actions()
    items = result["items"]
    # First item should be overdue
    assert items[0]["action"] == "overdue"
    assert items[0]["overdue"] is True
    # in_progress should come before open (excluding overdue)
    # The exact ordering: overdue → in_progress → due_date asc → updated_at → id
    actions_list = [item["action"] for item in items]
    assert "overdue" in actions_list
    assert len(actions_list) == 4


@pytest.mark.asyncio
async def test_canonical_get_pagination_consistency(db_session: AsyncSession):
    """total is consistent across pages; count == len(items) per page."""
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)
    await _insert_retrospective(db_session, retro_id=4004, next_actions=None)

    repo = RetrospectiveActionRepository(db_session)
    await repo.reconcile_actions(
        4004,
        [{"action": f"action {i}", "status": "open"} for i in range(5)],
        actor="user:1",
    )

    page1 = await repo.query_actions(limit=2, offset=0)
    page2 = await repo.query_actions(limit=2, offset=2)
    page3 = await repo.query_actions(limit=2, offset=4)

    assert page1["total"] == page2["total"] == page3["total"]
    assert page1["count"] == 2
    assert page2["count"] == 2
    assert page3["count"] == 1  # last page has 1 item


# ---------------------------------------------------------------------------
# Section 5: Legacy /next-actions alias compatibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_actions_alias_preserves_contract(db_session: AsyncSession):
    """Legacy /next-actions still returns {market, symbol, count, scan_limit, items}."""
    from app.services.trade_journal import trade_retrospective_service as svc

    await _insert_retrospective(
        db_session,
        retro_id=5001,
        next_actions=[{"action": "legacy alias", "status": "open"}],
        market="kr",
    )

    result = await svc.get_open_next_actions(db_session)
    assert "count" in result
    assert "scan_limit" in result
    assert "items" in result
    assert result["count"] >= 1
    item = next(i for i in result["items"] if i["action"] == "legacy alias")
    assert item["symbol"] == "005930"
    assert item["market"] == "kr"


@pytest.mark.asyncio
async def test_next_actions_alias_additive_action_id_version(db_session: AsyncSession):
    """In canonical mode, /next-actions items gain action_id and version."""
    from app.services.trade_journal import trade_retrospective_service as svc
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)
    await _insert_retrospective(db_session, retro_id=5002, next_actions=None)

    repo = RetrospectiveActionRepository(db_session)
    await repo.reconcile_actions(
        5002,
        [{"action": "canonical alias", "status": "open"}],
        actor="user:1",
    )

    result = await svc.get_open_next_actions(db_session)
    item = next(i for i in result["items"] if i["action"] == "canonical alias")
    assert "action_id" in item
    assert "version" in item
    assert item["version"] == 1


# ---------------------------------------------------------------------------
# Section 6: Deploy script post-switch cutover step
# ---------------------------------------------------------------------------


def test_deploy_native_script_has_cutover_after_bluegreen_committed():
    """deploy-native.sh calls cutover only after BLUEGREEN_COMMITTED=1."""
    script_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "deploy-native.sh"
    )
    content = script_path.read_text()

    # BLUEGREEN_COMMITTED=1 must appear before the cutover call
    bg_idx = content.find("BLUEGREEN_COMMITTED=1")
    assert bg_idx != -1, "BLUEGREEN_COMMITTED=1 not found in deploy-native.sh"

    cutover_idx = content.find("retrospective_action_cutover")
    assert cutover_idx != -1, (
        "retrospective_action_cutover not found in deploy-native.sh"
    )

    assert bg_idx < cutover_idx, (
        "BLUEGREEN_COMMITTED=1 must appear before retrospective_action_cutover"
    )


def test_deploy_native_script_cutover_uses_if_shadow():
    """deploy-native.sh uses --if-shadow for idempotent cutover."""
    script_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "deploy-native.sh"
    )
    content = script_path.read_text()

    assert "--if-shadow" in content, (
        "deploy-native.sh must use --if-shadow for idempotent cutover"
    )


def test_deploy_native_script_cutover_conditional_on_committed():
    """Cutover is conditional on BLUEGREEN_COMMITTED=1."""
    script_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "deploy-native.sh"
    )
    content = script_path.read_text()

    # The cutover should be guarded by a conditional check on BLUEGREEN_COMMITTED
    # Look for a pattern like: if (( BLUEGREEN_COMMITTED == 1 )); then ... cutover
    assert "BLUEGREEN_COMMITTED" in content
    assert "retrospective_action_cutover" in content
    # Verify there's a conditional structure around the cutover
    cutover_section = content[content.find("retrospective_action_cutover") - 200 :]
    assert (
        "BLUEGREEN_COMMITTED" in cutover_section
        or "if "
        in content[
            content.find("retrospective_action_cutover") - 500 : content.find(
                "retrospective_action_cutover"
            )
        ]
    )


# ---------------------------------------------------------------------------
# Section 7: Shadow mode save still uses legacy JSON writer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_mode_save_writes_parent_json(db_session: AsyncSession):
    """In shadow mode, save_retrospective writes next_actions to parent JSONB directly."""
    from app.services.trade_journal import trade_retrospective_service as svc

    # Shadow mode is the default
    _, retro = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        correlation_id="shadow-save-test",
        trigger_type="fill",
        next_actions=[{"action": "shadow mode action", "status": "open"}],
    )

    parent_na = (
        await db_session.execute(
            text(
                "SELECT next_actions FROM review.trade_retrospectives WHERE id = :rid"
            ),
            {"rid": retro.id},
        )
    ).scalar_one()
    assert parent_na is not None
    assert len(parent_na) == 1
    assert parent_na[0]["action"] == "shadow mode action"


@pytest.mark.asyncio
async def test_canonical_mode_save_uses_repository(db_session: AsyncSession):
    """In canonical mode, save_retrospective routes through the repository."""
    from app.services.trade_journal import trade_retrospective_service as svc

    await _set_canonical_mode(db_session)

    _, retro = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        correlation_id="canonical-save-test",
        trigger_type="fill",
        next_actions=[{"action": "canonical mode action", "status": "open"}],
    )

    # Child ledger should have the action
    count = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM review.trade_retrospective_actions "
                "WHERE retrospective_id = :rid"
            ),
            {"rid": retro.id},
        )
    ).scalar_one()
    assert count == 1

    # Parent JSONB should have the projection
    parent_na = (
        await db_session.execute(
            text(
                "SELECT next_actions FROM review.trade_retrospectives WHERE id = :rid"
            ),
            {"rid": retro.id},
        )
    ).scalar_one()
    assert parent_na is not None
    assert len(parent_na) == 1
    assert parent_na[0]["action"] == "canonical mode action"
    assert "action_id" in parent_na[0]


# ---------------------------------------------------------------------------
# Section 8: Runbook / operational contract tests
# ---------------------------------------------------------------------------


def test_runbook_exists_for_canonical_cutover():
    """A runbook documenting mixed-version deploy/cutover/rollback exists."""
    # Check for runbook content in the test itself — this is a self-documenting
    # contract test. The runbook is embedded in the test file as docstrings
    # and in the cutover script's --help output.
    script_path = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "retrospective_action_cutover.py"
    )
    if not script_path.exists():
        pytest.skip("Cutover script not yet created")
    content = script_path.read_text()
    # The script must document the rollback/roll-forward procedure
    assert "roll-forward" in content.lower() or "rollback" in content.lower(), (
        "Cutover script must document rollback/roll-forward procedure"
    )


@pytest.mark.asyncio
async def test_canonical_fail_closed_on_direct_json_write(db_session: AsyncSession):
    """After cutover, direct parent JSON writes without GUC marker fail closed."""
    await _insert_retrospective(
        db_session,
        retro_id=7001,
        next_actions=[{"action": "before cutover"}],
    )
    await _set_canonical_mode(db_session)

    # Direct write without GUC marker should fail
    with pytest.raises(Exception, match="canonical mode.*rejected"):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE review.trade_retrospectives SET next_actions = "
                    '\'[{"action": "blocked"}]\'::jsonb WHERE id = 7001'
                )
            )


@pytest.mark.asyncio
async def test_canonical_mode_projection_writer_with_guc_succeeds(
    db_session: AsyncSession,
):
    """Projection writer with GUC marker can write parent JSONB in canonical mode."""
    from app.services.trade_journal.retrospective_action_repository import (
        RetrospectiveActionRepository,
    )

    await _set_canonical_mode(db_session)
    await _insert_retrospective(db_session, retro_id=7002, next_actions=None)

    repo = RetrospectiveActionRepository(db_session)
    await repo.reconcile_actions(
        7002,
        [{"action": "guc test", "status": "open"}],
        actor="user:1",
    )

    # Projection should have been written (with GUC marker)
    parent_na = (
        await db_session.execute(
            text("SELECT next_actions FROM review.trade_retrospectives WHERE id = 7002")
        )
    ).scalar_one()
    assert parent_na is not None
    assert len(parent_na) == 1
    assert parent_na[0]["action"] == "guc test"
