#!/usr/bin/env python3
"""Read Naver KRX/NXT sessions and compare them with local KR session labels.

    uv run python -m scripts.diagnose_kr_sessions --date YYYY-MM-DD

Read-only: one unauthenticated Naver request, no database or session mutation.
Exit 1 when any compared window mismatches; otherwise exit 0.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date

from app.services.market_events import naver_disclosures as naver


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    return parser.parse_args(argv)


async def run(
    day: date, *, client: naver.NaverPublicClient | None = None
) -> tuple[dict[str, object], int]:
    own_client = client is None
    client = client or naver.NaverPublicClient()
    try:
        server_time, sessions = await client.sessions()
        comparisons = naver.compare_session_windows(day, sessions)
        report = {
            "date": day.isoformat(),
            "server_time": server_time.isoformat(),
            "comparisons": comparisons,
        }
        return report, int(any(row["verdict"] == "mismatch" for row in comparisons))
    except naver.NaverDisclosureError as exc:
        report = {
            "date": day.isoformat(),
            "error_class": type(exc).__name__,
            "comparisons": [
                {
                    "exchange": exchange,
                    "market": market,
                    "window": "unavailable",
                    "naver": "unavailable",
                    "ours": "unavailable",
                    "verdict": "unavailable",
                }
                for exchange in ("krx", "nxt")
                for market in ("KOSPI", "KOSDAQ")
            ],
        }
        return report, 0
    finally:
        if own_client:
            await client.aclose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report, code = asyncio.run(run(args.date))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
