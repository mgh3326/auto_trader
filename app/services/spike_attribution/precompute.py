"""ROB-1303 phase 2 — pre-open precompute and intraday incremental refresh.

Both modes do exactly what stage 1's CLI does per symbol, then persist the
result as a cache entry. They add no new judgment: a symbol that precompute
finds unattributed is cached as unattributed, and a symbol with no spike is
cached as *a computed negative*, not as an absence.

Failure handling is the point of this module. When a symbol's refresh raises,
the previous good entry is **not** overwritten — the error is stamped onto it
(``last_error`` / ``last_error_at``) while ``computed_at`` and ``payload`` stay
where they were, so the entry ages into ``stale`` on its own instead of
masquerading as a fresh success.

Nothing here registers a schedule. ``precompute_session`` is a function an
operator (or an operator-run CLI) calls.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.spike_attribution.attribute import build_attribution, record_summary
from app.services.spike_attribution.cache import (
    MODE_INTRADAY,
    MODE_PREOPEN,
    CacheEntry,
    expected_refresh_seconds,
    read_entry,
    write_entry,
)
from app.services.spike_attribution.catalyst_basis import build_catalyst_basis
from app.services.spike_attribution.detect import detect_spikes
from app.services.spike_attribution.materials import (
    load_daily_bars,
    load_spike_materials,
)
from app.services.spike_attribution.spec import spec_sha256

LOOKBACK_DAYS = 20

# Modes are the pinned refresh cadences, re-exported for callers.
MODES: tuple[str, str] = (MODE_PREOPEN, MODE_INTRADAY)


@dataclass(frozen=True)
class SymbolOutcome:
    symbol: str
    status: str  # "computed" | "failed"
    spike: bool | None = None
    scored_class: str | None = None
    error: str | None = None
    preserved_previous_entry: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "spike": self.spike,
            "scored_class": self.scored_class,
            "error": self.error,
            "preserved_previous_entry": self.preserved_previous_entry,
        }


@dataclass(frozen=True)
class PrecomputeRun:
    market: str
    session_date: dt.date
    mode: str
    started_at: dt.datetime
    finished_at: dt.datetime
    outcomes: tuple[SymbolOutcome, ...] = field(default_factory=tuple)

    @property
    def succeeded(self) -> int:
        return sum(o.status == "computed" for o in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(o.status == "failed" for o in self.outcomes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "session_date": self.session_date.isoformat(),
            "mode": self.mode,
            "expected_refresh_seconds": expected_refresh_seconds(self.mode),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "counts": {
                "attempted": len(self.outcomes),
                "succeeded": self.succeeded,
                "failed": self.failed,
            },
            # A run with failures is reported as partial, never as success.
            "run_status": "ok" if self.failed == 0 else "partial",
            "outcomes": [o.as_dict() for o in self.outcomes],
            "writes_performed": self.succeeded,
            "db_rows_written": 0,
            "scheduler_registration": False,
            "promote": False,
            "live_gate_impact": False,
        }


async def compute_symbol_payload(
    db: AsyncSession,
    *,
    market: str,
    symbol: str,
    session_date: dt.date,
) -> dict[str, Any]:
    """The cached unit: exactly stage 1's per-symbol answer, negatives included."""

    bars = await load_daily_bars(
        db,
        market=market,
        symbol=symbol,
        start=session_date - dt.timedelta(days=LOOKBACK_DAYS),
        end=session_date,
    )
    event, diagnostics = detect_spikes(
        market=market, symbol=symbol, bars=bars, session_date=session_date
    )
    if event is None:
        # A computed negative. This is an answer, and the cache layer must
        # surface it as fresh rather than as an absence.
        return {
            "symbol": symbol,
            "spike": False,
            "diagnostics": diagnostics,
            "computed_negative": True,
        }
    materials = await load_spike_materials(db, event)
    attribution = build_attribution(event=event, materials=materials)
    return {
        "symbol": symbol,
        "spike": True,
        "diagnostics": diagnostics,
        "summary": record_summary(attribution),
        "attribution_record": attribution.as_dict(),
        "catalyst_basis": build_catalyst_basis(attribution),
        "computed_negative": False,
    }


