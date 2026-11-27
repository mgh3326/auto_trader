#!/usr/bin/env python3
"""Build the KR daily-plan premarket input bundle from the database (hk 6902).

``--dry-run`` (default) reads in one READ ONLY transaction and prints the counts,
the sha256 and per-input present/unknown tallies. ``--commit`` also stores the
bundle through the daily-plan writer in a second transaction. No broker call, no
plan version, no order or proposal path, and no scheduler registration.

Exit 0 = built (or the date is not an XKRX session), 1 = failed, 2 = refused.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
from collections.abc import Callable
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.services.account_value.identity import configured_fingerprint
from app.services.daily_plan.bundle import bundle_sha256
from app.services.daily_plan.bundle_builder import AccountBinding, input_tallies
from app.services.daily_plan.bundle_reader import (
    CalendarUnavailable,
    assemble_bundle,
    session_facts,
)
from app.services.daily_plan.store import store_input_bundle

# The pure keyed digest the order path uses, so a plan row and a send agree on
# which account is meant. Nothing else of the mandate package is used.
from app.services.mandate.account_ref import derive_account_ref
from app.services.trading_policy_service import load_trading_policy

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
DEFAULT_CUTOFF = time(8, 20)
PLAN_CUTOFF = time(8, 30)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the KR daily-plan input bundle (dry-run by default)."
    )
    parser.add_argument(
        "--date", type=date.fromisoformat, required=True, help="Plan day (YYYY-MM-DD)."
    )
    parser.add_argument(
        "--cutoff",
        type=time.fromisoformat,
        default=DEFAULT_CUTOFF,
        help="Bundle close time in KST (HH:MM, at most 08:30; default 08:20).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and print without writing (default).",
    )
    mode.add_argument(
        "--commit", action="store_true", help="Store the bundle once per content."
    )
    return parser.parse_args(argv)


def resolve_account_bindings(config: Any) -> dict[str, AccountBinding]:
    """Map the configured accounts' snapshot series to their plan references.

    The series key is the one the account-value writer stores, so a snapshot of
    any other account stays unidentified. Uses configuration only.
    """
    configured: list[tuple[str, str | None, str | None]] = []
    account_no = config.kis_account_no
    app_key = config.kis_app_key
    if account_no and app_key and app_key.strip():
        digits = account_no.replace("-", "")
        if len(digits) >= 10:
            configured.append(
                (
                    "kis_live",
                    account_no,
                    configured_fingerprint(
                        "kis",
                        "kis_live",
                        digits[:10] + app_key,
                        config.kis_app_secret,
                    ),
                )
            )
    seq = config.toss_api_account_seq
    if seq is not None and config.SECRET_KEY:
        key = hmac.digest(
            config.SECRET_KEY.encode(), b"account-value-fingerprint:toss", "sha256"
        )
        configured.append(
            (
                "toss_live",
                str(seq),
                configured_fingerprint("toss", "toss_live", str(seq), key),
            )
        )
    bindings = {}
    for mode, identifier, series in configured:
        reference = derive_account_ref(mode, identifier, secret=config.SECRET_KEY)
        if series and reference:
            bindings[series] = AccountBinding(reference, mode)
    return bindings


async def run(
    args: argparse.Namespace,
    *,
    session_factory: Callable[[], Any],
    config: Any,
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    day = args.date
    report: dict[str, Any] = {
        "mode": "commit" if args.commit else "dry_run",
        "session_date": day.isoformat(),
    }

    def refused(reason: str) -> tuple[int, dict[str, Any]]:
        return 2, {**report, "status": "refused", "reason": reason}

    if args.cutoff.tzinfo is not None:
        # The cutoff is always a KST wall-clock time.
        return refused("invalid_cutoff")
    if args.cutoff > PLAN_CUTOFF:
        return refused("cutoff_after_premarket")
    try:
        session_facts(day)
    except CalendarUnavailable as exc:
        if str(exc) == "not_a_session":
            return 0, {**report, "status": "skipped", "reason": "not_a_session"}
        return 1, {**report, "status": "failed", "reason": str(exc)}
    closed_at = datetime.combine(day, args.cutoff, KST)
    if now < closed_at:
        # A bundle cannot be closed at a time that has not happened.
        return refused("cutoff_not_reached")
    if args.commit and now.astimezone(KST).date() != day:
        # No backfill: only today's bundle may be stored.
        return refused("commit_date_not_today")

    async with session_factory() as db, db.begin():
        await db.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        )
        await db.execute(text("SET LOCAL statement_timeout = '60s'"))
        body = await assemble_bundle(
            db,
            day=day,
            closed_at=closed_at,
            cutoff_at=datetime.combine(day, PLAN_CUTOFF, KST),
            bindings=resolve_account_bindings(config),
        )
    digest = bundle_sha256(body)
    report.update(
        status="built",
        bundle_closed_at=body["bundle_closed_at"],
        bundle_sha256=digest,
        candidate_set_sha256=body["candidate_set_sha256"],
        candidate_rule_sha256=body["candidate_rule_sha256"],
        candidates=len(body["rows"]),
        candidate_sources=body["candidate_sources"],
        excluded_candidates=body["excluded_candidates"],
        gap_reason=body["gap_reason"],
        accounts=len(body["accounts"]),
        accounts_unidentified=len(body["accounts_unidentified"]),
        tallies=input_tallies(body),
    )
    if args.commit:
        async with session_factory() as db, db.begin():
            bundle_id = await store_input_bundle(
                db,
                day=day,
                body=body,
                claimed_sha256=digest,
                policy=load_trading_policy(),
            )
        report.update(status="stored", bundle_id=str(bundle_id))
    return 0, report


async def _main(args: argparse.Namespace) -> int:
    try:
        from app.core.config import settings
        from app.core.db import AsyncSessionLocal

        code, report = await run(
            args,
            session_factory=AsyncSessionLocal,
            config=settings,
            now=datetime.now(KST),
        )
    except Exception as exc:
        # Class name only: a driver message can carry connection details.
        logger.error("daily plan bundle build failed: %s", type(exc).__name__)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from app.core.cli import setup_logging_and_sentry

    setup_logging_and_sentry(service_name="build_daily_plan_bundle")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
