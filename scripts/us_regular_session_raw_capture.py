#!/usr/bin/env python3
"""Manual, GET-only raw evidence capture for ROB-1161."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.services.us_regular_session_raw_capture import DEFAULT_SYMBOLS, capture


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
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    paths = await capture(
        symbols=tuple(args.symbols),
        artifact_root=args.artifact_root,
        u06_shadow=args.u06_shadow,
    )
    print("\n".join(str(path) for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
