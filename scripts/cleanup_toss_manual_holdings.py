"""ROB-1297 side task: toss AMZN/GOOGL manual leftover cleanup.

Default dry-run. Writes require ``--commit --confirm``.

    uv run python -m scripts.cleanup_toss_manual_holdings
    uv run python -m scripts.cleanup_toss_manual_holdings --commit --confirm --warn-session
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.core.db import AsyncSessionLocal
from app.services.manual_holdings_leftover import cleanup_toss_leftover_manual_rows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Delete matched manual_holdings rows (requires --confirm).",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Operator confirmation for --commit.",
    )
    parser.add_argument(
        "--warn-session",
        action="store_true",
        help="Persist session-context conflict warnings (write path).",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as session:
        result = await cleanup_toss_leftover_manual_rows(
            session,
            commit=bool(args.commit),
            confirm=bool(args.confirm),
            warn_session=bool(args.warn_session),
        )
    payload = {
        "matched": result.matched,
        "deleted": result.deleted,
        "warnings_written": result.warnings_written,
        "dry_run": result.dry_run,
        "rows": [
            {
                "holding_id": row.holding_id,
                "ticker": row.ticker,
                "quantity": row.quantity,
                "broker_account_id": row.broker_account_id,
                "is_mock": row.is_mock,
                "reasons": list(row.reasons),
            }
            for row in result.rows
        ],
        "conflicts": [
            {
                "ticker": conflict.ticker,
                "manual_holding_id": conflict.manual_holding_id,
                "reason": conflict.reason,
            }
            for conflict in result.conflicts
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
