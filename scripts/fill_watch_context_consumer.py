"""One-shot, default-off #137 Phase 0 context artifact consumer.

This command is intentionally scheduleless. It accepts a supplied JSON artifact
once, records only the context outcome when independently armed, and exits. It
does not poll, spawn, invoke a shell, contact a broker/gateway, or create any
economic intent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact",
        type=Path,
        required=True,
        help="one supplied .json lane-event/context artifact; no polling occurs",
    )
    return parser


def _load_artifact(path: Path) -> Mapping[str, Any]:
    if path.suffix != ".json" or not path.is_file():
        raise ValueError("--artifact must name one existing .json artifact")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("artifact must be a JSON object")
    return loaded


async def _amain(artifact_path: Path) -> int:
    # Lazy imports keep --help safe and check the independent gate before the
    # artifact is read or a database session can be opened.
    from app.core.config import settings
    from app.services.fill_watch_context.consumer import (
        EVENT_LOOP_FLAG,
        EventLoopDisabled,
        consume_once_if_armed,
    )

    if not bool(getattr(settings, EVENT_LOOP_FLAG, False)):
        print(
            json.dumps(
                {
                    "status": "disabled",
                    "reason": f"{EVENT_LOOP_FLAG} is false",
                    "delivery_ack": {"accepted": False, "persisted": False},
                },
                sort_keys=True,
            )
        )
        return 0
    try:
        receipt = await consume_once_if_armed(
            _load_artifact(artifact_path),
            settings_obj=settings,
        )
    except EventLoopDisabled:  # pragma: no cover - defensive config race
        print(json.dumps({"status": "disabled"}, sort_keys=True))
        return 0
    print(json.dumps({"status": "ok", **receipt.as_dict()}, sort_keys=True))
    return 0


def main() -> int:
    arguments = build_parser().parse_args()
    return asyncio.run(_amain(arguments.artifact))


if __name__ == "__main__":
    raise SystemExit(main())
