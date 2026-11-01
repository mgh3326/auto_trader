"""ROB-1450 acceptance 3 against PostgreSQL (the test database, in CI)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.core.db import AsyncSessionLocal, engine
from app.models.paper_trading import PaperAccount, PaperDailySnapshot, PaperPendingOrder
from app.services.dots_history_export import reader
from app.services.dots_history_export.projection import RefHasher, Window, project
from app.services.dots_history_export.spec import BALANCE_UNAVAILABLE
from tests._run_owned_database import validate_run_owned_database_url

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

# A window no other test or real row sits in.
NOW = datetime(2098, 4, 10, 14, 50, tzinfo=UTC)
WINDOW = Window(since=NOW - timedelta(days=5), until=NOW + timedelta(days=5))
SALT = b"db-test-salt-long-enough-for-the-minimum-length-0456"


@pytest_asyncio.fixture
async def paper_account(db_session) -> AsyncIterator[int]:
    account = PaperAccount(
        name=f"rob1450-{uuid.uuid4().hex[:10]}",
        initial_capital=Decimal("10000000"),
        cash_krw=Decimal("7000000"),
        cash_usd=Decimal("250.5"),
    )
    db_session.add(account)
    await db_session.flush()
    account_id = int(account.id)
    db_session.add_all(
        [
            PaperDailySnapshot(
                account_id=account_id,
                snapshot_date=date(2098, 4, 10),
                cash_krw=Decimal("7000000"),
                cash_usd=Decimal("250.5"),
                positions_value=Decimal("3000000"),
                total_equity=Decimal("10000250.5"),
                created_at=NOW,
            ),
            PaperPendingOrder(
                account_id=account_id,
                symbol="KRW-ETH",
                side="buy",
                order_type="limit",
                limit_price=Decimal("4000000"),
                quantity=Decimal("0.5"),
                reserved_krw=Decimal("2001000"),
                status="pending",
                placed_at=NOW - timedelta(hours=2),
            ),
            # Cancelled before the snapshot: its reservation was already returned.
            PaperPendingOrder(
                account_id=account_id,
                symbol="KRW-ETH",
                side="buy",
                order_type="limit",
                limit_price=Decimal("3900000"),
                quantity=Decimal("0.1"),
                reserved_krw=Decimal("390390"),
                status="cancelled",
                placed_at=NOW - timedelta(hours=3),
                cancelled_at=NOW - timedelta(hours=1),
            ),
        ]
    )
    await db_session.commit()
    try:
        yield account_id
    finally:
        await db_session.execute(
            sa.delete(PaperAccount).where(PaperAccount.id == account_id)
        )
        await db_session.commit()


async def test_balance_rows_come_from_the_paper_daily_snapshot(
    paper_account: int,
) -> None:
    async with AsyncSessionLocal() as session:
        try:
            sources = await reader.read_sources(session, WINDOW)
        finally:
            await session.rollback()
    hasher = RefHasher(SALT)
    rows = project(sources, hasher=hasher, window=WINDOW).tables[
        "account_balance_daily"
    ]
    account_ref = hasher.ref("acc", "db_paper", str(paper_account))
    mine = {row["currency"]: row for row in rows if row["account_ref"] == account_ref}
    assert set(mine) == {"KRW", "USD"}
    assert mine["KRW"]["balance_date"] == "2098-04-10"
    assert Decimal(mine["KRW"]["cash"]) == Decimal("7000000")
    assert Decimal(mine["KRW"]["reserved_open_orders"]) == Decimal("2001000")
    assert mine["KRW"]["settled_cash"] is None
    assert Decimal(mine["USD"]["cash"]) == Decimal("250.5")
    markers = [row for row in rows if row["available"] == "false"]
    assert sorted(row["account_alias"] for row in markers) == sorted(
        BALANCE_UNAVAILABLE
    )
