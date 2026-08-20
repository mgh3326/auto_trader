"""ROB-1303 read-only spike-attribution tool.

Answers "what could have caused this session's move?" from material this repo
already stores, and returns the ``catalyst_basis`` block the
``momentum_spike_profit_ladder`` tier asks for. It reads; it never writes a row,
calls a broker, or reaches an order / approval / watch surface.

An ``unattributed`` spike comes back ``unattributed`` — the tool has no path
that turns a blank into a cause.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.services.spike_attribution.attribute import build_attribution, record_summary
from app.services.spike_attribution.catalyst_basis import build_catalyst_basis
from app.services.spike_attribution.detect import ABS_CHANGE_PCT_MIN, detect_spikes
from app.services.spike_attribution.forecast_tag import (
    build_prereg_forecasts,
    prereg_skipped_reason,
)
from app.services.spike_attribution.materials import (
    DAILY_TABLE_BY_MARKET,
    load_daily_bars,
    load_spike_materials,
)
from app.services.spike_attribution.spec import (
    EXPERIMENT_ID,
    FORBIDDEN,
    PINNED_SPEC_SHA256,
    spec_sha256,
)

_LOOKBACK_DAYS = 20
_MAX_SYMBOLS = 20


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def _error(message: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": message,
        "promote": False,
        "live_gate_impact": False,
        "writes_performed": 0,
    }


async def get_spike_attribution_impl(
    symbols: list[str] | None,
    session_date: str,
    market: str = "kr",
    created_by: str = "",
) -> dict[str, Any]:
    author = (created_by or "").strip()
    if not author:
        return _error("created_by is required")
    if market not in DAILY_TABLE_BY_MARKET:
        return _error(f"market must be one of {sorted(DAILY_TABLE_BY_MARKET)}")
    cleaned = [s.strip() for s in (symbols or []) if s and s.strip()]
    if not cleaned:
        return _error("symbols must be a non-empty list")
    if len(cleaned) > _MAX_SYMBOLS:
        return _error(f"at most {_MAX_SYMBOLS} symbols per call")
    try:
        as_of = dt.date.fromisoformat(session_date.strip())
    except (AttributeError, ValueError):
        return _error("session_date must be YYYY-MM-DD")

    results: list[dict[str, Any]] = []
    async with _session_factory()() as db:
        for symbol in cleaned:
            bars = await load_daily_bars(
                db,
                market=market,
                symbol=symbol,
                start=as_of - dt.timedelta(days=_LOOKBACK_DAYS),
                end=as_of,
            )
            event, diagnostics = detect_spikes(
                market=market, symbol=symbol, bars=bars, session_date=as_of
            )
            if event is None:
                results.append(
                    {"symbol": symbol, "spike": False, "diagnostics": diagnostics}
                )
                continue
            materials = await load_spike_materials(db, event)
            attribution = build_attribution(event=event, materials=materials)
            results.append(
                {
                    "symbol": symbol,
                    "spike": True,
                    "diagnostics": diagnostics,
                    "summary": record_summary(attribution),
                    "attribution_record": attribution.as_dict(),
                    "catalyst_basis": build_catalyst_basis(attribution),
                    "prereg_forecast_save_kwargs": build_prereg_forecasts(
                        attribution, created_by=author
                    ),
                    "prereg_skipped_reason": prereg_skipped_reason(attribution),
                }
            )

    spikes = [row for row in results if row["spike"]]
    return {
        "success": True,
        "experiment_id": EXPERIMENT_ID,
        "spec_sha256": spec_sha256(),
        "pinned_spec_sha256": PINNED_SPEC_SHA256,
        "market": market,
        "session_date": as_of.isoformat(),
        "abs_change_pct_min": str(ABS_CHANGE_PCT_MIN),
        "results": results,
        "counts": {
            "examined": len(results),
            "spikes": len(spikes),
            "attributed": sum(not r["summary"]["unattributed"] for r in spikes),
            "unattributed": sum(r["summary"]["unattributed"] for r in spikes),
        },
        "writes_performed": 0,
        "forecast_save_called": False,
        "promote": False,
        "live_gate_impact": False,
        "forbidden": list(FORBIDDEN),
    }


__all__ = ["get_spike_attribution_impl"]
