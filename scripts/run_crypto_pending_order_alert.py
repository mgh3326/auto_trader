#!/usr/bin/env python3
"""Run the ROB-99 crypto pending-order alert manually.

Default mode is dry-run and never posts to Discord:
    uv run python scripts/run_crypto_pending_order_alert.py --dry-run

Execute mode applies the notification policy:
    uv run python scripts/run_crypto_pending_order_alert.py --execute
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.services.crypto_pending_order_alert_service import (
    run_crypto_pending_order_alert,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Print summary only; no Discord send (default).",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Send Discord alerts according to policy.",
    )
    return parser.parse_args()


async def _main() -> int:
    args = parse_args()
    result = await run_crypto_pending_order_alert(execute=bool(args.execute))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") in {"success", "skipped"} else 1


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
