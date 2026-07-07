# TradingCodex Execution MCP (ROB-762)

## Purpose

`tradingcodex_execution` exposes only the MCP tools required by the reviewed
TradingCodex `auto_trader` BrokerAdapter. It is not a general Codex tool
surface.

## Required Environment

```bash
MCP_PROFILE=tradingcodex_execution
MCP_PORT=8770
MCP_AUTH_TOKEN="$MCP_TRADINGCODEX_EXECUTION_AUTH_TOKEN"
ORDER_APPROVAL_HASH_MODE=required
TOSS_APPROVAL_HASH_MODE=required
```

`MCP_TRADINGCODEX_EXECUTION_AUTH_TOKEN` is the auto_trader server-side token.
TradingCodex should store the same raw value outside the repo and refer to it
as `env:AUTO_TRADER_TRADINGCODEX_EXECUTION_TOKEN`.

## Start

```bash
MCP_PROFILE=tradingcodex_execution \
MCP_PORT=8770 \
MCP_AUTH_TOKEN="$MCP_TRADINGCODEX_EXECUTION_AUTH_TOKEN" \
ORDER_APPROVAL_HASH_MODE=required \
TOSS_APPROVAL_HASH_MODE=required \
uv run python -m app.mcp_server.main
```

## Smoke

```bash
curl -s http://127.0.0.1:8770/health
```

Expected: HTTP 200 with `service=auto-trader-mcp`.

Mutation smoke must use TradingCodex `submit_approved_order`; do not call live
place tools directly except in fake/test fixtures.
