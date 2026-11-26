#!/usr/bin/env python3
"""Collect Naver KR market-wide daily observations (manual lever; dry-run default)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.services.kr_market_daily.client import NaverMarketDailyClient
from app.services.kr_market_daily.collector import collect_market_daily

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


def _today_kst() -> date:
    from datetime import datetime

    return datetime.now(KST).date()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect Naver KR market daily breadth and flows."
    )
    parser.add_argument(
        "--date", type=date.fromisoformat, default=None, help="Trade date (YYYY-MM-DD)."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print without database writes (default).",
    )
    mode.add_argument(
        "--commit", action="store_true", help="Insert fetched rows into the database."
    )
    args = parser.parse_args(argv)
    args.target_date = args.date or _today_kst()
    args.dry_run = not args.commit
    return args


def _json_default(value: Any) -> str:
    if isinstance(value, (date, Decimal)):
        return value.isoformat() if isinstance(value, date) else str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


async def run(args: argparse.Namespace) -> int:
    try:
        client = NaverMarketDailyClient()
        if args.commit:
            from app.core.db import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                result = await collect_market_daily(
                    target_date=args.target_date,
                    today=_today_kst(),
                    client=client,
                    session=session,
                )
        else:
            result = await collect_market_daily(
                target_date=args.target_date,
                today=_today_kst(),
                client=client,
            )
        print(json.dumps(result, ensure_ascii=False, default=_json_default))
        return 0
    except Exception as exc:
        logger.error("KR market daily collection failed: %s", type(exc).__name__)
        return 1


def main() -> int:
    args = parse_args()
    from app.core.cli import setup_logging_and_sentry

    setup_logging_and_sentry(service_name="collect_kr_market_daily")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
