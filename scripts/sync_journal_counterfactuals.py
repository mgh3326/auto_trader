"""ROB-405 Slice C — operator CLI for journal counterfactual sync.
``run`` forces a sync (fetches live no-action quotes per symbol).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="trade_journal counterfactual sync")
    p.add_argument("mode", choices=["run"])
    return p


async def _amain() -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.trade_journal.journal_counterfactual_service import (
        sync_journal_counterfactuals,
    )

    async with AsyncSessionLocal() as db:
        result = await sync_journal_counterfactuals(db, force=True)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    _build_parser().parse_args()
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
