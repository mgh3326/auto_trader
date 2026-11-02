#!/usr/bin/env python3
"""Operator CLI for ROB-1470 strategy sleeves.

Subcommands:

``create``   create a sleeve and its version 1. Rule, account mode, market,
             state, budget cap, loss limit, reason and actor are all required:
             nothing in code chooses an amount.
``set``      append the next version: any of ``--state``, ``--budget-cap``,
             ``--loss-limit`` and the pre-registration flags (``--metric``,
             ``--min-effect-pct``, ``--horizon-sessions``,
             ``--max-duration-days``); the rest are copied. ``live`` needs a
             complete pre-registration, and it is frozen once the sleeve has
             been live. ``--reason`` and
             ``--actor`` are required. History is never rewritten.
``history``  print every version of one sleeve.
``report``   read-only: budget, used, P&L, trades, canary metric and verdict
             state per sleeve. ``--json`` for machine output.

``create`` and ``set`` write only with ``--commit``; without it they print what
they would write and roll back.

The database is named explicitly: ``--database-url-env NAME`` reads the URL from
that one environment variable (preferred) or ``--database-url URL``. The URL
value is never printed.

Runbook: ``docs/runbooks/strategy-sleeves.md``.

Exit codes: 0 ok, 1 input or database error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.strategy_sleeves.report import load_report, render_text
from app.services.strategy_sleeves.store import (
    SleeveError,
    create_sleeve,
    get_sleeve_by_key,
    history,
    set_sleeve_version,
)
from app.services.strategy_sleeves.vocabulary import CANARY_RULES, SleeveState

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
EXIT_OK = 0
EXIT_ERROR = 1


def _prereg_arguments(parser: argparse.ArgumentParser) -> None:
    """ROB-1468 pre-registration; required before a sleeve can be live."""
    parser.add_argument("--metric", help="Pre-registered primary metric")
    parser.add_argument(
        "--min-effect-pct",
        help="Minimum effect worth acting on, in percent of the sleeve budget",
    )
    parser.add_argument("--horizon-sessions", help="Fixed horizon H in sessions")
    parser.add_argument("--max-duration-days", help="Maximum duration in days")


def _prereg_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "metric": args.metric,
        "min_effect_pct": args.min_effect_pct,
        "horizon_sessions": args.horizon_sessions,
        "max_duration_days": args.max_duration_days,
    }


def _prereg_dict(row: Any) -> dict[str, Any]:
    return {
        "metric": row.prereg_metric,
        "min_effect_pct": (
            None
            if row.prereg_min_effect_pct is None
            else str(row.prereg_min_effect_pct)
        ),
        "horizon_sessions": row.prereg_horizon_sessions,
        "max_duration_days": row.prereg_max_duration_days,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ROB-1470 strategy sleeves")
    database = parser.add_mutually_exclusive_group(required=True)
    database.add_argument("--database-url", help="Explicit PostgreSQL URL")
    database.add_argument(
        "--database-url-env",
        metavar="NAME",
        help="Environment variable holding the PostgreSQL URL (never printed)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Create a sleeve and its version 1")
    create.add_argument("--sleeve-key", required=True)
    create.add_argument("--rule-id", required=True, choices=sorted(CANARY_RULES))
    create.add_argument(
        "--account-mode", required=True, choices=["kis_live", "toss_live", "upbit"]
    )
    create.add_argument(
        "--market", required=True, choices=["equity_kr", "equity_us", "crypto"]
    )
    create.add_argument(
        "--state",
        required=True,
        choices=[s.value for s in SleeveState if s is not SleeveState.STOPPED],
    )
    create.add_argument("--budget-cap", required=True)
    create.add_argument("--loss-limit", required=True)
    create.add_argument("--reason", required=True)
    create.add_argument("--actor", required=True)
    _prereg_arguments(create)
    create.add_argument("--commit", action="store_true")

    set_cmd = sub.add_parser("set", help="Append the next sleeve version")
    set_cmd.add_argument("--sleeve-key", required=True)
    set_cmd.add_argument("--state", choices=[s.value for s in SleeveState])
    set_cmd.add_argument("--budget-cap")
    set_cmd.add_argument("--loss-limit")
    set_cmd.add_argument("--reason", required=True)
    set_cmd.add_argument("--actor", required=True)
    _prereg_arguments(set_cmd)
    set_cmd.add_argument("--commit", action="store_true")

    hist = sub.add_parser("history", help="Print every version of one sleeve")
    hist.add_argument("--sleeve-key", required=True)

    report = sub.add_parser("report", help="Read-only per-sleeve report")
    report.add_argument("--sleeve-key")
    report.add_argument("--json", action="store_true")
    return parser


def _validate_database_url(raw: str) -> str:
    url = make_url(raw)
    if url.get_backend_name() != "postgresql" or not url.database:
        raise ValueError("--database-url must be a complete PostgreSQL URL")
    return url.render_as_string(hide_password=False)


def _database_url(args: argparse.Namespace) -> str:
    env_name = getattr(args, "database_url_env", None)
    if env_name is None:
        try:
            return _validate_database_url(args.database_url)
        except Exception:
            raise ValueError(
                "--database-url must be a complete PostgreSQL URL"
            ) from None
    if not isinstance(env_name, str) or not _ENV_NAME.fullmatch(env_name):
        raise ValueError("--database-url-env must be an environment variable name")
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        raise ValueError(f"environment variable {env_name} is not set or empty")
    try:
        return _validate_database_url(raw.strip())
    except Exception:
        raise ValueError(
            f"environment variable {env_name} does not hold a complete PostgreSQL URL"
        ) from None


def _version_dict(row: Any) -> dict[str, Any]:
    return {
        "version": row.version,
        "state": row.state,
        "budget_cap": str(row.budget_cap),
        "loss_limit": str(row.loss_limit),
        "preregistration": _prereg_dict(row),
        "reason": row.reason,
        "set_by": row.set_by,
        "set_at": row.set_at.isoformat() if row.set_at else None,
    }


async def _dispatch(session: AsyncSession, args: argparse.Namespace) -> Any:
    if args.command == "create":
        sleeve, version = await create_sleeve(
            session,
            sleeve_key=args.sleeve_key,
            rule_id=args.rule_id,
            account_mode=args.account_mode,
            market=args.market,
            state=args.state,
            budget_cap=args.budget_cap,
            loss_limit=args.loss_limit,
            reason=args.reason,
            actor=args.actor,
            **_prereg_kwargs(args),
        )
        payload = {
            "mode": "commit" if args.commit else "preview",
            "sleeve_id": sleeve.id,
            "sleeve_key": sleeve.sleeve_key,
            "rule_id": sleeve.rule_id,
            "account_mode": sleeve.account_mode,
            "market": sleeve.market,
            "currency": sleeve.currency,
            "version": {
                "version": version.version,
                "state": version.state,
                "budget_cap": str(version.budget_cap),
                "loss_limit": str(version.loss_limit),
                "preregistration": _prereg_dict(version),
            },
        }
        await (session.commit() if args.commit else session.rollback())
        return payload
    sleeve = await get_sleeve_by_key(session, args.sleeve_key)
    if sleeve is None:
        raise SleeveError(f"sleeve {args.sleeve_key!r} does not exist")
    if args.command == "set":
        row = await set_sleeve_version(
            session,
            sleeve_id=sleeve.id,
            state=args.state,
            budget_cap=args.budget_cap,
            loss_limit=args.loss_limit,
            reason=args.reason,
            actor=args.actor,
            **_prereg_kwargs(args),
        )
        payload = {
            "mode": "commit" if args.commit else "preview",
            "sleeve_key": sleeve.sleeve_key,
            "version": {
                "version": row.version,
                "state": row.state,
                "budget_cap": str(row.budget_cap),
                "loss_limit": str(row.loss_limit),
                "preregistration": _prereg_dict(row),
            },
        }
        await (session.commit() if args.commit else session.rollback())
        return payload
    if args.command == "history":
        return {
            "sleeve_key": sleeve.sleeve_key,
            "versions": [
                _version_dict(row) for row in await history(session, sleeve.id)
            ],
        }
    raise SleeveError(f"unknown command {args.command}")


async def run(
    args: argparse.Namespace, *, session_factory: Any | None = None
) -> tuple[int, Any]:
    engine = None
    if session_factory is None:
        engine = create_async_engine(_database_url(args))
        session_factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
    try:
        async with session_factory() as session:
            if args.command == "report":
                rows = await load_report(
                    session, now=datetime.now(UTC), sleeve_key=args.sleeve_key
                )
                await session.rollback()
                return EXIT_OK, rows
            return EXIT_OK, await _dispatch(session, args)
    finally:
        if engine is not None:
            await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        code, payload = asyncio.run(run(args))
    except (SleeveError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - report the class, never the DSN
        print(
            json.dumps({"error": f"database_error:{exc.__class__.__name__}"}),
            file=sys.stderr,
        )
        return EXIT_ERROR
    if args.command == "report" and not args.json:
        print(render_text(payload), end="")
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
