import logging

from fastmcp import FastMCP

from app.core.config import settings
from app.mcp_server.auth import build_auth_provider
from app.mcp_server.env_utils import _env, _env_int
from app.mcp_server.sentry_middleware import McpSentryTracingMiddleware
from app.mcp_server.tooling import register_all_tools
from app.monitoring.sentry import capture_exception, init_sentry

_SENTRY_MIDDLEWARE_REGISTERED = False

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
    init_sentry(
        service_name="auto-trader-mcp",
        enable_sqlalchemy=True,
        enable_httpx=True,
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
