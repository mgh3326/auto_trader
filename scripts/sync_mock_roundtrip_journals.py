"""ROB-405 Slice A — operator CLI for the mock roundtrip journal bridge.
``preflight`` forces a run regardless of the env gate (read-mostly: only writes
account_type='mock' journals). ``run`` honors the gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="mock roundtrip → trade_journal bridge")
    p.add_argument("mode", choices=["preflight", "run"])
    return p


async def _amain(mode: str) -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.trade_journal.mock_roundtrip_journal_bridge import (
        sync_mock_roundtrip_journals,
    )

    async with AsyncSessionLocal() as db:
        result = await sync_mock_roundtrip_journals(db, force=(mode == "preflight"))
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    args = _build_parser().parse_args()
    return asyncio.run(_amain(args.mode))


if __name__ == "__main__":
    raise SystemExit(main())
