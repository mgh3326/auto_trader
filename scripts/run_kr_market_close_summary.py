"""Preview or record a KR market close summary.

Preview is the default. Pass ``--commit`` to append the session record.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date

from app.core.db import AsyncSessionLocal
from app.core.timezone import today_kst
from app.services.kr_market_close_summary.formatter import format_summary
from app.services.kr_market_close_summary.service import (
    SqlAlchemySummarySource,
    SqlAlchemySummaryStore,
    build_summary,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-date", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Append the summary if this KR session has no record yet.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    session_date = args.session_date or today_kst()
    async with AsyncSessionLocal() as session:
        summary, stored = await build_summary(
            session_date=session_date,
            source=SqlAlchemySummarySource(session),
            store=SqlAlchemySummaryStore(session),
            commit=args.commit,
        )
        if args.commit and stored:
            await session.commit()
        else:
            await session.rollback()
    print(format_summary(summary, commit=args.commit, stored=stored))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
