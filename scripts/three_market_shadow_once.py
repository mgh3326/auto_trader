"""Run one broker-neutral three-market shadow from a JSON snapshot.

No database, account, broker, scheduler, or order code is imported here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.services.three_market_shadow import shadow_decision
from research_contracts.canonical_hash import canonical_sha256


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one mutation-free market shadow")
    parser.add_argument("market", choices=("kr", "us", "crypto"))
    parser.add_argument(
        "snapshot", type=Path, help="JSON object containing the input snapshot"
    )
    args = parser.parse_args()
    snapshot: dict[str, Any] = json.loads(args.snapshot.read_text(encoding="utf-8"))
    result = shadow_decision(args.market, snapshot)
    result["input_hash"] = canonical_sha256(snapshot)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
