#!/usr/bin/env python3
"""KIS mock fill-evidence read-only smoke (ROB-334).

Read-only: queries the KIS **mock** daily order-execution inquiry
(inquire_daily_order_domestic, is_mock=True) and runs the fill-evidence
classifier. Never submits, modifies, or cancels an order. Default-disabled —
requires KIS_MOCK_SCALPING_WS_ENABLED=true and KIS mock config. Prints only the
verdict/category and non-sensitive row keys; never prints secrets.

This smoke ALSO validates the real KIS daily-execution field names (it prints
the observed row keys) so the classifier candidate-key lists can be tightened.

Exit codes:
    0  - success (classified, or rows listed)
    1  - unexpected exception
    2  - inquiry error / unsupported mock API
    4  - disabled or KIS mock not configured (env/config no-op)

Usage:
    KIS_MOCK_SCALPING_WS_ENABLED=true uv run python -m scripts.kis_mock_fill_evidence_smoke \
        --order-no 0000123456 --symbol 005930
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import sys

from app.core.config import settings
from app.mcp_server.tooling.order_execution import _create_kis_client
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    classify_fill_evidence,
)

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KIS mock fill-evidence read-only smoke"
    )
    today = datetime.datetime.now().strftime("%Y%m%d")
    parser.add_argument("--from-date", default=today, help="YYYYMMDD (default: today)")
    parser.add_argument("--to-date", default=today, help="YYYYMMDD (default: today)")
    parser.add_argument(
        "--symbol", default=None, help="KR stock code filter (optional)"
    )
    parser.add_argument(
        "--order-no", default=None, help="Order number to classify (optional)"
    )
    parser.add_argument("--max-rows", type=int, default=20)
    return parser.parse_args(argv)


async def run_smoke(args: argparse.Namespace) -> int:
    if not settings.kis_mock_scalping_ws_enabled:
        logger.info(
            "KIS_MOCK_SCALPING_WS_ENABLED is not set; fill-evidence smoke disabled (no-op)."
        )
        return 4
    if not (
        settings.kis_mock_app_key
        and settings.kis_mock_app_secret
        and settings.kis_mock_account_no
    ):
        logger.error(
            "KIS mock not configured. Set: KIS_MOCK_APP_KEY, KIS_MOCK_APP_SECRET, "
            "KIS_MOCK_ACCOUNT_NO (names only — values not read here)."
        )
        return 4

    client = _create_kis_client(is_mock=True)
    try:
        rows = await client.inquire_daily_order_domestic(
            start_date=args.from_date,
            end_date=args.to_date,
            stock_code=args.symbol or "",
            order_number=args.order_no or "",
            is_mock=True,
        )
    except Exception as exc:  # noqa: BLE001 - read-only smoke, classify the fault
        logger.error("daily order-execution inquiry failed: %s", str(exc)[:300])
        return 2

    logger.info("rows=%d (showing up to %d)", len(rows), args.max_rows)
    for row in rows[: args.max_rows]:
        logger.info("row keys: %s", sorted(str(k) for k in row.keys()))

    if args.order_no:
        ev = classify_fill_evidence(order_no=args.order_no, rows=rows)
        logger.info(
            "verdict=%s category=%s reason=%s filled_qty=%s avg_price=%s",
            ev.verdict.value,
            ev.category.value if ev.category else "-",
            ev.reason_code,
            ev.filled_qty,
            ev.avg_price,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        return asyncio.run(run_smoke(_parse_args(argv)))
    except KeyboardInterrupt:
        return 1
    except Exception:  # noqa: BLE001
        logger.exception("unexpected error in fill-evidence smoke")
        return 1


if __name__ == "__main__":
    sys.exit(main())
