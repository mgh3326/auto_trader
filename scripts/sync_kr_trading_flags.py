#!/usr/bin/env python3
"""hk 6503 — sync KR 관리·시장경고·정리매매·거래정지 flags into the store.

  --source kis-master      daily baseline from the KIS 종목정보 zip files
  --source naver-intraday  intraday overlay from the Naver 관리·거래정지·경보 list

``--dry-run`` parses and prints counts without writing. Without it, rows are
written through the store's guard. A failed fetch or an unreadable payload exits
1 before any write. This script is manual; no scheduler registration lives in
the repository. The intraday overlay DAG is an unregistered copy under
``deploy/dagu/at-kr-trading-flags/``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime

from app.core.cli import run_async_job, setup_logging_and_sentry
from app.services.kr_trading_flags.naver_status import DEFAULT_PAGE_SIZE
from app.services.kr_trading_flags.sync import (
    sync_kis_master_flags,
    sync_naver_intraday_flags,
)

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--source",
        required=True,
        choices=("kis-master", "naver-intraday"),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help="naver-intraday only: pageSize sent to the status endpoint.",
    )
    return parser.parse_args(argv)


async def main(args: argparse.Namespace) -> int:
    setup_logging_and_sentry(service_name="kr-trading-flags-sync")
    now = datetime.now(UTC)

    async def _job() -> int:
        if args.source == "kis-master":
            summary = await sync_kis_master_flags(dry_run=args.dry_run, now=now)
        else:
            if args.page_size < 1:
                logger.error("page-size must be positive")
                return 2
            summary = await sync_naver_intraday_flags(
                dry_run=args.dry_run, now=now, page_size=args.page_size
            )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0

    return await run_async_job(_job, process="sync_kr_trading_flags")


if __name__ == "__main__":
    parsed = parse_args()
    raise SystemExit(asyncio.run(main(parsed)))
