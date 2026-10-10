#!/usr/bin/env python3
"""Close kis_mock ledger rows 80/66/64/63 as expired[inference] (#1250, Q-46).

Operator decision hk #706 comment 1092 (2026-10-05): apply the #1112
``expired[inference]`` rule to exactly these four stale shadow pending buys,
decision reference ``Q-46``, skipping the strategy match. This CLI is that one
lever and nothing more.

A preview is the default: it prints each row's per-condition verdict and writes
nothing. ``--commit`` locks the four rows, re-checks every condition under the
lock, and then closes all four in one transaction with one append-only audit
row each — or changes nothing if any row fails any condition. A batch that this
tool has already closed is a no-op. Nothing is ever deleted.

``--ids`` must name exactly 80, 66, 64 and 63 (any order, comma lists or
repeated flags); any other id, a subset, a duplicate, a range or a sign is
refused before the database is opened. ``--decision-ref`` must be exactly
``Q-46``. ``--reason`` and ``--actor`` are required.

The database is named explicitly, as in ``scripts/quarantine_execution_ledger_rows.py``:
``--database-url-env NAME`` reads the URL from that one environment variable
(preferred: the password never reaches the process arguments) or
``--database-url URL`` for manual use. The URL value is never printed.

Exit codes: 0 eligible preview / committed / no-op, 1 input or database error,
2 refused batch.
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

from app.services.kis_mock_inference_expiry import (
    MAX_ACTOR_CHARS,
    MAX_REASON_CHARS,
    InferenceInputError,
    parse_ids,
    validate_decision_ref,
    validate_text,
)
from app.services.kis_mock_inference_expiry_service import (
    InferenceBatchResult,
    commit_inference_expiry,
    preview_inference_expiry,
)

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Close kis_mock ledger rows 80/66/64/63 as expired[inference] under "
            "operator decision Q-46; preview unless --commit"
        )
    )
    database = parser.add_mutually_exclusive_group(required=True)
    database.add_argument(
        "--database-url",
        help=(
            "Explicit PostgreSQL database URL for manual use; it is visible in "
            "the process arguments, so prefer --database-url-env"
        ),
    )
    database.add_argument(
        "--database-url-env",
        metavar="NAME",
        help=(
            "Name of the one environment variable that holds the PostgreSQL URL; "
            "there is no other environment fallback and the value is never printed"
        ),
    )
    parser.add_argument(
        "--ids",
        action="append",
        required=True,
        metavar="ID[,ID...]",
        help="Exactly the kis_mock ledger ids 80,66,64,63",
    )
    parser.add_argument(
        "--decision-ref", required=True, help="Operator decision; must be Q-46"
    )
    parser.add_argument("--reason", required=True, help="Why the rows are closed")
    parser.add_argument("--actor", required=True, help="Who is closing them")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Apply the close; without it only a preview is printed",
    )
    return parser


def _validate_database_url(raw: str) -> str:
    url = make_url(raw)
    if url.get_backend_name() != "postgresql" or not url.database:
        raise ValueError("--database-url must be a complete PostgreSQL URL")
    return url.render_as_string(hide_password=False)


def _database_url(args: argparse.Namespace) -> str:
    """Resolve the explicitly named database without ever exposing the value."""

    env_name = getattr(args, "database_url_env", None)
    if env_name is None:
        try:
            return _validate_database_url(args.database_url)
        except Exception:
            raise ValueError(
                "--database-url must be a complete PostgreSQL URL"
            ) from None
    if not isinstance(env_name, str) or not _ENV_NAME.fullmatch(env_name):
        raise ValueError(
            "--database-url-env must be an environment variable name "
            "(letters, digits, underscore; not starting with a digit)"
        )
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        raise ValueError(f"environment variable {env_name} is not set or empty")
    try:
        return _validate_database_url(raw.strip())
    except Exception:
        # Parser messages can quote the URL; never chain or repeat them.
        raise ValueError(
            f"environment variable {env_name} does not hold a complete PostgreSQL URL"
        ) from None


def _exit_code(result: InferenceBatchResult) -> int:
    return EXIT_REFUSED if result.status == "refused" else EXIT_OK


async def run(
    args: argparse.Namespace,
    *,
    session_factory: Any | None = None,
) -> tuple[int, dict[str, Any]]:
    # All operator input is validated before any database connection exists.
    ids = parse_ids(args.ids)
    decision_ref = validate_decision_ref(args.decision_ref)
    reason = validate_text("reason", args.reason, max_chars=MAX_REASON_CHARS)
    actor = validate_text("actor", args.actor, max_chars=MAX_ACTOR_CHARS)
    engine = None
    if session_factory is None:
        engine = create_async_engine(_database_url(args))
        session_factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
    try:
        async with session_factory() as session:
            if args.commit:
                result = await commit_inference_expiry(
                    session,
                    ids,
                    decision_ref=decision_ref,
                    reason=reason,
                    actor=actor,
                )
            else:
                result = await preview_inference_expiry(
                    session, ids, decision_ref=decision_ref
                )
    finally:
        if engine is not None:
            await engine.dispose()
    payload = {
        "mode": "commit" if args.commit else "preview",
        "decision_ref": decision_ref,
        "reason": reason,
        "actor": actor,
        **result.as_dict(),
    }
    return _exit_code(result), payload


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        code, payload = asyncio.run(run(args))
    except (InferenceInputError, ValueError) as exc:
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
