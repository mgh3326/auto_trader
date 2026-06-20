"""Read-only CLI: 최근 DELIVERED watch 이벤트를 JSON으로 stdout 출력.

운영자-호스트 alert poller가 새 fire를 감지하는 데이터 소스(ROB-602).
브로커/주문/감시 mutation 없음. DB 쓰기 없음.

사용:
    uv run python -m scripts.list_recent_watch_events --market crypto --since 2026-06-20T12:00:00Z --limit 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime

from app.core.db import AsyncSessionLocal
from app.services.investment_reports.repository import InvestmentReportsRepository


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def collect(*, market: str | None, since: str | datetime | None, limit: int) -> dict:
    parsed = since if isinstance(since, datetime) else _parse_since(since)
    async with AsyncSessionLocal() as db:
        repo = InvestmentReportsRepository(db)
        events = await repo.list_events_by_delivery_status(
            delivery_status="delivered",
            delivered_since=parsed,
            market=market,
            limit=limit,
        )
    return {
        "success": True,
        "count": len(events),
        "events": [
            {
                "event_uuid": str(e.event_uuid),
                "symbol": e.symbol,
                "market": e.market,
                "source_report_uuid": str(e.source_report_uuid) if e.source_report_uuid else None,
                "metric": e.metric,
                "operator": e.operator,
                "threshold": str(e.threshold) if e.threshold is not None else None,
                "current_value": str(e.current_value) if e.current_value is not None else None,
                "delivered_at": e.delivered_at.isoformat() if e.delivered_at else None,
                "kst_date": e.kst_date,
            }
            for e in events
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="최근 delivered watch 이벤트(read-only JSON)")
    parser.add_argument("--market", default=None, help="kr|us|crypto (기본 전체)")
    parser.add_argument("--since", default=None, help="ISO8601, delivered_at >= since")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    out = asyncio.run(collect(market=args.market, since=args.since, limit=args.limit))
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
