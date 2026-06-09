"""ROB-469 PR3: MCP server liveness heartbeat.

The MCP server writes a heartbeat file from its event loop every N seconds. If the
loop wedges (e.g. a synchronous-blocking tool that PR2's timeout cannot cancel), the
heartbeat goes stale and the external watchdog (scripts/mcp_watchdog.py) restarts the
process. Atomic write mirrors websocket_monitor._write_heartbeat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def write_heartbeat(path: str | Path, *, color: str, is_running: bool) -> None:
    """Atomically write the heartbeat file. Never raises (a heartbeat failure must
    not crash the server loop)."""
    data = {
        "updated_at_unix": time.time(),
        "service": "auto-trader-mcp",
        "color": color,
        "is_running": is_running,
    }
    hb = Path(path)
    try:
        hb.parent.mkdir(parents=True, exist_ok=True)
        tmp = hb.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f)
        tmp.replace(hb)
    except OSError as exc:
        logger.warning("mcp.heartbeat.write_failed path=%s err=%s", path, exc)


async def heartbeat_loop(path: str | Path, *, interval_s: float, color: str) -> None:
    """Write a heartbeat immediately, then every ``interval_s`` seconds. On
    cancellation (graceful shutdown) write a final ``is_running=False`` so the
    watchdog distinguishes a clean stop from a wedge."""
    try:
        while True:
            write_heartbeat(path, color=color, is_running=True)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        write_heartbeat(path, color=color, is_running=False)
        raise
