"""hk 7119 — kill rows on PostgreSQL (CI only): seeds and the system guard."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.services.daily_loss_monitor.accounts import ACCOUNTS
from app.services.daily_loss_monitor.writer import freeze_if_active

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "alembic/versions/20261128_hk7119_daily_loss_freeze.py"
)


def _run_downgrade(sync_conn) -> None:
    spec = importlib.util.spec_from_file_location(
        "hk7119_loss_freeze_migration_db", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    context = MigrationContext.configure(sync_conn)
    with Operations.context(context):
        migration.downgrade()


async def _mode(db, account: str) -> str:
    return (
        await db.execute(
            sa.text(
                "SELECT mode FROM review.execution_account_control WHERE account = :a"
            ),
            {"a": account},
        )
    ).scalar_one()


async def test_every_account_has_a_row_seeded_frozen(db_session) -> None:
    rows = dict(
        (
            await db_session.execute(
                sa.text("SELECT account, mode FROM review.execution_account_control")
            )
        ).all()
    )
    await db_session.rollback()
    assert set(ACCOUNTS) <= set(rows)
    for account in ("kis_live_kr", "kis_live_us", "toss_live_kr", "toss_live_us"):
        assert rows[account] in (
            "GLOBAL_FREEZE",
            "ACTIVE",
            "ENTRY_HALT",
            "DAILY_LOSS_FREEZE",
        )


async def test_writer_freezes_active_and_never_unfreezes(db_session) -> None:
    account = "toss_live_us"
    before = await _mode(db_session, account)
    try:
        await db_session.execute(
            sa.text(
                "UPDATE review.execution_account_control SET mode = 'ACTIVE', "
                "reason = 'test arm', updated_by = 'operator-test' WHERE account = :a"
            ),
            {"a": account},
        )
        assert await freeze_if_active(db_session, account=account, reason="loss")
        assert await _mode(db_session, account) == "DAILY_LOSS_FREEZE"
        # A second run leaves the frozen row alone.
        assert not await freeze_if_active(db_session, account=account, reason="loss")
        # The DB refuses a system writer that unfreezes.
        with pytest.raises(sa.exc.DBAPIError):
            async with db_session.begin_nested():
                await db_session.execute(
                    sa.text(
                        "UPDATE review.execution_account_control SET mode = 'ACTIVE', "
                        "updated_by = 'system:daily_loss_monitor' WHERE account = :a"
                    ),
                    {"a": account},
                )
        # The savepoint rolled back; the row is still frozen.
        assert await _mode(db_session, account) == "DAILY_LOSS_FREEZE"
    finally:
        await db_session.rollback()
    assert await _mode(db_session, account) == before
    await db_session.rollback()


async def test_downgrade_with_only_seed_rows_removes_exactly_them(db_session) -> None:
    """Seeds only: the downgrade succeeds and the table is back to hk 5441."""
    try:
        await (await db_session.connection()).run_sync(_run_downgrade)
        rows = {
            row.account: (row.mode, row.reason, row.updated_by)
            for row in (
                await db_session.execute(
                    sa.text(
                        "SELECT account, mode, reason, updated_by "
                        "FROM review.execution_account_control"
                    )
                )
            ).all()
        }
        assert rows == {
            "upbit_live": ("GLOBAL_FREEZE", "initial_control_row", "migration-hk5441")
        }
        mode_checks = (
            (
                await db_session.execute(
                    sa.text(
                        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint AS c "
                        "WHERE c.conrelid = 'review.execution_account_control'::regclass "
                        "AND c.conname = 'ck_execution_account_control_mode'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(mode_checks) == 1
        assert "DAILY_LOSS_FREEZE" not in mode_checks[0]
        triggers = {
            row[0]
            for row in (
                await db_session.execute(
                    sa.text(
                        "SELECT tgname FROM pg_trigger WHERE tgrelid = "
                        "'review.execution_account_control'::regclass "
                        "AND NOT tgisinternal"
                    )
                )
            ).all()
        }
        assert "trg_execution_account_control_update" not in triggers
        assert "trg_execution_account_control_delete" in triggers
    finally:
        await db_session.rollback()


async def test_downgrade_refuses_operator_changed_and_frozen_rows(db_session) -> None:
    """An operator arm or a monitor freeze is state the downgrade must keep."""
    account = "kis_live_kr"
    try:
        await db_session.execute(
            sa.text(
                "UPDATE review.execution_account_control SET mode = 'ACTIVE', "
                "reason = 'test arm', updated_by = 'operator-test' "
                "WHERE account = :a"
            ),
            {"a": account},
        )
        with pytest.raises(RuntimeError, match="refusing downgrade"):
            await (await db_session.connection()).run_sync(_run_downgrade)
        assert await freeze_if_active(db_session, account=account, reason="loss")
        with pytest.raises(RuntimeError, match="refusing downgrade"):
            await (await db_session.connection()).run_sync(_run_downgrade)
        assert await _mode(db_session, account) == "DAILY_LOSS_FREEZE"
    finally:
        await db_session.rollback()
