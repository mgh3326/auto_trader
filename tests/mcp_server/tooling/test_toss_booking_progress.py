"""hk 6288 — what a pass may write once it re-reads booked progress (no DB)."""

from __future__ import annotations

from contextlib import ExitStack
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.mcp_server.tooling import order_journal
from app.mcp_server.tooling import toss_live_ledger as mod
from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_BOOKING_STEPS = (
    "_save_order_fill",
    "_create_trade_journal_for_buy",
    "_link_journal_to_fill",
    "_close_journals_on_sell",
    "upsert_toss_execution_fill",
)


def _row(filled_qty: str, status: str = "partial") -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        broker_order_id="order",
        client_order_id="client",
        market="us",
        symbol="AAPL",
        side="buy",
        operation_kind="place",
        status=status,
        filled_qty=Decimal(filled_qty),
        journal_id=7,
        currency="USD",
    )


async def _run(
    *,
    scanned: str,
    locked: SimpleNamespace | None,
    broker: str,
    cancelled: bool = False,
    project: bool = False,
):
    events = []
    projection = AsyncMock(side_effect=lambda *args, **kwargs: events.append("project"))
    evidence = TossFillEvidence(
        verdict="partial",
        local_status="cancelled" if cancelled else "partial",
        broker_status="CANCELED" if cancelled else "PARTIAL_FILLED",
        filled_qty=Decimal(broker),
        avg_price=Decimal("190"),
        commission=None,
        tax=None,
        fee_total=Decimal("0"),
        settlement_date=None,
        raw_order={},
        reason="test",
    )
    service = MagicMock()
    service.lock_row_for_booking = AsyncMock(
        side_effect=lambda *args: events.append("lock") or locked
    )
    service.update_reconcile_outcome = AsyncMock(
        side_effect=lambda **kwargs: events.append("commit")
    )
    service.record_transient_reconcile_error = AsyncMock()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=object())
    session.__aexit__ = AsyncMock(side_effect=lambda *args: events.append("exit"))
    steps = {name: AsyncMock() for name in _BOOKING_STEPS}
    steps["upsert_toss_execution_fill"].return_value = ("inserted", 42)
    with ExitStack() as stack:
        for name, mock in steps.items():
            stack.enter_context(patch.object(mod, name, new=mock))
        for item in (
            patch.object(mod, "_converge_toss_proposal_rung", new=projection),
            patch.object(mod, "follow_committed_fills", new=AsyncMock()),
            patch.object(mod, "_notify_toss_fill", new=AsyncMock(return_value=False)),
            patch.object(mod, "_order_session_factory", return_value=lambda: session),
            patch.object(mod, "TossLiveOrderLedgerService", return_value=service),
            patch.object(
                mod, "capture_reconcile_spot_fx", new=AsyncMock(return_value=None)
            ),
        ):
            stack.enter_context(item)
        out = await mod._reconcile_one_toss_row(
            _row(scanned),
            dry_run=False,
            evidence_source=SimpleNamespace(
                evidence_for=AsyncMock(return_value=evidence)
            ),
            project_proposal_rungs=project,
        )
    if out["action"] == "booked":
        steps["_save_order_fill"].assert_awaited_once()
        steps["upsert_toss_execution_fill"].assert_awaited_once()
    else:
        for step in steps.values():
            step.assert_not_awaited()
    if project:
        projection.assert_awaited_once_with(
            _row(scanned),
            ledger_status="cancelled",
            filled_qty=Decimal(broker),
        )
        assert events == ["project", "lock", "commit", "exit"]
    return out, service


async def test_locked_row_writes_nothing():
    out, service = await _run(scanned="0", locked=None, broker="2")

    assert out["action"] == "skipped_row_locked"
    service.update_reconcile_outcome.assert_not_awaited()
    service.record_transient_reconcile_error.assert_not_awaited()


async def test_progress_booked_by_another_pass_is_a_quiet_stale_noop():
    out, service = await _run(scanned="0", locked=_row("3"), broker="1")

    assert out["action"] == "noop_stale_evidence"
    assert out["delta_qty"] == -2.0
    assert "requires_manual_review" not in out
    service.update_reconcile_outcome.assert_not_awaited()
    service.record_transient_reconcile_error.assert_not_awaited()


async def test_broker_below_booked_without_a_race_needs_review():
    out, service = await _run(scanned="2", locked=_row("2"), broker="1")

    assert out["action"] == "noop_stale_evidence"
    assert out["requires_manual_review"] is True
    assert out["reason_code"] == "evidence_contradiction"
    service.update_reconcile_outcome.assert_not_awaited()
    service.record_transient_reconcile_error.assert_awaited_once()


async def test_quantity_the_column_rounded_up_still_closes_the_row():
    out, service = await _run(
        scanned="0.12345679", locked=_row("0.12345679"), broker="0.123456789"
    )

    assert out["action"] == "noop_already_booked"
    assert "requires_manual_review" not in out
    service.update_reconcile_outcome.assert_awaited_once()


@pytest.mark.parametrize(
    "closed_as",
    ["expired", "cancelled", "filled", "replaced", "rejected", "anomaly"],
)
async def test_row_closed_by_another_pass_is_not_rewritten(closed_as):
    out, service = await _run(
        scanned="1", locked=_row("1", status=closed_as), broker="1"
    )

    assert out["action"] == "noop_terminal"
    service.update_reconcile_outcome.assert_not_awaited()


