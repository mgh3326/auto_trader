#!/usr/bin/env python3
"""수동 계좌 스냅샷 및 외부 현금 흐름 기록. 기본 동작은 미리보기다."""

import argparse
import asyncio
import json
import sys
from datetime import date
from decimal import Decimal
from uuid import UUID

from app.services.account_value.completeness import (
    record_confirmation,
    record_revocation,
)
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
        "--confirm-flows",
        nargs=5,
        metavar=("ACCOUNT", "START", "END", "CONFIRMED_BY", "REASON"),
    )
    result.add_argument(
        "--revoke-confirmation",
        nargs=3,
        metavar=("ID", "REVOKED_BY", "REASON"),
        help="흐름 완전성 확인을 추가 전용으로 철회합니다",
    )
    result.add_argument("--flow-id", type=UUID, help="재시도에 재사용할 흐름 UUID")
    result.add_argument(
        "--commit", action="store_true", help="미리보기가 아닌 실제 기록"
    )
    return result


def session_factory():
    from app.core.db import AsyncSessionLocal

    return AsyncSessionLocal()


async def run(args):
    async with session_factory() as session:
        if getattr(args, "revoke_confirmation", None):
            confirmation_id, revoked_by, reason = args.revoke_confirmation
            if (
                args.flow
                or args.account
                or getattr(args, "confirm_flows", None)
                or args.flow_id
            ):
                raise ValueError("하나의 작업만 지정해야 합니다")
            status = await record_revocation(
                session,
                int(confirmation_id),
                revoked_by,
                reason,
                commit=args.commit,
            )
            print(
                json.dumps(
                    {
                        "mode": "commit" if args.commit else "preview",
                        "confirmation_id": int(confirmation_id),
                        "write_status": status,
                    },
                    ensure_ascii=False,
                )
            )
            return
        if getattr(args, "confirm_flows", None):
            account, start, end, confirmed_by, reason = args.confirm_flows
            if args.flow or args.account:
                raise ValueError("하나의 작업만 지정해야 합니다")
            status = await record_confirmation(
                session,
                account,
                date.fromisoformat(start),
                date.fromisoformat(end),
                confirmed_by,
                reason,
                commit=args.commit,
            )
            print(
                json.dumps(
                    {
                        "mode": "commit" if args.commit else "preview",
                        "account": account.rsplit(":", 1)[0] + ":***",
                        "start": start,
                        "end": end,
                        "write_status": status,
                    },
                    ensure_ascii=False,
                )
            )
            return
        if args.flow:
            account, currency, amount, day = args.flow
            market = "account"
            if args.flow_id is None:
                raise ValueError("--flow-id가 필요합니다")
            flow = ExternalFlow(
                account,
                market,
                currency,
                Decimal(amount),
                date.fromisoformat(day),
                args.flow_id,
            )
            status = await record_flow(session, flow, commit=args.commit)
            print(
                json.dumps(
                    {
                        "mode": "commit" if args.commit else "preview",
                        "flow": [
                            args.flow[0].rsplit(":", 1)[0] + ":***",
                            *args.flow[1:],
                        ],
                        "flow_id": str(args.flow_id),
                        "write_status": status,
                    },
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
