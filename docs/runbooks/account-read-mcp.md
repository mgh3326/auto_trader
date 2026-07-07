# Account Read MCP (ROB-760)

## Purpose

`account_read` exposes only account synchronization reads for TradingCodex:

- `get_holdings`
- `toss_get_positions`
- `get_cash_balance`
- `toss_get_orderable_cash`
- `get_order_history`
- `kis_live_get_order_history`
- `toss_get_order_history`

It does not register order placement, cancel, modify, preview, reconcile, persistence, settings, watch, report-writing, admin, or manual holdings mutation tools.

## Token Provisioning

Generate a dedicated random token outside the repository and store it in the production secret store or host env file as:

```bash
MCP_ACCOUNT_READ_AUTH_TOKEN="<dedicated account-read token>"
```

Do not reuse `MCP_AUTH_TOKEN`. Do not commit the token. Rotate by writing a new `MCP_ACCOUNT_READ_AUTH_TOKEN`, reloading the account-read service, and updating downstream clients that read `MCP_ACCOUNT_READ_AUTH_TOKEN`.

## Start

Docker:

```bash
MCP_ACCOUNT_READ_AUTH_TOKEN="$MCP_ACCOUNT_READ_AUTH_TOKEN" \
docker compose -f docker-compose.prod.yml up -d mcp-account-read
```

Native launchd:

```bash
launchctl bootstrap gui/$(id -u) /Users/mgh3326/services/auto_trader/plists/com.robinco.auto-trader.mcp-account-read.plist
launchctl kickstart -k gui/$(id -u)/com.robinco.auto-trader.mcp-account-read
```

Manual local:

```bash
MCP_PROFILE=account_read MCP_PORT=8769 MCP_AUTH_TOKEN="$MCP_ACCOUNT_READ_AUTH_TOKEN" uv run python -m app.mcp_server.main
```

## Health

```bash
curl -s http://127.0.0.1:8769/health
```

Expected: HTTP 200 with `service=auto-trader-mcp`.

## Authenticated Smoke

```bash
export MCP_ENDPOINT="http://127.0.0.1:8769/mcp"
export MCP_AUTH_TOKEN="$MCP_ACCOUNT_READ_AUTH_TOKEN"
export MCP_SESSION_ID="<session id>"
export PAPERCLIP_AGENT_ID="tradingcodex-account-read"
envsubst '$MCP_ENDPOINT $MCP_AUTH_TOKEN $MCP_SESSION_ID $PAPERCLIP_AGENT_ID' \
  < scripts/templates/mcp_call.sh.tmpl > /tmp/mcp_call_account_read.sh
chmod 700 /tmp/mcp_call_account_read.sh

/tmp/mcp_call_account_read.sh get_holdings '{"account":"kis"}'
```

Expected: a tool payload, not 401/403.

## Forbidden Smoke

```bash
/tmp/mcp_call_account_read.sh place_order '{"symbol":"005930","side":"buy","quantity":1}'
```

Expected: MCP tool-not-found or unknown-tool response because `place_order` is not registered.

## Fail-Closed Startup Smoke

```bash
MCP_PROFILE=account_read MCP_PORT=8769 MCP_AUTH_TOKEN="" uv run python -m app.mcp_server.main
```

Expected: process exits before serving with `MCP_PROFILE=account_read requires non-empty MCP_AUTH_TOKEN`.
