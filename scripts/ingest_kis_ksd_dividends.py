#!/usr/bin/env python3
"""수동 KSD 배당 일정 수집. 자동 스케줄에 등록하지 않는다."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.services.brokers.kis.client import KISClient
from app.services.market_events.kis_ksd_dividends import (
    fetch_ksd_dividend_window,
    normalize_ksd_dividend_row,
)
from app.services.market_events.repository import MarketEventsRepository

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KSD 배당 일정 수동 수집")
    parser.add_argument("--from-date", required=True, type=date.fromisoformat)
    parser.add_argument("--to-date", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--dry-run", action="store_true", help="조회·정규화만 하고 저장하지 않음"
    )
    args = parser.parse_args()
    if args.from_date > args.to_date:
        parser.error("--from-date는 --to-date보다 늦을 수 없습니다")
    return args


async def run(args: argparse.Namespace) -> int:
    client = KISClient(is_mock=False)
    try:
        result = await fetch_ksd_dividend_window(
            client.ksd_dividends, args.from_date, args.to_date
        )
    finally:
        await client.close()

    normalized = [normalize_ksd_dividend_row(row) for row in result.rows]
    for start, end in result.truncated_windows:
        logger.warning("응답 상한으로 일부 누락 가능: %s .. %s", start, end)

    if args.dry_run:
        logger.info(
            "dry-run 조회=%d, 정규화=%d, 절단 구간=%d",
            len(result.rows),
            len(normalized),
            len(result.truncated_windows),
        )
        return 0

    async with AsyncSessionLocal() as db:
        repo = MarketEventsRepository(db)
        for event, values in normalized:
            await repo.upsert_event_with_values(event, values, replace_values=True)
        await db.commit()
    logger.info(
        "KSD 배당 일정 저장=%d, 절단 구간=%d",
        len(normalized),
        len(result.truncated_windows),
    )
    return 0


def main() -> int:
    setup_logging_and_sentry()
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
