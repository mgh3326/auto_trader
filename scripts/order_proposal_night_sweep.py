"""Run the existing gated order-proposal night sweep."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from app.core.config import settings
from app.core.timezone import now_kst
from app.mcp_server.tooling.order_proposal_tools import run_order_proposal_night_sweep


async def _run() -> tuple[dict[str, Any], int]:
    if not settings.order_proposal_night_sweep_enabled:
        return {"status": "skipped"}, 0

    try:
        result = await run_order_proposal_night_sweep(now=now_kst())
    except Exception:
        print("order proposal night sweep raised an exception", file=sys.stderr)
        return {}, 1

    output = {
        "status": "ok",
        "swept": result["expiry"].get("swept_count", 0),
        "inferred": result["inference"].get("applied", 0),
        "inference_blocked": len(result["inference"].get("blocked_rows", [])),
    }
    return output, 0


def main() -> int:
    result, exit_code = asyncio.run(_run())
    if result:
        print(json.dumps(result, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
