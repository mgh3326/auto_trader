#!/usr/bin/env python3
"""ROB-918: read-only 2-week shadow report for kr-preopen new candidates.

Scans ``trading_decision_sessions.market_brief -> 'new_candidates'`` for
recent kr-preopen research_run sessions, joins each candidate's baseline
close against the next available ``kr_candles_1d`` close to compute a D+1
% move, and prints a recovered-rate / false-positive-rate summary per
selection reason (consecutive_gainers / theme_leader / double_buy).

NEVER writes to the database. SELECT only, everywhere in this file.

Examples:
    uv run python -m scripts.shadow_new_candidates_report
    uv run python -m scripts.shadow_new_candidates_report --since-days 14
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.models.trading_decision import TradingDecisionSession

logger = logging.getLogger(__name__)

_CANDIDATE_SECTIONS = ("consecutive_gainers", "theme_leaders", "double_buy")


@dataclass(frozen=True)
class ShadowCandidateRow:
    session_id: int
    session_uuid: str
    generated_at: dt.datetime
    reason: str
    symbol: str
    name: str | None
    baseline_date: dt.date | None
    baseline_close: float | None
    d1_close: float | None
    d1_time: dt.datetime | None
    d1_close_pct: float | None
    evaluable: bool


async def _fetch_next_close(
    session: AsyncSession, *, symbol: str, venue: str, after_date: dt.date
) -> tuple[float, dt.datetime] | None:
    """Return (close, time) of the first kr_candles_1d row strictly after after_date.

    Read-only. Returns None when the table is unavailable or no such row
    exists (never raises — the caller marks the candidate unevaluable).
    """
    try:
        result = await session.execute(
            text(
                """
                SELECT close, time
                FROM public.kr_candles_1d
                WHERE symbol = :symbol AND venue = :venue AND time > :after_date
                ORDER BY time ASC
                LIMIT 1
                """
            ),
            {"symbol": symbol, "venue": venue, "after_date": after_date},
        )
        row = result.first()
    except Exception as exc:  # noqa: BLE001 -- read-only diagnostic, never raise
        logger.warning(
            "shadow_new_candidates_report: kr_candles_1d read failed for %s: %s",
            symbol,
            exc,
            exc_info=True,
        )
        return None
    if row is None:
        return None
    return float(row.close), row.time


def _iter_candidates(market_brief: dict[str, Any]) -> list[dict[str, Any]]:
    new_candidates = (market_brief or {}).get("new_candidates") or {}
    out: list[dict[str, Any]] = []
    for section in _CANDIDATE_SECTIONS:
        out.extend(new_candidates.get(section) or [])
    return out


def _parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


async def build_shadow_report(
    session: AsyncSession, *, since: dt.date, market_scope: str = "kr"
) -> list[ShadowCandidateRow]:
    """Read-only: flatten new_candidates from recent sessions and join D+1 closes.

    NEVER writes. `since` filters on trading_decision_sessions.generated_at.
    """
    stmt = select(TradingDecisionSession).where(
        TradingDecisionSession.source_profile == "research_run",
        TradingDecisionSession.market_scope == market_scope,
        TradingDecisionSession.generated_at
        >= dt.datetime.combine(since, dt.time.min, tzinfo=dt.UTC),
    )
    sessions = (await session.execute(stmt)).scalars().all()

    rows: list[ShadowCandidateRow] = []
    for sess in sessions:
        for candidate in _iter_candidates(sess.market_brief or {}):
            symbol = candidate.get("symbol")
            if not symbol:
                continue
            baseline_date = _parse_date(candidate.get("baseline_date"))
            baseline_close = candidate.get("baseline_close")

            d1_close: float | None = None
            d1_time: dt.datetime | None = None
            if baseline_date is not None:
                next_close = await _fetch_next_close(
                    session, symbol=symbol, venue="KRX", after_date=baseline_date
                )
                if next_close is not None:
                    d1_close, d1_time = next_close

            d1_close_pct: float | None = None
            evaluable = False
            if (
                baseline_close is not None
                and d1_close is not None
                and float(baseline_close) != 0.0
            ):
                d1_close_pct = (
                    (d1_close - float(baseline_close)) / float(baseline_close) * 100
                )
                evaluable = True

            rows.append(
                ShadowCandidateRow(
                    session_id=sess.id,
                    session_uuid=str(sess.session_uuid),
                    generated_at=sess.generated_at,
                    reason=candidate.get("reason", "unknown"),
                    symbol=symbol,
                    name=candidate.get("name"),
                    baseline_date=baseline_date,
                    baseline_close=(
                        float(baseline_close) if baseline_close is not None else None
                    ),
                    d1_close=d1_close,
                    d1_time=d1_time,
                    d1_close_pct=d1_close_pct,
                    evaluable=evaluable,
                )
            )
    return rows


def summarize_rows(rows: list[ShadowCandidateRow]) -> dict[str, dict[str, Any]]:
    """Group rows by selection reason and compute recovered/false-positive rates.

    "recovered" = d1_close_pct > 0 (the candidate was up the next session).
    """
    by_reason: dict[str, list[ShadowCandidateRow]] = {}
    for row in rows:
        by_reason.setdefault(row.reason, []).append(row)

    summary: dict[str, dict[str, Any]] = {}
    for reason, reason_rows in by_reason.items():
        evaluated = [
            r for r in reason_rows if r.evaluable and r.d1_close_pct is not None
        ]
        recovered = [r for r in evaluated if r.d1_close_pct > 0]
        summary[reason] = {
            "total": len(reason_rows),
            "evaluated": len(evaluated),
            "recovered_count": len(recovered),
            "recovered_rate": (len(recovered) / len(evaluated) if evaluated else None),
            "false_positive_rate": (
                (len(evaluated) - len(recovered)) / len(evaluated)
                if evaluated
                else None
            ),
            "avg_d1_close_pct": (
                sum(r.d1_close_pct for r in evaluated) / len(evaluated)
                if evaluated
                else None
            ),
        }
    return summary


def _print_report(
    rows: list[ShadowCandidateRow], summary: dict[str, dict[str, Any]]
) -> None:
    print(f"\ncandidates={len(rows)}\n")
    for reason, stats in summary.items():
        recovered_rate = stats["recovered_rate"]
        false_positive_rate = stats["false_positive_rate"]
        avg_pct = stats["avg_d1_close_pct"]
        print(
            f"  {reason:20s} total={stats['total']:4d} evaluated={stats['evaluated']:4d} "
            f"recovered_rate={recovered_rate if recovered_rate is None else f'{recovered_rate:.1%}':>7} "
            f"false_positive_rate={false_positive_rate if false_positive_rate is None else f'{false_positive_rate:.1%}':>7} "
            f"avg_d1_close_pct={avg_pct if avg_pct is None else f'{avg_pct:+.2f}%':>8}"
        )
    print()
    for row in rows:
        pct_label = (
            f"{row.d1_close_pct:+.2f}%" if row.d1_close_pct is not None else "n/a"
        )
        print(
            f"  [{row.generated_at.date().isoformat()}] {row.reason:20s} "
            f"{row.symbol:8s} {row.name or '':10s} "
            f"baseline={row.baseline_close!r} d1={row.d1_close!r} pct={pct_label}"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only ROB-918 kr-preopen new-candidate shadow report "
            "(never writes to the database)."
        )
    )
    parser.add_argument("--market", choices=["kr"], default="kr")
    parser.add_argument(
        "--since-days",
        type=int,
        default=14,
        help="Look back this many days from today (default: 14, the shadow window).",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    setup_logging_and_sentry(service_name="shadow-new-candidates-report")
    args = parse_args(argv)
    since = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=args.since_days)
    try:
        async with AsyncSessionLocal() as session:
            rows = await build_shadow_report(
                session, since=since, market_scope=args.market
            )
        summary = summarize_rows(rows)
        _print_report(rows, summary)
        return 0
    except Exception:
        logger.exception("shadow_new_candidates_report crashed")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
