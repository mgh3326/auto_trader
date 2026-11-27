#!/usr/bin/env python3
"""SEC EDGAR 공시 수집 CLI (hk 6407, 읽기 전용).

기본은 저장하지 않는 드라이런이다. ``--commit`` 일 때만
``market_events`` 에 기록한다. 스케줄러 등록 없음 — 수동 실행 전용.

SEC fair access 는 선언된 User-Agent 를 요구한다. ``SEC_EDGAR_USER_AGENT``
가 비어 있으면 네트워크 호출·DB 접속 전에 종료 코드 2 로 거부한다.

Examples:
    python -m scripts.collect_sec_edgar --symbols NVDA --dry-run
    python -m scripts.collect_sec_edgar --symbols NVDA MSFT --commit
    python -m scripts.collect_sec_edgar --ciks 1045810 --from-date 2025-01-01
    python -m scripts.collect_sec_edgar --symbols AAPL --ir   # unsupported only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date

from app.core.config import settings
from app.services.market_events.repository import MarketEventsRepository
from app.services.market_events.sec_edgar import (
    USER_AGENT_ENV,
    SecEdgarUserAgentMissing,
    collect_ir_requests,
    collect_sec_filings,
)

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SEC EDGAR 공시 수집 (읽기 전용, 수동 실행)"
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="EDGAR 티커 목록 (예: NVDA MSFT). company_tickers.json 으로 CIK 조회",
    )
    parser.add_argument(
        "--ciks",
        nargs="+",
        type=int,
        default=None,
        help="CIK 숫자 목록 (예: 1045810). 티커 조회 없이 submissions 직행",
    )
    parser.add_argument(
        "--from-date",
        type=date.fromisoformat,
        dest="from_date",
        default=None,
        help="filingDate 하한 (YYYY-MM-DD, 포함). 미지정 시 recent 전체",
    )
    parser.add_argument(
        "--to-date",
        type=date.fromisoformat,
        dest="to_date",
        default=None,
        help="filingDate 상한 (YYYY-MM-DD, 포함)",
    )
    parser.add_argument(
        "--ir",
        action="store_true",
        help="기업 IR 피드 요청으로 처리 — 공식 API 가 없으므로 unsupported 만 기록",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="조회·정규화만 하고 저장하지 않습니다 (기본값)",
    )
    mode.add_argument(
        "--commit",
        action="store_true",
        help="market_events 저장소에 기록합니다",
    )
    args = parser.parse_args(argv)
    if not args.symbols and not args.ciks:
        parser.error("--symbols 또는 --ciks 중 하나는 필요합니다")
    if args.from_date and args.to_date and args.from_date > args.to_date:
        parser.error("--from-date 는 --to-date 이하여야 합니다")
    return args


async def _run(args: argparse.Namespace) -> int:
    if args.ir:
        labels = list(args.symbols or []) + [f"CIK{cik}" for cik in (args.ciks or [])]
        result = {
            "mode": "ir",
            "note": "IR pages have no official API/feed; nothing was collected",
            "issuers": collect_ir_requests(labels),
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    if not (settings.sec_edgar_user_agent or "").strip():
        # Gate-off: refuse before opening a DB session or any network call.
        logger.error("%s is empty; refusing SEC EDGAR requests", USER_AGENT_ENV)
        return 2

    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await collect_sec_filings(
            db,
            symbols=args.symbols,
            ciks=args.ciks,
            from_date=args.from_date,
            to_date=args.to_date,
            commit=args.commit,
            repo=MarketEventsRepository(db),
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    partition = result.get("partition") or {}
    if result["totals"]["failed"] or partition.get("status") == "failed":
        return 1
    if not result["issuers"]:
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except SecEdgarUserAgentMissing as exc:
        logger.error("%s", exc)
        return 2
    except Exception as exc:
        logger.error("SEC EDGAR 수집 실패: %s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
