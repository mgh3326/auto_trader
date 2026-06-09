# Runbook: MCP server health & supervision (ROB-469)

The auto_trader MCP server is a single FastMCP `streamable-http` uvicorn process.
Production runs the native launchd blue/green path (8766 blue / 8767 green →
HAProxy → stable 8765). `docker-compose.prod.yml` is the legacy/secondary path.

## /health endpoint
- `GET http://127.0.0.1:<port>/health` → `200 {"status":"ok","service":"auto-trader-mcp","version":"...","uptime_s":...}`.
- **Unauthenticated** (bypasses `MCP_AUTH_TOKEN`) and **dependency-free** (no DB/Redis).
  A 200 means the event loop is responsive; a wedged loop stops answering it.
- Ports: stable 8765 (HAProxy), blue 8766, green 8767.

## Probe interpretation
- `/health` 200 → process up and loop responsive.
- `/health` non-200 / timeout → process down OR event loop wedged. HAProxy
  (`inter 5s`) marks the backend DOWN; launchd `KeepAlive` restarts only on
  process EXIT (a wedged-but-alive process is NOT restarted until PR3's watchdog).

## Lifecycle logs (diagnose a disconnect)
Filter Sentry / logs for `service:auto-trader-mcp`. Log lines:
- `mcp.lifecycle.starting ...` — env/config at boot.
- `mcp.lifecycle.startup_complete tools=N ...` — server ready, N tools registered.
- `mcp.lifecycle.shutdown ...` — graceful stop (teardown ran).
- `mcp.lifecycle.crashed ...` — unhandled `mcp.run()` exception.
- **Diagnosis:** `startup_complete` with NO matching `shutdown` before the next
  `starting` ⇒ hard-kill/OOM/SIGKILL (teardown never ran). A `shutdown` ⇒ graceful.

## Manual checks
```bash
# native, active color (find color from HAProxy or launchctl):
curl -s http://127.0.0.1:8766/health   # blue
curl -s http://127.0.0.1:8767/health   # green
curl -s http://127.0.0.1:8765/health   # stable (via HAProxy)
```

## Restart
- **Native (launchd):** `launchctl kickstart -k gui/$(id -u)/com.robinco.auto-trader.mcp-<color>`
- **Docker (legacy):** `docker compose -f docker-compose.prod.yml restart mcp`

## Notes
- True in-session client reconnect is the Claude Code harness's job, not the server's.
- Continuous hung-but-alive recovery (heartbeat watchdog) lands in ROB-469 PR3.
