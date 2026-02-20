# Raspberry Pi Zero-Downtime Deployment Runbook

## Scope

- Single-node Raspberry Pi production runtime.
- Blue-green slots for API and MCP.
- Worker overlap with drain (`worker_blue`/`worker_green`).
- Scheduler and websocket monitor sequential restart.
- Database migration is excluded from the default zero-downtime script path.

## Files and Runtime Contracts

- Deploy entrypoint: `scripts/deploy-zero-downtime.sh`
- Compose overlay: `docker-compose.prod.zero.yml`
- Active upstream files:
  - `caddy/upstreams/api_active.caddy`
  - `caddy/upstreams/mcp_active.caddy`
- Slot upstream files:
  - `caddy/upstreams/api_blue.caddy`, `caddy/upstreams/api_green.caddy`
  - `caddy/upstreams/mcp_blue.caddy`, `caddy/upstreams/mcp_green.caddy`
- State file: `tmp/deploy/zero-downtime-state.env`

State keys:

- `ACTIVE_SLOT`
- `ACTIVE_IMAGE_DIGEST`
- `PREVIOUS_SLOT`
- `PREVIOUS_IMAGE_DIGEST`
- `LAST_DEPLOYED_AT`

## Preconditions

1. `.env.prod` is present and valid.
2. Caddy container name is `caddy` and running.
3. `docker-compose.prod.yml`, `docker-compose.prod.zero.yml`, and upstream files exist.
4. `readyz` endpoint is available in API runtime.

## Standard Deployment

```bash
./scripts/deploy-zero-downtime.sh
```

Optional flags:

- `--image-ref ghcr.io/<org>/<repo>:production`
- `--skip-pull`
- `--health-timeout 90`
- `--skip-worker-rotate` (emergency only)
- `--dry-run`

## Deployment Flow

1. Resolve deployment image digest.
2. Detect active slot from state file (fallback: `api_active.caddy`).
3. Start inactive slot: `api_<slot>` + `mcp_<slot>`.
4. Wait for slot health:
   - `http://127.0.0.1:<slot-api-port>/readyz`
   - `http://127.0.0.1:<slot-mcp-port>/mcp` (non-5xx)
5. Cutover:
   - Replace `api_active.caddy` + `mcp_active.caddy`
   - `docker exec caddy caddy validate --config /etc/caddy/Caddyfile`
   - `docker exec caddy caddy reload --config /etc/caddy/Caddyfile`
6. Post-cutover checks:
   - `http://127.0.0.1:18080/healthz`
   - `http://127.0.0.1:18080/readyz`
   - `http://127.0.0.1:18065/mcp` (non-5xx)
7. Worker rotate:
   - Start `worker_<new-slot>`
   - Drain old slot worker with `docker stop -t 660`
8. Restart scheduler sequentially.
9. Restart websocket monitors sequentially.
10. Stop legacy single-slot containers if present.
11. Update `tmp/deploy/zero-downtime-state.env`.

## Automatic Rollback Rules

- Failure before cutover:
  - Stop inactive slot services.
- Failure after cutover:
  - Restore `PREVIOUS_SLOT` active files.
  - Validate + reload Caddy.
  - Stop failed target slot services.

## Validation Checklist

Run after deployment:

```bash
curl http://127.0.0.1:18080/healthz
curl http://127.0.0.1:18080/readyz
curl -X POST http://127.0.0.1:18065/mcp
./scripts/healthcheck.sh
```

Also verify:

- `ACTIVE_SLOT` in state file matches `api_active.caddy`.
- Only one scheduler instance is running.
- Inactive slot is not serving production traffic.

## Manual Recovery

If rollback is needed outside automatic deployment rollback:

1. Set `api_active.caddy` and `mcp_active.caddy` to previous slot.
2. Reload Caddy (validate then reload).
3. Restart required services using previous image digest.
4. Update `tmp/deploy/zero-downtime-state.env` to reflect the recovered slot.