async def test_difference_below_the_column_scale_books_nothing():
    out, service = await _run(
        scanned="0.12345678", locked=_row("0.12345678"), broker="0.123456781"
    )

    assert out["action"] == "noop_already_booked"
    assert out["delta_qty"] == 0.0
    service.update_reconcile_outcome.assert_awaited_once()


def _database_error(sqlstate: str | None, *, invalidated: bool = False):
    original = Exception("database said no")
    original.sqlstate = sqlstate
    return OperationalError("stmt", {}, original, connection_invalidated=invalidated)


async def _booking_failure(error: Exception, *, project: bool = False) -> Exception:
    """The exception a booking pass raises when a write fails with ``error``."""
    service = MagicMock()
    service.lock_row_for_booking = AsyncMock(return_value=_row("0"))
    service.update_reconcile_outcome = AsyncMock()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=object())
    session.__aexit__ = AsyncMock(return_value=False)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("1"),
        avg_price=Decimal("190"),
        commission=None,
        tax=None,
        fee_total=Decimal("0"),
        settlement_date=None,
        raw_order={},
        reason="test",
    )
    projection = AsyncMock()
    with (
        patch.object(mod, "_converge_toss_proposal_rung", new=projection),
        patch.object(mod, "_save_order_fill", new=AsyncMock(side_effect=error)),
        patch.object(mod, "_order_session_factory", return_value=lambda: session),
        patch.object(mod, "TossLiveOrderLedgerService", return_value=service),
        patch.object(
            mod, "capture_reconcile_spot_fx", new=AsyncMock(return_value=None)
        ),
    ):
        with pytest.raises(Exception) as raised:  # noqa: PT011 - type is asserted
            await mod._reconcile_one_toss_row(
                _row("0"),
                dry_run=False,
                evidence_source=SimpleNamespace(
                    evidence_for=AsyncMock(return_value=evidence)
                ),
                project_proposal_rungs=project,
            )
    service.update_reconcile_outcome.assert_not_awaited()
    if project:
        projection.assert_awaited_once_with(
            _row("0"), ledger_status="filled", filled_qty=Decimal("1")
        )
    return raised.value


@pytest.mark.parametrize(
    "error",
    [
        _database_error("40001"),
        _database_error("40P01"),
        _database_error("55P03"),
        _database_error("57014"),
        _database_error("08006"),
        _database_error(None, invalidated=True),
        PoolTimeoutError("pool exhausted"),
        ConnectionRefusedError("database is restarting"),
    ],
    ids=[
        "serialization",
        "deadlock",
        "lock",
        "cancel",
        "conn",
        "invalid",
        "pool",
        "socket",
    ],
)
async def test_passing_database_failure_leaves_the_row_retryable(error):
    raised = await _booking_failure(error)

    assert mod._is_transient_reconcile_error(raised) is True
    assert raised.__cause__ is error


@pytest.mark.parametrize(
    "error",
    [
        _database_error("23505"),
        _database_error("22003"),
        DBAPIError("stmt", {}, Exception("no sqlstate")),
        RuntimeError("code fault"),
    ],
    ids=["unique", "numeric", "unknown-db", "code"],
)
async def test_other_booking_failure_keeps_the_manual_review_path(error):
    raised = await _booking_failure(error)

    assert raised is error
    assert mod._is_transient_reconcile_error(raised) is False


def test_database_error_outside_the_booking_is_classified_as_before():
    assert mod._is_transient_reconcile_error(_database_error("40P01")) is False


async def _call_helper(helper: str, db):
    if helper == "trade":
        return await order_journal._save_order_fill(
            symbol="AAPL",
            instrument_type="equity_us",
            side="buy",
            price=1.0,
            quantity=1.0,
            total_amount=1.0,
            fee=0.0,
            currency="USD",
            account="toss",
            order_id="order",
            db=db,
        )
    return await order_journal._link_journal_to_fill(
        symbol="AAPL", trade_id=1, account="toss", db=db
    )


def _failing_session() -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock(side_effect=RuntimeError("write failed"))
    session.commit = AsyncMock()
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.mark.parametrize("helper", ["trade", "link"])
async def test_failed_write_in_the_callers_booking_is_raised(helper):
    db = _failing_session()

    with pytest.raises(RuntimeError, match="write failed"):
        await _call_helper(helper, db)

    db.commit.assert_not_awaited()


@pytest.mark.parametrize("helper", ["trade", "link"])
async def test_failed_write_in_the_helpers_own_session_is_still_swallowed(helper):
    session = _failing_session()

    with patch.object(
        order_journal, "_order_session_factory", return_value=lambda: session
    ):
        assert await _call_helper(helper, None) is None

    session.execute.assert_awaited_once()


async def test_trade_conflict_in_the_callers_booking_still_returns_none():
    db = MagicMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(inserted_primary_key=None))
    db.commit = AsyncMock()

    assert await _call_helper("trade", db) is None

    db.commit.assert_not_awaited()


async def test_cancelled_fill_projects_before_terminal_booking_commit():
    out, _ = await _run(
        scanned="2", locked=_row("2"), broker="3", cancelled=True, project=True
    )

    assert out["action"] == "booked"


async def test_confirmed_fill_projects_when_booking_fails():
    error = RuntimeError("booking unavailable")

    assert await _booking_failure(error, project=True) is error
