#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
# The caller must set MCP_PROFILE (and may set MCP_PORT) before invoking this
# wrapper, e.g. MCP_PROFILE=analysis_readonly MCP_PORT=8768 scripts/mcp_server.sh
# or MCP_PROFILE=default for the DEFAULT surface. #1189: the server refuses a
# blank or missing MCP_PROFILE; this wrapper deliberately supplies no fallback.
export ENV_FILE=.env.mcp
exec uv run python -m app.mcp_server.main