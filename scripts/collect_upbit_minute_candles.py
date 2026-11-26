"""Collect closed Upbit KRW one-minute candles (dry-run by default)."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from app.services.minute_candles.upbit_collector import collect_upbit_minute_candles


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--top-n", type=int, help="24시간 거래대금 상위 KRW 시장 수 (기본 20, 최대 100)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="DB 없이 수집 결과만 출력 (기본값)"
    )
    mode.add_argument("--commit", action="store_true", help="crypto_candles_1m 에 삽입")
    return parser


async def _run(args: argparse.Namespace) -> int:
    session = None
    if args.commit:
        from app.core.db import AsyncSessionLocal

        session = AsyncSessionLocal()
    try:
        result = await collect_upbit_minute_candles(
            session=session, top_n=args.top_n, dry_run=not args.commit
        )
        print(json.dumps(asdict(result), ensure_ascii=False, default=str, indent=2))
        return 1 if result.errors else 0
    finally:
        if session is not None:
            await session.close()


def main() -> int:
    args = _parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
