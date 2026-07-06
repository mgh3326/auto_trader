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
from app.services.execution_ledger.repository import ExecutionLedgerRepository

VALID_SOURCES: set[str] = {"websocket", "reconciler", "manual_import", "all"}


def _derive_market(instrument_type: str) -> str:
    """instrument_type 컬럼을 폴러 친화적인 market 코드로 변환.

    --market 입력과 무관하게 row의 instrument_type에서 결정된다 (도메인 단일 출처).
    """
    if instrument_type == "equity_kr":
        return "kr"
    if instrument_type == "equity_us":
        return "us"
    if instrument_type == "crypto":
        return "crypto"
    # forex/index 등 (체결 row에는 사실상 없음) — 그대로 소문자.
    return instrument_type.lower()


def _sanitize(row) -> dict:
    """ExecutionLedger ORM row → triage용 dict (raw_payload_json 미포함).

    보안 제약: raw_payload_json은 절대 stdout에 노출되지 않는다.
    """
    return {
        "ledger_id": row.id,
        "event_key": f"execution_ledger:{row.id}",
        "broker": row.broker,
        "account_mode": row.account_mode,
        "venue": row.venue,
        "instrument_type": row.instrument_type,
        "market": _derive_market(row.instrument_type),
        "symbol": row.symbol,
        "raw_symbol": row.raw_symbol,
        "side": row.side,
        "filled_qty": str(row.filled_qty),
        "filled_price": str(row.filled_price),
        "filled_notional": str(row.filled_notional),
        "currency": row.currency,
        "broker_order_id": row.broker_order_id,
        "fill_seq": row.fill_seq,
        "correlation_id": str(row.correlation_id) if row.correlation_id is not None else None,
        "source": row.source,
        "filled_at": row.filled_at.isoformat(),
        "created_at": row.created_at.isoformat(),
    }


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
        "fills": [_sanitize(row) for row in rows],
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
    parser.add_argument("--broker", default=None, help="kis|upbit (기본 전체)")
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
