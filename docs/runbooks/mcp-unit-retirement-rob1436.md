# MCP unit retirement and memory trim (ROB-1436)

Date: 2026-11-01. Operator decision (2026-11-01, final): retire the NCP MCP
units and clean up first; a surface that is needed later gets added back then.
Most of the MCP surface is unused, and the paper surfaces may be rebuilt.

Every MCP unit is its own Python process with a resident baseline of roughly
250 MB on NCP. This change removes seven units from the deploy and makes the
remaining low-traffic units start smaller.

## 1. What is retired and who called it

Sentry counts are `tools/call` spans over 30 days, tagged `mcp.profile`
(sampled; relative counts). "Caller config" means a client configuration that
points at the unit: `.mcp*.json` templates, prefect argv, runner code, systemd
units. The search covered the `origin/main` of auto_trader-operator,
robin-prefect-automations, fillwire, agent-skills, rstack, runtug,
fleet-monitoring, mergewake and panewire, plus this repo's scripts, docs,
config and ops. It did not read any env file, rendered `.mcp*.json` or local
agent configuration, because those hold tokens. It had no production access.

| Unit (port, profile) | Sentry 30 d | Caller config today | Decision |
|---|---:|---|---|
| at-mcp-analysis-readonly (8768, `analysis_readonly`) | 62 (2 in the last 7 d) | **In use until now**: the desk and strategy-lab sessions' local `auto_trader_readonly` entry (per the desk; not in any searched repo). `scripts/mcp_tool_usage_audit.py` mapped lane fable-workbench (host mbp-server) to it. | Retired; profile and lane deleted. Consumers are in §1.1 |
| at-mcp-account-read (8769, `account_read`) | 0 | None. Its documented client was the external TradingCodex desk. The operator considered it for read-only `dots` access, and will add a surface when that is needed. | Retired; profile deleted |
| at-mcp-tradingcodex-execution (8770, `tradingcodex_execution`) | 0 | None. Its client was the external TradingCodex desk, which is not in these repos. | Retired; profile deleted |
| at-mcp-paper-001 (8771, `hermes-paper-kis`) | 0 | Wired but never rendered. Operator `runners/h3_pilot_core.py:530` and `deploy/h3-managed-envelope-kr.service.example:19` name the profile for the H3-KR pilot. `scripts/render_mcp_configs.py:33-40` and `docs/mcp-token-env.md:32-33` state that kr is "declared and not rendered", and the runner fails closed with rc 78. | Unit retired; profile code kept (see §2) |
| at-mcp-live-kr / -us / -crypto (8773-8775) | 1 each | Opt-in only. Prefect `src/robin_automation/kr_live_sessions.py:225,300-313,1064-1076` uses them only when `KR_LIVE_MCP_MODE=dedicated` and `SPAWN_BACKEND=ncp-herdr`. The default `shared` uses the operator `.mcp.json` pointing at 8765 (DEFAULT). No prefect.yaml or job variable sets `dedicated`. | Retired; profiles, registrar and `config/mcp_profiles/live.yaml` deleted |

Units that stay, and their callers:

- kiwoom (8772): operator `mock/.mcp.json.template:5`, rendered by `scripts/render_mcp_configs.py:30`. The H1 runner uses it.
- h3-crypto-paper (8776): operator `mock/.mcp.h3-crypto.json.template:5`, `runners/h3_pilot_runner.py:78`.
- h3-us-paper (8777): operator `mock/.mcp.h3-us.json.template:5`, `runners/h3_pilot_runner.py:77`.

### 1.1 Contracts and consumers bound to `analysis_readonly`

The only last-use evidence available without production access is the brief's
Sentry count: 62 sampled calls in 30 days, 2 of them in the last 7 days. Sentry
gives no per-consumer split. The desk's reading is that the recent 2 are its
own `analyze_stock_batch` and `get_trading_policy` calls. §3.0 step 2 counts the
real calls on the host before anything stops.

