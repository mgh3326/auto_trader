#!/usr/bin/env python3
"""Preview or commit the hk 4178 KIS US inference; no broker requests."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.kis_us_inference_expiry import DECISION_REF
from app.services.kis_us_inference_expiry_service import (
    check_conflicts,
    commit,
    preview,
)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--database-url-env", required=True)
    p.add_argument("--ids", help="comma-separated eligible ledger IDs")
    p.add_argument("--decision-ref")
    p.add_argument("--actor")
    p.add_argument("--reason")
    p.add_argument("--commit", action="store_true")
    return p


async def run(args: argparse.Namespace) -> dict:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.database_url_env):
        raise ValueError("invalid database URL environment variable name")
    ids = []
    if args.commit:
        if (
            args.decision_ref != DECISION_REF
            or not args.actor
            or not args.actor.strip()
            or not args.reason
            or not args.reason.strip()
            or not args.ids
        ):
            raise ValueError(
                "commit requires exact decision-ref, ids, actor and reason"
            )
        parts = args.ids.split(",")
        if any(not re.fullmatch(r"[1-9][0-9]*", item) for item in parts):
            raise ValueError("invalid ids")
        ids = [int(part) for part in parts]
        if len(ids) > 45 or len(set(ids)) != len(ids):
            raise ValueError("ids must be unique and at most 45")
    raw = os.environ.get(args.database_url_env)
    if not raw:
        raise ValueError(f"missing environment variable {args.database_url_env}")
    try:
        url = make_url(raw)
        if url.get_backend_name() != "postgresql" or not url.database:
            raise ValueError
        engine = create_async_engine(url)
    except Exception:
        raise ValueError("invalid database URL") from None
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            if args.commit:
                return await commit(
                    session,
                    ids=ids,
                    decision_ref=args.decision_ref,
                    actor=args.actor,
                    reason=args.reason,
                )
            return {
                "rows": await preview(session),
                "inference_checks": await check_conflicts(session),
            }
    finally:
        await engine.dispose()


def main() -> int:
    args = parser().parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        # Database exceptions may contain connection credentials: report only the class.
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if isinstance(exc, TimeoutError) or sqlstate in {"55P03", "57014"}:
            print(json.dumps({"error": "timeout", "action": "retry_preview"}))
        else:
            print(json.dumps({"error": type(exc).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
