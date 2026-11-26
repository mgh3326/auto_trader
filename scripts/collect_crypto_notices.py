#!/usr/bin/env python3
"""Upbit·Bithumb 공지 수집 CLI. 기본은 저장하지 않는 드라이런이다."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from app.services.market_events.crypto_notices import collect_crypto_notices
from app.services.market_events.repository import MarketEventsRepository

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upbit·Bithumb 거래소 공지 수집")
    parser.add_argument(
        "--pages", type=int, default=5, help="Upbit 조회 페이지 수 (1~20, 기본 5)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="저장 없이 조회합니다 (기본값)"
    )
    mode.add_argument(
        "--commit", action="store_true", help="시장 이벤트 저장소에 기록합니다"
    )
    args = parser.parse_args(argv)
    if not 1 <= args.pages <= 20:
        parser.error("--pages는 1부터 20까지 지정해야 합니다")
    return args


async def _run(args: argparse.Namespace) -> int:
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await collect_crypto_notices(
            db,
            pages=args.pages,
            commit=args.commit,
            repo=MarketEventsRepository(db),
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if any(source["error"] for source in result.values()) else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        logger.error("공지 수집 실패: %s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
