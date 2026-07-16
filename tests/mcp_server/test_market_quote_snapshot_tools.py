from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.db import AsyncSessionLocal
from app.mcp_server.profiles import McpProfile

# We will import these once the module is implemented
# For now, we import them inside functions or handle potential ImportError during TDD RED phase.
# To make it fail cleanly during TDD RED phase, we will do top-level imports that will fail.
from app.mcp_server.tooling.market_quote_snapshot_tools import (
    market_quote_snapshot_ensure,
    market_quote_snapshot_latest,
)
from app.mcp_server.tooling.registry import register_all_tools
from app.models.market_quote_snapshot import MarketQuoteSnapshot
from tests._mcp_tooling_support import DummyMCP

pytestmark = [pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    stmt = delete(MarketQuoteSnapshot).where(
        MarketQuoteSnapshot.symbol == "MOCKSNAP",
        MarketQuoteSnapshot.market == "us",
    )
    async with AsyncSessionLocal() as db:
        await db.execute(stmt)
        await db.commit()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(stmt)
        await db.commit()


async def _seed_snapshot(
    snapshot_at: dt.datetime, price: Decimal = Decimal("150.00")
) -> int:
    async with AsyncSessionLocal() as db:
        row = MarketQuoteSnapshot(
            market="us",
            symbol="MOCKSNAP",
            source="yahoo",
            snapshot_at=snapshot_at,
            price=price,
            raw_payload={"source_api": "yahoo", "regularMarketPrice": float(price)},
        )
        db.add(row)
        await db.commit()
        return row.id


async def test_latest_row_exists():
    now = dt.datetime.now(dt.UTC)

    # 4m 59s ago -> fresh
    fresh_time = now - dt.timedelta(minutes=4, seconds=59)
    fresh_id = await _seed_snapshot(fresh_time, Decimal("100.00"))

    # 5m 1s ago -> stale
    stale_time = now - dt.timedelta(minutes=5, seconds=1)
    stale_id = await _seed_snapshot(stale_time, Decimal("90.00"))

    # latest should return the fresh one because it's newer (ordered by snapshot_at desc)
    res = await market_quote_snapshot_latest("us", "MOCKSNAP")
    assert res["success"] is True
    assert res["found"] is True
    assert res["id"] == fresh_id
    assert res["price"] == 100.0
    assert res["is_fresh"] is True

    # Clean up the fresh one to see the stale one
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(MarketQuoteSnapshot).where(MarketQuoteSnapshot.id == fresh_id)
        )
        await db.commit()

    res = await market_quote_snapshot_latest("us", "MOCKSNAP")
    assert res["success"] is True
    assert res["found"] is True
    assert res["id"] == stale_id
    assert res["price"] == 90.0
    assert res["is_fresh"] is False


async def test_latest_no_row():
    res = await market_quote_snapshot_latest("us", "MOCKSNAP")
    assert res["success"] is True
    assert res["found"] is False


async def test_ensure_fresh_exists(monkeypatch):
    now = dt.datetime.now(dt.UTC)
    fresh_time = now - dt.timedelta(minutes=2)
    sid = await _seed_snapshot(fresh_time, Decimal("120.00"))

    # Mock the build function to assert it is NOT called
    mock_build = AsyncMock()
    monkeypatch.setattr(
        "app.mcp_server.tooling.market_quote_snapshot_tools.run_market_quote_snapshot_build",
        mock_build,
    )

    res = await market_quote_snapshot_ensure("us", "MOCKSNAP")
    assert res["success"] is True
    assert res["found"] is True
    assert res["reused"] is True
    assert res["id"] == sid
    assert res["price"] == 120.0
    assert res["is_fresh"] is True
    mock_build.assert_not_called()


async def test_ensure_stale_or_missing_triggers_build(monkeypatch):
    # Scenario: Missing row, triggers build and returns new snapshot
    from app.jobs.market_quote_snapshots import MarketQuoteSnapshotBuildResult

    async def fake_build(request):
        # Seed a new snapshot inside the build
        await _seed_snapshot(dt.datetime.now(dt.UTC), Decimal("130.00"))
        return MarketQuoteSnapshotBuildResult(
            market=request.market,
            symbols_resolved=1,
            snapshots_built=1,
            committed=True,
            batches=1,
            started_at=dt.datetime.now(dt.UTC),
            finished_at=dt.datetime.now(dt.UTC),
        )

    monkeypatch.setattr(
        "app.mcp_server.tooling.market_quote_snapshot_tools.run_market_quote_snapshot_build",
        fake_build,
    )

    res = await market_quote_snapshot_ensure("us", "MOCKSNAP")
    assert res["success"] is True
    assert res["found"] is True
    assert res["reused"] is False
    assert res["price"] == 130.0
    assert res["is_fresh"] is True


async def test_ensure_build_failure(monkeypatch):
    from app.jobs.market_quote_snapshots import MarketQuoteSnapshotBuildResult

    async def fake_build(request):
        return MarketQuoteSnapshotBuildResult(
            market=request.market,
            symbols_resolved=1,
            snapshots_built=0,
            committed=True,
            batches=1,
            started_at=dt.datetime.now(dt.UTC),
            finished_at=dt.datetime.now(dt.UTC),
            warnings=("quote source unavailable",),
        )

    monkeypatch.setattr(
        "app.mcp_server.tooling.market_quote_snapshot_tools.run_market_quote_snapshot_build",
        fake_build,
    )

    res = await market_quote_snapshot_ensure("us", "MOCKSNAP")
    assert res["success"] is False
    assert "error" in res
    assert "unavailable" in res["error"]


@pytest.mark.unit
async def test_registration_gate(monkeypatch):
    from app.mcp_server.tooling import registry as registry_mod

    # default off -> absent
    monkeypatch.setattr(
        registry_mod.settings,
        "alpaca_paper_default_tools_enabled",
        False,
        raising=False,
    )
    mcp = DummyMCP()
    register_all_tools(mcp, profile=McpProfile.DEFAULT)
    assert "market_quote_snapshot_latest" not in mcp.tools
    assert "market_quote_snapshot_ensure" not in mcp.tools

    # default on -> present
    monkeypatch.setattr(
        registry_mod.settings,
        "alpaca_paper_default_tools_enabled",
        True,
        raising=False,
    )
    mcp2 = DummyMCP()
    register_all_tools(mcp2, profile=McpProfile.DEFAULT)
    assert "market_quote_snapshot_latest" in mcp2.tools
    assert "market_quote_snapshot_ensure" in mcp2.tools

    # US_PAPER profile -> always present
    for flag in (False, True):
        monkeypatch.setattr(
            registry_mod.settings,
            "alpaca_paper_default_tools_enabled",
            flag,
            raising=False,
        )
        mcp3 = DummyMCP()
        register_all_tools(mcp3, profile=McpProfile.US_PAPER)
        assert "market_quote_snapshot_latest" in mcp3.tools
        assert "market_quote_snapshot_ensure" in mcp3.tools