| Bound thing | Kind | Last real use | What this PR does | What is left |
|---|---|---|---|---|
| Desk and strategy-lab `auto_trader_readonly` (local Claude config) | Consumer | Sentry: 2 sampled calls in the last 7 d, split unknown | Nothing; the config is outside the repo | Desk removes or replaces the entry after merge and before the NCP stop |
| Strategy-lab persistence through `analysis_artifact_save`, `forecast_save`, `session_context_append` (HK #657) | Consumer path | No per-profile split. The tools are used on DEFAULT too (Sentry 90 d: 496, 784, 432, all profiles) | The tools stay on DEFAULT. The `created_by_required` guard existed only on this profile and goes with it | A session moved to DEFAULT can save without a `created_by` label |
| `config/mcp_lane_allowlists/fable-workbench.txt`, its draft, count and hash pins | Lane contract | Same Sentry count; lane host mbp-server | Deleted, including `LANE_PROFILES`, `LANE_COUNTS`, `LANE_SHA256` and the audit script's `LaneSpec` | None. Other lanes' hashes are unchanged |
| `test_fable_workbench_lane_has_no_write_tools` | Test | n/a | Deleted with the lane | None |
| `test_fable_get_order_history_paper_route_does_not_write` | Test | n/a | Kept against DEFAULT, renamed: a read must not lazily create a paper account | None |
| Registry-membership asserts in the fanout, buy-gate shadow, decision-table and spike-attribution tests | Tests | n/a | That one assert removed; each test's other checks stay | None |
| `_AllowlistedMCP` proxy, also used by `kiwoom_kr` | Shared code | n/a | Moved to `tooling/allowlist_proxy.py`; the `watch_repricing` copy now uses it too | None |
| Operator `scripts/e2_runner_preparation.py:241-242,348-349` (and `:330-340` for tradingcodex) | Consumer (static path) | Preparation only, per its own doc | Nothing; different repo | Its preflight names deleted registrar paths, so repoint them to `analysis_artifact_registration.py` etc. |
| Operator `docs/mcp-token-env.md:16`, `report.md:54`: `MCP_ANALYSIS_READONLY_AUTH_TOKEN` | Doc | Reserved; no template uses it | Nothing; different repo | Drop the name |

🔴 **DEFAULT is not a read-only replacement.** It registers order tools. After
this change the repo has no physically read-only MCP surface, so a consumer
moved from `auto_trader_readonly` to DEFAULT gains order capability unless its
client restricts tools. Prefer removing the entry, or add an allowlist on the
client.

## 2. What changed in the repo

- `scripts/deploy-ncp-pull.sh` manages three fixed units: kiwoom,
  h3-crypto-paper and h3-us-paper. The tradingcodex-only approval-hash env
  override is gone with its unit.
- `ops/ncp/haproxy/haproxy.cfg.tmpl` drops the tailnet frontends for
  8768-8771 and 8773-8775.
- `docker-compose.prod.yml` drops `mcp-analysis-readonly`, `mcp-account-read`
  and `mcp-tradingcodex-execution`.
- Profiles `analysis_readonly`, `account_read`, `tradingcodex_execution`,
  `live-kr`, `live-us` and `live-crypto` are deleted, with their registrars,
  `config/mcp_profiles/live.yaml` and `scripts/live_mcp_tools_list.py`.
  Starting a server with one of those names fails like any unknown profile.
- `hermes-paper-kis` stays as profile code. The claude-mock and orch-mock
  lane contracts name it, the operator contract declares it for H3-KR, and it
  is the only home of the Task 881 tool `kis_mock_ledger_expire_day_orders`.
  Only its NCP unit is retired.
- The #1003 live-prompt contract
  (`tests/mcp_server/test_live_prompt_profile_contract.py`) now checks the
  live prompts against the DEFAULT registration, which is what live sessions
  run on. All three lanes pass with no known gap, so the test is strict and no
  longer xfail. A later tool cleanup that unregisters a prompt-required tool
  fails it.
- Low-traffic units defer heavy imports (§4).

## 3. Desk runbook (NCP)

Run these on the NCP host. Never print a token or an env value: the commands
below only count matches. `UNITS` below is the stop order, lowest risk first:

    UNITS="account-read tradingcodex-execution paper-001 analysis-readonly live-crypto live-us live-kr"

### 3.0 Pre-checks (read-only)

1. Prefect must be in shared mode:

       grep -c '^KR_LIVE_MCP_MODE=dedicated' /root/at-secrets/.env.prefect-worker

   The result must be `0`. If it is not 0, stop: switch prefect back to shared
   first (robin-prefect `docs/runbooks/ncp-prefect-worker.md`).
2. Count the recent calls on each retired unit. ToolCallLogMiddleware writes
   one `mcp.tool.called` line per call, unsampled:

       for u in $UNITS; do
         printf '%s ' "$u"; docker logs --since 168h "at-mcp-$u" 2>&1 | grep -c 'mcp.tool.called'
       done

   Expect 0 or 1, except `analysis-readonly`, where the brief's Sentry sample
   shows recent use. To see which tools it served:

       docker logs --since 168h at-mcp-analysis-readonly 2>&1 | grep 'mcp.tool.called' | sed 's/.*tool=\([^ ]*\).*/\1/' | sort | uniq -c

   Before stopping `analysis-readonly`, remove or replace every local
   `auto_trader_readonly` entry (the desk owns this; see the DEFAULT warning in
   §1.1), then rerun the count to confirm no new calls. Any other unit showing
   more than a few calls means stop and report the unit and count to the desk.
3. Record the memory before the change:

       docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}' | grep at-mcp
       free -m

