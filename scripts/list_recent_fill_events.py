"""Read-only CLI: 최근 체결(fill) 이벤트를 triage용 sanitized JSON으로 stdout 출력.

운영자-호스트 alert poller가 새 fire를 감지하는 데이터 소스(ROB-755).
브로커/주문/감시 mutation 없음. DB 쓰기 없음. raw_payload_json 미노출.

사용:
    uv run python -m scripts.list_recent_fill_events --source websocket --limit 50
    uv run python -m scripts.list_recent_fill_events --after-id 1234 --side buy --market kr
    uv run python -m scripts.list_recent_fill_events --source all --broker upbit --limit 100
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.core.db import AsyncSessionLocal
from app.services.execution_ledger.fill_event_sanitizer import sanitize_fill
from app.services.execution_ledger.repository import ExecutionLedgerRepository

VALID_MARKETS: set[str] = {"kr", "us", "crypto"}
VALID_SOURCES: set[str] = {"websocket", "reconciler", "manual_import", "all"}


async def collect(
    *,
    after_id: int | None,
    market: str | None,
    side: str | None,
    source: str,
    broker: str | None,
    account_mode: str | None,
    limit: int,
) -> dict:
    """DB에서 fill row 조회 후 sanitized JSON 직렬화 가능한 dict로 변환.

    ``source="all"``은 repo에 ``None``으로 전달되어 모든 source를 조회한다.
    """
    repo_source: str | None = None if source == "all" else source
    async with AsyncSessionLocal() as db:
        repo = ExecutionLedgerRepository(db)
        rows = await repo.list_recent_fills_for_triage(
            after_id=after_id,
            market=market,
            side=side,
            source=repo_source,
            broker=broker,
            account_mode=account_mode,
            limit=limit,
        )
    return {
        "success": True,
        "count": len(rows),
        "fills": [sanitize_fill(row) for row in rows],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="최근 체결(fill) 이벤트 read-only JSON (triage용)",
    )
    parser.add_argument("--after-id", type=int, default=None)
    parser.add_argument("--market", default=None, help="kr|us|crypto (기본 전체)")
    parser.add_argument("--side", default=None, help="buy|sell (기본 전체)")
    parser.add_argument(
        "--source",
        default="websocket",
        help="websocket|reconciler|manual_import|all (기본 websocket)",
    )
    parser.add_argument("--broker", default=None, help="kis|upbit|toss (default all)")
    parser.add_argument(
        "--account-mode",
        default=None,
        help="live|mock (기본 전체)",
    )
    parser.add_argument("--limit", type=int, default=50)

    # main() 시그니처는 호출자가 argparse의 SystemExit(2)를 피하기 위해
    # parse_args 전에 검증을 시도할 수도 있지만, --source는 모든 호출 경로에서
    # 유효해야 하므로 parse_known_args를 쓰지 않고 기본 parse_args + 사후 검증으로
    # 통일한다.
    args = parser.parse_args(argv)

    if args.source not in VALID_SOURCES:
        # DB에 닿기 전에 거부 — 잘못된 인풋은 poller의 무한 루프에서 즉시 차단되어야 함.
        json.dump(
            {
                "success": False,
                "error": f"invalid --source {args.source!r}; expected one of {sorted(VALID_SOURCES)}",
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 1

    if args.market is not None and args.market not in VALID_MARKETS:
        # DB에 닿기 전에 거부 — market typo가 전체 market 조회로 번지는 것을 막는다.
        json.dump(
            {
                "success": False,
                "error": f"invalid --market {args.market!r}; expected one of {sorted(VALID_MARKETS)}",
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 1

    try:
        out = asyncio.run(
            collect(
                after_id=args.after_id,
                market=args.market,
                side=args.side,
                source=args.source,
                broker=args.broker,
                account_mode=args.account_mode,
                limit=args.limit,
            )
        )
    except Exception as exc:  # noqa: BLE001 — 운영자 CLI는 모든 예외를 JSON으로 감싼다
        json.dump({"success": False, "error": str(exc)}, sys.stdout)
        sys.stdout.write("\n")
        return 1
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
