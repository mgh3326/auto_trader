#!/usr/bin/env python3
"""Correct exit_date and total_pnl_krw on lots closed by a late reconcile (hk 5357).

Preview is the default and writes nothing: for every requested journal it prints
the current values, the new values, the ledger row that closed it and whether it
is eligible. ``--commit`` applies exactly the previewed plan in one transaction
and only if every journal is eligible and the facts still equal the preview
(``--expect-digest`` takes the ``plan_digest`` the preview printed); otherwise it
changes nothing. A journal that already has the new values and its audit note is
a no-op.

Ids are exact: ``--ids 243,244,245``. Ranges, patterns, signs, spaces and repeats
are refused. ``--reason`` and ``--actor`` are required. The database is named
with ``--database-url-env NAME``; its value is never printed.

Exit codes: 0 eligible preview / committed / no-op, 1 input or database error,
2 refused batch or plan changed since the preview.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Sequence
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.journal_exit_correction import (
    MAX_ACTOR_CHARS,
    MAX_REASON_CHARS,
    CorrectionInputError,
    commit,
    parse_ids,
    preview,
    validate_text,
)

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DIGEST = re.compile(r"[0-9a-f]{64}")
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url-env", required=True, metavar="NAME")
    parser.add_argument("--ids", required=True, metavar="ID[,ID...]")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument(
        "--expect-digest",
        metavar="SHA256",
        help="plan_digest printed by the preview; required with --commit",
    )
    return parser


def _database_url(env_name: str) -> str:
    if not _ENV_NAME.fullmatch(env_name):
        raise CorrectionInputError("--database-url-env must be a variable name")
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        raise CorrectionInputError(f"environment variable {env_name} is not set")
    try:
        url = make_url(raw.strip())
        if url.get_backend_name() != "postgresql" or not url.database:
            raise ValueError
        return url.render_as_string(hide_password=False)
    except Exception:
        # Parser messages can quote the URL; never chain or repeat them.
        raise CorrectionInputError(
            f"environment variable {env_name} is not a complete PostgreSQL URL"
        ) from None


async def run(
    args: argparse.Namespace, *, session_factory: Any | None = None
) -> tuple[int, dict[str, Any]]:
    # Everything below up to the engine is validated before any connection.
    ids = parse_ids(args.ids)
    reason = validate_text("reason", args.reason, max_chars=MAX_REASON_CHARS)
    actor = validate_text("actor", args.actor, max_chars=MAX_ACTOR_CHARS)
    if args.commit and not (
        args.expect_digest and _DIGEST.fullmatch(args.expect_digest)
    ):
        raise CorrectionInputError(
            "--commit requires --expect-digest from a preview (64 hex characters)"
        )
    engine = None
    if session_factory is None:
        engine = create_async_engine(_database_url(args.database_url_env))
        session_factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
    try:
        async with session_factory() as session:
            if args.commit:
                result = await commit(
                    session,
                    ids,
                    expected_digest=args.expect_digest,
                    reason=reason,
                    actor=actor,
                )
            else:
                result = await preview(session, ids)
    finally:
        if engine is not None:
            await engine.dispose()
    payload = {
        "mode": "commit" if args.commit else "preview",
        "reason": reason,
        "actor": actor,
        **result.as_dict(),
    }
    refused = result.status in {"refused", "plan_changed"}
    return (EXIT_REFUSED if refused else EXIT_OK), payload


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        code, payload = asyncio.run(run(args))
    except CorrectionInputError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - report the class, never the DSN
        print(
            json.dumps({"error": f"database_error:{exc.__class__.__name__}"}),
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
