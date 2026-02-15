import logging

from app.core.config import settings
from app.mcp_server.env_utils import _env, _env_int
from app.monitoring.sentry import capture_exception, init_sentry

# ──────────────────────────────────────────────────────────────────────
# 1) Sentry MUST be initialised BEFORE FastMCP is instantiated.
#
#    MCPIntegration.setup_once() monkey-patches
#      mcp.server.lowlevel.Server.call_tool   (decorator)
#      mcp.server.streamable_http.StreamableHTTPServerTransport.handle_request
#      fastmcp.FastMCP._get_prompt_mcp / _read_resource_mcp
#
#    FastMCP.__init__() → _setup_handlers() → _mcp_server.call_tool()(...)
#    registers the tool handler through that decorator.  If Sentry has NOT
#    yet patched it, the handler is registered *un-instrumented* and no
#    child spans (tools/call, DB, httpx) ever appear in the trace.
# ──────────────────────────────────────────────────────────────────────
init_sentry(
    service_name="auto-trader-mcp",
    enable_sqlalchemy=True,
    enable_httpx=True,
    enable_mcp=True,
)

# 2) Now it is safe to create the FastMCP instance and register tools.
from fastmcp import FastMCP  # noqa: E402

from app.mcp_server.auth import build_auth_provider  # noqa: E402
from app.mcp_server.sentry_middleware import McpSentryTracingMiddleware  # noqa: E402
from app.mcp_server.tooling import register_all_tools  # noqa: E402

_auth_token = _env("MCP_AUTH_TOKEN", "")
auth_provider = build_auth_provider(_auth_token)
mcp = FastMCP(
    name="auto_trader-mcp",
    instructions=(
        "Market data, holdings lookup, and order execution tools for auto_trader "
        "(symbol search, quote, holdings, OHLCV, indicators, trade, order management)."
    ),
    version="0.1.0",
    auth=auth_provider,
)

register_all_tools(mcp)

# 3) Fallback middleware — skips automatically when native MCPIntegration is active.
_SENTRY_MIDDLEWARE_REGISTERED = False


def _register_sentry_middleware() -> None:
    global _SENTRY_MIDDLEWARE_REGISTERED
    if _SENTRY_MIDDLEWARE_REGISTERED:
        return
    mcp.add_middleware(McpSentryTracingMiddleware())
    _SENTRY_MIDDLEWARE_REGISTERED = True


_register_sentry_middleware()


def main() -> None:
    log_level_name = str(getattr(settings, "LOG_LEVEL", "INFO") or "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    if not isinstance(log_level, int):
        log_level = logging.INFO

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    mcp_type = _env("MCP_TYPE", "streamable-http")
    mcp_host = _env("MCP_HOST", "0.0.0.0")
    mcp_port = _env_int("MCP_PORT", 8765)
    mcp_path = _env("MCP_PATH", "/mcp")

    logging.info(
        f"Starting MCP server: type={mcp_type} host={mcp_host} port={mcp_port} path={mcp_path}"
    )

    try:
        if mcp_type == "stdio":
            mcp.run(transport="stdio")
        elif mcp_type == "sse":
            mcp.run(transport="sse", host=mcp_host, port=mcp_port, path=mcp_path)
        elif mcp_type == "streamable-http":
            mcp.run(
                transport="streamable-http", host=mcp_host, port=mcp_port, path=mcp_path
            )
        else:
            raise ValueError(f"Unsupported MCP_TYPE: {mcp_type}")
    except Exception as exc:
        capture_exception(
            exc,
            mcp_type=mcp_type,
            mcp_host=mcp_host,
            mcp_port=mcp_port,
            mcp_path=mcp_path,
        )
        raise


if __name__ == "__main__":
    main()
