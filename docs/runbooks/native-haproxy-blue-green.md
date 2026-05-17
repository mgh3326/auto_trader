# Native HAProxy Blue/Green Deploy (ROB-259)

## Architecture summary

```
Cloudflare Tunnel
  ├─ trader.robinco.dev      → 127.0.0.1:8000  (HAProxy) → api-blue :8001 | api-green :8002
  └─ trader-mcp.robinco.dev  → 127.0.0.1:8765  (HAProxy) → mcp-blue :8766 | mcp-green :8767
```

HAProxy owns the stable origin ports. The deploy script bootstraps the inactive color, smokes it directly, atomically swaps HAProxy's backend, smokes the public path, then drains the old color. Worker, scheduler, kis-websocket, upbit-websocket remain single-active and continue to use `current`.

## First-time bootstrap (CRITICAL ORDERING)

**The first `scripts/deploy-native.sh` invocation after this PR merges will fail catastrophically if the operator skips the cutover script.** Reason: the new `deploy-native.sh` flow calls `haproxy_swap_to_color`, which expects HAProxy to be already loaded as a launchd job; if HAProxy was never bootstrapped, the SIGUSR2 reload errors out under `set -e` and the deploy bails after `rsync --delete` has already removed the legacy single-port `api`/`mcp` plists from disk. The previously-running `api`/`mcp` processes survive but are orphaned with no plist on disk.

Run the cutover EXACTLY ONCE, before any `deploy-native.sh` invocation that includes ROB-259 changes.

Prerequisites:

- `brew install haproxy` on the production Mac. Verify with `haproxy -v`.
- A release containing `ops/native/*` is on disk under `$AUTO_TRADER_BASE/current`. If this is the very first deploy of ROB-259, stage it MANUALLY (do NOT run `deploy-native.sh` yet):
  1. `cd $AUTO_TRADER_BASE/releases && git clone --local /Users/mgh3326/work/auto_trader <sha>`
  2. `cd <sha> && git checkout --detach <sha> && uv sync --frozen`
  3. Build frontends if needed: `(cd frontend/trading-decision && npm ci && npm run build)` and same for `frontend/invest`
  4. `ln -sfn $AUTO_TRADER_BASE/releases/<sha> $AUTO_TRADER_BASE/current`
- Confirm `.env.prod.native` either does not set `MCP_HOST` or sets it to `127.0.0.1`. The MCP server default is `0.0.0.0` which still works behind HAProxy but exposes the per-color ports on all interfaces.

Run:

```
/Users/mgh3326/services/auto_trader/current/scripts/native_haproxy_first_cutover.sh
```

Expected window: ~10–30 s during which `trader.robinco.dev` may briefly 502 while the single-port api at `:8000` is stopped and HAProxy takes over `:8000`. Run outside business hours.

Verify after:

- `launchctl list | grep auto-trader` shows `haproxy`, `api-blue`, `mcp-blue`, plus worker, scheduler, kis-websocket, upbit-websocket.
- `curl http://127.0.0.1:8000/healthz` returns 200 (HAProxy → :8001).
- `curl -H 'Accept: text/event-stream' http://127.0.0.1:8765/mcp` returns 400 or 401 (HAProxy → :8766).
- `curl https://trader.robinco.dev/healthz` returns 200.

**Only after this verification is complete is `scripts/deploy-native.sh <sha>` safe to run.**

After cutover, if HAProxy starts but `logs/com.robinco.auto-trader.haproxy.err.log` shows nothing for the first request, check whether `/var/run/syslog` is accessible to the user-domain launchd context (macOS Sequoia hardened this). If syslog access is denied, switch `log /var/run/syslog ...` to `log stderr ...` in `ops/native/haproxy/haproxy.cfg.tmpl` for the affected box.

## Normal deploy

```
scripts/deploy-native.sh <sha>
```

The script does:

1. Stage release under `releases/<sha>`.
2. Sync `ops/native/{plists,scripts,haproxy}` into `$AUTO_TRADER_BASE`.
3. Run alembic migrations (must remain expand-only / backwards-compatible).
4. Detect active color (e.g. `blue`); bootstrap the inactive color (`green`) on `:8002`/`:8767`.
5. Direct-probe green at `:8002` and `:8767`.
6. Render new HAProxy config with green primary; `haproxy -c -f` validate; atomically move into live path; SIGUSR2 reload.
7. Public smoke via `:8000` and `:8765` (HAProxy now routes to green).
8. Drain blue (bootout).
9. Flip the `current` symlink, restart worker/scheduler/kis-websocket/upbit-websocket against the new `current`.
10. Final stable-mode healthcheck.

