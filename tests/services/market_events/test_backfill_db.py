"""CI-only PostgreSQL contracts for the backfill request table (hk 6409).

The table is append-only, so committed rows stay in the run-owned test database.
Each test uses its own far-future quota day and its own random window, so rows
of other tests never change its quota or hash.
"""

from __future__ import annotations

import asyncio
import importlib.util
import secrets
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.db import AsyncSessionLocal, engine
from app.services.market_events.backfill.policy import (
    BackfillRefused,
    BackfillRequest,
)
from app.services.market_events.backfill.service import run_request, submit_request
from app.services.market_events.backfill.store import BackfillStore
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.market_events.backfill_support import make_request

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TABLE = "review.market_event_backfill_requests"
MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic/versions/20261128_hk6409_backfill_requests.py"
)


def _quota_day() -> datetime:
    start = datetime(2100, 1, 1, 12, tzinfo=UTC)
    return start + timedelta(days=secrets.randbelow(200_000))


def _request(**over) -> BackfillRequest:
    start = date(1990, 1, 1) + timedelta(days=secrets.randbelow(10_000))
    return make_request(from_date=start, to_date=start + timedelta(days=1), **over)


class Store(BackfillStore):
    """The real SQL store with the real lock and a pinned decision clock."""

    def __init__(self, session, clock: datetime) -> None:
        super().__init__(session)
        self.clock = clock

    async def lock_source(self, source: str) -> datetime:
        await super().lock_source(source)
        return self.clock


async def _rows(session, request: BackfillRequest) -> list[tuple]:
    result = await session.execute(
        text(
            f"SELECT state, exit_code, failure_code, request_ref FROM {TABLE} "
            "WHERE request_hash = :h ORDER BY id"
        ),
        {"h": request.request_hash},
    )
    return [tuple(row) for row in result.all()]


async def test_the_same_request_again_adds_no_row_and_a_run_appends_two(
    db_session,
) -> None:
    clock, request = _quota_day(), _request()
    store = Store(db_session, clock)
    assert (await submit_request(store, request)).status == "recorded"
    assert (await submit_request(store, request)).status == "duplicate"
    assert [r[0] for r in await _rows(db_session, request)] == ["requested"]

    async def runner(_argv: list[str]) -> int:
        return 2

    outcome = await run_request(store, request, runner)
    assert outcome.failure_code == "exit_nonzero"
    rows = await _rows(db_session, request)
    assert [r[:3] for r in rows] == [
        ("requested", None, None),
        ("started", None, None),
        ("failed", 2, "exit_nonzero"),
    ]
    assert {r[3] for r in rows} == {request.request_ref}
    with pytest.raises(BackfillRefused, match="already_run"):
        await run_request(store, request, runner)
    assert len(await _rows(db_session, request)) == 3


async def test_two_operators_cannot_both_pass_the_daily_quota(db_session) -> None:
    clock = _quota_day()
    requests = [_request(reason_code="gap_fill"), _request(reason_code="verification")]

    async def submit(request: BackfillRequest) -> str:
        async with AsyncSessionLocal() as session:
            try:
                return (await submit_request(Store(session, clock), request)).status
            except BackfillRefused as refused:
                return refused.code

    outcomes = await asyncio.gather(*(submit(r) for r in requests))
    assert sorted(outcomes) == ["recorded", "request_quota_exceeded"]
    stored = [len(await _rows(db_session, r)) for r in requests]
    assert sorted(stored) == [0, 1]


async def test_update_delete_and_truncate_are_rejected(db_session) -> None:
    request = _request()
    await submit_request(Store(db_session, _quota_day()), request)
    for statement in (
        f"UPDATE {TABLE} SET reason = 'x' WHERE request_hash = :h",
        f"DELETE FROM {TABLE} WHERE request_hash = :h",
        f"TRUNCATE {TABLE}",
    ):
        with pytest.raises(DBAPIError) as err:
            async with db_session.begin_nested():
                await db_session.execute(text(statement), {"h": request.request_hash})
        assert "append-only" in str(err.value)
    await db_session.rollback()
    assert len(await _rows(db_session, request)) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"state": "rejected"},
        {"to_date": date(2000, 2, 1)},
        {"from_date": date(2000, 1, 2)},
        {"symbols": [f"S{i}" for i in range(21)]},
        {"reason": "  "},
        {"state": "done"},
        {"state": "done", "exit_code": 1},
        {"state": "failed", "failure_code": "exit_nonzero"},
        {"state": "failed", "failure_code": "timeout", "exit_code": 1},
        {"state": "failed"},
        {"exit_code": 0},
    ],
)
async def test_check_constraints_refuse_a_bad_row(db_session, bad: dict) -> None:
    row = {
        "request_ref": "bfr-test",
        "request_hash": secrets.token_hex(32),
        "source": "dart",
        "market": "kr",
        "category": "disclosure",
        "symbols": [],
        "from_date": date(2000, 1, 1),
        "to_date": date(2000, 1, 1),
        "reason_code": "gap_fill",
        "reason": "x",
        "requested_by": "operator",
        "state": "requested",
        "exit_code": None,
        "failure_code": None,
        **bad,
    }
    columns = ", ".join(row)
    values = ", ".join(f":{name}" for name in row)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(f"INSERT INTO {TABLE} ({columns}) VALUES ({values})"), row
            )
    await db_session.rollback()


def _migration(step: str):
    def run(sync_conn) -> None:
        spec = importlib.util.spec_from_file_location("hk6409_db", MIGRATION)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(module, step)()

    return run


async def test_downgrade_refuses_rows_and_the_migration_round_trips(
    db_session,
) -> None:
    validate_run_owned_database_url(engine.url)
    request = _request()
    await submit_request(Store(db_session, _quota_day()), request)
    async with engine.connect() as conn:
        try:
            with pytest.raises(RuntimeError, match="refusing to downgrade"):
                await conn.run_sync(_migration("downgrade"))
            await conn.rollback()
            # Empty the table only inside this rolled-back transaction.
            await conn.execute(text(f"ALTER TABLE {TABLE} DISABLE TRIGGER USER"))
            await conn.execute(text(f"DELETE FROM {TABLE}"))
            await conn.run_sync(_migration("downgrade"))
            assert (
                await conn.execute(text(f"SELECT to_regclass('{TABLE}')"))
            ).scalar_one() is None
            await conn.run_sync(_migration("upgrade"))
            triggers = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_trigger WHERE tgrelid = "
                        f"'{TABLE}'::regclass AND NOT tgisinternal"
                    )
                )
            ).scalar_one()
            assert triggers == 2
        finally:
            await conn.rollback()
    assert len(await _rows(db_session, request)) == 1
