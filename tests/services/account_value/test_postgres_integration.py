"""Postgres-only checks, run by CI against its isolated migrated database."""

import importlib.util
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from app.models.account_value import AccountValueSnapshot
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
    await capture(reader, db_session, commit=True)
    await capture(reader, db_session, commit=True)
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
