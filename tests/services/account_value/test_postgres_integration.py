"""Postgres-only checks, run by CI against its isolated migrated database."""

import importlib.util
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from app.models.account_value import AccountExternalFlow, AccountValueSnapshot
from app.services.account_value.flows import ExternalFlow, record_flow
from app.services.account_value.snapshot import Observation, capture

pytestmark = pytest.mark.integration


class Reader:
    def __init__(self, account):
        self.account = account

    async def read(self):
        return [
            Observation(
                self.account,
                "crypto",
                date(2026, 1, 1),
                datetime(2026, 1, 1, tzinfo=UTC),
                (),
                {"KRW": None},
            )
        ]


@pytest.mark.asyncio
async def test_preview_commit_and_repeat_noop(db_session):
    account = "upbit:upbit:" + uuid4().hex * 2
    reader = Reader(account)
    await capture(reader, db_session)
    query = (
        select(func.count())
        .select_from(AccountValueSnapshot)
        .where(AccountValueSnapshot.account == account)
    )
    assert (await db_session.scalar(query)) == 0
    inserted = await capture(reader, db_session, commit=True)
    assert inserted[0]["write_status"] == "inserted"

    class ChangedReader:
        async def read(self):
            return [
                Observation(
                    account,
                    "crypto",
                    date(2026, 1, 1),
                    datetime(2026, 1, 1, tzinfo=UTC),
                    (),
                    {"KRW": Decimal(99)},
                )
            ]

    repeated = await capture(ChangedReader(), db_session, commit=True)
    assert repeated[0]["write_status"] == "already_present"
    assert repeated[0]["cash"] == {"KRW": None}
    assert (await db_session.scalar(query)) == 1


