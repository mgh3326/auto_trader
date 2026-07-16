from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.db import AsyncSessionLocal
from app.mcp_server.profiles import McpProfile
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
        MarketQuoteSnapshot.symbol.in_(["MOCKSNAP", "KRW-BTC"])
    )
    async with AsyncSessionLocal() as db:
        await db.execute(stmt)
        await db.commit()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(stmt)
        await db.commit()


async def _seed_snapshot(
    snapshot_at: dt.datetime,
    price: Decimal = Decimal("150.00"),
    market: str = "us",
    symbol: str = "MOCKSNAP",
    raw_payload: dict | None = None,
) -> int:
    if raw_payload is None:
        raw_payload = {"source_api": "yahoo", "regularMarketPrice": float(price)}
    async with AsyncSessionLocal() as db:
        row = MarketQuoteSnapshot(
            market=market,
            symbol=symbol,
            source="yahoo" if market == "us" else "upbit",
            snapshot_at=snapshot_at,
            price=price,
            raw_payload=raw_payload,
        )
        db.add(row)
        await db.commit()
        return row.id


@pytest.mark.integration
async def test_latest_row_exists():
    now = dt.datetime.now(dt.UTC)

    # 4m 59s ago -> fresh
    fresh_time = now - dt.timedelta(minutes=4, seconds=59)
    fresh_id = await _seed_snapshot(fresh_time, Decimal("100.00"))

    # 5m 1s ago -> stale
    stale_time = now - dt.timedelta(minutes=5, seconds=1)
    stale_id = await _seed_snapshot(stale_time, Decimal("90.00"))

    res = await market_quote_snapshot_latest("us", "MOCKSNAP")
    assert res["success"] is True
    assert res["found"] is True
    assert res["id"] == fresh_id
    assert res["price"] == 100.0
    assert res["is_fresh"] is True
    assert res["submit_ready"] is True

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
    assert res["submit_ready"] is False
    assert res["reason_code"] == "stale_trusted_snapshot"


@pytest.mark.integration
async def test_latest_no_row():
    res = await market_quote_snapshot_latest("us", "MOCKSNAP")
    assert res["success"] is True
    assert res["found"] is False
    assert res["submit_ready"] is False


@pytest.mark.integration
async def test_ensure_fresh_exists(monkeypatch):
    now = dt.datetime.now(dt.UTC)
    fresh_time = now - dt.timedelta(minutes=2)
    sid = await _seed_snapshot(fresh_time, Decimal("120.00"))

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


@pytest.mark.integration
async def test_ensure_stale_or_missing_triggers_build(monkeypatch):
    from app.jobs.market_quote_snapshots import MarketQuoteSnapshotBuildResult

    async def fake_build(request):
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


@pytest.mark.integration
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
    assert res["reason_code"] == "build_failed"


@pytest.mark.integration
async def test_synthetic_snapshot_rejected(monkeypatch):
    now = dt.datetime.now(dt.UTC)
    await _seed_snapshot(now, Decimal("100.00"), raw_payload={"synthetic": True})

    res_latest = await market_quote_snapshot_latest("us", "MOCKSNAP")
    assert res_latest["submit_ready"] is False
    assert res_latest["reason_code"] == "synthetic_snapshot"

    from app.jobs.market_quote_snapshots import MarketQuoteSnapshotBuildResult

    async def fake_build_synthetic(request):
        await _seed_snapshot(
            dt.datetime.now(dt.UTC), Decimal("100.00"), raw_payload={"synthetic": True}
        )
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
        fake_build_synthetic,
    )

    res_ensure = await market_quote_snapshot_ensure("us", "MOCKSNAP")
    assert res_ensure["success"] is False
    assert res_ensure["reason_code"] == "synthetic_snapshot"


@pytest.mark.integration
async def test_invalid_price_rejected(monkeypatch):
    now = dt.datetime.now(dt.UTC)
    await _seed_snapshot(now, Decimal("0.00"))

    res_latest = await market_quote_snapshot_latest("us", "MOCKSNAP")
    assert res_latest["submit_ready"] is False
    assert res_latest["reason_code"] == "invalid_snapshot_price"

    from app.jobs.market_quote_snapshots import MarketQuoteSnapshotBuildResult

    async def fake_build_zero_price(request):
        await _seed_snapshot(dt.datetime.now(dt.UTC), Decimal("0.00"))
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
        fake_build_zero_price,
    )

    res_ensure = await market_quote_snapshot_ensure("us", "MOCKSNAP")
    assert res_ensure["success"] is False
    assert res_ensure["reason_code"] == "invalid_snapshot_price"


@pytest.mark.integration
async def test_post_build_stale_rejected(monkeypatch):
    from app.jobs.market_quote_snapshots import MarketQuoteSnapshotBuildResult

    async def fake_build_stale(request):
        six_mins_ago = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=6)
        await _seed_snapshot(six_mins_ago, Decimal("100.00"))
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
        fake_build_stale,
    )

    res_ensure = await market_quote_snapshot_ensure("us", "MOCKSNAP")
    assert res_ensure["success"] is False
    assert res_ensure["reason_code"] == "stale_after_build"


@pytest.mark.integration
async def test_future_timestamp_not_reused_and_fails(monkeypatch):
    now = dt.datetime.now(dt.UTC)
    future_time = now + dt.timedelta(minutes=2)
    await _seed_snapshot(future_time, Decimal("100.00"))

    res_latest = await market_quote_snapshot_latest("us", "MOCKSNAP")
    assert res_latest["is_fresh"] is False
    assert res_latest["submit_ready"] is False
    assert res_latest["reason_code"] == "future_snapshot_at"

    from app.jobs.market_quote_snapshots import MarketQuoteSnapshotBuildResult

    async def fake_build_future(request):
        await _seed_snapshot(
            dt.datetime.now(dt.UTC) + dt.timedelta(minutes=2), Decimal("100.00")
        )
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
        fake_build_future,
    )

    res_ensure = await market_quote_snapshot_ensure("us", "MOCKSNAP")
    assert res_ensure["success"] is False
    assert res_ensure["reason_code"] == "future_snapshot_at"


@pytest.mark.integration
async def test_crypto_market_flow(monkeypatch):
    now = dt.datetime.now(dt.UTC)
    fresh_time = now - dt.timedelta(minutes=2)
    sid = await _seed_snapshot(
        fresh_time, Decimal("100000.00"), market="crypto", symbol="KRW-BTC"
    )

    res_latest = await market_quote_snapshot_latest("crypto", "KRW-BTC")
    assert res_latest["success"] is True
    assert res_latest["found"] is True
    assert res_latest["id"] == sid
    assert res_latest["submit_ready"] is True

    mock_build = AsyncMock()
    monkeypatch.setattr(
        "app.mcp_server.tooling.market_quote_snapshot_tools.run_market_quote_snapshot_build",
        mock_build,
    )

    res_ensure = await market_quote_snapshot_ensure("crypto", "KRW-BTC")
    assert res_ensure["success"] is True
    assert res_ensure["reused"] is True
    assert res_ensure["id"] == sid
    mock_build.assert_not_called()


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
