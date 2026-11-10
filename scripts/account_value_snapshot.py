#!/usr/bin/env python3
"""수동 계좌 스냅샷 및 외부 현금 흐름 기록. 기본 동작은 미리보기다."""

import argparse
import asyncio
import json
import sys
from datetime import date
from decimal import Decimal

from app.services.account_value.flows import ExternalFlow, record_flow
from app.services.account_value.readers import CompleteBrokerReader
from app.services.account_value.snapshot import capture


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--account", choices=("kis", "upbit", "toss"))
    result.add_argument(
        "--flow", nargs=4, metavar=("ACCOUNT", "CURRENCY", "AMOUNT", "DATE")
    )
    result.add_argument(
        "--commit", action="store_true", help="미리보기가 아닌 실제 기록"
    )
    return result


def session_factory():
    from app.core.db import AsyncSessionLocal

    return AsyncSessionLocal()


async def run(args):
    async with session_factory() as session:
        if args.flow:
            account, currency, amount, day = args.flow
            market = "account"
            flow = ExternalFlow(
                account, market, currency, Decimal(amount), date.fromisoformat(day)
            )
            await record_flow(session, flow, commit=args.commit)
            print(
                json.dumps(
                    {"mode": "commit" if args.commit else "preview", "flow": args.flow},
                    ensure_ascii=False,
                )
            )
            return
        if not args.account:
            raise ValueError("--account가 필요합니다")
        rows = await capture(
            CompleteBrokerReader(session, args.account),
            session,
            commit=args.commit,
        )
        print(
            json.dumps(
                {"mode": "commit" if args.commit else "preview", "snapshots": rows},
                default=str,
                ensure_ascii=False,
            )
        )


def main():
    args = parser().parse_args()
    try:
        asyncio.run(run(args))
    except Exception as exc:
        print(f"실패: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