@pytest.mark.asyncio
async def test_downgrade_refuses_retained_rows(db_session):
    await capture(Reader("upbit:upbit:" + uuid4().hex * 2), db_session, commit=True)
    path = Path("alembic/versions/20261110_hk3800_account_value.py")
    spec = importlib.util.spec_from_file_location("account_value_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()

    with pytest.raises(DBAPIError, match="account_value_rows_retained"):
        async with db_session.begin_nested():
            connection = await db_session.connection()
            await connection.run_sync(downgrade)


@pytest.mark.asyncio
async def test_flow_retry_conflict_distinct_equal_and_reversal(db_session):
    account = "upbit:upbit:" + uuid4().hex * 2
    key = uuid4()
    flow = ExternalFlow(account, "account", "KRW", Decimal("10"), date(2026, 1, 2), key)
    assert await record_flow(db_session, flow) == "preview"
    assert await record_flow(db_session, flow, commit=True) == "inserted"
    assert await record_flow(db_session, flow, commit=True) == "already_present"
    with pytest.raises(ValueError, match="different flow"):
        await record_flow(
            db_session,
            ExternalFlow(account, "account", "KRW", Decimal("11"), flow.day, key),
            commit=True,
        )
    same_amount = ExternalFlow(
        account, "account", "KRW", Decimal("10"), flow.day, uuid4()
    )
    reversal = ExternalFlow(
        account, "account", "KRW", Decimal("-10"), flow.day, uuid4()
    )
    assert await record_flow(db_session, same_amount, commit=True) == "inserted"
    assert await record_flow(db_session, reversal, commit=True) == "inserted"
    rows = (
        (
            await db_session.execute(
                select(AccountExternalFlow).where(
                    AccountExternalFlow.account == account
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 3
    assert sum(row.amount for row in rows) == Decimal("10")


@pytest.mark.asyncio
async def test_flow_confirmation_is_immutable_and_downgrade_refuses(db_session):
    from sqlalchemy import text

    from app.services.account_value.completeness import record_confirmation

    account = "upbit:upbit:" + uuid4().hex * 2
    await record_confirmation(
        db_session,
        account,
        date(2026, 1, 1),
        date(2026, 1, 2),
        "operator",
        "checked",
        commit=True,
    )
    for statement in (
        "UPDATE review.account_flow_confirmations SET reason = 'changed'",
        "DELETE FROM review.account_flow_confirmations",
        "TRUNCATE review.account_flow_confirmations",
    ):
        with pytest.raises(DBAPIError, match="account_value_immutable"):
            async with db_session.begin_nested():
                await db_session.execute(text(statement))
    spec = importlib.util.spec_from_file_location(
        "flow_confirm_migration",
        Path("alembic/versions/20261113_hk4282_flow_confirmations.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()

    with pytest.raises(DBAPIError, match="account_flow_confirmation_rows_retained"):
        async with db_session.begin_nested():
            connection = await db_session.connection()
            await connection.run_sync(downgrade)


@pytest.mark.asyncio
async def test_flow_confirmation_revocation_is_immutable_and_degrades_coverage(
    db_session,
):
    from sqlalchemy import text

    from app.services.account_value.completeness import (
        covered,
        load_confirmations,
        record_confirmation,
        record_revocation,
    )

    account = "upbit:upbit:" + uuid4().hex * 2
    start, end = date(2026, 1, 1), date(2026, 1, 2)
    assert (
        await record_confirmation(
            db_session, account, start, end, "operator", "checked", commit=True
        )
        == "inserted"
    )
    confirmation_id = await db_session.scalar(
        text(
            "SELECT id FROM review.account_flow_confirmations WHERE account = :account"
        ),
        {"account": account},
    )
    assert confirmation_id is not None
    assert covered(start, end, await load_confirmations(db_session, start, end), [])
    assert (
        await record_revocation(
            db_session,
            confirmation_id,
            "operator",
            "잘못된 확인",
            commit=True,
        )
        == "revoked"
    )
    assert not covered(start, end, await load_confirmations(db_session, start, end), [])

    for statement in (
        "UPDATE review.account_flow_confirmation_revocations SET reason = 'changed'",
        "DELETE FROM review.account_flow_confirmation_revocations",
        "TRUNCATE review.account_flow_confirmation_revocations",
    ):
        with pytest.raises(DBAPIError, match="account_value_immutable"):
            async with db_session.begin_nested():
                await db_session.execute(text(statement))

    path = Path("alembic/versions/20261115_hk4617_flow_confirmation_revocations.py")
    spec = importlib.util.spec_from_file_location("flow_revoke_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()

    with pytest.raises(
        DBAPIError, match="account_flow_confirmation_revocation_rows_retained"
    ):
        async with db_session.begin_nested():
            connection = await db_session.connection()
            await connection.run_sync(downgrade)


@pytest.mark.asyncio
async def test_confirmation_waits_for_in_flight_flow_commit(db_session):
    """The two CLI writers run on independent sessions: a confirmation must
    block until a same-account flow insert commits, so it cannot appear to
    cover a flow it never saw."""
    import asyncio

    from sqlalchemy import text

    from app.core.db import AsyncSessionLocal
    from app.services.account_value.completeness import record_confirmation

    account = "upbit:upbit:" + uuid4().hex * 2
    day = date(2026, 1, 4)
    async with AsyncSessionLocal() as writer:
        await writer.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:account))"),
            {"account": account},
        )
        await writer.execute(
            text(
                "INSERT INTO review.account_external_flows "
                "(flow_id, account, market, currency, amount, day, created_at) "
                "VALUES (:flow_id, :account, 'account', 'KRW', 10, :day, "
                "clock_timestamp())"
            ),
            {"flow_id": uuid4(), "account": account, "day": day},
        )

        async def confirm():
            async with AsyncSessionLocal() as confirmer:
                return await record_confirmation(
                    confirmer,
                    account,
                    day,
                    day,
                    "operator",
                    "checked",
                    commit=True,
                )

        task = asyncio.create_task(confirm())
        for _ in range(100):
            await asyncio.sleep(0.1)
            if task.done():
                break
            waiting = await db_session.scalar(
                text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND NOT granted"
                )
            )
            if waiting:
                break
        assert not task.done()
        await writer.commit()
        assert await asyncio.wait_for(task, 15) == "inserted"
