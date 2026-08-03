"""Monthly, operator-run promotion of production 1m bars into research history.

Default-disabled and dry-run by default. There is no scheduler entry for this
script by design (R-2): cadence is an operator checklist item, not a cron job.

    uv run python -m scripts.promote_kr_candles_to_research --venue KRX          # dry run
    uv run python -m scripts.promote_kr_candles_to_research --venue KRX --confirm

`--source` defaults to UNKNOWN because production `public.kr_candles_1m` has no
provider column; see promotion.py. Passing a concrete provider is an explicit
operator assertion and requires --assert-source-justification.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.research_candles.promotion import (  # noqa: E402
    KST,
    PromotionBlocked,
    promote,
)

ENABLE_ENV = "RESEARCH_CANDLE_PROMOTION_ENABLED"


def dsn() -> str:
    return os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", required=True, choices=["KRX", "NTX"])
    ap.add_argument(
        "--source", default="UNKNOWN", choices=["UNKNOWN", "KIWOOM", "KIS", "TOSS"]
    )
    ap.add_argument("--assert-source-justification", default=None)
    ap.add_argument("--max-sessions", type=int, default=None)
    ap.add_argument("--confirm", action="store_true", help="without this, dry-run only")
    args = ap.parse_args()

    if os.environ.get(ENABLE_ENV, "").lower() != "true":
        print(f"{ENABLE_ENV}=true is required; refusing to run.", file=sys.stderr)
        return 2

    if args.source != "UNKNOWN" and not args.assert_source_justification:
        print(
            f"--source {args.source} asserts provenance production cannot prove. "
            f"Pass --assert-source-justification '<why>' to record the claim.",
            file=sys.stderr,
        )
        return 2

    batch_id = f"promote-{datetime.now(KST):%Y%m%dT%H%M%S}-{args.source}-{args.venue}"
    conn = await asyncpg.connect(dsn())
    try:
        if not args.confirm:
            # Belt and braces: a dry run must not be able to write even if the
            # service layer had a bug.
            await conn.execute("SET default_transaction_read_only = on")
        result = await promote(
            conn,
            source=args.source,
            venue=args.venue,
            dry_run=not args.confirm,
            batch_id=batch_id,
            max_sessions=args.max_sessions,
        )
    except PromotionBlocked as exc:
        print(
            json.dumps(
                {"status": "BLOCKED", "reason": str(exc)}, ensure_ascii=False, indent=2
            )
        )
        return 1
    finally:
        await conn.close()

    payload = result.as_dict()
    payload["status"] = "DRY_RUN" if not args.confirm else "APPLIED"
    payload["batch_id"] = batch_id
    if args.assert_source_justification:
        payload["source_assertion"] = args.assert_source_justification
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
