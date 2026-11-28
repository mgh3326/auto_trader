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
    KSDUnscheduledRowError,
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
# A run exits 1 only when malformed rows exceed this count. Documented blank
# schedule rows (skipped_unscheduled) never count toward it. Transport and
# HTTP errors still raise, so they also exit nonzero.
MALFORMED_EXIT_THRESHOLD = 5


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


def _normalize_rows(
    kind: str, rows: list[dict[str, Any]]
) -> tuple[list[tuple[dict[str, Any], list[dict[str, Any]]]], int, int]:
    """Return (normalized, malformed, skipped_unscheduled) for one kind."""
    normalized: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    malformed = 0
    unscheduled = 0
    for index, row in enumerate(rows, start=1):
        try:
            normalized.append(_normalize(kind, row))
        except KSDUnscheduledRowError as exc:
            unscheduled += 1
            logger.debug(
                "KSD 미정 일정 생략 kind=%s row=%d reason=%s", kind, index, exc
            )
        except ValueError as exc:
            malformed += 1
            logger.warning(
                "KSD 행 정규화 실패 kind=%s row=%d reason=%s", kind, index, exc
            )
    return normalized, malformed, unscheduled


def _exit_code(malformed: int) -> int:
    return 1 if malformed > MALFORMED_EXIT_THRESHOLD else 0


async def run(args: argparse.Namespace) -> int:
    client = KISClient(is_mock=False)
    normalized: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    malformed_total = 0
    unscheduled_total = 0
    truncation_count = 0
    try:
        for kind in args.kinds:
            fetch = getattr(client, FETCH_METHODS[kind])
            result = await fetch_ksd_event_window(fetch, args.from_date, args.to_date)
            kind_events, kind_malformed, kind_unscheduled = _normalize_rows(
                kind, result.rows
            )
            normalized.extend(kind_events)
            malformed_total += kind_malformed
            unscheduled_total += kind_unscheduled
            for start, end in result.truncated_windows:
                truncation_count += 1
                logger.warning(
                    "응답 상한으로 일부 누락 가능 kind=%s window=%s..%s",
                    kind,
                    start,
                    end,
                )
            logger.info(
                "KSD 조회 kind=%s rows=%d malformed=%d skipped_unscheduled=%d",
                kind,
                len(result.rows),
                kind_malformed,
                kind_unscheduled,
            )
    finally:
        await client.close()

    if not args.commit:
        logger.info(
            "dry-run 조회·정규화=%d, 저장=0, 정규화 실패(malformed)=%d, "
            "미정 생략(skipped_unscheduled)=%d, 절단 구간=%d",
            len(normalized),
            malformed_total,
            unscheduled_total,
            truncation_count,
        )
        return _exit_code(malformed_total)

    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        repo = MarketEventsRepository(db)
        for event, values in normalized:
            await repo.upsert_event_with_values(event, values, replace_values=True)
        await db.commit()
    logger.info(
        "KSD 저장=%d, 정규화 실패(malformed)=%d, 미정 생략(skipped_unscheduled)=%d, "
        "절단 구간=%d",
        len(normalized),
        malformed_total,
        unscheduled_total,
        truncation_count,
    )
    return _exit_code(malformed_total)


def main() -> int:
    setup_logging_and_sentry(service_name="kis-ksd-events-ingest")
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
