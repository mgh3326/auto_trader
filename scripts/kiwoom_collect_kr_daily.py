#!/usr/bin/env python3
"""Collect bounded KR daily charts through the Kiwoom Stage 1 read-only client.

The command defaults to a fetch-and-report dry run.  ``--commit`` is required
before any ``kr_candles_1d`` row can be written; existing candle keys are
preserved regardless of their source.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from app.services.brokers.kiwoom.stage2_daily_collect import (
    MAX_BARS_PER_REQUEST,
    MIN_RATE_SECONDS,
    KiwoomStage2CollectionDisabled,
    ResumeCheckpoint,
    arm_scoped_environment,
    build_default_collector,
)

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        required=True,
        help="dedicated Kiwoom read-only environment file (never a prod-named file)",
    )
    parser.add_argument(
        "--redis-url",
        required=True,
        help="isolated Redis URL for the Kiwoom OAuth token cache",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="optional six-digit KRX symbols; comma-separated values are accepted",
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=MAX_BARS_PER_REQUEST,
        help=f"daily bars per symbol, 1-{MAX_BARS_PER_REQUEST} (default: 600)",
    )
    parser.add_argument(
        "--rate-seconds",
        type=float,
        default=2.0,
        help="minimum interval between Kiwoom requests (default: 2.0; floor: 0.5)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume after the local checkpoint's last committed success",
    )
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and report without writing rows (the default)",
    )
    execution.add_argument(
        "--commit",
        action="store_true",
        help="permit insert-only writes to kr_candles_1d",
    )
    parser.add_argument(
        "--verify-sample",
        type=int,
        default=0,
        help="compare this many existing KRX candle symbols before collection",
    )
    args = parser.parse_args(argv)
    if "prod" in Path(args.env_file).name.casefold():
        parser.error("refusing to read a production env file")
    if not str(args.redis_url).strip():
        parser.error("--redis-url must not be empty")
    if not 1 <= args.bars <= MAX_BARS_PER_REQUEST:
        parser.error(f"--bars must be between 1 and {MAX_BARS_PER_REQUEST}")
    if args.rate_seconds < MIN_RATE_SECONDS:
        parser.error("--rate-seconds must be at least 0.5")
    if args.verify_sample < 0:
        parser.error("--verify-sample must be zero or greater")
    return args


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    env_file = Path(args.env_file)
    try:
        arm_scoped_environment(env_file=env_file, redis_url=args.redis_url)
        collector = build_default_collector()
        result = await collector.collect(
            symbols=args.symbols,
            bars=args.bars,
            rate_seconds=args.rate_seconds,
            commit=bool(args.commit),
            resume=bool(args.resume),
            checkpoint=ResumeCheckpoint.for_env_file(env_file),
            verify_sample=args.verify_sample,
        )
    except (KiwoomStage2CollectionDisabled, ValueError) as exc:
        logger.error("Kiwoom Stage 2 collection rejected: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001 - never print an untrusted response
        logger.error(
            "Kiwoom Stage 2 collection terminated error_code=%s",
            type(exc).__name__,
        )
        return 1

    logger.info(
        "Kiwoom Stage 2 summary mode=%s processed=%d/%d rows_received=%d "
        "rows_inserted=%d conflict_skipped=%d",
        "commit" if result.commit else "dry-run",
        result.processed_symbols,
        result.total_symbols,
        result.rows_received,
        result.rows_inserted,
        result.rows_conflict_skipped,
    )
    logger.info(
        "Kiwoom Stage 2 failed_symbols=%s verification_mismatches=%s "
        "verification_failures=%s",
        list(result.failed_symbols),
        list(result.verification_mismatch_symbols),
        [failure.symbol for failure in result.verification_failures],
    )
    return 2 if result.failures or result.verification_failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
