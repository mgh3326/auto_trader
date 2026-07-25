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


async def check_submit_ready(
    db: Any, snap: MarketQuoteSnapshot
) -> tuple[bool, str | None]:
    """Verify if a snapshot is ready for submission under server-side trust constraints.

    Uses load_market_evidence from alpaca_paper_market_evidence.py.
    """
    market = snap.market.lower()
    symbol = snap.symbol.upper()

    if market == "us":
        asset_class = "us_equity"
        execution_symbol = symbol
    elif market == "crypto":
        asset_class = "crypto"
        from app.services.crypto_execution_mapping import (
            CryptoExecutionMappingError,
            map_upbit_to_alpaca_paper,
        )

        try:
            mapping = map_upbit_to_alpaca_paper(symbol)
            execution_symbol = mapping.execution_symbol
        except CryptoExecutionMappingError:
            return False, "snapshot_symbol_mismatch"
    else:
        return False, "snapshot_symbol_mismatch"

    from app.services.alpaca_paper_market_evidence import (
        MarketEvidenceError,
        load_market_evidence,
    )

    now = dt.datetime.now(dt.UTC)
    snapshot_at = snap.snapshot_at
    if snapshot_at.tzinfo is None:
        snapshot_at = snapshot_at.replace(tzinfo=dt.UTC)

    # Future timestamp check (F2: age_seconds must be >= 0)
    age_seconds = (now - snapshot_at).total_seconds()
    if age_seconds < 0:
        return False, "future_snapshot_at"

    try:
        await load_market_evidence(
            db,
            snap.id,
            execution_symbol=execution_symbol,
            asset_class=asset_class,
            now=now,
            max_age=_MANUAL_QUOTE_MAX_AGE,
        )
        return True, None
    except MarketEvidenceError as exc:
        return False, exc.code
    except Exception:
        return False, "snapshot_not_submittable"


async def market_quote_snapshot_latest(market: str, symbol: str) -> dict[str, Any]:
    """Retrieve the latest quote snapshot for a market and symbol.

    Args:
        market: "kr", "us", or "crypto".
        symbol: The ticker symbol.
    """
    market = str(market).strip().lower()
    symbol = str(symbol).strip().upper()
    if market not in ("kr", "us", "crypto"):
        raise ValueError("market must be 'kr', 'us', or 'crypto'")

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
            return {"success": True, "found": False, "submit_ready": False}

        submit_ready, reason_code = await check_submit_ready(db, snap)

        snapshot_at = snap.snapshot_at
        if snapshot_at.tzinfo is None:
            snapshot_at = snapshot_at.replace(tzinfo=dt.UTC)

        now = dt.datetime.now(dt.UTC)
        age_seconds = (now - snapshot_at).total_seconds()
        is_fresh = 0.0 <= age_seconds <= 300.0

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
        "submit_ready": submit_ready,
        "reason_code": reason_code,
    }


async def market_quote_snapshot_ensure(market: str, symbol: str) -> dict[str, Any]:
    """Ensure a fresh quote snapshot exists for a market and symbol, building one if needed.

    Args:
        market: "kr", "us", or "crypto".
        symbol: The ticker symbol.
    """
    market = str(market).strip().lower()
    symbol = str(symbol).strip().upper()
    if market not in ("kr", "us", "crypto"):
        raise ValueError("market must be 'kr', 'us', or 'crypto'")

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
            age_seconds = (now - snapshot_at).total_seconds()

            # F2: 0 <= age_seconds <= 300 for reuse eligibility
            if 0.0 <= age_seconds <= 300.0:
                submit_ready, reason_code = await check_submit_ready(db, snap)
                if submit_ready:
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
                        "age_seconds": age_seconds,
                        "is_fresh": True,
                        "submit_ready": True,
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
        return {"success": False, "error": str(exc), "reason_code": "build_failed"}

    if build_result.snapshots_built == 0:
        warning_msg = (
            build_result.warnings[0]
            if build_result.warnings
            else "failed to build snapshot"
        )
        return {
            "success": False,
            "error": f"Build failed: {warning_msg}",
            "reason_code": "build_failed",
        }

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
            return {
                "success": False,
                "error": "New snapshot not found after build",
                "reason_code": "build_failed",
            }

        # Validate newly built snapshot
        submit_ready, reason_code = await check_submit_ready(db, new_snap)

        snapshot_at = new_snap.snapshot_at
        if snapshot_at.tzinfo is None:
            snapshot_at = snapshot_at.replace(tzinfo=dt.UTC)
        now = dt.datetime.now(dt.UTC)
        age_seconds = (now - snapshot_at).total_seconds()
        is_fresh = 0.0 <= age_seconds <= 300.0

        if not submit_ready:
            error_code = (
                "stale_after_build"
                if reason_code == "stale_trusted_snapshot"
                else reason_code
            )
            return {
                "success": False,
                "error": f"Build resulted in an invalid or stale snapshot: {error_code}",
                "reason_code": error_code,
            }

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
        "submit_ready": True,
    }


def register_market_quote_snapshot_tools(mcp: FastMCP) -> None:
    """Register market quote snapshot MCP tools."""
    _ = mcp.tool(
        name="market_quote_snapshot_latest",
        description="Retrieve the latest quote snapshot for a given market ('kr', 'us', or 'crypto') and symbol.",
    )(market_quote_snapshot_latest)
    _ = mcp.tool(
        name="market_quote_snapshot_ensure",
        description="Ensure a fresh quote snapshot (age < 5m) exists for a market and symbol. Reuses if fresh, builds if stale.",
    )(market_quote_snapshot_ensure)
