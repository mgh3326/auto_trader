#!/usr/bin/env python3
"""Reconcile the filled D2 remediation roots in the Binance Demo ledger (#1268).

The three ``d2_remediation_single`` SELL LIMIT roots sit in ``filled``, a
blocking root state, so the H5 truth gate's ``demo_ledger_no_open_roots``
check fails on them. The D2 runbook has no step that moves a filled
remediation root on; this is that step, and it does nothing else.

A preview is the default: it reads the requested ledger rows, reads each
order back from the Binance **Spot Demo** account (``GET /api/v3/order``,
read-only), prints every verdict and writes nothing. ``--commit`` moves exactly
the given roots ``filled → closed → reconciled`` through
``BinanceDemoLedgerService`` in one transaction, with one audit record per
root, or changes nothing if any id is ineligible or any broker evidence is
missing or mismatching. A batch whose ids were all already reconciled by this
tool is a no-op. Nothing is ever deleted and no order is sent.

Eligibility and evidence rules: ``app/services/brokers/binance/spot_demo/
d2_root_reconcile.py``. Runbook: ``docs/runbooks/binance-spot-demo-d2-remediation.md``.

Ids are exact: ``--ids 442,443,444`` (repeatable, at most 3). Ranges,
patterns, signs, spaces and duplicates are refused. ``--reason`` and
``--actor`` are required.

The database is named explicitly: ``--database-url-env NAME`` reads the URL
from that one environment variable (preferred) or ``--database-url URL`` for
manual use. The URL value is never printed. The broker client is
``BinanceSpotDemoExecutionClient.from_env()`` — ``BINANCE_SPOT_DEMO_ENABLED``
must be true and the Spot Demo credentials present; it must resolve to the
Spot Demo host and the sealed D2 credential, or nothing is read.

Exit codes: 0 eligible preview / committed / no-op, 1 input, client or
database error, 2 refused batch.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.brokers.binance.spot_demo.d2_root_reconcile import (
    MAX_ACTOR_CHARS,
    MAX_REASON_CHARS,
    TOOL_NAME,
    BatchResult,
    D2RootReconcileInputError,
    commit_reconcile,
    parse_ids,
    preview_reconcile,
    validate_text,
)

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile filled d2_remediation_single SPOT roots in "
            "binance_demo_order_ledger against Spot Demo broker evidence; "
            "preview unless --commit"
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
        help="Exact binance_demo_order_ledger ids (repeatable; comma lists allowed)",
    )
    parser.add_argument(
        "--reason", required=True, help="Why these roots are being reconciled"
    )
    parser.add_argument("--actor", required=True, help="Who is reconciling them")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Apply the transitions; without it only a preview is printed",
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


def _default_client_factory() -> Any:
    from app.services.brokers.binance.spot_demo.execution_client import (
        BinanceSpotDemoExecutionClient,
    )

    return BinanceSpotDemoExecutionClient.from_env()


def _exit_code(result: BatchResult) -> int:
    return EXIT_REFUSED if result.status == "refused" else EXIT_OK


async def run(
    args: argparse.Namespace,
    *,
    session_factory: Any | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    ids = parse_ids(args.ids)
    reason = validate_text("reason", args.reason, max_chars=MAX_REASON_CHARS)
    actor = validate_text("actor", args.actor, max_chars=MAX_ACTOR_CHARS)
    database_url = None if session_factory is not None else _database_url(args)
    client = (client_factory or _default_client_factory)()
    engine = None
    try:
        if session_factory is None:
            engine = create_async_engine(database_url)
            session_factory = async_sessionmaker(
                bind=engine, class_=AsyncSession, expire_on_commit=False
            )
        async with session_factory() as session:
            if args.commit:
                result = await commit_reconcile(
                    session, client, ids, reason=reason, actor=actor
                )
            else:
                result = await preview_reconcile(session, client, ids)
    finally:
        try:
            if engine is not None:
                await engine.dispose()
        finally:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()
    payload = {
        "tool": TOOL_NAME,
        "mode": "commit" if args.commit else "preview",
        "reason": reason,
        "actor": actor,
        "broker_reads": "GET /api/v3/order only",
        "broker_mutation_count": 0,
        **result.as_dict(),
    }
    return _exit_code(result), payload


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        code, payload = asyncio.run(run(args))
    except (D2RootReconcileInputError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - report the class, never the DSN
        print(
            json.dumps({"error": f"error:{exc.__class__.__name__}"}),
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
