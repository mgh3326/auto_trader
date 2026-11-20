"""Read the venue best ask N times and append one record per read (Q10 E1).

Read-only: the only broker calls are the KIS orderbook and price reads behind
``app.services.order_proposals.mandate_quote``. It sends no order, writes no
database row and is not scheduled. Each line of ``--out`` is a closed
``QuoteReadRecord`` (no account number, no secret); the elapsed time from a read
to a later send is the difference between its ``observed_at`` and the send time
the sender records.

    uv run python -m scripts.mandate_ask_probe --symbol 005930 --count 20 \
        --interval-seconds 2 --out /path/outside/the/repo/e1.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from app.services.order_proposals.mandate_quote import (
    JsonlSink,
    has_book_source,
    read_venue_quote,
)

MAX_COUNT = 500


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--account-mode", default="kiwoom_mock")
    parser.add_argument("--market", default="equity_kr")
    parser.add_argument("--symbol", action="append", required=True)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--premium-bps", default="30")
    parser.add_argument("--out", type=Path, required=True)
    return parser


async def probe(args: argparse.Namespace) -> int:
    sink = JsonlSink(args.out)
    premium = Decimal(args.premium_bps)
    for index in range(args.count):
        for symbol in args.symbol:
            await read_venue_quote(
                account_mode=args.account_mode,
                market=args.market,
                symbol=symbol,
                premium_bps=premium,
                sink=sink,
            )
        if index + 1 < args.count:
            await asyncio.sleep(args.interval_seconds)
    # A measurement with lost or no records is not a measurement.
    if sink.failed or not sink.written:
        print(f"기록 실패: 저장 {sink.written}건, 유실 {sink.failed}건")
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.count <= MAX_COUNT or not 0.5 <= args.interval_seconds <= 3600:
        print("--count 은 1~500, --interval-seconds 는 0.5~3600 이어야 합니다.")
        return 2
    if not has_book_source(args.account_mode, args.market):
        print("이 계좌·시장에는 호가 읽기 소스가 없습니다(대체 없음).")
        return 2
    if not args.out.resolve().parent.is_dir():
        print("--out 의 상위 디렉터리가 없습니다.")
        return 2
    repo_root = Path(__file__).resolve().parents[1]
    if repo_root in args.out.resolve().parents:
        print("--out 은 레포 밖 경로여야 합니다.")
        return 2
    return asyncio.run(probe(args))


if __name__ == "__main__":
    sys.exit(main())
