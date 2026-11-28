"""hk 7119 — run the daily-loss monitor once (manual; no schedule).

Preview by default: prints each account's KST-day P&L in KRW, or the closed
reasons it cannot be measured, and whether it would freeze. ``--commit`` moves
an ``ACTIVE`` kill row to ``DAILY_LOSS_FREEZE`` for an account at a loss of
500,000 KRW or more or with an unmeasured day. Nothing here unfreezes.

Marks come from the database only: the day-start mark is the previous session's
daily close; the fresh quote is the newest stored 1-minute bar (at most ten
minutes old) inside the regular session, else the last completed session
close. Unreported fees use the policy round-trip cost estimate. USD accounts
use a run-time USD/KRW snapshot (recorded with source and time).

    uv run python -m scripts.daily_loss_monitor --database-url-env DATABASE_URL
    uv run python -m scripts.daily_loss_monitor --database-url-env DATABASE_URL --commit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.daily_loss_monitor.accounts import ACCOUNTS, AccountScope
from app.services.daily_loss_monitor.marks import FxSnapshot, session_aware_marks
from app.services.daily_loss_monitor.monitor import kst_day_start, run_once
from app.services.daily_loss_monitor.rules import Mark

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")

_QUOTE_SQL = {
    "equity_kr": (
        "SELECT DISTINCT ON (symbol) symbol, close, time_utc FROM research.kr_candles_1m "
        "WHERE symbol = ANY(:symbols) AND venue = 'KRX' AND time_utc >= :since "
        "ORDER BY symbol, time_utc DESC"
    ),
    "equity_us": (
        "SELECT DISTINCT ON (symbol) symbol, close, time_utc FROM research.us_candles_1m "
        "WHERE symbol = ANY(:symbols) AND time_utc >= :since "
        "ORDER BY symbol, time_utc DESC, venue"
    ),
    "crypto": (
        "SELECT DISTINCT ON (i.venue_symbol) i.venue_symbol AS symbol, c.close, "
        "c.time AS time_utc FROM public.crypto_candles_1m c "
        "JOIN public.crypto_instruments i ON i.id = c.instrument_id "
        "WHERE i.venue = 'upbit' AND i.product = 'spot' "
        "AND i.venue_symbol = ANY(:symbols) AND c.time >= :since "
        "ORDER BY i.venue_symbol, c.time DESC"
    ),
}


async def reference_marks(
    session: AsyncSession, scope: AccountScope, symbols: Sequence[str], now: datetime
) -> dict[str, Mark | None]:
    from app.services.strategy_sleeves.marks import latest_close_marks

    day_start = kst_day_start(now)
    closes = await latest_close_marks(
        session, scope.instrument_type, list(symbols), day_start
    )
    return {
        s: None if closes.get(s) is None else Mark(closes[s], day_start, None)
        for s in symbols
    }


async def fresh_quote_marks(
    session: AsyncSession, scope: AccountScope, symbols: Sequence[str], now: datetime
) -> dict[str, Mark | None]:
    if not symbols:
        return {}
    rows = (
        await session.execute(
            text(_QUOTE_SQL[scope.instrument_type]),
            {"symbols": list(symbols), "since": now - timedelta(days=3)},
        )
    ).all()
    found = {
        row.symbol: Mark(
            Decimal(str(row.close)), row.time_utc + timedelta(minutes=1), None
        )
        for row in rows
    }
    return {s: found.get(s) for s in symbols}


async def usd_krw() -> FxSnapshot | None:
    """A run-time USD/KRW snapshot (no per-fill FX is stored)."""
    from app.services.exchange_rate_service import get_usd_krw_rate_details

    quote = await get_usd_krw_rate_details()
    rate = Decimal(str(quote.default_rate))
    if not rate.is_finite() or rate <= 0:
        return None
    return FxSnapshot(rate, f"exchange_rate_service:{quote.source}", datetime.now(UTC))


def _database_url(args: argparse.Namespace) -> str:
    if args.database_url_env:
        if not _ENV_NAME.fullmatch(args.database_url_env):
            raise ValueError("--database-url-env must be an environment variable name")
        raw = os.environ.get(args.database_url_env, "").strip()
        if not raw:
            raise ValueError(f"environment variable {args.database_url_env} is not set")
    else:
        raw = args.database_url or ""
    url = make_url(raw)
    if url.get_backend_name() != "postgresql" or not url.database:
        raise ValueError("database URL must be a complete PostgreSQL URL")
    return url.render_as_string(hide_password=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    database = parser.add_mutually_exclusive_group(required=True)
    database.add_argument("--database-url-env")
    database.add_argument("--database-url")
    parser.add_argument("--account", action="append", choices=sorted(ACCOUNTS))
    parser.add_argument("--commit", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> int:
    engine = create_async_engine(_database_url(args))
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            outcomes = await run_once(
                session,
                now=datetime.now(UTC),
                reference_fn=reference_marks,
                quote_fn=session_aware_marks(fresh_quote_marks),
                fx_fn=usd_krw,
                commit=args.commit,
                accounts=args.account,
            )
    finally:
        await engine.dispose()
    for outcome in outcomes:
        print(
            json.dumps(
                {
                    "account": outcome.account,
                    "pnl_krw": None
                    if outcome.pnl_krw is None
                    else str(outcome.pnl_krw),
                    "reasons": list(outcome.reasons),
                    "would_freeze": outcome.freeze,
                    "frozen_now": outcome.frozen_now,
                    "fee_bases": list(outcome.fee_bases),
                    "mark_bases": list(outcome.mark_bases),
                    "cost_estimate": None
                    if outcome.cost_estimate is None
                    else {
                        "round_trip_cost_bps": str(
                            outcome.cost_estimate.round_trip_cost_bps
                        ),
                        "sell_tax_bps": str(outcome.cost_estimate.sell_tax_bps),
                        "rate_source": outcome.cost_estimate.rate_source,
                        "policy_version": outcome.cost_estimate.policy_version,
                        "policy_content_hash": outcome.cost_estimate.policy_content_hash,
                    },
                    "fx": None
                    if outcome.fx is None
                    else {
                        "rate": str(outcome.fx.rate),
                        "source": outcome.fx.source,
                        "observed_at": outcome.fx.observed_at.isoformat(),
                    },
                    "committed": args.commit,
                },
                ensure_ascii=False,
            )
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
