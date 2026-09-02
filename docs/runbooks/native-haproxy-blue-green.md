# Native HAProxy API Blue/Green Deploy (ROB-259)

## Scope

The Mac native deployment owns only the API blue/green pair:

```
trader.robinco.dev → 127.0.0.1:8000 (HAProxy) → api-blue :8001 | api-green :8002
```

MCP is operated on NCP. Its deployment, blue/green state, watchdog, and
operator procedures are in [the NCP MCP runbook](ncp-mcp.md). Do not add MCP
launchd plists, color state, or deploy steps back to the Mac native bundle.
Worker, scheduler, and WebSocket monitors are also NCP-owned and intentionally
absent from the Mac deploy.

## Normal deploy

```
scripts/deploy-native.sh <sha>
```

The script stages the release, runs expand-only migrations, bootstraps and
direct-probes the inactive API color, atomically reloads HAProxy, probes the
stable API listener, then drains the old API color. `current` is updated after
the API cutover; there are no Mac single-active services to restart.

## Rollback

If a post-cutover step fails, `rollback()` restores the recorded API color,
color symlinks, and HAProxy configuration before it restores `current`.

For an operator-driven API rollback:

```bash
cd $AUTO_TRADER_BASE
source scripts/native_bluegreen_lib.sh
set_active_color api blue   # or green
bash scripts/haproxy_switch.sh

source scripts/native_deploy_lib.sh
bootstrap_color api blue
scripts/healthcheck-native.sh
```

## Inspect API state

```bash
cat $AUTO_TRADER_BASE/shared/api-active-color
launchctl list | grep auto-trader
readlink $AUTO_TRADER_BASE/current-blue
readlink $AUTO_TRADER_BASE/current-green
```

## Related files

- `scripts/deploy-native.sh` — native deploy entrypoint
- `ops/native/scripts/native_deploy_lib.sh` — API blue/green primitives
- `ops/native/scripts/native_bluegreen_lib.sh` — shared color helpers
- `ops/native/plists/` — API and HAProxy launchd plists only
