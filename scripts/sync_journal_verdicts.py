"""ROB-405 Slice B — operator CLI for journal verdicts.
``sync`` runs auto verdicts (force). ``manual`` records an override.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="trade_journal verdict bridge")
    sub = p.add_subparsers(dest="mode", required=True)
    sub.add_parser("sync")
    m = sub.add_parser("manual")
    m.add_argument("--journal-id", type=int, required=True)
    m.add_argument("--verdict", choices=["good", "neutral", "bad"], required=True)
    m.add_argument("--comment", default=None)
    return p


async def _amain(args) -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.trade_journal.journal_verdict_service import (
        record_manual_verdict,
        sync_journal_verdicts,
    )

    async with AsyncSessionLocal() as db:
        if args.mode == "sync":
            result = await sync_journal_verdicts(db, force=True)
        else:
            result = await record_manual_verdict(
                db,
                journal_id=args.journal_id,
                verdict=args.verdict,
                comment=args.comment,
            )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(_amain(_build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
