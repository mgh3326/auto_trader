"""Manual entry point for ROB-26 research-run refresh.

Read-only by default (dry-run). Examples:

  uv run python scripts/run_research_run_refresh.py --stage preopen
  uv run python scripts/run_research_run_refresh.py --stage nxt_aftermarket --no-dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.jobs.research_run_refresh_runner import run_research_run_refresh
from app.schemas.research_run_decision_session import ResearchRunSelector
from app.services import (
    research_run_decision_session_service,
    research_run_live_refresh_service,
)

logger = logging.getLogger(__name__)


async def _dry_run(*, stage: str, market_scope: str) -> dict:
    user_id = settings.research_run_refresh_user_id
    if user_id is None:
        return {"status": "dry_run", "reason": "no_operator_user_configured"}
    async with AsyncSessionLocal() as db:
        try:
            run = await research_run_decision_session_service.resolve_research_run(
                db,
                user_id=user_id,
                selector=ResearchRunSelector(
                    market_scope=market_scope, stage=stage, status="open"
                ),
            )
        except research_run_decision_session_service.ResearchRunNotFound:
            return {"status": "dry_run", "reason": "no_research_run", "would_create": False}
        snapshot = await research_run_live_refresh_service.build_live_refresh_snapshot(
            db, run=run
        )
        return {
            "status": "dry_run",
            "would_create": True,
            "research_run_uuid": str(run.run_uuid),
            "candidate_count": len(run.candidates),
            "snapshot_warnings": list(snapshot.warnings),
            "refreshed_at": snapshot.refreshed_at.isoformat(),
        }


def main() -> None:
    parser = argparse.ArgumentParser(prog="run_research_run_refresh")
    parser.add_argument("--stage", choices=["preopen", "nxt_aftermarket"], required=True)
    parser.add_argument("--market-scope", default="kr", choices=["kr"])
    parser.add_argument(
        "--dry-run", dest="dry_run", default=True, action=argparse.BooleanOptionalAction
    )
    args = parser.parse_args()

    if args.dry_run:
        result = asyncio.run(
            _dry_run(stage=args.stage, market_scope=args.market_scope)
        )
    else:
        result = asyncio.run(
            run_research_run_refresh(
                stage=args.stage, market_scope=args.market_scope
            )
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
