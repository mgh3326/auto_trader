import logging

from app.core.config import settings
from app.mcp_server.env_utils import (
    _env,
    _env_int,
    get_mcp_graceful_shutdown_timeout,
    get_mcp_tool_timeout_default,
    get_mcp_tool_timeout_enabled,
)
from app.mcp_server.profiles import McpProfile, resolve_mcp_profile
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
from app.mcp_server.caller_identity_middleware import (  # noqa: E402
    CallerIdentityMiddleware,
)
from app.mcp_server.lifecycle import (  # noqa: E402
    build_server_lifespan,
    register_health_route,
)
from app.mcp_server.sentry_middleware import McpToolCallSentryMiddleware  # noqa: E402
from app.mcp_server.timeout_middleware import ToolTimeoutMiddleware  # noqa: E402
from app.mcp_server.tooling import register_all_tools  # noqa: E402

_auth_token = _env("MCP_AUTH_TOKEN", "")
_mcp_profile = resolve_mcp_profile(_env("MCP_PROFILE"))


def _validate_profile_auth_token(profile: McpProfile, token: str | None) -> None:
    token_required_profiles = {
        McpProfile.ACCOUNT_READ,
        McpProfile.TRADINGCODEX_EXECUTION,
    }
    if profile in token_required_profiles and not (token or "").strip():
        raise RuntimeError(
            f"MCP_PROFILE={profile.value} requires non-empty MCP_AUTH_TOKEN"
        )


def _validate_profile_runtime_settings(profile: McpProfile) -> None:
    if profile is not McpProfile.TRADINGCODEX_EXECUTION:
        return
    if settings.order_approval_hash_mode != "required":
        raise RuntimeError(
            "MCP_PROFILE=tradingcodex_execution requires "
            "ORDER_APPROVAL_HASH_MODE=required"
        )
    if settings.toss_approval_hash_mode != "required":
        raise RuntimeError(
            "MCP_PROFILE=tradingcodex_execution requires "
            "TOSS_APPROVAL_HASH_MODE=required"
        )


_validate_profile_auth_token(_mcp_profile, _auth_token)
_validate_profile_runtime_settings(_mcp_profile)
auth_provider = build_auth_provider(_auth_token)
mcp = FastMCP(
    name="auto_trader-mcp",
    instructions=(
        "Market data, holdings lookup, and order execution tools for auto_trader "
        "(symbol search, quote, holdings, OHLCV, indicators, trade, order management)."
    ),
    version="0.1.0",
    auth=auth_provider,
    # ROB-447: fail-fast on duplicate tool names at boot instead of the default "warn"
    # (last-registration-silently-wins), which had let the brief판 shadow the report판's
    # get_market_reports / get_latest_market_brief. Any future collision now raises.
    on_duplicate="error",
    # ROB-469: startup/shutdown lifecycle logging (diagnose disconnect root cause).
    lifespan=build_server_lifespan(),
)

mcp.add_middleware(McpToolCallSentryMiddleware())
mcp.add_middleware(CallerIdentityMiddleware())
# ROB-469 PR2: per-tool timeout — added LAST so it is the INNERMOST middleware
# (wraps the tool) while Sentry stays outermost and captures the ToolError with the
# tool-call context. Bounds the single event loop so one slow tool can't take all
# 128 tools down (the ROB-469 SPOF).
mcp.add_middleware(
    ToolTimeoutMiddleware(
        default_timeout_s=get_mcp_tool_timeout_default(),
        enabled=get_mcp_tool_timeout_enabled(),
    )
)
register_all_tools(mcp, profile=_mcp_profile)
# ROB-469: unauthenticated, dependency-free liveness probe for HAProxy / native
# healthcheck / docker healthcheck. Registered after tools so the count is final.
register_health_route(mcp, version="0.1.0")


def _validate_caller_agent_id_fallback(mcp_type: str) -> None:
    fallback_agent_id = settings.mcp_caller_agent_id_fallback
    if mcp_type in {"streamable-http", "sse"} and fallback_agent_id:
        raise RuntimeError(
            "MCP_CALLER_AGENT_ID is only allowed for stdio/local dev transports; "
            "unset it for production HTTP deployments and send "
            "x-paperclip-agent-id explicitly."
        )


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
    graceful_shutdown_timeout = get_mcp_graceful_shutdown_timeout()
    auth_enabled = bool(_auth_token and _auth_token.strip())

    logging.info(
        "mcp.lifecycle.starting type=%s host=%s port=%s path=%s "
        "graceful_shutdown_timeout=%s auth_enabled=%s",
        mcp_type,
        mcp_host,
        mcp_port,
        mcp_path,
        graceful_shutdown_timeout,
        auth_enabled,
    )

    try:
        _validate_caller_agent_id_fallback(mcp_type)

        if mcp_type == "stdio":
            mcp.run(transport="stdio")
        elif mcp_type == "sse":
            mcp.run(
                transport="sse",
                host=mcp_host,
                port=mcp_port,
                path=mcp_path,
                uvicorn_config={"timeout_graceful_shutdown": graceful_shutdown_timeout},
            )
        elif mcp_type == "streamable-http":
            mcp.run(
                transport="streamable-http",
                host=mcp_host,
                port=mcp_port,
                path=mcp_path,
                uvicorn_config={"timeout_graceful_shutdown": graceful_shutdown_timeout},
            )
        else:
            raise ValueError(f"Unsupported MCP_TYPE: {mcp_type}")
    except Exception as exc:
        # ROB-469: an unhandled mcp.run() exception is a CRASH, distinct from a
        # graceful mcp.lifecycle.shutdown. Log it explicitly before Sentry capture.
        logging.exception(
            "mcp.lifecycle.crashed type=%s host=%s port=%s",
            mcp_type,
            mcp_host,
            mcp_port,
        )
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
