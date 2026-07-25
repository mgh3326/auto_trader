#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
# Operators may override MCP_PROFILE and MCP_PORT before invoking this wrapper,
# e.g. MCP_PROFILE=analysis_readonly MCP_PORT=8768 scripts/mcp_server.sh.
export ENV_FILE=.env.mcp
exec uv run python -m app.mcp_server.main