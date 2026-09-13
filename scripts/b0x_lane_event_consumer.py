"""Blocked legacy direct-artifact entry; use the locked HTTP poller CLI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-file", required=True, type=_absolute_path)
    parser.add_argument("--state-db", required=True, type=_absolute_path)
    parser.parse_args(argv)
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
    print(
        json.dumps(
            {
                "consumer_execution_evidence": None,
                "reason": (
                    "direct event-file ingress is prohibited; use the binding-"
                    "validated stable-lock HTTP poller"
                ),
                "status": "blocked",
            },
            sort_keys=True,
        )
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
