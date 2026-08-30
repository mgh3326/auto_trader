#!/usr/bin/env python3
"""Collect NHPLUG live period quotes through a bounded, manual CLI.

The command is dry-run by default.  It accepts only a dedicated 0600 NHPLUG
live credential file, requires an explicit date window, and never schedules
itself.  ``--commit`` is the sole lever for insert-only KR/US writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from pydantic import ValidationError

from app.services.brokers.nhplug.live_period_collect import (
    DEFAULT_BARS,
    DEFAULT_RATE_SECONDS,
    INDEXFX_SCHEMA_PROPOSAL,
    MIN_RATE_SECONDS,
    CollectionResult,
    NHPlugPeriodCollectionDisabled,
    NHPlugPeriodSettingsLoadError,
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
        help="dedicated 0600 NHPLUG live read-only env file (never prod-named)",
    )
    parser.add_argument(
        "--token-cache",
        required=True,
        help="0600 shared file used only for the NHPLUG live access token",
    )
    parser.add_argument("--market", required=True, choices=("kr", "us", "indexfx"))
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="optional symbols; comma-separated values are accepted",
    )
    parser.add_argument(
        "--start-date",
        required=True,
        help="inclusive YYYYMMDD lower bound; rows outside it are not written",
    )
    parser.add_argument(
        "--end-date",
        required=True,
        help="inclusive YYYYMMDD broker query end date",
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=DEFAULT_BARS,
        help="daily rows requested before local date-window filtering",
    )
    parser.add_argument(
        "--rate-seconds",
        type=float,
        default=DEFAULT_RATE_SECONDS,
        help=(
            f"minimum seconds between requests (documented floor {MIN_RATE_SECONDS})"
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume committed collection from its local checkpoint",
    )
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and report without writes (the default)",
    )
    execution.add_argument(
        "--commit",
        action="store_true",
        help="allow insert-only KR/US candle persistence",
    )
    parser.add_argument(
        "--verify-sample",
        type=int,
        default=0,
        help="for KR, compare N Kiwoom rows and classify against frozen KIS samples",
    )
    args = parser.parse_args(argv)
    if "prod" in Path(args.env_file).name.casefold():
        parser.error("refusing to read a production env file")
    if args.rate_seconds < MIN_RATE_SECONDS:
        parser.error(f"--rate-seconds must be at least {MIN_RATE_SECONDS}")
    if args.bars < 1:
        parser.error("--bars must be at least 1")
    if args.verify_sample < 0:
        parser.error("--verify-sample must be zero or greater")
    if args.resume and not args.commit:
        parser.error("--resume requires --commit")
    return args


def _safe_summary(result: CollectionResult) -> dict[str, object]:
    summary: dict[str, object] = {
        "market": result.market,
        "mode": "commit" if result.commit else "dry-run",
        "processed_symbols": result.processed_symbols,
        "total_symbols": result.total_symbols,
        "rows_received": result.rows_received,
        "rows_inserted": result.rows_inserted,
        "rows_conflict_skipped": result.rows_conflict_skipped,
        "invalid_rows": result.invalid_rows,
        "failed_symbols": [failure.symbol for failure in result.failures],
        "verification_failures": [
            failure.symbol for failure in result.verification_failures
        ],
        "verification": [
            {
                "symbol": item.symbol,
                "common_dates": item.common_dates,
                "mismatch_dates": list(item.mismatch_dates),
                "classification": item.classification.value,
            }
            for item in result.verification
        ],
        "resumed_from": result.resumed_from,
        "persistence_status": result.persistence_status,
    }
    if result.persistence_status == "SCHEMA_PROPOSAL_REQUIRED":
        summary["schema_proposal"] = list(INDEXFX_SCHEMA_PROPOSAL)
    return summary


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    env_file = Path(args.env_file)
    try:
        arm_scoped_environment(env_file=env_file)
        collector = build_default_collector(token_cache_path=Path(args.token_cache))
        result = await collector.collect(
            market=args.market,
            symbols=args.symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            bars=args.bars,
            rate_seconds=args.rate_seconds,
            commit=bool(args.commit),
            resume=bool(args.resume),
            checkpoint=ResumeCheckpoint.for_env_file(
                env_file=env_file, market=args.market
            ),
            verify_sample=args.verify_sample,
        )
    except (NHPlugPeriodSettingsLoadError, ValidationError) as exc:
        logger.error(
            "NHPLUG period collection settings failed stage=settings_load "
            "error_code=%s",
            type(exc).__name__,
        )
        return 2
    except (NHPlugPeriodCollectionDisabled, ValueError) as exc:
        logger.error(
            "NHPLUG period collection rejected stage=collection_validation "
            "error_code=%s",
            type(exc).__name__,
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - never render a response or secret
        logger.error(
            "NHPLUG period collection terminated error_code=%s", type(exc).__name__
        )
        return 1

    print(json.dumps(_safe_summary(result), sort_keys=True))
    return 2 if result.failures or result.verification_failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
