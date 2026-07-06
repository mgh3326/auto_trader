# Analysis Readonly MCP (ROB-745)

## Start

Docker:

```bash
MCP_ANALYSIS_READONLY_PORT=8768 \
docker compose -f docker-compose.prod.yml up -d mcp-analysis-readonly
```

Manual local:

```bash
MCP_PROFILE=analysis_readonly MCP_PORT=8768 uv run python -m app.mcp_server.main
```

## Health

```bash
curl -s http://127.0.0.1:8768/health
```

Expected: HTTP 200 with `service=auto-trader-mcp`.

## Smoke

Use the same JSON-RPC bridge pattern as the main MCP server, but point it at `8768` and use the analysis-readonly token.

```bash
export MCP_ENDPOINT="http://127.0.0.1:8768/mcp"
export MCP_AUTH_TOKEN="$MCP_ANALYSIS_READONLY_AUTH_TOKEN"
export MCP_SESSION_ID="<session id>"
export PAPERCLIP_AGENT_ID="codex-analysis-readonly"
envsubst '$MCP_ENDPOINT $MCP_AUTH_TOKEN $MCP_SESSION_ID $PAPERCLIP_AGENT_ID' \
  < scripts/templates/mcp_call.sh.tmpl > /tmp/mcp_call_analysis_readonly.sh
chmod 700 /tmp/mcp_call_analysis_readonly.sh

/tmp/mcp_call_analysis_readonly.sh get_quote '{"symbol":"005930","market":"kr"}'
```

Expected: a tool payload, not 401/403.

Forbidden smoke:

```bash
/tmp/mcp_call_analysis_readonly.sh place_order '{"symbol":"005930","side":"buy","quantity":1}'
```

Expected: MCP tool-not-found / unknown-tool response because `place_order` is not registered.
