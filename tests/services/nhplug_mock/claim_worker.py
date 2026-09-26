"""Separate-process PostgreSQL claim and fence actor for the T3 tests."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from uuid import UUID

from sqlalchemy.ext.asyncio import create_async_engine

from app.services.nhplug_mock.ledger import (
    LeaseIdentity,
    LedgerConflict,
    NHPlugMockLedger,
)
from tests._run_owned_database import validate_run_owned_database_url


async def main() -> None:
    payload = json.loads(os.environ["NHPLUG_TEST_WORKER_PAYLOAD"])
    database_url = os.environ["NHPLUG_TEST_WORKER_DB_URL"]
    validate_run_owned_database_url(database_url)
    engine = create_async_engine(database_url)
    ledger = NHPlugMockLedger(engine)
    print("ready", flush=True)
    if sys.stdin.readline().strip() != "go":
        raise RuntimeError("worker start signal missing")
    try:
        claim = await ledger.claim(
            payload["row_id"],
            UUID(payload["request_id"]),
            payload["digest"],
            UUID(payload["account_ref"]),
            LeaseIdentity(
                payload["machine"],
                payload["boot"],
                payload["namespace"],
                os.getpid(),
                777,
            ),
            claim_window_seconds=5,
        )
    except LedgerConflict as exc:
        if exc.code != "claim_rejected":
            raise
        print("rejected", flush=True)
    else:
        if payload["mode"] in {"fence", "fence_die"}:
            if not await ledger.fence(claim, lease_seconds=1):
                raise RuntimeError("fence rejected")
            print("fenced", flush=True)
        else:
            print("claimed", flush=True)
        if payload["mode"] in {"fence_die", "claim_die"}:
            os._exit(0)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
