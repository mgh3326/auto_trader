#!/usr/bin/env python3
"""수동 KIS KSD 일정 수집. Dagu 복사본은 등록하지 않는다."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date
from typing import Any

from app.core.cli import setup_logging_and_sentry
from app.services.brokers.kis.client import KISClient
from app.services.market_events.kis_ksd_dividends import (
    KSD_EVENT_KINDS,
    fetch_ksd_event_window,
    normalize_ksd_dividend_row,
    normalize_ksd_event_row,
)
from app.services.market_events.repository import MarketEventsRepository

logger = logging.getLogger(__name__)

ALL_KINDS = ("dividend", *KSD_EVENT_KINDS)
FETCH_METHODS: dict[str, str] = {
    "dividend": "ksd_dividends",
    **{kind: f"ksd_{kind}" for kind in KSD_EVENT_KINDS},
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KIS KSD 기업 일정 수집")
    parser.add_argument("--from-date", required=True, type=date.fromisoformat)
    parser.add_argument("--to-date", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--kinds",
        nargs="+",
        choices=ALL_KINDS,
        default=list(ALL_KINDS),
        help="수집할 KSD 종류 (기본: 전체)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="commit", action="store_false")
    mode.add_argument("--commit", dest="commit", action="store_true")
    parser.set_defaults(commit=False)
    args = parser.parse_args(argv)
    if args.from_date > args.to_date:
        parser.error("--from-date는 --to-date보다 늦을 수 없습니다")
    return args


def _normalize(
    kind: str, row: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if kind == "dividend":
        return normalize_ksd_dividend_row(row)
    return normalize_ksd_event_row(kind, row)


async def run(args: argparse.Namespace) -> int:
    client = KISClient(is_mock=False)
    normalized: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    truncation_count = 0
    try:
        for kind in args.kinds:
            fetch = getattr(client, FETCH_METHODS[kind])
            result = await fetch_ksd_event_window(fetch, args.from_date, args.to_date)
            normalized.extend(_normalize(kind, row) for row in result.rows)
            for start, end in result.truncated_windows:
                truncation_count += 1
                logger.warning(
                    "응답 상한으로 일부 누락 가능 kind=%s window=%s..%s",
                    kind,
                    start,
                    end,
                )
            logger.info("KSD 조회 kind=%s rows=%d", kind, len(result.rows))
    finally:
        await client.close()

    if not args.commit:
        logger.info(
            "dry-run 조회·정규화=%d, 저장=0, 절단 구간=%d",
            len(normalized),
            truncation_count,
        )
        return 0

    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        repo = MarketEventsRepository(db)
        for event, values in normalized:
            await repo.upsert_event_with_values(event, values, replace_values=True)
        await db.commit()
    logger.info("KSD 저장=%d, 절단 구간=%d", len(normalized), truncation_count)
    return 0


def main() -> int:
    setup_logging_and_sentry(service_name="kis-ksd-events-ingest")
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
