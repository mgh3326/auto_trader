#!/usr/bin/env python3
"""수동 기간 성과표: 기본 기간은 최근 7일이다."""

import argparse
import asyncio
import sys
from datetime import date, timedelta

from app.core.timezone import today_kst
from app.services.account_value.load_report import load_weekly_report
from app.services.account_value.report import render


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--end", type=date.fromisoformat, default=today_kst())
    result.add_argument("--start", type=date.fromisoformat)
    result.add_argument(
        "--flows-complete",
        action="store_true",
        help="해당 기간의 외부 흐름 입력을 확인함",
    )
    return result


def session_factory():
    from app.core.db import AsyncSessionLocal

    return AsyncSessionLocal()


async def run(args):
    start = args.start or args.end - timedelta(days=7)
    if start >= args.end:
        raise ValueError("시작일은 종료일보다 빨라야 합니다")
    async with session_factory() as session:
        rows = await load_weekly_report(
            session, start, args.end, flows_complete=args.flows_complete
        )
    print(render(rows))


def main():
    args = parser().parse_args()
    try:
        asyncio.run(run(args))
    except Exception as exc:
        print(f"실패: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
