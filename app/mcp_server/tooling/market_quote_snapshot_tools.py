from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field
from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.jobs.market_quote_snapshots import (
    MarketQuoteSnapshotBuildRequest,
    run_market_quote_snapshot_build,
)
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.shared import is_us_equity_symbol
from app.models.market_quote_snapshot import MarketQuoteSnapshot
from app.services.trade_journal.forecast_service import (
    ForecastValidationError,
    get_forecast,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Matches _MANUAL_QUOTE_MAX_AGE from app/mcp_server/tooling/alpaca_paper_orders.py
_MANUAL_QUOTE_MAX_AGE = dt.timedelta(minutes=5)

MARKET_QUOTE_SNAPSHOT_TOOL_NAMES: set[str] = {
    "market_quote_snapshot_latest",
    "market_quote_snapshot_ensure",
}
MARKET_QUOTE_SNAPSHOT_READONLY_TOOL_NAMES: frozenset[str] = frozenset(
    {"market_quote_snapshot_latest"}
)
US_FORECAST_MARKET_QUOTE_SNAPSHOT_TOOL_NAMES: frozenset[str] = frozenset(
    {"us_forecast_market_quote_snapshot_ensure"}
)
MARKET_QUOTE_SNAPSHOT_MUTATION_TOOL_NAMES: frozenset[str] = frozenset(
    {"market_quote_snapshot_ensure"} | US_FORECAST_MARKET_QUOTE_SNAPSHOT_TOOL_NAMES
)

_DIRECTIONAL_LAB_CREATED_BY = "directional-lab"
_NONBLANK_EXACT_STRING = Annotated[
    str,
    Field(min_length=1, pattern=r".*\S.*"),
]


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


def _forecast_quote_failure(error: str, detail: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": error,
        "detail": detail,
    }


async def _ensure_us_forecast_market_quote_snapshot(
    forecast_id: str,
    correlation_id: str,
    *,
    runtime_profile: McpProfile,
) -> dict[str, Any]:
    """Ensure US quote evidence bound to one persisted directional-lab forecast.

    ``runtime_profile`` is supplied by the registry-owned closure, never by the
    MCP caller.  The forecast's exact ``correlation_id`` is the persisted run
    binding because ``trade_forecasts`` has no stronger guaranteed run/session
    identity field.  ``forecast_target`` direction/entry-mode fields are not
    enforced here because they are not guaranteed by the persisted forecast
    schema.
    """
    if runtime_profile is not McpProfile.US_PAPER:
        return _forecast_quote_failure(
            "wrong_mcp_profile",
            "us_forecast_market_quote_snapshot_ensure requires MCP_PROFILE=us-paper",
        )
    if not isinstance(forecast_id, str) or not forecast_id.strip():
        return _forecast_quote_failure(
            "invalid_forecast_id",
            "forecast_id must be a non-blank UUID string",
        )
    if not isinstance(correlation_id, str) or not correlation_id.strip():
        return _forecast_quote_failure(
            "invalid_correlation_id",
            "correlation_id must be a non-blank exact persisted value",
        )

    try:
        async with AsyncSessionLocal() as db:
            forecast = await get_forecast(db, forecast_id)
    except ForecastValidationError:
        return _forecast_quote_failure(
            "invalid_forecast_id",
            "forecast_id must be a valid UUID",
        )
    except Exception:
        return _forecast_quote_failure(
            "forecast_lookup_failed",
            "forecast lookup failed closed",
        )

    if forecast is None:
        return _forecast_quote_failure(
            "forecast_not_found",
            "no persisted forecast matches forecast_id",
        )
    if forecast.status != "open":
        return _forecast_quote_failure(
            "forecast_not_open",
            "forecast status must be exactly open",
        )
    if forecast.created_by != _DIRECTIONAL_LAB_CREATED_BY:
        return _forecast_quote_failure(
            "forecast_created_by_mismatch",
            "forecast created_by must be exactly directional-lab",
        )

    instrument_type = (
        forecast.instrument_type.value
        if hasattr(forecast.instrument_type, "value")
        else str(forecast.instrument_type)
    )
    if instrument_type != "equity_us":
        return _forecast_quote_failure(
            "forecast_instrument_type_mismatch",
            "forecast instrument_type must be exactly equity_us",
        )
    if forecast.correlation_id != correlation_id:
        return _forecast_quote_failure(
            "forecast_correlation_mismatch",
            "correlation_id must exactly match the persisted forecast",
        )

    stored_symbol = forecast.symbol
    if not isinstance(stored_symbol, str):
        return _forecast_quote_failure(
            "invalid_forecast_symbol",
            "persisted forecast symbol is not a valid US equity symbol",
        )
    symbol = stored_symbol.strip().upper()
    if not is_us_equity_symbol(symbol):
        return _forecast_quote_failure(
            "invalid_forecast_symbol",
            "persisted forecast symbol is not a valid US equity symbol",
        )

    result = await market_quote_snapshot_ensure(market="us", symbol=symbol)
    if result.get("success") and (
        result.get("market") != "us" or result.get("symbol") != symbol
    ):
        return _forecast_quote_failure(
            "snapshot_binding_mismatch",
            "ensured quote snapshot did not match the persisted forecast identity",
        )
    return {
        **result,
        "forecast_id": str(forecast.forecast_id),
        "correlation_id": forecast.correlation_id,
        "forecast_bound": True,
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


def register_us_forecast_market_quote_snapshot_tool(
    mcp: FastMCP,
    *,
    runtime_profile: McpProfile,
) -> None:
    """Register the forecast-bound quote mutation only on the US paper profile."""
    if runtime_profile is not McpProfile.US_PAPER:
        return

    async def us_forecast_market_quote_snapshot_ensure(
        forecast_id: _NONBLANK_EXACT_STRING,
        correlation_id: _NONBLANK_EXACT_STRING,
    ) -> dict[str, Any]:
        return await _ensure_us_forecast_market_quote_snapshot(
            forecast_id,
            correlation_id,
            runtime_profile=runtime_profile,
        )

    _ = mcp.tool(
        name="us_forecast_market_quote_snapshot_ensure",
        description=(
            "Ensure a trusted US quote snapshot for the symbol stored on one open "
            "directional-lab equity_us forecast. The caller supplies only the "
            "forecast UUID and its exact persisted correlation_id; market, symbol, "
            "price, source, and runtime profile remain server-owned."
        ),
    )(us_forecast_market_quote_snapshot_ensure)
