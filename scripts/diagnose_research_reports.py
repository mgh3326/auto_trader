#!/usr/bin/env python3
"""ROB-207 diagnose CLI — print research_reports freshness, read-only."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.research_reports.freshness import (
    compute_research_reports_readiness,
)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Diagnose research_reports freshness (ROB-207).")
    p.add_argument("--source", default=None)
    p.add_argument("--max-age-hours", type=int, default=None)
    return p.parse_args(argv)


async def main_async(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    ns = parse_args(argv)
    budget = ns.max_age_hours or settings.RESEARCH_REPORTS_FRESHNESS_MAX_AGE_HOURS
    async with AsyncSessionLocal() as db:
        out = await compute_research_reports_readiness(
            db, source=ns.source, max_age_hours=budget,
        )
    print(json.dumps(out.model_dump(mode="json"), default=str))
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
