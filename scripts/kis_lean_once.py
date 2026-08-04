"""Manual KR lean shadow runner; intentionally has no loop or scheduler mode."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.services.kis_lean_execution import result_dict, run_once


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one KR KIS shadow lifecycle")
    parser.add_argument("--symbol", default="005930")
    parser.add_argument("--events", type=Path, help="append observable JSONL events")
    args = parser.parse_args()

    correlation_id = f"kr-lean-once:{uuid4().hex}"
    events: list[dict[str, object]] = []

    def emit(event: dict[str, object]) -> None:
        events.append(event)
        print(json.dumps(event, ensure_ascii=False, sort_keys=True))

    result = run_once(
        {"symbol": args.symbol, "source": "synthetic_fixed_input"},
        correlation_id=correlation_id,
        emit=emit,
    )
    if args.events:
        args.events.parent.mkdir(parents=True, exist_ok=True)
        with args.events.open("a", encoding="utf-8") as stream:
            for event in events:
                stream.write(
                    json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
                )
            stream.write(
                json.dumps(
                    {
                        "correlation_id": correlation_id,
                        "occurred_at": datetime.now(UTC).isoformat(),
                        "result": result_dict(result),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    return 0 if result.status == "shadow_complete" else 1


if __name__ == "__main__":
    sys.exit(main())
