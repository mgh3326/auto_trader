#!/usr/bin/env python3
"""Collect Naver research metadata into research_reports (manual only)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from app.services.research_reports.naver_client import NaverResearchClient

logger = logging.getLogger(__name__)


class _EmptyRepository:
    async def known_source_ids(self, *, source: str, category: str) -> set[str]:
        return set()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect Naver research metadata (choose --dry-run or --commit)."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and map without opening a database session.",
    )
    mode.add_argument(
        "--commit",
        action="store_true",
        help="Write mapped metadata through research_reports ingestion service.",
    )
    return parser.parse_args(argv)


async def main_async(argv: list[str] | None = None) -> int:
    from app.services.research_reports.naver_collector import (
        collect_naver_reports,
        run_naver_research_ingestion,
    )

    args = parse_args(argv)
    client = NaverResearchClient()
    try:
        if args.dry_run:
            result = await collect_naver_reports(client, _EmptyRepository())
            output = {
                "dry_run": True,
                "report_count": len(result.reports),
                "category_counts": result.category_counts,
                "category_errors": result.category_errors,
                "category_truncated": result.category_truncated,
                "dedup_keys": [row["dedup_key"] for row in result.reports],
            }
            print(json.dumps(output, ensure_ascii=False))
            return (
                1
                if result.category_errors or any(result.category_truncated.values())
                else 0
            )

        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            try:
                result, response = await run_naver_research_ingestion(db, client)
                await db.commit()
            except Exception:
                await db.rollback()
                logger.exception("Naver research ingestion failed")
                return 3
        output = {
            "dry_run": False,
            "report_count": len(result.reports),
            "category_counts": result.category_counts,
            "category_errors": result.category_errors,
            "category_truncated": result.category_truncated,
            "ingestion": response.model_dump() if response else None,
        }
        print(json.dumps(output, ensure_ascii=False))
        return (
            1
            if result.category_errors or any(result.category_truncated.values())
            else 0
        )
    finally:
        await client.aclose()


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