def _failure_entry(
    previous: CacheEntry | None,
    *,
    market: str,
    session_date: dt.date,
    symbol: str,
    mode: str,
    error: str,
    now: dt.datetime,
) -> CacheEntry | None:
    """Stamp a failure without destroying the last good answer.

    Returns None when there is nothing to preserve — we deliberately do NOT
    write a synthetic empty entry in that case, because a written entry reads
    as "computed" and an empty one would be indistinguishable from a genuine
    "no catalyst". A never-computed symbol must stay ``missing``.
    """

    if previous is None:
        return None
    return CacheEntry(
        market=market,
        session_date=session_date,
        symbol=symbol,
        mode=mode,
        computed_at=previous.computed_at,  # untouched: the entry keeps aging
        spec_sha256=previous.spec_sha256,
        payload=previous.payload,
        last_success_at=previous.last_success_at or previous.computed_at,
        last_error=error,
        last_error_at=now,
    )


async def refresh_symbol(
    db: AsyncSession,
    *,
    market: str,
    symbol: str,
    session_date: dt.date,
    mode: str,
    now: dt.datetime,
    root: Any = None,
) -> SymbolOutcome:
    """Refresh one symbol's entry. Never raises for a per-symbol failure."""

    try:
        payload = await compute_symbol_payload(
            db, market=market, symbol=symbol, session_date=session_date
        )
    except Exception as exc:  # noqa: BLE001 - per-symbol isolation is the point
        previous = None
        try:
            previous = read_entry(
                market=market, session_date=session_date, symbol=symbol, root=root
            )
        except Exception:  # noqa: BLE001 - unreadable previous is still a failure
            previous = None
        stamped = _failure_entry(
            previous,
            market=market,
            session_date=session_date,
            symbol=symbol,
            mode=mode,
            error=f"{type(exc).__name__}: {exc}",
            now=now,
        )
        if stamped is not None:
            write_entry(stamped, root=root)
        return SymbolOutcome(
            symbol=symbol,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            preserved_previous_entry=stamped is not None,
        )

    entry = CacheEntry(
        market=market,
        session_date=session_date,
        symbol=symbol,
        mode=mode,
        computed_at=now,
        spec_sha256=spec_sha256(),
        payload=payload,
        last_success_at=now,
        last_error=None,
        last_error_at=None,
    )
    write_entry(entry, root=root)
    summary = payload.get("summary") or {}
    return SymbolOutcome(
        symbol=symbol,
        status="computed",
        spike=bool(payload.get("spike")),
        scored_class=summary.get("scored_class"),
    )


async def precompute_session(
    db: AsyncSession,
    *,
    market: str,
    session_date: dt.date,
    symbols: list[str],
    mode: str,
    now: dt.datetime,
    root: Any = None,
) -> PrecomputeRun:
    """Refresh every symbol for one session. Operator-invoked; no schedule."""

    if mode not in MODES:
        raise ValueError(f"mode must be one of {list(MODES)}")
    started = now
    outcomes = [
        await refresh_symbol(
            db,
            market=market,
            symbol=symbol,
            session_date=session_date,
            mode=mode,
            now=now,
            root=root,
        )
        for symbol in symbols
    ]
    return PrecomputeRun(
        market=market,
        session_date=session_date,
        mode=mode,
        started_at=started,
        finished_at=now,
        outcomes=tuple(outcomes),
    )


__all__ = [
    "LOOKBACK_DAYS",
    "MODES",
    "PrecomputeRun",
    "SymbolOutcome",
    "compute_symbol_payload",
    "precompute_session",
    "refresh_symbol",
]
