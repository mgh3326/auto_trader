from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.jobs.market_quote_snapshots import (
    MarketQuoteSnapshotBuildRequest,
    run_market_quote_snapshot_build,
)
from app.models.market_quote_snapshot import MarketQuoteSnapshot

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Matches _MANUAL_QUOTE_MAX_AGE from app/mcp_server/tooling/alpaca_paper_orders.py
_MANUAL_QUOTE_MAX_AGE = dt.timedelta(minutes=5)

MARKET_QUOTE_SNAPSHOT_TOOL_NAMES: set[str] = {
    "market_quote_snapshot_latest",
    "market_quote_snapshot_ensure",
}


async def market_quote_snapshot_latest(market: str, symbol: str) -> dict[str, Any]:
    """Retrieve the latest quote snapshot for a market and symbol.

    Args:
        market: "kr" or "us".
        symbol: The ticker symbol.
    """
    market = str(market).strip().lower()
    symbol = str(symbol).strip().upper()
    if market not in ("kr", "us"):
        raise ValueError("market must be 'kr' or 'us'")

    async with AsyncSessionLocal() as db:
        stmt = (
            select(MarketQuoteSnapshot)
            .where(
                MarketQuoteSnapshot.market == market,
                MarketQuoteSnapshot.symbol == symbol,
            )
            .order_by(MarketQuoteSnapshot.snapshot_at.desc())
            .limit(1)
        )
        snap = (await db.execute(stmt)).scalar_one_or_none()

    if snap is None:
        return {"success": True, "found": False}

    snapshot_at = snap.snapshot_at
    if snapshot_at.tzinfo is None:
        snapshot_at = snapshot_at.replace(tzinfo=dt.UTC)

    now = dt.datetime.now(dt.UTC)
    age_seconds = (now - snapshot_at).total_seconds()
    is_fresh = (now - snapshot_at) <= _MANUAL_QUOTE_MAX_AGE

    return {
        "success": True,
        "found": True,
        "id": snap.id,
        "market": snap.market,
        "symbol": snap.symbol,
        "price": float(snap.price),
        "source": snap.source,
        "snapshot_at": snapshot_at.isoformat(),
        "age_seconds": age_seconds,
        "is_fresh": is_fresh,
    }


async def market_quote_snapshot_ensure(market: str, symbol: str) -> dict[str, Any]:
    """Ensure a fresh quote snapshot exists for a market and symbol, building one if needed.

    Args:
        market: "kr" or "us".
        symbol: The ticker symbol.
    """
    market = str(market).strip().lower()
    symbol = str(symbol).strip().upper()
    if market not in ("kr", "us"):
        raise ValueError("market must be 'kr' or 'us'")

    async with AsyncSessionLocal() as db:
        stmt = (
            select(MarketQuoteSnapshot)
            .where(
                MarketQuoteSnapshot.market == market,
                MarketQuoteSnapshot.symbol == symbol,
            )
            .order_by(MarketQuoteSnapshot.snapshot_at.desc())
            .limit(1)
        )
        snap = (await db.execute(stmt)).scalar_one_or_none()

    if snap is not None:
        snapshot_at = snap.snapshot_at
        if snapshot_at.tzinfo is None:
            snapshot_at = snapshot_at.replace(tzinfo=dt.UTC)
        now = dt.datetime.now(dt.UTC)
        age = now - snapshot_at
        if age <= _MANUAL_QUOTE_MAX_AGE:
            return {
                "success": True,
                "found": True,
                "reused": True,
                "id": snap.id,
                "market": snap.market,
                "symbol": snap.symbol,
                "price": float(snap.price),
                "source": snap.source,
                "snapshot_at": snapshot_at.isoformat(),
                "age_seconds": age.total_seconds(),
                "is_fresh": True,
            }

    # Otherwise build it
    request = MarketQuoteSnapshotBuildRequest(
        market=market,
        symbols=(symbol,),
        commit=True,
        limit=None,
    )

    try:
        build_result = await run_market_quote_snapshot_build(request)
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    if build_result.snapshots_built == 0:
        warning_msg = (
            build_result.warnings[0]
            if build_result.warnings
            else "failed to build snapshot"
        )
        return {"success": False, "error": f"Build failed: {warning_msg}"}

    async with AsyncSessionLocal() as db:
        stmt = (
            select(MarketQuoteSnapshot)
            .where(
                MarketQuoteSnapshot.market == market,
                MarketQuoteSnapshot.symbol == symbol,
            )
            .order_by(MarketQuoteSnapshot.snapshot_at.desc())
            .limit(1)
        )
        new_snap = (await db.execute(stmt)).scalar_one_or_none()

    if new_snap is None:
        return {"success": False, "error": "New snapshot not found after build"}

    snapshot_at = new_snap.snapshot_at
    if snapshot_at.tzinfo is None:
        snapshot_at = snapshot_at.replace(tzinfo=dt.UTC)
    now = dt.datetime.now(dt.UTC)
    age_seconds = (now - snapshot_at).total_seconds()
    is_fresh = (now - snapshot_at) <= _MANUAL_QUOTE_MAX_AGE

    return {
        "success": True,
        "found": True,
        "reused": False,
        "id": new_snap.id,
        "market": new_snap.market,
        "symbol": new_snap.symbol,
        "price": float(new_snap.price),
        "source": new_snap.source,
        "snapshot_at": snapshot_at.isoformat(),
        "age_seconds": age_seconds,
        "is_fresh": is_fresh,
    }


def register_market_quote_snapshot_tools(mcp: FastMCP) -> None:
    """Register market quote snapshot MCP tools."""
    _ = mcp.tool(
        name="market_quote_snapshot_latest",
        description="Retrieve the latest quote snapshot for a given market ('kr' or 'us') and symbol.",
    )(market_quote_snapshot_latest)
    _ = mcp.tool(
        name="market_quote_snapshot_ensure",
        description="Ensure a fresh quote snapshot (age < 5m) exists for a market and symbol. Reuses if fresh, builds if stale.",
    )(market_quote_snapshot_ensure)
