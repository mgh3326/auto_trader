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

## TradingCodex Smoke

```bash
cd /Users/mgh3326/services/tradingcodex-desk
./tcx connectors providers
./tcx connectors validate auto-trader
./tcx mcp call preview_order_translation '{"broker":"auto-trader","symbol":"005930","side":"buy","quantity":1,"limit_price":70000,"thesis":"smoke thesis","strategy":"smoke strategy"}'
```

Expected: preview response includes `approval_hash`, `approval_expires_at`,
`idempotency_key`, and no mutation was sent.

Live smoke must be done only with a real approved order ticket and exact
`live_confirmation` through `submit_approved_order`.
