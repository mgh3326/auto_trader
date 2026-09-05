"""Move new fill evidence into durable operator session context.

This operational entrypoint never invokes a language model or trading surface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

import httpx

from app.core.db import AsyncSessionLocal
from app.services.fill_event_handoff import FillHandoffRunner, HandoffConfig
from app.services.lane_events import LANE_PATTERN, LaneEventConfig


def _enabled(value: str | None) -> bool:
    return value is not None and value.strip().lower() == "true"


def _targets(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _mapping(value: str | None, env_name: str) -> dict[str, str]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise ValueError(f"{env_name} must be a string map")
    return parsed


def _lanes(value: str | None) -> dict[str, str]:
    lanes = _mapping(value, "FILL_HANDOFF_LANES")
    if not set(lanes).issubset({"crypto", "kr", "us"}) or not all(
        LANE_PATTERN.fullmatch(lane) for lane in lanes.values()
    ):
        raise ValueError("FILL_HANDOFF_LANES must map crypto, kr, or us to valid lanes")
    return lanes


def _lane_event_config() -> LaneEventConfig:
    timeout_s = float(os.getenv("FILL_HANDOFF_EMIT_TIMEOUT_S", "3"))
    if timeout_s <= 1:
        raise ValueError("FILL_HANDOFF_EMIT_TIMEOUT_S must be greater than 1")
    return LaneEventConfig(
        binary=os.getenv("FILL_HANDOFF_EMIT_BIN", "panewire"),
        host=os.getenv("FILL_HANDOFF_EMIT_HOST", socket.gethostname()),
        pane=os.getenv("FILL_HANDOFF_EMIT_PANE", ""),
        inbox_root=os.getenv(
            "FILL_HANDOFF_EMIT_INBOX_ROOT",
            str(Path("~/work/herdr-inbox").expanduser()),
        ),
        timeout_s=timeout_s,
    )


async def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=body)
        response.raise_for_status()
        result = response.json()
    if not isinstance(result, dict):
        raise ValueError("Prefect response must be an object")
    return result


def _ledger_id(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("ledger id must be non-negative")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since-ledger-id",
        type=_ledger_id,
        help="on a new state directory, process only fills after this ledger id",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read candidate fills without writing context, state, or notifications",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="perform one poll (the systemd timer always uses this mode)",
    )
    return parser.parse_args(argv)


async def main_async(
    *, since_ledger_id: int | None = None, dry_run: bool = False
) -> dict[str, Any]:
    config = HandoffConfig(
        state_dir=Path(
            os.getenv("FILL_HANDOFF_STATE_DIR", "/var/lib/fill-event-handoff")
        ),
        herdr_targets=_targets(os.getenv("FILL_HANDOFF_HERDR_TARGETS")),
        kick_enabled=_enabled(os.getenv("FILL_HANDOFF_KICK_ENABLED")),
        kick_cooldown_seconds=int(os.getenv("FILL_HANDOFF_KICK_COOLDOWN_S", "3600")),
        kick_deployments=_mapping(
            os.getenv("FILL_HANDOFF_KICK_DEPLOYMENTS"),
            "FILL_HANDOFF_KICK_DEPLOYMENTS",
        ),
        prefect_api_url=os.getenv("PREFECT_API_URL"),
        discord_webhook=os.getenv("DISCORD_FILL_HANDOFF_WEBHOOK"),
        since_ledger_id=since_ledger_id,
        dry_run=dry_run,
        lane_events=_lanes(os.getenv("FILL_HANDOFF_LANES")),
        lane_event=_lane_event_config(),
    )
    async with AsyncSessionLocal() as db:
        return await FillHandoffRunner(config, http_post=_post).run(db)


def main() -> int:
    args = parse_args()
    try:
        print(
            json.dumps(
                asyncio.run(
                    main_async(
                        since_ledger_id=args.since_ledger_id,
                        dry_run=args.dry_run,
                    )
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - timer needs a nonzero failure signal
        print(f"fill-event-handoff: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
