"""R4 P0 production-public historical seed backfill.

Default invocation is a no-network, no-write contract print. The one-shot
network run requires both ``R4_P0_BACKFILL_ENABLED=true`` and ``--run``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from app.services.brokers.binance.r4_p0_backfill import (
    BACKFILL_DB_FILENAME,
    BACKFILL_REST_PATH_ALLOWLIST,
    BACKFILL_VERSION,
    TARGET_EPOCHS,
    BackfillConfig,
    BackfillPITStore,
    BinanceR4P0Backfill,
    build_coverage_report,
    dump_report,
)
from app.services.brokers.binance.r4_p0_collector import PIT_COLUMNS, SYMBOLS

ENABLED_ENV = "R4_P0_BACKFILL_ENABLED"
DEFAULT_ARTIFACT_ROOT = "~/work/herdr-artifacts/r4-p0-seed-backfill"
REPORT_FILENAME = "coverage_report.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="execute one-shot backfill")
    mode.add_argument(
        "--audit",
        action="store_true",
        help="offline DB integrity and coverage audit; no network",
    )
    parser.add_argument("--artifact-root", default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--request-delay", type=float, default=0.35)
    return parser.parse_args()


def dry_run_payload(root: Path) -> dict[str, object]:
    return {
        "mode": "dry_run",
        "network": False,
        "database_write": False,
        "broker_mutation": False,
        "production_database_write": False,
        "live_artifact_write": False,
        "version": BACKFILL_VERSION,
        "symbols": list(SYMBOLS),
        "target_epochs": TARGET_EPOCHS,
        "rest_method": "GET",
        "rest_host": "fapi.binance.com",
        "authentication": "none",
        "rest_paths": sorted(BACKFILL_REST_PATH_ALLOWLIST),
        "pit_columns": list(PIT_COLUMNS),
        "artifact": str(root.expanduser() / BACKFILL_DB_FILENAME),
        "local_receive_time_semantics": (
            "backfill response completion time, not historical live receive time"
        ),
        "arm": f"{ENABLED_ENV}=true plus --run",
    }


def main() -> int:
    args = parse_args()
    root = Path(args.artifact_root).expanduser()
    if not args.run and not args.audit:
        print(json.dumps(dry_run_payload(root), ensure_ascii=False, indent=2))
        return 0
    if args.request_delay < 0:
        raise SystemExit("--request-delay must be non-negative")
    if args.run and os.getenv(ENABLED_ENV, "").strip().lower() != "true":
        raise SystemExit(f"refusing --run: set {ENABLED_ENV}=true explicitly")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with BackfillPITStore(root) as store:
        if args.audit:
            report = build_coverage_report(store)
        else:
            runner = BinanceR4P0Backfill(
                BackfillConfig(
                    artifact_root=root,
                    request_delay_seconds=args.request_delay,
                ),
                store,
            )
            report = asyncio.run(runner.run())
        dump_report(root / REPORT_FILENAME, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        acceptance = report["acceptance"]
        return (
            0
            if acceptance["three_signal_symbols_ofi_premium_252_100pct"]
            and acceptance["oi_matches_measured_retention"]
            and report["artifact"]["integrity_audit"]["ok"]
            else 1
        )


if __name__ == "__main__":
    raise SystemExit(main())
