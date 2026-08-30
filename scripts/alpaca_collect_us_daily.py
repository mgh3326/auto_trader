#!/usr/bin/env python3
"""Collect Alpaca IEX US daily bars into the insert-only US candle store.

The command is dry-run by default.  It accepts only a dedicated 0600 env file
and never reads the paper-trading credential namespace.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from app.services.brokers.alpaca.us_daily_collect import (
    DEFAULT_BARS,
    DEFAULT_BATCH_SIZE,
    MAX_BARS_PER_REQUEST,
    MAX_BATCH_SIZE,
    MIN_RATE_SECONDS,
    AlpacaUsDailyCollectionDisabled,
    ResumeCheckpoint,
    arm_scoped_environment,
    build_default_collector,
)

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file", required=True, help="dedicated 0600 Alpaca data env file"
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=DEFAULT_BARS,
        help=f"bars per symbol, 1-{MAX_BARS_PER_REQUEST}",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"symbols per request, 1-{MAX_BATCH_SIZE}",
    )
    parser.add_argument(
        "--rate-seconds",
        type=float,
        default=0.35,
        help=f"minimum seconds between requests (floor {MIN_RATE_SECONDS})",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="optional US symbols; comma-separated values accepted",
    )
    parser.add_argument(
        "--all-active",
        action="store_true",
        help="include active non-common-stock symbols",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume committed collection from local checkpoint",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="permit insert-only writes (default is dry-run)",
    )
    parser.add_argument(
        "--verify-sample",
        type=int,
        default=0,
        help="compare N existing symbols before collection",
    )
    args = parser.parse_args(argv)
    if "prod" in Path(args.env_file).name.casefold():
        parser.error("refusing to read a production env file")
    if not 1 <= args.bars <= MAX_BARS_PER_REQUEST:
        parser.error(f"--bars must be between 1 and {MAX_BARS_PER_REQUEST}")
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        parser.error(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")
    if args.rate_seconds < MIN_RATE_SECONDS:
        parser.error(f"--rate-seconds must be at least {MIN_RATE_SECONDS}")
    if args.verify_sample < 0:
        parser.error("--verify-sample must be zero or greater")
    if args.resume and not args.commit:
        parser.error("--resume requires --commit")
    return args


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    env_file = Path(args.env_file)
    try:
        arm_scoped_environment(env_file=env_file)
        collector = build_default_collector(all_active=bool(args.all_active))
        result = await collector.collect(
            symbols=args.symbols,
            bars=args.bars,
            batch_size=args.batch_size,
            rate_seconds=args.rate_seconds,
            commit=bool(args.commit),
            resume=bool(args.resume),
            checkpoint=ResumeCheckpoint.for_env_file(env_file),
            verify_sample=args.verify_sample,
        )
    except (AlpacaUsDailyCollectionDisabled, ValueError) as exc:
        logger.error("Alpaca US daily collection rejected: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001 - never render broker response or secret values
        logger.error(
            "Alpaca US daily collection terminated error_code=%s", type(exc).__name__
        )
        return 1
    logger.info(
        "Alpaca US daily summary mode=%s processed=%d/%d rows_received=%d rows_inserted=%d conflict_skipped=%d",
        "commit" if result.commit else "dry-run",
        result.processed_symbols,
        result.total_symbols,
        result.rows_received,
        result.rows_inserted,
        result.rows_conflict_skipped,
    )
    logger.info(
        "Alpaca US daily failed_symbols=%s verification_mismatches=%s verification_failures=%s",
        list(result.failed_symbols),
        list(result.verification_mismatch_symbols),
        [failure.symbol for failure in result.verification_failures],
    )
    return 2 if result.failures or result.verification_failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
