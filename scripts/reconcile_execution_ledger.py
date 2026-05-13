"""Dry-run-first CLI for ROB-211 execution ledger reconciliation."""

from __future__ import annotations

import argparse
import asyncio
import json

from app.core.db import AsyncSessionLocal
from app.services.execution_ledger.reconciler import ExecutionLedgerReconciler
from app.services.execution_ledger.repository import ExecutionLedgerRepository


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconcile broker filled orders into the execution ledger."
    )
    parser.add_argument("--broker", choices=["kis", "upbit"], required=True)
    parser.add_argument("--window-hours", type=int, default=24)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--commit", action="store_true")
    return parser.parse_args()


async def _main() -> int:
    args = parse_args()
    dry_run = not bool(args.commit)
    async with AsyncSessionLocal() as db:
        reconciler = ExecutionLedgerReconciler(ExecutionLedgerRepository(db))
        diff = await reconciler.run(
            args.broker, window_hours=args.window_hours, dry_run=dry_run
        )
        if not dry_run:
            await db.commit()
        else:
            await db.rollback()
    print(json.dumps(diff.model_dump(mode="json"), ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
