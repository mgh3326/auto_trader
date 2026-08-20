#!/usr/bin/env python
"""ROB-1303 — attribute one session's spikes to candidate causes. Read-only.

    uv run python -m scripts.attribute_daily_spikes --date 2026-08-20 \
        --symbol 035420 --symbol 035720

    uv run python -m scripts.attribute_daily_spikes --date 2026-08-20 --all

Emits one JSON document: the detected spikes, every evidence item with its
eligibility ruling, the resulting attribution (or the ``unattributed`` verdict),
the ``catalyst_basis`` block for hook ⓐ, and the ``forecast_save`` kwargs for
hook ⓑ. It prints those kwargs; it never calls ``forecast_save`` — recording is
the session's or the operator's decision.

No writes of any kind. No broker call. No scheduler registration: this runs when
a human runs it.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.services.spike_attribution.attribute import build_attribution, record_summary
from app.services.spike_attribution.catalyst_basis import build_catalyst_basis
from app.services.spike_attribution.detect import (
    ABS_CHANGE_PCT_MIN,
    detect_spikes,
)
from app.services.spike_attribution.forecast_tag import (
    build_prereg_forecasts,
    prereg_skipped_reason,
)
from app.services.spike_attribution.materials import (
    DAILY_TABLE_BY_MARKET,
    SESSION_TZ_BY_MARKET,
    load_daily_bars,
    load_spike_materials,
)
from app.services.spike_attribution.scoring import (
    WINDOWS_TRADING_DAYS,
    FollowThroughScore,
    aggregate_by_class,
    score_event,
)
from app.services.spike_attribution.spec import (
    EXPERIMENT_ID,
    FORBIDDEN,
    PINNED_SPEC_SHA256,
    spec_sha256,
)

# Enough history behind the session to reach a halt-suspicion verdict, and
# enough ahead to fill the longest pre-registered scoring window.
LOOKBACK_DAYS = 20
LOOKAHEAD_DAYS = max(WINDOWS_TRADING_DAYS) * 2 + 10


async def _prefilter_symbols(
    db: Any, *, market: str, session_date: dt.date, limit: int
) -> list[str]:
    """Cheap SQL shortlist: symbols whose session move could clear the bar.

    The pure detector still rules on every shortlisted symbol; this only avoids
    pulling twenty days of bars for four thousand names.
    """

    table = DAILY_TABLE_BY_MARKET[market]
    tzname = str(SESSION_TZ_BY_MARKET[market])
    stmt = sa.text(
        f"""
        WITH d AS (
            SELECT symbol,
                   (time AT TIME ZONE :tz)::date AS session_date,
                   high, low, close,
                   lag(close) OVER (
                       PARTITION BY symbol ORDER BY time
                   ) AS prev_close
            FROM {table}
            WHERE (time AT TIME ZONE :tz)::date
                  BETWEEN :lo AND :session_date
        )
        SELECT symbol
        FROM d
        WHERE session_date = :session_date
          AND prev_close IS NOT NULL
          AND prev_close > 0
          AND (
                abs((close - prev_close) / prev_close) * 100 >= :threshold
             OR abs((high - prev_close) / prev_close) * 100 >= :threshold
             OR abs((low - prev_close) / prev_close) * 100 >= :threshold
          )
        ORDER BY abs((close - prev_close) / prev_close) DESC
        LIMIT :limit
        """  # noqa: S608 - table name comes from a closed literal mapping
    )
    rows = await db.execute(
        stmt,
        {
            "tz": tzname,
            "lo": session_date - dt.timedelta(days=10),
            "session_date": session_date,
            "threshold": float(ABS_CHANGE_PCT_MIN),
            "limit": limit,
        },
    )
    return [row.symbol for row in rows]


async def _attribute_symbol(
    db: Any,
    *,
    market: str,
    symbol: str,
    session_date: dt.date,
    created_by: str,
    score: bool,
    scores_out: list[FollowThroughScore],
) -> dict[str, Any]:
    bars = await load_daily_bars(
        db,
        market=market,
        symbol=symbol,
        start=session_date - dt.timedelta(days=LOOKBACK_DAYS),
        end=session_date + dt.timedelta(days=LOOKAHEAD_DAYS),
    )
    history = [row for row in bars if row.session_date <= session_date]
    event, diagnostics = detect_spikes(
        market=market, symbol=symbol, bars=history, session_date=session_date
    )
    if event is None:
        return {"symbol": symbol, "spike": False, "diagnostics": diagnostics}

    materials = await load_spike_materials(db, event)
    attribution = build_attribution(event=event, materials=materials)
    payload: dict[str, Any] = {
        "symbol": symbol,
        "spike": True,
        "diagnostics": diagnostics,
        "summary": record_summary(attribution),
        "attribution_record": attribution.as_dict(),
        "catalyst_basis": build_catalyst_basis(attribution),
        "prereg_forecast_save_kwargs": build_prereg_forecasts(
            attribution, created_by=created_by
        ),
        "prereg_skipped_reason": prereg_skipped_reason(attribution),
    }
    if score:
        future = [row for row in bars if row.session_date > session_date]
        scored = [
            score_event(
                attribution=attribution,
                subsequent_bars=future,
                window_trading_days=window,
            )
            for window in WINDOWS_TRADING_DAYS
        ]
        scores_out.extend(scored)
        payload["follow_through"] = [row.as_dict() for row in scored]
    return payload


async def run(args: argparse.Namespace) -> dict[str, Any]:
    session_date = dt.date.fromisoformat(args.date)
    async with AsyncSessionLocal() as db:
        symbols = list(args.symbol or [])
        if args.all:
            symbols.extend(
                await _prefilter_symbols(
                    db,
                    market=args.market,
                    session_date=session_date,
                    limit=args.limit,
                )
            )
        seen: set[str] = set()
        ordered = [s for s in symbols if not (s in seen or seen.add(s))]
        scores: list[FollowThroughScore] = []
        results = [
            await _attribute_symbol(
                db,
                market=args.market,
                symbol=symbol,
                session_date=session_date,
                created_by=args.created_by,
                score=args.score,
                scores_out=scores,
            )
            for symbol in ordered
        ]

    spikes = [row for row in results if row["spike"]]
    return {
        "experiment_id": EXPERIMENT_ID,
        "spec_sha256": spec_sha256(),
        "pinned_spec_sha256": PINNED_SPEC_SHA256,
        "market": args.market,
        "session_date": session_date.isoformat(),
        "abs_change_pct_min": str(ABS_CHANGE_PCT_MIN),
        "symbols_examined": ordered,
        "counts": {
            "examined": len(results),
            "spikes": len(spikes),
            "attributed": sum(not r["summary"]["unattributed"] for r in spikes),
            "unattributed": sum(r["summary"]["unattributed"] for r in spikes),
        },
        "results": results,
        "follow_through_aggregate": aggregate_by_class(scores) if scores else None,
        "writes_performed": 0,
        "forecast_save_called": False,
        "promote": False,
        "live_gate_impact": False,
        "forbidden": list(FORBIDDEN),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", default="kr", choices=sorted(DAILY_TABLE_BY_MARKET))
    parser.add_argument("--date", required=True, help="session date, YYYY-MM-DD local")
    parser.add_argument("--symbol", action="append", help="repeatable")
    parser.add_argument(
        "--all", action="store_true", help="scan the market for the session's spikes"
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--created-by", default="operator")
    parser.add_argument(
        "--score",
        action="store_true",
        help="also score follow-through (unscorable until the window fills)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.symbol and not args.all:
        print("nothing to do: pass --symbol or --all", file=sys.stderr)
        return 2
    payload = asyncio.run(run(args))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    return 0


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value)!r}")


if __name__ == "__main__":
    raise SystemExit(main())
