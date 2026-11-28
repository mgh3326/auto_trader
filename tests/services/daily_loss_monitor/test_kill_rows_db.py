"""hk 7119 — kill rows on PostgreSQL (CI only): seeds and the system guard."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.services.daily_loss_monitor.accounts import ACCOUNTS
from app.services.daily_loss_monitor.writer import freeze_if_active

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


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
