#!/usr/bin/env python3
"""hk 6154: manual intraday KRX movers observation (insert-only).

    uv run python -m scripts.intraday_movers [--dry-run]

Fetches the KRX rising / falling / trading-value rankings from Naver and records
them insert-only into ``invest_momentum_event_snapshots`` with one status row
per surface in ``invest_momentum_event_runs`` (ok / empty / error + reason).
Outside KRX regular hours (09:00-15:30 KST) on an XKRX trading day it does
nothing and reports ``outside_session``.

Writes need BOTH no ``--dry-run`` and ``INVEST_MOMENTUM_EVENTS_COMMIT_ENABLED``
(default false; this PR does not turn it on). With the gate off the run is a
dry run and says so (``commit_gate_off``). Not scheduled by this repository.

Output: one JSON object. Exit 0 = recorded / dry run / outside session,
1 = at least one surface errored or failed to write, 2 = usage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.cli import setup_logging_and_sentry
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.invest_momentum_events import intraday
from app.services.naver_stock.client import NaverStockClient


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    gate_on = bool(settings.invest_momentum_events_commit_enabled)
    commit = not args.dry_run and gate_on
    client = NaverStockClient()
    try:
        if commit:
            async with AsyncSessionLocal() as session:
                report = await intraday.run_intraday_movers(
                    fetcher=client,
                    session=session,
                    now=datetime.now(UTC),
                    commit=True,
                )
        else:
            report = await intraday.run_intraday_movers(
                fetcher=client, session=None, now=datetime.now(UTC), commit=False
            )
    finally:
        await client.aclose()
    payload = report.as_dict()
    payload["failed"] = report.failed
    if not args.dry_run and not gate_on:
        payload["commit_gate_off"] = True
    return payload


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging_and_sentry(service_name="intraday-movers")
    payload = asyncio.run(run(args))
    print(json.dumps(payload, sort_keys=True, default=str))
    return 1 if payload["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
