"""Deterministic, one-shot local replay for #137 context artifacts.

This command is a local harness, not a scheduler or deployment entrypoint. It
consumes a supplied JSON array only when the independent context event-loop
gate is true, records no economic intent, and reports counters with real shadow
state explicitly left as ``NOT_STARTED``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def _aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must be timezone-aware")
    return parsed


def _nonnegative_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected non-negative seconds") from exc
    if seconds < 0:
        raise argparse.ArgumentTypeError("expected non-negative seconds")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts",
        type=Path,
        required=True,
        help="one supplied .json array of lane-event/context artifacts",
    )
    parser.add_argument(
        "--finished-at",
        type=_aware_datetime,
        required=True,
        help="fixed, timezone-aware replay completion timestamp",
    )
    parser.add_argument(
        "--elapsed-seconds",
        type=_nonnegative_seconds,
        required=True,
        help="fixed elapsed duration used only for the readiness predicate",
    )
    return parser


def _load_artifacts(path: Path) -> list[dict[str, Any]]:
    if path.suffix != ".json" or not path.is_file():
        raise ValueError("--artifacts must name one existing .json artifact array")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list) or not all(
        isinstance(item, Mapping) for item in loaded
    ):
        raise ValueError("artifacts must be a JSON array of objects")
    return [dict(item) for item in loaded]


async def _amain(
    artifacts_path: Path,
    *,
    finished_at: datetime,
    elapsed_seconds: float,
) -> int:
    # The gate is intentionally checked before artifact read or DB resolution.
    from app.core.config import settings
    from app.services.fill_watch_context.consumer import (
        EVENT_LOOP_FLAG,
        build_default_consumer,
    )
    from app.services.fill_watch_context.shadow import LocalReplayHarness

    if not bool(getattr(settings, EVENT_LOOP_FLAG, False)):
        print(
            json.dumps(
                {
                    "status": "disabled",
                    "reason": f"{EVENT_LOOP_FLAG} is false",
                    "real_shadow": "NOT_STARTED",
                },
                sort_keys=True,
            )
        )
        return 0
    _, counters = await LocalReplayHarness(build_default_consumer()).replay(
        _load_artifacts(artifacts_path),
        replay_finished_at=finished_at,
        elapsed=timedelta(seconds=elapsed_seconds),
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "counters": counters.as_dict(),
                "real_shadow": "NOT_STARTED",
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    arguments = build_parser().parse_args()
    return asyncio.run(
        _amain(
            arguments.artifacts,
            finished_at=arguments.finished_at,
            elapsed_seconds=arguments.elapsed_seconds,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
