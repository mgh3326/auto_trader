"""ROB-1461: real committed ledger reads; run only in CI's test database."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.core.db import AsyncSessionLocal, engine
from app.models.execution_ledger import ExecutionLedger
from app.services.execution_intent_gateway.query import SqlLedgerEvidenceReader
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.execution_intent_gateway.support import NOW, intent_snapshot
from tests.services.execution_intent_gateway.test_intent_fills import children, fill
from tests.services.execution_ledger._quarantine_fixtures import purge_test_ledger_rows

validate_run_owned_database_url(engine.url)
pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_fills_read_only_committed_effective_own_orders(db_session):
    tag = uuid4().hex[:10]
    number = f"fills-{tag}"
    intent = intent_snapshot(symbol=f"s-{tag}")
    journal = [replace(children()[0], broker_order_id=number)]
    rows = [
        fill(None, number, symbol=intent.symbol, fill_seq=1),
        fill(
            None,
            number,
            symbol=intent.symbol,
            fill_seq=2,
            source="reconciler",
            filled_qty=Decimal("3"),
        ),
        fill(None, f"other-{tag}", symbol=intent.symbol, fill_seq=3),
        fill(None, number, symbol=intent.symbol, fill_seq=4, account_mode="live"),
        fill(
            None,
            number,
            symbol=intent.symbol,
            fill_seq=5,
            filled_at=NOW - timedelta(days=1),
        ),
        fill(
            None,
            number,
            symbol=intent.symbol,
            fill_seq=6,
            quarantined_at=NOW,
            quarantine_reason="test",
            quarantined_by="test",
        ),
        fill(None, number, symbol=intent.symbol, fill_seq=7, side="sell"),
    ]
    db_session.add_all(rows)
    await db_session.commit()
    reader = SqlLedgerEvidenceReader(AsyncSessionLocal)
    statements = []

    def record(conn, cursor, statement, *rest):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        orders = await reader.read_fills(intent, journal)
        assert all(
            statement.lstrip()
            .upper()
            .startswith(("SET TRANSACTION READ ONLY", "SELECT"))
            for statement in statements
        )
        assert "raw_payload_json" not in " ".join(statements)
        async with AsyncSessionLocal() as pending:
            pending.add(
                fill(
                    None,
                    number,
                    symbol=intent.symbol,
                    fill_seq=8,
                    source="manual_import",
                )
            )
            await pending.flush()
            assert await reader.read_fills(intent, journal) == orders
            await pending.rollback()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
        await purge_test_ledger_rows(
            db_session, ExecutionLedger.symbol == intent.symbol
        )
        await db_session.commit()
    assert orders[0]["cumulative_qty"] == 3
    assert [f["fill_id"] for f in orders[0]["fills"]] == [str(rows[1].id)]
    assert any(s == "SET TRANSACTION READ ONLY" for s in statements)
    assert "FOR UPDATE" not in " ".join(statements)
