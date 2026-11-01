"""ROB-1438 acceptance 1 and 3 against PostgreSQL (the test database, in CI)."""

from __future__ import annotations

import csv
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from app.core.db import AsyncSessionLocal, engine
from app.models.execution_ledger import ExecutionLedger
from app.models.review import KISLiveOrderLedger
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services.dots_history_export import reader
from app.services.dots_history_export.projection import Window
from app.services.execution_ledger import quarantine as q
from scripts import export_dots_history as cli
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.execution_ledger._quarantine_fixtures import (
    purge_test_ledger_rows,
    row_kwargs,
)

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

# A window no other test or real row sits in.
NOW = datetime(2098, 3, 10, 5, 30, tzinfo=UTC)
WINDOW = Window(since=NOW - timedelta(days=5), until=NOW + timedelta(days=5))
SALT = b"db-test-salt-long-enough-for-the-minimum-length-0123"


class _Seeded:
    def __init__(self, *, symbol: str, order_no: str, phantom_order_no: str) -> None:
        self.symbol = symbol
        self.order_no = order_no
        self.phantom_order_no = phantom_order_no


@pytest_asyncio.fixture
async def seeded(db_session) -> AsyncIterator[_Seeded]:
    tag = uuid.uuid4().hex[:5].upper()
    symbol = f"D{tag}"
    data = _Seeded(
        symbol=symbol, order_no=f"00R{tag}01", phantom_order_no=f"00P{tag}01"
    )
    order = KISLiveOrderLedger(
        trade_date=NOW,
        symbol=symbol,
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=Decimal("10"),
        price=Decimal("5000"),
        order_no=data.order_no,
        status="filled",
        lifecycle_state="filled",
        strategy="db-test",
        filled_qty=Decimal("10"),
        avg_fill_price=Decimal("5000"),
        created_at=NOW,
    )
    real = ExecutionLedger(
        **ExecutionLedgerUpsert(
            **row_kwargs(
                symbol=symbol,
                order_no=data.order_no.lstrip("0"),
                source="reconciler",
                raw_payload_json=None,
                fill_seq=0,
                filled_qty="10",
                filled_notional="50000",
                filled_at=(NOW + timedelta(minutes=5)).isoformat(),
            )
        ).model_dump()
    )
    phantom = ExecutionLedger(
        **ExecutionLedgerUpsert(
            **row_kwargs(
                symbol=symbol,
                order_no=data.phantom_order_no,
                filled_at=(NOW + timedelta(minutes=6)).isoformat(),
            )
        ).model_dump()
    )
    db_session.add_all([order, real, phantom])
    await db_session.flush()
    phantom_id = int(phantom.id)
    await db_session.commit()
    result = await q.commit_quarantine(
        db_session, [phantom_id], reason="db test phantom", actor="db-test"
    )
    assert result.status == "committed"
    await db_session.rollback()
    try:
        yield data
    finally:
        await purge_test_ledger_rows(db_session, ExecutionLedger.symbol == symbol)
        await db_session.execute(
            sa.delete(KISLiveOrderLedger).where(KISLiveOrderLedger.symbol == symbol)
        )
        await db_session.commit()


def _args(tmp_path: Path) -> object:
    salt = tmp_path / "salt"
    salt.write_bytes(SALT)
    os.chmod(salt, 0o600)
    return cli._parser().parse_args(
        [
            "--database-url-env",
            "UNUSED_FOR_INJECTED_FACTORY",
            "--salt-file",
            str(salt),
            "--out",
            str(tmp_path / "export"),
            "--since",
            WINDOW.since.isoformat(),
            "--until",
            WINDOW.until.isoformat(),
            "--code-commit",
            "d" * 40,
        ]
    )


def _rows(out: Path, table: str) -> list[dict[str, str]]:
    with (out / f"{table}.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


async def _ledger_digest(db_session) -> str:
    rows = (
        await db_session.execute(
            sa.text(
                "SELECT count(*), coalesce(max(updated_at)::text, '') "
                "FROM review.execution_ledger UNION ALL "
                "SELECT count(*), coalesce(max(updated_at)::text, '') "
                "FROM review.kis_live_order_ledger"
            )
        )
    ).all()
    await db_session.rollback()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


async def test_export_joins_order_and_fill_and_excludes_the_quarantined_row(
    db_session, seeded: _Seeded, tmp_path: Path
) -> None:
    before = await _ledger_digest(db_session)
    manifest = await cli.run(_args(tmp_path), session_factory=AsyncSessionLocal)
    out = tmp_path / "export"

    [order] = _rows(out, "kis_live_order_ledger")
    [fill] = _rows(out, "execution_ledger")
    assert order["order_ref"] == fill["order_ref"]
    assert fill["fill_source"] == "reconciler"
    assert order["terminal_outcome"] == "filled"
    assert order["filled_at_utc"] == fill["filled_at_utc"]
    assert manifest["tables"]["execution_ledger"]["row_count"] == 1
    assert manifest["tables"]["kis_live_order_ledger"]["row_count"] == 1

    body = "".join(path.read_text("utf-8") for path in out.glob("*.csv"))
    assert seeded.order_no not in body
    assert seeded.order_no.lstrip("0") not in body
    assert seeded.phantom_order_no not in body
    assert await _ledger_digest(db_session) == before


async def test_the_read_session_refuses_a_write(seeded: _Seeded) -> None:
    async with AsyncSessionLocal() as session:
        await reader.read_sources(session, WINDOW)
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await session.execute(
                sa.text("UPDATE review.kis_live_order_ledger SET notes = 'x'")
            )
        await session.rollback()
