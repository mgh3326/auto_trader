# auto_trader MCP server

Read-only MCP tools (market data) exposed via `fastmcp`.

## Tools
- `search_symbol(query, limit=20)`
- `get_quote(symbol, market=None)`
- `get_ohlcv(symbol, days=100, market=None)`

## Run (docker-compose.prod)
Environment variables:
- `MCP_TYPE` : `streamable-http` (default) | `sse` | `stdio`
- `MCP_HOST` : `0.0.0.0`
- `MCP_PORT` : `8765`
- `MCP_PATH` : `/mcp`

Example:
```bash
docker compose -f docker-compose.prod.yml up -d mcp
```

> Note: current prod compose uses `network_mode: host`, so port publishing is handled by the host network.
