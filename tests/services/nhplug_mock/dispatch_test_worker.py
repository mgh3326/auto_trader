"""Subprocess dispatcher with a file-backed fake broker observer."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sys
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import create_async_engine

import app.services.brokers.nhplug.client as client_module
from app.services.brokers.nhplug.client import NHPlugMockClient
from app.services.nhplug_mock.account_identity import KeyMaterial
from app.services.nhplug_mock.ledger import LedgerConflict, NHPlugMockLedger
from app.services.nhplug_mock.readiness import Stage2Readiness
from app.services.nhplug_mock.transport import Stage2Timing
from tests._run_owned_database import validate_run_owned_database_url


async def main() -> None:
    payload = json.loads(os.environ["NHPLUG_PROBE_PAYLOAD"])
    database_url = os.environ["NHPLUG_TEST_WORKER_DB_URL"]
    validate_run_owned_database_url(database_url)
    wire_log = payload["wire_log"]

    class Wire(httpx.AsyncBaseTransport):
        def arm(self, deadline: float) -> None:
            assert deadline > 0

        async def hard_close(self, timeout: float) -> None:
            return None

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            with open(wire_log, "a", encoding="utf-8") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.write(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "path": request.url.path,
                            "body": json.loads(request.content),
                        }
                    )
                    + "\n"
                )
                handle.flush()
            return httpx.Response(
                200,
                json={"rsp_cd": "00000", "Output_0": {"mkt_orr_no": "901"}},
                request=request,
            )

    client_module.GatedTransport = Wire  # type: ignore[misc]
    engine = create_async_engine(database_url)
    ledger = NHPlugMockLedger(engine)
    key = KeyMaterial(1, payload["key_id"], payload["key"].encode("latin-1"))

    async def token() -> str:
        return "probe-token"

    read_wire = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "rsp_cd": "00000",
                "Output_0": [{"acct_no": payload["act_no"], "acct_type": "03"}],
            },
            request=request,
        )
    )
    client = NHPlugMockClient(
        app_key="probe", app_secret="probe", token_provider=token, transport=read_wire
    )
    await client.verify_and_bind_mock_account(payload["act_no"])
    print("ready " + str(os.getpid()), flush=True)
    if sys.stdin.readline().strip() != "go":
        raise RuntimeError("worker start signal missing")
    try:
        outcome = await client.dispatch_claimed_order(
            ledger,
            payload["row_id"],
            UUID(payload["request_id"]),
            payload["digest"],
            UUID(payload["account_ref"]),
            keys={1: key},
            readiness=Stage2Readiness(True, True, True, True, True),
            timing=Stage2Timing(lease_seconds=payload.get("lease_seconds", 120)),
            dry_run=False,
            confirm=True,
        )
        print("outcome:" + outcome.state, flush=True)
    except LedgerConflict as exc:
        print("conflict:" + exc.code, flush=True)
    finally:
        await engine.dispose()
    if payload.get("stay_alive"):
        # The sender stays alive (a live, possibly stopped, old dispatcher).
        line = await asyncio.get_running_loop().run_in_executor(
            None, sys.stdin.readline
        )
        if line.strip() != "exit":
            raise RuntimeError("worker exit signal missing")


if __name__ == "__main__":
    asyncio.run(main())
