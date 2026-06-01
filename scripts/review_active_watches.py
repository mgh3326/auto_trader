"""ROB-337 Slice 2 — operator CLI for the watch validity review job.

Default-disabled. --dry-run (default) prints the plan without DB/HTTP/secrets
and lazy-imports Settings-backed modules only in the --run path.

Modes:
  --dry-run : print plan; no DB, no HTTP, no secrets required.
  --run     : execute review (writes alert_metadata.last_review + throttled
              Hermes notifications). Read-only w.r.t. broker/orders.

Exit codes:
  0 — disabled, dry-run, or successful run
  1 — unexpected exception during --run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

logger = logging.getLogger("review_active_watches")

_ENABLE_ENV = "WATCH_VALIDITY_REVIEW_ENABLED"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="watch validity review (read-only)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="print plan; no writes")
    group.add_argument("--run", action="store_true", help="execute review")
    return parser.parse_args(argv)


async def _run() -> int:
    # Lazy imports — only here, so --help / --dry-run need no Settings/secrets.
    from app.services.investment_reports.watch_validity_review import (
        WatchValidityReviewService,
    )

    service = WatchValidityReviewService()
    try:
        results = await service.run(dry_run=False)
    finally:
        await service.close()
    print(f"review complete: {results}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not _truthy(os.environ.get(_ENABLE_ENV)):
        print(f"watch validity review disabled — set {_ENABLE_ENV}=true to opt in")
        return 0
    if args.run:
        try:
            return asyncio.run(_run())
        except Exception:
            logger.exception("watch validity review --run failed")
            return 1
    # default / --dry-run
    print(
        "dry-run: would review active watches across crypto/kr/us "
        "(read-only; writes only alert_metadata.last_review on --run). "
        "Pass --run to execute."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
