"""The shared paper/demo intent contract needs no database or broker."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.mock_strategy import VENUES
from app.services.mock_strategy.recorder import MockStrategyRecorder, OrderIntent


def intent(**overrides):
    values = {
        "strategy_id": "fixture.v1",
        "venue": "alpaca_paper",
        "account_ref": "fixture",
        "episode_id": "episode",
        "symbol": "AAPL",
        "side": "buy",
        "qty": None,
        "notional": Decimal("10000"),
        "client_order_id": "fixture-entry",
        "exit_due_at": datetime(2026, 11, 9, 14, 30, tzinfo=UTC),
        "meta": {},
    }
    return OrderIntent(**(values | overrides))


@pytest.mark.parametrize("venue", VENUES)
def test_all_four_mock_venues_and_large_paper_sizes(venue):
    assert intent(venue=venue, notional=Decimal("100000000")).venue == venue


@pytest.mark.parametrize(
    "changes",
    [
        {"venue": "alpaca_live"},
        {"side": "liquidate"},
        {"strategy_id": " "},
        {"qty": None, "notional": None},
        {"notional": Decimal("NaN")},
        {"notional": Decimal("Infinity")},
        {"qty": Decimal("-1")},
        {"exit_due_at": datetime(2026, 11, 9)},
    ],
)
def test_invalid_boundary(changes):
    with pytest.raises(ValueError):
        replace(intent(), **changes)


@pytest.mark.asyncio
async def test_recorder_keeps_first_open_timestamp_with_fake_session():
    row = SimpleNamespace(
        status="accepted", broker_order_id=None, opened_at=None, meta={}
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def begin(self):
            return self

        async def scalars(self, statement):
            return SimpleNamespace(one=lambda: row)

        async def flush(self):
            pass

        def expunge(self, value):
            assert value is row

    recorder = MockStrategyRecorder(session_factory=Session)
    first_open = datetime(2026, 11, 9, 14, 30, tzinfo=UTC)
    await recorder.update("fixture-entry", status="filled", opened_at=first_open)
    refreshed = await recorder.update(
        "fixture-entry", status="filled", opened_at=first_open + timedelta(days=1)
    )
    assert refreshed.opened_at == first_open


class ExpiringSession:
    """Fake SQL results, real SQLAlchemy commit/expiration/detachment semantics."""

    def __init__(self, expire):
        from sqlalchemy.orm import Session

        self.orm = Session(expire_on_commit=expire)
        self.rows = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.orm.close()

    @asynccontextmanager
    async def begin(self):
        with self.orm.begin():
            yield

    async def execute(self, statement):
        from sqlalchemy.orm import make_transient_to_detached

        from app.models.mock_strategy import MockStrategyOrder

        values = statement.compile().params
        coid = values["client_order_id"]
        if coid not in self.rows:
            fields = {
                column.name: None for column in MockStrategyOrder.__table__.columns
            }
            row = MockStrategyOrder(**(fields | values | {"id": len(self.rows) + 1}))
            make_transient_to_detached(row)
            self.orm.add(row)  # persistent and clean; commit needs no database
            self.rows[coid] = row

    async def scalars(self, statement):
        from types import SimpleNamespace

        coid = statement.compile().params["client_order_id_1"]
        return SimpleNamespace(one=lambda: self.rows[coid])

    def expunge(self, row):
        self.orm.expunge(row)


@pytest.mark.asyncio
@pytest.mark.parametrize("expire", [False, True])
async def test_record_many_returns_loaded_rows_after_real_orm_commit(expire):
    from sqlalchemy import inspect

    from app.services.mock_strategy.recorder import MockStrategyRecorder

    recorder = MockStrategyRecorder(lambda: ExpiringSession(expire))
    values = [intent(), intent(client_order_id="fixture-second", symbol="NVDA")]
    rows = await recorder.record_many(values)
    assert [row.client_order_id for row in rows] == [i.client_order_id for i in values]
    assert [row.symbol for row in rows] == ["AAPL", "NVDA"]
    assert all(row.notional == Decimal("10000") and row.meta == {} for row in rows)
    assert all(
        inspect(row).detached and not inspect(row).expired_attributes for row in rows
    )


class LockSession:
    def __init__(self, held, *, fail_unlock=False):
        self.held = held
        self.xact_keys = set()
        self.fail_unlock = fail_unlock
        self.in_transaction = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.held.difference_update(self.xact_keys)

    @asynccontextmanager
    async def begin(self):
        self.in_transaction = True
        try:
            yield
        finally:
            self.held.difference_update(self.xact_keys)
            self.in_transaction = False

    async def scalar(self, statement, parameters):
        key = parameters["key"]
        if key in self.held:
            return False
        self.held.add(key)
        if "pg_try_advisory_xact_lock" in str(statement):
            assert self.in_transaction
            self.xact_keys.add(key)
        return True

    async def execute(self, statement, parameters):
        if self.fail_unlock:
            raise RuntimeError("fixture session unlock failed")
        self.held.discard(parameters["key"])


@pytest.mark.asyncio
async def test_account_lock_excludes_other_callers_and_releases_on_exit():
    from app.services.mock_strategy.recorder import MockStrategyRecorder

    held = set()
    recorder = MockStrategyRecorder(lambda: LockSession(held))
    async with recorder.account_lock(venue="alpaca_paper", account_ref="fixture"):
        with pytest.raises(ValueError, match="already in use"):
            async with recorder.account_lock(
                venue="alpaca_paper", account_ref="fixture"
            ):
                pytest.fail("same account lock was acquired twice")
        assert held
    assert not held


@pytest.mark.asyncio
async def test_account_lock_cancellation_needs_no_session_unlock_and_releases():
    from app.services.mock_strategy.recorder import MockStrategyRecorder

    held = set()
    recorder = MockStrategyRecorder(lambda: LockSession(held, fail_unlock=True))
    stop = asyncio.CancelledError("fixture cancellation")
    with pytest.raises(asyncio.CancelledError) as caught:
        async with recorder.account_lock(venue="alpaca_paper", account_ref="fixture"):
            raise stop
    assert caught.value is stop and not held
    async with recorder.account_lock(venue="alpaca_paper", account_ref="fixture"):
        assert held
    assert not held
