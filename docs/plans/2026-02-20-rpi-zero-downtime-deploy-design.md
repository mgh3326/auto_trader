# Raspberry Pi Zero-Downtime Deployment Design

## Goal
- Eliminate API/MCP downtime during deployment on single-node Raspberry Pi.
- Keep background processing safe: worker drain, scheduler single-instance, websocket reconnect resilience.

## Current Constraints
- Production runtime uses host networking and fixed ports (`8000`, `8765`).
- Current deployment script performs `docker compose down` then `up -d`, causing unavoidable downtime.
- TaskIQ broker is `ListQueueBroker` (Redis list / BRPOP), so exactly-once is not guaranteed.

## Decision Summary
1. Use **host network + blue/green dual ports** for API/MCP.
2. Route traffic via Caddy and switch active upstream only after readiness passes.
3. Keep scheduler sequential restart only (never dual active).
4. Worker rollout uses overlap + drain with explicit timeout.
5. Add baseline cron-miss visibility and websocket client reconnect backoff hardening.

## Architecture
- API slots: `api_blue` (`18000`), `api_green` (`18001`)
- MCP slots: `mcp_blue` (`18650`), `mcp_green` (`18651`)
- Caddy routes:
  - External: `https://$DOMAIN_NAME/` -> active API, `/mcp` -> active MCP
  - Internal fixed endpoints:
    - `http://127.0.0.1:18080` -> active API
    - `http://127.0.0.1:18065/mcp` -> active MCP
- OpenClaw internal MCP/API calls move to fixed internal proxy endpoints.

## Runtime Safety Rules
### Worker overlap (10 min)
- Start new worker first, then drain old worker.
- Configure TaskIQ wait timeout and container stop grace period to support graceful drain.
- Note: current scan tasks use redis cooldown / hdel patterns, which reduce duplicates but are not fully atomic idempotency locks.

### Scheduler
- Always stop old scheduler completely before starting new one.
- No blue/green scheduler.
- Accept short cron gap during restart, but expose detection signal.

### WebSocket
- Sequential restart for `upbit_websocket` and `kis_websocket` is acceptable.
- Frontend websocket reconnect should use backoff instead of fixed retry interval.

## API / Interface Changes
- Add `GET /readyz` for deployment gate (dependency-ready check).
- Keep `GET /healthz` as liveness endpoint.
- Add deployment state file for active slot + previous digest rollback reference.

## Deployment Flow
1. Pull `production` branch.
2. Resolve image digest and start inactive API/MCP slot.
3. Wait up to 90s for readiness (`/readyz`).
4. Switch Caddy active upstream.
5. Validate external + internal proxy endpoints.
6. Roll worker (new up, old drain up to 10m).
7. Restart scheduler sequentially.
8. Restart websocket monitors sequentially.
9. Run post-checks (health, cron heartbeat, logs).

## Rollback Policy
- If failure before cutover: remove inactive slot, keep current active.
- If failure after cutover: immediately switch Caddy back to previous active slot.
- Persist previous digest/slot metadata to support deterministic rollback.

## Test Scenarios
1. No downtime during API/MCP cutover under continuous requests.
2. Automatic rollback on forced readiness failure.
3. Worker drain completes in timeout without forced task loss.
4. Scheduler never runs in parallel during rollout.
5. Websocket client reconnect succeeds after forced disconnect/restart.

## Out of Scope
- Mandatory expand/contract DB migration policy enforcement.
- Full exactly-once task delivery redesign (e.g., stream broker migration).

