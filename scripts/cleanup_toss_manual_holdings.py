"""ROB-1297 side task: toss AMZN/GOOGL manual leftover cleanup.

Default dry-run. Writes require ``--commit --confirm``.
The commit path always prints the delete-target list and count first.

    uv run python -m scripts.cleanup_toss_manual_holdings
    uv run python -m scripts.cleanup_toss_manual_holdings --commit --confirm --warn-session
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.core.db import AsyncSessionLocal
from app.services.manual_holdings_leftover import (
    LeftoverManualRow,
    cleanup_toss_leftover_manual_rows,
)


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


def _row_payload(row: LeftoverManualRow) -> dict[str, object]:
    return {
        "holding_id": row.holding_id,
        "ticker": row.ticker,
        "quantity": row.quantity,
        "broker_account_id": row.broker_account_id,
        "is_mock": row.is_mock,
        "reasons": list(row.reasons),
    }


def print_delete_targets(rows: tuple[LeftoverManualRow, ...]) -> None:
    """Print the delete set and count before any write."""
    payload = {
        "delete_target_count": len(rows),
        "rows": [_row_payload(row) for row in rows],
    }
    sys.stdout.write("=== DELETE TARGETS ===\n")
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.stdout.write(f"\ndelete_target_count={len(rows)}\n")
    sys.stdout.flush()


def _result_payload(result: object) -> dict[str, object]:
    return {
        "matched": result.matched,
        "deleted": result.deleted,
        "skipped_without_evidence": result.skipped_without_evidence,
        "warnings_written": result.warnings_written,
        "dry_run": result.dry_run,
        "rows": [_row_payload(row) for row in result.rows],
        "skipped_rows": [_row_payload(row) for row in result.skipped_rows],
        "conflicts": [
            {
                "ticker": conflict.ticker,
                "manual_holding_id": conflict.manual_holding_id,
                "reason": conflict.reason,
            }
            for conflict in result.conflicts
        ],
    }


async def _run(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as session:
        result = await cleanup_toss_leftover_manual_rows(
            session,
            commit=bool(args.commit),
            confirm=bool(args.confirm),
            warn_session=bool(args.warn_session),
            reporter=print_delete_targets,
        )
    print(json.dumps(_result_payload(result), ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
