#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
export ENV_FILE=.env.mcp
exec uv run python -m app.mcp_server.main