### 3.1 Stage 1: brown-out (stop, do not remove)

Disable restart so a host reboot does not bring them back:

    for u in $UNITS; do
      docker update --restart=no "at-mcp-$u" && docker stop "at-mcp-$u"
    done

Verify:

    docker ps -a --format '{{.Names}}\t{{.Status}}' | grep '^at-mcp-'
    for p in 8768 8769 8770 8771 8773 8774 8775; do
      printf '%s ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' --max-time 3 "http://100.122.100.56:$p/health"
    done   # 503 from HAProxy (backend down) is expected
    for p in 8765 8772 8776 8777; do
      printf '%s ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' --max-time 3 "http://100.122.100.56:$p/health"
    done   # all 200

Keep stage 1 for at least one full trading day for each market (KR, US and
crypto sessions). Rollback for stage 1 is immediate:

    for u in <unit>...; do docker update --restart=unless-stopped "at-mcp-$u" && docker start "at-mcp-$u"; done

### 3.2 Stage 2: deploy the merged change

Install the merged `scripts/deploy-ncp-pull.sh` and
`ops/ncp/haproxy/haproxy.cfg.tmpl` as described in
[ncp-pull-deploy.md](ncp-pull-deploy.md), then run the normal deploy with the
merged image tag:

    /root/at-run/deploy-ncp-pull.sh sha-<short7>

The script no longer starts, replaces, captures or rolls back the retired
units, and the rendered HAProxy has no frontend on their ports.

**The merged script refuses to deploy until stage 1 is done.** Removing a unit
from the deploy inventory and HAProxy closes neither its loopback listener nor
its restart policy, and `at-mcp-tradingcodex-execution` and the live units can
place orders. So a forward deploy checks every retired container before its
first mutation and exits 78 unless each one is absent, or stopped with restart
policy `no`. It also exits 78 when the daemon cannot answer. The refusal names
each unit and changes nothing. The check starts when the merged script is
installed on the host, and it then blocks every deploy, including unrelated
ones, so run stage 1 before installing the script. `--dry-run` prints each
unit's status without refusing:

    /root/at-run/deploy-ncp-pull.sh sha-<short7> --dry-run | grep '  retired '

The script never stops a unit itself, because consumers move first (§3.0
step 2). `--rollback` does not run the check, so an emergency rollback is never
blocked by it. If a stage 1 rollback restarts a unit, the next forward deploy
refuses until that unit is stopped and disabled again.

Verify:

    grep -c -E 'bind 100\.122\.100\.56:(8768|8769|8770|8771|8773|8774|8775)$' /root/at-run/haproxy.cfg   # 0
    for p in 8765 8772 8776 8777; do
      printf '%s ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' --max-time 3 "http://100.122.100.56:$p/health"
    done   # all 200

### 3.3 Stage 3: remove (no sooner than 7 days after stage 1)

    for u in $UNITS; do docker rm "at-mcp-$u"; done

The next deploy's image prune (#934) reclaims their old image once no
container uses it. The token names `MCP_ANALYSIS_READONLY_AUTH_TOKEN`,
`MCP_ACCOUNT_READ_AUTH_TOKEN`, `MCP_TRADINGCODEX_EXECUTION_AUTH_TOKEN`,
`MCP_PAPER_001_AUTH_TOKEN` and `MCP_LIVE_{KR,US,CRYPTO}_AUTH_TOKEN` are no
longer read. The operator may delete them from the env files and revoke them.