Cloudflare Tunnel sees zero `connection refused` because HAProxy never closes its listeners.

## Rollback (automatic, on failed deploy)

The `deploy_bluegreen_flow` library function handles api+mcp rollback internally:

- **Probe green failed (step 5):** green is bootout. HAProxy never swaps. Blue stays active. Deploy exits non-zero.
- **HAProxy swap api failed:** `api-active-color` is restored to blue. Green is bootout. Deploy exits non-zero.
- **HAProxy swap mcp failed:** both `api-active-color` and `mcp-active-color` are restored to blue. A compensating `haproxy_switch.sh` puts HAProxy back on blue. Green is bootout. Deploy exits non-zero.
- **Public smoke failed (step 7):** both state files restored, compensating `haproxy_switch.sh` runs, green is bootout. Deploy exits non-zero.

The outer `rollback()` in `deploy-native.sh` reverts the `current` symlink (worker/scheduler/websocket path) only when bluegreen succeeded but a later step failed.

## Manual rollback (operator-driven)

To revert to the previous color after a successful deploy:

```bash
cd $AUTO_TRADER_BASE
source scripts/native_bluegreen_lib.sh
set_active_color api blue   # or green
set_active_color mcp blue
bash scripts/haproxy_switch.sh

source scripts/native_deploy_lib.sh
bootstrap_color api blue
bootstrap_color mcp blue
scripts/healthcheck-native.sh
```

## How to inspect active color

```
cat $AUTO_TRADER_BASE/shared/api-active-color
cat $AUTO_TRADER_BASE/shared/mcp-active-color
launchctl list | grep auto-trader
readlink $AUTO_TRADER_BASE/current-blue
readlink $AUTO_TRADER_BASE/current-green
```

## HAProxy ops

```
# Reload config without dropping listeners (after manual cfg edit)
launchctl kill SIGUSR2 gui/$(id -u)/com.robinco.auto-trader.haproxy

# Validate a candidate config before swapping
haproxy -c -f /Users/mgh3326/services/auto_trader/shared/haproxy/haproxy.cfg

# Render the live config from current state files (no reload)
AUTO_TRADER_HAPROXY_RELOAD=skip bash $AUTO_TRADER_BASE/scripts/haproxy_switch.sh
```

## Cloudflared

No changes. `trader.robinco.dev` and `trader-mcp.robinco.dev` still target `127.0.0.1:8000` and `127.0.0.1:8765` — those are now HAProxy stable listeners.

## Known limitations

- **FastMCP long-lived sessions** still reconnect during HAProxy reload because session state lives in the MCP server process. Acceptable per the ROB-259 success criteria; clients should retry on `Session terminated`.
- **Worker, scheduler, websockets** still have a brief `launchctl bootstrap` window. Not in scope for ROB-259.
- **`bootstrap_color`** does a single `launchctl bootstrap` attempt with no retry. Transient launchd timing failures during deploy will surface as a deploy failure that drains the new color; rerun the deploy to recover. Tracked for a follow-up hardening.
- **DB migrations must remain expansion-only** while api+mcp colors briefly coexist. Destructive downgrades require a separate migration runbook.

## Related files

- `scripts/deploy-native.sh` — main deploy entrypoint
- `scripts/native_haproxy_first_cutover.sh` — one-shot installer
- `ops/native/scripts/native_deploy_lib.sh` — blue/green primitives
- `ops/native/scripts/native_bluegreen_lib.sh` — color detection helpers
- `ops/native/scripts/haproxy_render.sh` / `haproxy_switch.sh` — config rendering + atomic swap
- `ops/native/scripts/healthcheck-native.sh` — `--direct` and stable modes
- `ops/native/haproxy/haproxy.cfg.tmpl` — config template with `{{*_LINE}}` placeholders
- `ops/native/plists/` — all launchd plists (5 blue/green + haproxy + 4 single-active)
