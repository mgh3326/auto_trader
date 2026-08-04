#!/usr/bin/env python3
"""Manual, GET-only raw evidence capture for ROB-1161."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

from app.services.us_regular_session_raw_capture import (
    DEFAULT_SYMBOLS,
    capture_run,
    missing_credentials,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append-only US market-data raw capture (GET-only; no database)."
    )
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("artifacts/us-regular-session-raw-capture"),
    )
    parser.add_argument(
        "--u06-shadow",
        action="store_true",
        help="Record SIP NBB and hypothetical 0.998 × NBB only.",
    )
    parser.add_argument(
        "--historical-sip-date",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "ET trading date for the 09:30–09:36 historical SIP 1Min reread; "
            "defaults to the current America/New_York date."
        ),
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    for provider, names in missing_credentials().items():
        print(
            f"{provider}: missing required env keys: {', '.join(names)}",
            file=sys.stderr,
        )
    run = await capture_run(
        symbols=tuple(args.symbols),
        artifact_root=args.artifact_root,
        u06_shadow=args.u06_shadow,
        historical_sip_date=args.historical_sip_date,
    )
    print("\n".join(str(path) for path in run.artifact_paths))
    print(str(run.manifest_path))
    return run.exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
