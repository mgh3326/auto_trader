"""ROB-469: MCP server lifecycle observability.

Unauthenticated, dependency-free /health route and startup/shutdown logging,
factored out of app/mcp_server/main.py so they can be unit-tested against a
minimal FastMCP instance without importing the full 128-tool production server.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from fastmcp.server.lifespan import lifespan as fastmcp_lifespan
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings
from app.mcp_server.env_utils import (
    get_mcp_color,
    get_mcp_heartbeat_interval_s,
    get_mcp_heartbeat_path,
)
from app.mcp_server.heartbeat import heartbeat_loop
from app.monitoring.trade_notifier.runtime import (
    configure_trade_notifier_from_settings,
    shutdown_trade_notifier,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module import is the closest in-process proxy to process start time.
STARTED_MONOTONIC = time.monotonic()


def register_health_route(
    mcp: FastMCP,
    *,
    service: str = "auto-trader-mcp",
    version: str = "0.1.0",
) -> None:
    """Register an UNAUTHENTICATED, dependency-free GET /health route.

    fastmcp 3.2.0 mounts custom routes outside RequireAuthMiddleware (which wraps
    only the /mcp route), so /health returns 200 even when MCP_AUTH_TOKEN gates
    /mcp. The handler touches NO DB/Redis/broker, so it is a true event-loop
    liveness probe: a wedged loop stops answering it and supervision detects that.
    """

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(
            {
                "status": "ok",
                "service": service,
                "version": version,
                "uptime_s": round(time.monotonic() - STARTED_MONOTONIC, 1),
            }
        )


def build_server_lifespan(*, service: str = "auto-trader-mcp"):
    """Build a FastMCP lifespan that logs startup-complete and shutdown.

    Diagnosis property (ROB-469): a startup log with NO matching shutdown log
    before the next startup ⇒ hard-kill/OOM/SIGKILL (teardown never ran); a
    shutdown log ⇒ graceful stop. Signal type cannot be distinguished — uvicorn
    owns signal handling, so we do NOT install signal handlers here.
    """

    @fastmcp_lifespan
    async def _server_lifespan(server: FastMCP) -> AsyncIterator[dict]:
        try:
            tool_count = len(await server.list_tools())
        except Exception:  # never block startup on a best-effort count
            tool_count = -1
        logger.info(
            "mcp.lifecycle.startup_complete service=%s tools=%d uptime_s=%.1f",
            service,
            tool_count,
            time.monotonic() - STARTED_MONOTONIC,
        )
        notifier_configured = False
        if settings.toss_fill_notify_enabled:
            notifier_configured = configure_trade_notifier_from_settings(
                log_context="MCP trade notifier"
            )
        # ROB-469 PR3: liveness heartbeat task (no-op when MCP_HEARTBEAT_PATH unset).
        heartbeat_task: asyncio.Task | None = None
        hb_path = get_mcp_heartbeat_path()
        if hb_path:
            heartbeat_task = asyncio.create_task(
                heartbeat_loop(
                    hb_path,
                    interval_s=get_mcp_heartbeat_interval_s(),
                    color=get_mcp_color(),
                )
            )
            logger.info("mcp.heartbeat.started path=%s", hb_path)
        try:
            yield {}
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            if notifier_configured:
                await shutdown_trade_notifier(log_context="MCP trade notifier")
            logger.info(
                "mcp.lifecycle.shutdown service=%s uptime_s=%.1f",
                service,
                time.monotonic() - STARTED_MONOTONIC,
            )

    return _server_lifespan