Record the memory after the change with the §3.0 step 3 commands.

### 3.4 Rollback after stage 2

`deploy-ncp-pull.sh --rollback` restores the previous image only for the
units the installed script manages, so it does not bring a retired unit back.
To restore one:

1. Revert the ROB-1436 PR on `main` (or check out its parent) and wait for the
   image build.
2. Install that revision's `scripts/deploy-ncp-pull.sh` and HAProxy template.
3. Make sure the unit's token name is still present in the env files.
4. Run `/root/at-run/deploy-ncp-pull.sh sha-<short7-of-the-revert>`. It starts
   the unit and renders its tailnet frontend again.

### 3.5 Follow-ups outside this repo

- robin-prefect-automations: remove the `KR_LIVE_MCP_MODE=dedicated` branch,
  `LIVE_MCP_ENDPOINTS` and `REP_MCP_PROFILES` or make them refuse loudly.
  Today, setting `dedicated` would point sessions at ports that no longer
  listen, and they would fail closed with `dedicated_mcp_config_invalid` or a
  connection error.
- auto_trader-operator:
  - The H3-KR declaration (`hermes-paper-kis`, 8771) has no unit to point at.
    Drop it, or give H3-KR a closed-world profile before it is rendered.
  - `scripts/e2_runner_preparation.py` names deleted registrar paths (§1.1).
- `/root/at-operator/.mcp.live-*.json`, if present on NCP, can be deleted.
- Local Claude configs: remove `auto_trader_readonly` (desk, §3.0 step 2).

## 4. Memory: lazy imports on the low-traffic units

`app/mcp_server/lazy_imports.py` makes pandas, numpy, yfinance,
exchange_calendars and finnhub lazy in the `kiwoom`,
`kiwoom_kr`, `h3-crypto-paper` and `h3-us-paper` processes. `main.py` installs
a placeholder module for each package before the tooling import. The real
package runs on its first attribute access, which is the first tool call that
uses it. DEFAULT and the other profiles stay eager. Tool output is unchanged.
To keep the boot path free of these packages, 15 modules that touched them at
import time were changed:

- They got `from __future__ import annotations`.
- Annotation-only `from pandas import DataFrame` moved under `TYPE_CHECKING`.
- One `pd.Timestamp(...).time()` constant became `datetime.time(9, 0)`.
- OpenDartReader now resolves on first use.

`tests/mcp_server/test_lazy_imports.py` fails if any of these packages runs at
boot on a lazy profile again.

Measured with `scripts/mcp_profile_import_rss.py --compare --repeat 3` (memory
after registration, then after the deferred packages are first touched). The
numbers come from a developer Mac with no server running and no tool called,
not from NCP, so read them as relative:

| Profile | Tools | Before (eager) at boot | After (lazy) at boot | After first use of the deferred packages |
|---|---:|---:|---:|---:|
| h3-us-paper | 20 | 221.0 MB | 175.1 MB | 220.8 MB |
| kiwoom | 134 | 221.0 MB | 175.0 MB | 220.8 MB |
| h3-crypto-paper | 20 | 221.2 MB | 175.1 MB | 220.8 MB |

Median of 3 fresh processes per mode, macOS physical footprint.

The saving is about 46 MB (21%) per unit at boot. The other 175 MB is the
interpreter, fastmcp, pydantic, SQLAlchemy and the app's own modules. All
profiles land on the same number because every profile imports the same
tooling modules, including 20-tool h3 units: `registry.py` imports every
registrar at module level.

What this buys: the saving holds until a tool first needs pandas. h3 runners
call `get_ohlcv`/`get_quote` early in a session, which load pandas, and after
that the unit holds the same memory as before. So the change cuts the boot
footprint and an idle unit's footprint, not a busy unit's steady state. The
steady-state options are in
[rob-1436-mcp-single-process-evaluation.md](../plans/rob-1436-mcp-single-process-evaluation.md).

Risk: if one of these packages fails to import, a lazy unit reports it on the
first tool call instead of at boot. DEFAULT runs the same image eagerly, so a
broken dependency still fails the deploy health check.
