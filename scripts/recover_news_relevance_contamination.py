#!/usr/bin/env python3
"""Bounded recovery for the 2026-07-27 news-relevance contamination incident.

The default mode is a database-read-only dry-run. It proves the fixed
``expected_count=96`` gate and writes the complete pre-reset judgment values to
an exclusive, mode-0600 JSON audit artifact. Database mutation requires the
explicit ``--execute`` flag; the service then locks and compares the selected
rows with that exported snapshot before resetting them.

Production dry-run:

    ENV_FILE=/Users/mgh3326/services/auto_trader/shared/.env.prod.native \
      uv run python scripts/recover_news_relevance_contamination.py

Do not use ``--execute`` until the artifact and dry-run output are approved.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.db import AsyncSessionLocal
from app.services import symbol_news_store

EXPECTED_COUNT = 96
DEFAULT_AUDIT_DIR = Path(".smoke-out/news-relevance-contamination-recovery")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preview the bounded news-relevance contamination reset and export "
            "its pre-reset audit snapshot. Default: DB-read-only dry-run."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Reset the 96 selected rows. Default is read-only dry-run.",
    )
    parser.add_argument(
        "--audit-output",
        type=Path,
        help=(
            "Exclusive JSON audit path. Default: a timestamped mode-0600 file "
            "under .smoke-out/news-relevance-contamination-recovery/."
        ),
    )
    args = parser.parse_args(argv)
    args.dry_run = not args.execute
    return args


def _default_audit_path() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return DEFAULT_AUDIT_DIR / f"news-relevance-contamination-audit-{timestamp}.json"


def _serialize_snapshot(
    row: symbol_news_store.NewsRelevanceJudgmentSnapshot,
) -> dict[str, Any]:
    payload = asdict(row)
    payload["judged_at"] = row.judged_at.isoformat() if row.judged_at else None
    return payload


def _selected_id_sha256(
    rows: tuple[symbol_news_store.NewsRelevanceJudgmentSnapshot, ...],
) -> str:
    encoded = ",".join(str(row.id) for row in rows).encode()
    return hashlib.sha256(encoded).hexdigest()


def _audit_payload(
    *,
    rows: tuple[symbol_news_store.NewsRelevanceJudgmentSnapshot, ...],
    execute_requested: bool,
) -> dict[str, Any]:
    return {
        "artifact_kind": "news_relevance_contamination_pre_reset_snapshot",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "execute_requested": execute_requested,
        "expected_count": EXPECTED_COUNT,
        "selected_count": len(rows),
        "selected_id_sha256": _selected_id_sha256(rows),
        "selection_predicate": {
            "judged_at_gte": (
                symbol_news_store.NEWS_RELEVANCE_CONTAMINATION_START.isoformat()
            ),
            "judged_at_lt": (
                symbol_news_store.NEWS_RELEVANCE_CONTAMINATION_END.isoformat()
            ),
            "judged_by": symbol_news_store.NEWS_RELEVANCE_CONTAMINATION_JUDGE,
            "title_copy": ("reason contains substr(news_articles.title, 1, 20)"),
        },
        "rows": [_serialize_snapshot(row) for row in rows],
    }


def _write_audit_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as artifact:
            json.dump(payload, artifact, ensure_ascii=False, indent=2)
            artifact.write("\n")
            artifact.flush()
            os.fsync(artifact.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _summary(
    *,
    result: symbol_news_store.NewsRelevanceRecoveryResult,
    audit_path: Path,
) -> dict[str, Any]:
    buckets: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    for row in result.selected:
        if row.judged_at is not None:
            minute = row.judged_at.minute // 10 * 10
            bucket = row.judged_at.replace(
                minute=minute, second=0, microsecond=0
            ).isoformat()
            buckets[bucket] += 1
        statuses[row.status] += 1
    return {
        "mode": "dry-run" if result.dry_run else "execute",
        "transaction_read_only": result.dry_run,
        "expected_count": EXPECTED_COUNT,
        "selected_count": result.selected_count,
        "updated_count": result.updated_count,
        "selected_buckets_utc": dict(sorted(buckets.items())),
        "selected_status_counts": dict(sorted(statuses.items())),
        "selected_id_sha256": _selected_id_sha256(result.selected),
        "audit_artifact": str(audit_path.resolve()),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    audit_path = args.audit_output or _default_audit_path()
    async with AsyncSessionLocal() as db:
        try:
            if args.dry_run:
                await db.execute(text("SET TRANSACTION READ ONLY"))

            preview = await symbol_news_store.recover_contaminated_news_relevance(
                db,
                expected_count=EXPECTED_COUNT,
            )
            payload = _audit_payload(
                rows=preview.selected,
                execute_requested=args.execute,
            )
            _write_audit_artifact(audit_path, payload)

            if args.dry_run:
                await db.rollback()
                return _summary(result=preview, audit_path=audit_path)

            executed = await symbol_news_store.recover_contaminated_news_relevance(
                db,
                expected_count=EXPECTED_COUNT,
                execute=True,
                audit_snapshot=preview.selected,
            )
            await db.commit()
            return _summary(result=executed, audit_path=audit_path)
        except BaseException:
            await db.rollback()
            raise


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = asyncio.run(run(args))
    except (symbol_news_store.NewsRelevanceRecoveryError, OSError) as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("DRY-RUN ONLY: no symbol_news_relevance rows were changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
