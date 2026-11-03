"""A stale eligibility snapshot must be visible without a broker call."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.services.krx_after_market_health import (
    assess_krx_after_market_health,
    load_krx_after_market_health,
)

NOW = datetime(2026, 11, 3, tzinfo=UTC)


@pytest.mark.parametrize(
    ("rows", "oldest", "newest", "status"),
    [
        (0, None, None, "missing"),
        (2766, NOW - timedelta(days=34), NOW - timedelta(days=34), "stale"),
        (2, NOW - timedelta(days=8), NOW, "stale"),
        (1, NOW - timedelta(days=7), NOW - timedelta(days=7), "fresh"),
        (1, NOW - timedelta(days=7, microseconds=1), NOW, "stale"),
        (1, NOW, NOW, "fresh"),
        (1, NOW + timedelta(seconds=1), NOW + timedelta(seconds=1), "unknown"),
        (1, None, NOW, "unknown"),
    ],
)
def test_snapshot_health(rows, oldest, newest, status):
    health = assess_krx_after_market_health(
        row_count=rows, oldest_asof=oldest, newest_asof=newest, now=NOW
    )
    assert health["status"] == status
    assert health["requires_attention"] is (status != "fresh")
    assert health["row_count"] == rows
    assert health["stale_after_days"] == 7
    assert bool(health["warning"]) is (status != "fresh")


@pytest.mark.asyncio
async def test_loader_uses_one_aggregate_select_and_no_writes():
    result = MagicMock()
    result.one.return_value = (2766, NOW - timedelta(days=34), NOW - timedelta(days=34))
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    health = await load_krx_after_market_health(db, now=NOW)
    assert health["status"] == "stale"
    assert health["requires_attention"] is True
    db.execute.assert_awaited_once()
    sql = str(db.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert sql.startswith("SELECT count(")
    assert "min(krx_after_market_eligibility.list_asof)" in sql
    assert "max(krx_after_market_eligibility.list_asof)" in sql
    db.commit.assert_not_called()
    db.add.assert_not_called()
