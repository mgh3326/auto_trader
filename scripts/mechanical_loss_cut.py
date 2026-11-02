"""Run or read the mechanical loss-cut shadow evaluation (ROB-1460).

Usage:
  uv run python -m scripts.mechanical_loss_cut evaluate            # dry run
  uv run python -m scripts.mechanical_loss_cut evaluate --commit
  uv run python -m scripts.mechanical_loss_cut summary --session 2026-11-02

Needs MECHANICAL_LOSS_CUT_SHADOW_ENABLED=true. Evaluation reads only the database
unless the live switch is also on. There is no schedule; see
docs/runbooks/mechanical-loss-cut.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime

from app.core.db import AsyncSessionLocal
from app.services.mechanical_loss_cut.job import ALL_MARKETS, run_mechanical_loss_cut
from app.services.mechanical_loss_cut.store import (
    MechanicalLossCutStore,
    summarize_decisions,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    evaluate = commands.add_parser("evaluate", help="evaluate every held lot")
    evaluate.add_argument("--commit", action="store_true", help="write the rows")
    evaluate.add_argument(
        "--market", action="append", choices=ALL_MARKETS, help="repeatable; default all"
    )
    summary = commands.add_parser("summary", help="one session's recorded decisions")
    summary.add_argument("--session", type=date.fromisoformat, required=True)
    summary.add_argument("--mode", choices=("shadow", "live"), default="shadow")
    return parser


async def _summary(session_date: date, mode: str) -> dict[str, object]:
    async with AsyncSessionLocal() as session:
        rows = await MechanicalLossCutStore(session).decisions_for_session(
            session_date, mode=mode
        )
    return summarize_decisions(rows, session_date=session_date)


def main() -> int:
    args = _parser().parse_args()
    if args.command == "summary":
        result = asyncio.run(_summary(args.session, args.mode))
    else:
        result = asyncio.run(
            run_mechanical_loss_cut(
                now=datetime.now(UTC),
                markets=tuple(args.market or ALL_MARKETS),
                commit=args.commit,
            )
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
