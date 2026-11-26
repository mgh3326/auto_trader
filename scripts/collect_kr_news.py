"""Collect bounded Korean Naver news and print a source-level JSON report."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

from app.services.kr_news_collector import collect_kr_news


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="국내 네이버 종목 뉴스를 수집합니다.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="DB 세션 없이 조회 결과만 출력합니다(기본값).",
    )
    mode.add_argument(
        "--commit", action="store_true", help="기사와 종목 연결을 저장합니다."
    )
    parser.add_argument(
        "--symbols",
        help="선택한 6자리 국내 종목 코드 목록(쉼표 구분). 드라이런에서 종목별 조회에도 사용합니다.",
    )
    parser.add_argument("--symbol-cap", type=int, default=30)
    args = parser.parse_args(argv)
    if args.symbol_cap < 0:
        parser.error("--symbol-cap must be non-negative")
    args.dry_run = not args.commit
    args.symbols = (
        [item.strip() for item in args.symbols.split(",") if item.strip()]
        if args.symbols
        else None
    )
    return args


async def _run(args: argparse.Namespace) -> dict:
    if args.dry_run:
        report = await collect_kr_news(
            db=None,
            now=datetime.now(UTC),
            explicit_symbols=args.symbols,
            symbol_cap=args.symbol_cap,
        )
    else:
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            report = await collect_kr_news(
                db=db,
                now=datetime.now(UTC),
                explicit_symbols=args.symbols,
                symbol_cap=args.symbol_cap,
            )
    return report.as_dict()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = asyncio.run(_run(args))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if any(source["status"] == "error" for source in report["sources"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
