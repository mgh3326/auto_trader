"""Default-off one-shot B0X lane-event consumer."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.services.b0x_lane_consumer import consume_lane_event


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-file", required=True, type=_absolute_path)
    parser.add_argument("--state-db", required=True, type=_absolute_path)
    args = parser.parse_args(argv)
    enabled = os.getenv("B0X_LANE_EVENT_CONSUMER_ENABLED", "").strip().lower() == "true"
    if not enabled:
        print(
            json.dumps(
                {
                    "consumer_execution_evidence": None,
                    "reason": "B0X_LANE_EVENT_CONSUMER_ENABLED is false",
                    "status": "disabled",
                },
                sort_keys=True,
            )
        )
        return 0
    event = json.loads(args.event_file.read_text(encoding="utf-8"))
    receipt = consume_lane_event(event, state_db=args.state_db)
    print(json.dumps(receipt.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
