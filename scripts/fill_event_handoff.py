"""Move new fill evidence into durable operator session context.

This operational entrypoint never invokes a language model or trading surface.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from app.core.db import AsyncSessionLocal
from app.services.fill_event_handoff import FillHandoffRunner, HandoffConfig


def _enabled(value: str | None) -> bool:
    return value is not None and value.strip().lower() == "true"


def _targets(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _mapping(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise ValueError("FILL_HANDOFF_KICK_DEPLOYMENTS must be a string map")
    return parsed


async def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=body)
        response.raise_for_status()
        result = response.json()
    if not isinstance(result, dict):
        raise ValueError("Prefect response must be an object")
    return result


async def main_async() -> dict[str, int]:
    config = HandoffConfig(
        state_dir=Path(
            os.getenv("FILL_HANDOFF_STATE_DIR", "/var/lib/fill-event-handoff")
        ),
        herdr_targets=_targets(os.getenv("FILL_HANDOFF_HERDR_TARGETS")),
        kick_enabled=_enabled(os.getenv("FILL_HANDOFF_KICK_ENABLED")),
        kick_cooldown_seconds=int(os.getenv("FILL_HANDOFF_KICK_COOLDOWN_S", "3600")),
        kick_deployments=_mapping(os.getenv("FILL_HANDOFF_KICK_DEPLOYMENTS")),
        prefect_api_url=os.getenv("PREFECT_API_URL"),
        discord_webhook=os.getenv("DISCORD_FILL_HANDOFF_WEBHOOK"),
    )
    async with AsyncSessionLocal() as db:
        return await FillHandoffRunner(config, http_post=_post).run(db)


def main() -> int:
    try:
        print(json.dumps(asyncio.run(main_async()), ensure_ascii=False, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - timer needs a nonzero failure signal
        print(f"fill-event-handoff: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
