"""ROB-405 Slice E — operator CLI for watch follow-up linking.
``run`` forces a sync (creates follow-up report items + links events)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="watch follow-up link sync")
    p.add_argument("mode", choices=["run"])
    return p


async def _amain() -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.trade_journal.watch_follow_up_service import (
        sync_watch_follow_up_items,
    )

    async with AsyncSessionLocal() as db:
        result = await sync_watch_follow_up_items(db, force=True)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    _build_parser().parse_args()
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())