#!/usr/bin/env python3
"""Ingest a validated_run_card.v1 JSON artifact as an InvestmentSnapshot (ROB-332).

Usage:
    uv run python -m scripts.ingest_validated_run_card --file run_card.json --market crypto
    uv run python -m scripts.ingest_validated_run_card --file run_card.json --market crypto --commit --confirm

Defaults to dry-run (prints the JSON-safe citation headline, no DB write). Commit
mode also requires --confirm. The snapshot is append-only audit evidence with no
broker mutation, so --commit --confirm is the only operator gate (no env flag).

Boundary (ROB-332): reuses RunCardSnapshotIngestor from PR #979. The local
run-card file path is never recorded as a source_uri (the ingestor sets
source_kind="manual"). No broker/order/watch mutation, no scheduler.

Import boundary: only the pure ``validated_run_card`` schema helper is imported
at module top. Settings-loading modules (``app.core.cli``/``app.core.db``/
``app.monitoring.sentry``) and the DB repository/ingestor are imported lazily
inside the commit path, so ``--help``, dry-run, and file-parse work in a clean
env without KIS/Upbit/DATABASE_URL/SECRET_KEY (no Settings validation).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from app.schemas.validated_run_card import RunCardCitation, build_run_card_citation

logger = logging.getLogger(__name__)

_MARKETS = ("kr", "us", "crypto")
_ACCOUNT_SCOPES = ("kis_live", "kis_mock", "alpaca_paper", "upbit_live")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest a validated_run_card.v1 JSON artifact (ROB-332)."
    )
    parser.add_argument("--file", required=True, type=Path, help="Run-card JSON path.")
    parser.add_argument("--market", required=True, choices=_MARKETS)
    parser.add_argument("--account-scope", choices=_ACCOUNT_SCOPES, default=None)
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 as_of; defaults to run-card generated_at.",
    )
    parser.add_argument(
        "--commit", action="store_true", help="Persist; default is dry-run only."
    )
    parser.add_argument(
        "--confirm", action="store_true", help="Required with --commit."
    )
    return parser.parse_args(argv)


def _headline(citation: RunCardCitation) -> dict[str, Any]:
    return {
        "recognized": citation.recognized,
        "verdict": citation.verdict,
        "framing": citation.framing,
        "trade_count": citation.trade_count,
        "is_pass_stamp": citation.is_pass_stamp,
        "symbols": citation.symbols,
    }


def _parse_as_of(raw: str | None) -> dt.datetime | None:
    if raw is None:
        return None
    parsed = dt.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


async def run_ingest(
    *,
    db: Any,
    raw_payload: dict[str, Any],
    market: str,
    account_scope: str | None,
    as_of: dt.datetime | None,
    commit: bool,
    confirm: bool,
) -> tuple[int, dict[str, Any]]:
    """Core ingest. Returns (exit_code, summary). Does not commit the session;

    the caller (main_async) commits on success so tests can introspect + roll back.
    """
    citation = build_run_card_citation(raw_payload)

    if not commit:
        return 0, {"dry_run": True, **_headline(citation)}

    if not confirm:
        return 4, {"error": "commit mode requires --confirm"}

    # Lazy: these pull in app.core.db (Settings). Only the commit path needs them.
    from app.services.investment_snapshots.repository import (
        InvestmentSnapshotsRepository,
    )
    from app.services.investment_snapshots.run_card_ingest import (
        RunCardSnapshotIngestor,
    )

    ingestor = RunCardSnapshotIngestor(InvestmentSnapshotsRepository(db))
    snapshot, citation = await ingestor.ingest(
        run_card_payload=raw_payload,
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        as_of=as_of,
    )
    return 0, {
        "dry_run": False,
        "snapshot_uuid": str(snapshot.snapshot_uuid),
        **_headline(citation),
    }


async def main_async(argv: list[str] | None = None) -> int:
    # Parse + file read + dry-run happen BEFORE any settings-loading import so
    # --help / dry-run / file-parse work without DB/broker secrets. Logging is
    # left at the stdlib default for these paths (no Sentry init needed locally).
    ns = parse_args(argv)

    if not ns.file.is_file():
        logger.error("file not found: %s", ns.file)
        return 1

    try:
        raw_payload = json.loads(ns.file.read_text(encoding="utf-8"))
        as_of = _parse_as_of(ns.as_of)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("payload/as-of parse failed: %s", exc)
        return 2

    if not ns.commit:
        _code, summary = await run_ingest(
            db=None,
            raw_payload=raw_payload,
            market=ns.market,
            account_scope=ns.account_scope,
            as_of=as_of,
            commit=False,
            confirm=False,
        )
        print(json.dumps(summary, allow_nan=False, ensure_ascii=False))
        return 0

    # Commit path only — now it is safe to load Settings-backed modules.
    from app.core.cli import setup_logging_and_sentry
    from app.core.db import AsyncSessionLocal
    from app.monitoring.sentry import capture_exception

    setup_logging_and_sentry(service_name="ingest-validated-run-card")

    async with AsyncSessionLocal() as db:
        try:
            code, summary = await run_ingest(
                db=db,
                raw_payload=raw_payload,
                market=ns.market,
                account_scope=ns.account_scope,
                as_of=as_of,
                commit=True,
                confirm=ns.confirm,
            )
            if code == 0:
                await db.commit()
            else:
                await db.rollback()
        except Exception as exc:
            await db.rollback()
            logger.error("ingest failed: %s", exc, exc_info=True)
            capture_exception(exc, process="ingest_validated_run_card")
            return 3

    print(json.dumps(summary, allow_nan=False, ensure_ascii=False))
    return code


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
