"""ROB-286 — Smoke test that BinanceTestnetOrderLedger is registered.

Verifies the ORM model is importable from ``app.models`` and that the
table is created by ``Base.metadata.create_all`` (via the ``db_session``
fixture). Migration-vs-create_all parity is verified by the operator
runbook on the real server; we cannot run alembic against the test_db
because the create_all path drives schema.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import BinanceTestnetOrderLedger


@pytest.mark.asyncio
async def test_model_registered_and_table_exists(db_session) -> None:
    """The table is created by ``Base.metadata.create_all`` in the fixture."""
    # An empty select against the table works without raising
    # (would raise UndefinedTable if the table wasn't created).
    result = await db_session.execute(select(BinanceTestnetOrderLedger).limit(1))
    rows = result.all()
    assert rows == []  # fresh test_db
