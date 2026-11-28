#!/usr/bin/env python3
"""Bounded official-event backfill requests (hk 6409, manual, no schedule).

Dry-run is the default: it validates the request and prints the plan without
opening a database. ``--commit`` records the request once. ``--commit --run``
also runs the existing collector CLI for it, once, and appends the outcome.
Nothing here is scheduled and nothing else calls this module.

    uv run python -m scripts.request_market_event_backfill \\
        --source dart --market kr \\
        --from-date 2026-11-01 --to-date 2026-11-20 \\
        --reason-code gap_fill --reason "DART gap after outage" \\
        --requested-by operator [--commit [--run]]

    # read-only: what is collected and what is missing
    uv run python -m scripts.request_market_event_backfill \\
        --show-collection --market kr --from-date 2026-11-01 --to-date 2026-11-20

Exit codes: 0 = done (or a duplicate request, which writes nothing), 2 =
refused with zero writes, 1 = the collector run failed or the CLI errored.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.services.benzinga_http import calendars_enabled
from app.services.market_events.backfill.policy import (
    BackfillRefused,
    BackfillRequest,
    build_request,
    market_today,
    producer_argv,
)
from app.services.market_events.backfill.service import (
    Runner,
    SubmitResult,
    run_request,
    submit_request,
)
from app.services.market_events.collection import (
    COLLECTOR_SOURCES_BY_MARKET,
    read_collection,
)

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
RUN_TIMEOUT_SECONDS = 3600
_REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=None)
    parser.add_argument("--market", required=True)
    parser.add_argument("--category", default=None, help="Benzinga calendar category")
    parser.add_argument(
        "--symbols",
        "--symbol",
        nargs="+",
        action="extend",
        default=None,
        help="sec (required) or benzinga symbols",
    )
    parser.add_argument(
        "--from-date", type=date.fromisoformat, required=True, dest="from_date"
    )
    parser.add_argument(
        "--to-date", type=date.fromisoformat, required=True, dest="to_date"
    )
    parser.add_argument("--reason-code", default=None, dest="reason_code")
    parser.add_argument("--reason", default=None)
    parser.add_argument("--requested-by", default=None, dest="requested_by")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="validate only, no database (default)"
    )
    mode.add_argument("--commit", action="store_true", help="record the request")
    parser.add_argument(
        "--run",
        action="store_true",
        help="with --commit: run the collector CLI once and record the outcome",
    )
    parser.add_argument(
        "--show-collection",
        action="store_true",
        dest="show_collection",
        help="read-only collection coverage and gap reasons",
    )
    ns = parser.parse_args(argv)
    if ns.show_collection:
        if ns.commit or ns.run:
            parser.error("--show-collection is read-only")
        return ns
    missing = [
        flag
        for flag, value in (
            ("--source", ns.source),
            ("--reason-code", ns.reason_code),
            ("--reason", ns.reason),
            ("--requested-by", ns.requested_by),
        )
        if value is None
    ]
    if missing:
        parser.error(f"required: {', '.join(missing)}")
    if ns.run and not ns.commit:
        parser.error("--run requires --commit")
    return ns


def producer_gate(source: str) -> str | None:
    """Name the closed collector gate, so a refused run costs no quota."""
    if source == "sec" and not (settings.sec_edgar_user_agent or "").strip():
        return "SEC_EDGAR_USER_AGENT is empty"
    if source == "benzinga" and not calendars_enabled():
        return "BENZINGA_CALENDARS_ENABLED is not exactly 'true'"
    return None


async def run_collector(argv: list[str]) -> int:
    """Run ``python -m <argv>``; output goes to this terminal."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", *argv, cwd=str(_REPO_ROOT)
    )
    try:
        return await asyncio.wait_for(process.wait(), timeout=RUN_TIMEOUT_SECONDS)
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _emit_submitted(submitted: SubmitResult) -> None:
    _emit(
        {
            "status": submitted.status,
            "request_ref": submitted.request_ref,
            "states": list(submitted.states),
        }
    )


def _plan(request: BackfillRequest) -> dict[str, Any]:
    return {
        "request_ref": request.request_ref,
        "request_hash": request.request_hash,
        "source": request.source,
        "market": request.market,
        "category": request.category,
        "symbols": list(request.symbols),
        "from_date": request.from_date.isoformat(),
        "to_date": request.to_date.isoformat(),
        "reason_code": request.reason_code,
        "collector": producer_argv(request),
    }


async def _show_collection(ns: argparse.Namespace, session_factory: Any) -> int:
    from sqlalchemy import text

    if ns.market not in COLLECTOR_SOURCES_BY_MARKET:
        _emit({"status": "refused", "code": "unsupported_market"})
        return EXIT_REFUSED
    if ns.from_date > ns.to_date:
        _emit({"status": "refused", "code": "window_reversed"})
        return EXIT_REFUSED
    calendar_market = "kr" if ns.market == "crypto" else ns.market
    today = market_today(calendar_market, datetime.now(UTC))
    async with session_factory() as session:
        await session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        )
        await session.execute(text("SET LOCAL statement_timeout = 8000"))
        block = await read_collection(
            session,
            market=ns.market,
            start=ns.from_date,
            end=ns.to_date,
            today=today,
        )
        await session.rollback()
    _emit({"status": "ok", "collection": block})
    return EXIT_OK


async def run(
    ns: argparse.Namespace,
    *,
    session_factory: Any = None,
    store_factory: Callable[[Any], Any] | None = None,
    runner: Runner = run_collector,
    now: datetime | None = None,
) -> int:
    if session_factory is None:
        from app.core.db import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    if ns.show_collection:
        return await _show_collection(ns, session_factory)

    try:
        request = build_request(
            source=ns.source,
            market=ns.market,
            category=ns.category,
            symbols=ns.symbols or [],
            from_date=ns.from_date,
            to_date=ns.to_date,
            reason_code=ns.reason_code,
            reason=ns.reason,
            requested_by=ns.requested_by,
            now=now or datetime.now(UTC),
        )
    except BackfillRefused as refused:
        _emit({"status": "refused", "code": refused.code, "detail": refused.detail})
        return EXIT_REFUSED

    if not ns.commit:
        _emit({"status": "dry_run", "plan": _plan(request)})
        return EXIT_OK

    if ns.run:
        closed = producer_gate(request.source)
        if closed is not None:
            _emit(
                {"status": "refused", "code": "producer_gate_closed", "detail": closed}
            )
            return EXIT_REFUSED

    if store_factory is None:
        from app.services.market_events.backfill.store import BackfillStore

        store_factory = BackfillStore
    async with session_factory() as session:
        store = store_factory(session)
        try:
            if not ns.run:
                _emit_submitted(await submit_request(store, request))
                return EXIT_OK
            outcome = await run_request(
                store, request, runner, on_submitted=_emit_submitted
            )
        except BackfillRefused as refused:
            _emit({"status": "refused", "code": refused.code, "detail": refused.detail})
            return EXIT_REFUSED
    _emit(
        {
            "status": outcome.state,
            "request_ref": outcome.request_ref,
            "exit_code": outcome.exit_code,
            "failure_code": outcome.failure_code,
        }
    )
    return EXIT_OK if outcome.state == "done" else EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ns = parse_args(argv)
    try:
        return asyncio.run(run(ns))
    except Exception as exc:  # noqa: BLE001 - report the class, never a secret
        logger.error("backfill request failed: %s", type(exc).__name__)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
