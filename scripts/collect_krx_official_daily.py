#!/usr/bin/env python3
"""Collect KRX Open API official daily trading for one session (manual lever).

Dry-run is the default and never opens a database session. ``--commit`` inserts
new rows only; existing (symbol, session_date) rows are never overwritten.
Exit codes: 0 ok (an empty market is a no-data day), 1 fetch/parse failure,
2 missing or rejected auth key, 3 service not approved.
The key is read from ``KRX_OPENAPI_AUTH_KEY`` and is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import date
from typing import Any

from app.services.krx_official_daily.client import KrxOpenApiClient
from app.services.krx_official_daily.collector import MARKETS, collect_official_daily

logger = logging.getLogger(__name__)
KEY_ENV = "KRX_OPENAPI_AUTH_KEY"
MISSING_KEY_EXIT = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect KRX Open API official daily trading (dry-run default)."
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        required=True,
        help="Session date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--market",
        choices=MARKETS,
        default=None,
        help="Limit to one market (default: both).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print without database access (default).",
    )
    mode.add_argument("--commit", action="store_true", help="Insert new rows.")
    args = parser.parse_args(argv)
    args.dry_run = not args.commit
    args.markets = (args.market,) if args.market else MARKETS
    return args


def _json_default(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


async def run(args: argparse.Namespace) -> int:
    key = os.environ.get(KEY_ENV, "")
    if not key.strip():
        logger.error("missing environment variable: %s", KEY_ENV)
        return MISSING_KEY_EXIT
    try:
        client = KrxOpenApiClient(auth_key=key)
    except ValueError:
        logger.error("missing environment variable: %s", KEY_ENV)
        return MISSING_KEY_EXIT
    try:
        if args.commit:
            from app.core.db import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                result = await collect_official_daily(
                    session_date=args.date,
                    markets=args.markets,
                    client=client,
                    session=session,
                )
        else:
            result = await collect_official_daily(
                session_date=args.date, markets=args.markets, client=client
            )
    except Exception as exc:  # noqa: BLE001 - report the class only
        logger.error("KRX official daily collection failed: %s", type(exc).__name__)
        return 1
    print(json.dumps(result, ensure_ascii=False, default=_json_default))
    if result["status"] != "ok":
        logger.error("KRX official daily collection failed: %s", result["error_code"])
        return int(result["exit_code"])
    return 0


def main() -> int:
    args = parse_args()
    from app.core.cli import setup_logging_and_sentry

    setup_logging_and_sentry(service_name="collect_krx_official_daily")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
