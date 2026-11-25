#!/usr/bin/env python3
"""hk 6510: manual US intraday movers observation (insert-only).

    uv run python -m scripts.us_intraday_movers [--dry-run]

Fetches the Alpaca screener (gainers / losers / most actives) and the KIS
overseas up/down ranking and records them insert-only into
``invest_momentum_event_snapshots`` with ``market='us'`` and the
``us:<source>:<session>:<side>`` surfaces, one status row per surface in
``invest_momentum_event_runs`` (ok / empty / error + a closed reason). Outside
04:00-20:00 ET on an XNYS trading day it does nothing and reports
``outside_session``.

Sources: ``ALPACA_DATA_API_KEY_ID`` / ``ALPACA_DATA_API_SECRET_KEY`` (the paper-data
pair ``scripts/mock_data_refresh`` uses; missing = ``source_unavailable``) and the
KIS credentials. No key value is printed.

Writes need BOTH no ``--dry-run`` and ``INVEST_MOMENTUM_EVENTS_COMMIT_ENABLED``
(default false; this PR does not turn it on). With the gate off the run is a dry
run and says so (``commit_gate_off``). Not scheduled by this repository.

Output: one JSON object. Exit 0 = recorded / dry run / outside session,
1 = at least one surface errored or failed to write, 2 = usage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.cli import setup_logging_and_sentry
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.brokers.kis.client import KISClient
from app.services.invest_momentum_events import us_intraday
from app.services.invest_momentum_events.us_sources import (
    AlpacaScreenerClient,
    alpaca_data_credentials,
)

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    gate_on = bool(settings.invest_momentum_events_commit_enabled)
    commit = not args.dry_run and gate_on
    credentials = alpaca_data_credentials()
    if credentials is None:
        logger.warning("alpaca screener credentials absent vars=%s", "paper-data")
        alpaca: AlpacaScreenerClient | None = None
    else:
        alpaca = AlpacaScreenerClient(*credentials)
    try:
        kis: KISClient | None = KISClient()
    except Exception as exc:  # noqa: BLE001 - reported as source_unavailable
        logger.warning("kis reader unavailable error=%s", type(exc).__name__)
        kis = None
    try:
        if commit:
            async with AsyncSessionLocal() as session:
                report = await us_intraday.run_us_intraday_movers(
                    alpaca=alpaca,
                    kis=kis,
                    session=session,
                    now=datetime.now(UTC),
                    commit=True,
                )
        else:
            report = await us_intraday.run_us_intraday_movers(
                alpaca=alpaca,
                kis=kis,
                session=None,
                now=datetime.now(UTC),
                commit=False,
            )
    finally:
        if alpaca is not None:
            await alpaca.aclose()
    payload = report.as_dict()
    payload["sources"] = {
        "alpaca_screener": alpaca is not None,
        "kis_overseas_ranking": kis is not None,
    }
    payload["failed"] = report.failed
    if not args.dry_run and not gate_on:
        payload["commit_gate_off"] = True
    return payload


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging_and_sentry(service_name="us-intraday-movers")
    payload = asyncio.run(run(args))
    print(json.dumps(payload, sort_keys=True, default=str))
    return 1 if payload["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
