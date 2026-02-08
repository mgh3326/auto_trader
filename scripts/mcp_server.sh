#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
exec uv run python -m app.mcp_server.main