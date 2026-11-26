#!/usr/bin/env python3
"""Collect per-stock Naver disclosure receipt times into market_events.

    uv run python -m scripts.collect_naver_disclosures [--symbols 005930 000660]
        [--dry-run|--commit]

Dry-run is the default. Naver rows are provider-keyed and insert-only; DART receipt
numbers are linked only in the Naver row's raw payload. This command is manual and
does not schedule itself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.models.market_events import MarketEvent


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and report without writing (default)",
    )
    mode.add_argument(
        "--commit", action="store_true", help="insert new disclosure rows"
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="six-digit KR symbols; replaces build_targets",
    )
    parser.add_argument("--user-id", type=int, default=1)
    return parser.parse_args(argv)


async def run(
    args: argparse.Namespace,
    *,
    client: Any = None,
    session_factory: Any = None,
) -> dict[str, object]:
    from app.core.config import settings
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.kis.client import KISClient
    from app.services.market_events import naver_disclosures as naver
    from app.services.market_events.repository import MarketEventsRepository
    from app.services.research_candles.session_minutes_collector import (
        build_targets,
        parse_symbol_list,
    )

    own_client = client is None
    client = client or naver.NaverPublicClient()
    session_factory = session_factory or AsyncSessionLocal
    result: dict[str, object] = {
        "dry_run": not args.commit,
        "symbols": [],
        "errors": [],
    }
    errors: list[dict[str, str]] = []
    now = datetime.now(UTC)
    try:
        async with session_factory() as session:
            if args.symbols:
                symbols = parse_symbol_list("kr", ",".join(args.symbols))
            else:
                targets = await build_targets(
                    market="kr",
                    session=session,
                    kis=KISClient(),
                    user_id=args.user_id,
                    fixed_symbols_raw=settings.SESSION_MINUTES_FIXED_SYMBOLS_KR,
                    now=now,
                    screener_top_n=settings.SESSION_MINUTES_SCREENER_TOP_N,
                    max_symbols=settings.SESSION_MINUTES_MAX_SYMBOLS_KR,
                    fixed_budget=settings.SESSION_MINUTES_BUDGET_FIXED,
                    intraday_budget=settings.SESSION_MINUTES_BUDGET_INTRADAY,
                    screener_budget=settings.SESSION_MINUTES_BUDGET_SCREENER,
                )
                symbols = targets.symbols
                result["target_errors"] = targets.errors
                errors.extend(
                    {
                        "origin": item["origin"],
                        "error_class": item["error_class"],
                    }
                    for item in targets.errors
                )

            result["symbols"] = symbols
            if not symbols:
                return result

            newest_result = await session.execute(
                select(MarketEvent.symbol, func.max(MarketEvent.provider_created_at))
                .where(
                    MarketEvent.source == "naver_disclosure",
                    MarketEvent.category == "disclosure",
                    MarketEvent.market == "kr",
                    MarketEvent.symbol.in_(symbols),
                )
                .group_by(MarketEvent.symbol)
            )
            newest_by_symbol = dict(newest_result.all())
            dart_result = await session.execute(
                select(MarketEvent).where(
                    MarketEvent.source == "dart",
                    MarketEvent.category == "disclosure",
                    MarketEvent.market == "kr",
                    MarketEvent.symbol.in_(symbols),
                )
            )
            dart_by_symbol: dict[str, list[MarketEvent]] = {}
            for row in dart_result.scalars().all():
                if row.symbol:
                    dart_by_symbol.setdefault(row.symbol, []).append(row)

            repository = MarketEventsRepository(session)
            symbol_results: list[dict[str, object]] = []
            for symbol in symbols:
                try:
                    stats = await naver.collect_symbol_disclosures(
                        symbol=symbol,
                        fetch_page=lambda code, page: client.disclosures(
                            code, page=page
                        ),
                        insert_event=repository.insert_provider_event,
                        newest_stored_at=newest_by_symbol.get(symbol),
                        received_at=lambda: datetime.now(UTC),
                        dart_rows=dart_by_symbol.get(symbol, ()),
                        commit=args.commit,
                    )
                    if args.commit:
                        await session.commit()
                    symbol_results.append({"symbol": symbol, **stats})
                except Exception as exc:  # noqa: BLE001 - isolate source failures per symbol
                    await session.rollback()
                    errors.append({"symbol": symbol, "error_class": type(exc).__name__})
            result["results"] = symbol_results
            result["errors"] = errors
    finally:
        if own_client:
            await client.aclose()
    return result


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    from app.core.cli import setup_logging_and_sentry

    setup_logging_and_sentry(service_name="collect-naver-disclosures")
    report = asyncio.run(run(args))
    print(json.dumps(report, sort_keys=True, default=str))
    return 1 if report.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
