# Dedicated live-* MCP servers on NCP (task 975)

Operator Q-87 A (2026-09-29) approved three production MCP containers, one per
live market, each serving exactly one closed-world profile from
`config/mcp_profiles/live.yaml` (see `live-mcp-profiles.md` for what the
profiles contain). This runbook is the desk procedure: tokens, deploy,
read-only smoke, the robin-prefect-automations switch that moves live
sessions onto them, and rollback. Merging changes nothing for running
sessions; the deploy adds the containers, and sessions move only when desk
flips the prefect switch.

## Units

| Unit | MCP_PROFILE | Container port (loopback) | HAProxy frontend (tailnet only) | Token env name |
| --- | --- | ---: | --- | --- |
| at-mcp-live-kr | live-kr | 127.0.0.1:8773 | 100.122.100.56:8773 | MCP_LIVE_KR_AUTH_TOKEN |
| at-mcp-live-us | live-us | 127.0.0.1:8774 | 100.122.100.56:8774 | MCP_LIVE_US_AUTH_TOKEN |
| at-mcp-live-crypto | live-crypto | 127.0.0.1:8775 | 100.122.100.56:8775 | MCP_LIVE_CRYPTO_AUTH_TOKEN |

Ports checked for collisions (repo-wide in auto_trader, robin-prefect-automations,
auto_trader-operator, fillwire, go-kis, handoffkeep, herdr): 8000 (HAProxy API),
8001/8002 (API colors), 8765 (HAProxy main MCP), 8766/8767 (MCP colors),
8768-8772 (fixed profiles analysis-readonly, account-read,
tradingcodex-execution, paper-001, kiwoom). 8773-8775 had no user. The live
host listener table was not inspected; step 2 below checks it.

Like the other fixed units they run `--network host` with the two deploy
`--env-file`s, `MCP_HOST=127.0.0.1`, `MCP_TYPE=streamable-http`,
`MCP_PATH=/mcp`, and their own bearer token passed as `MCP_AUTH_TOKEN`. Since
this change a live-* profile refuses to boot on a network transport without a
token. HAProxy is the only tailnet listener; there is no loopback or public
frontend for these ports, and the deploy refuses to render a config with any
bind other than `127.0.0.1:<port>` or `100.122.100.56:<port>`.

The deploy treats them like every other fixed MCP unit: digest-pinned from the
same image, logged before replacement, restored (or removed, if they did not
exist before) on any failure, reported in the digest table, and kept by the
#934 image prune while a container uses their image. After the MCP HAProxy
reload the deploy also polls each live unit's tailnet route
(`http://100.122.100.56:877x/health`) with the #2046 bounds; a route that never
answers fails the deploy and rolls everything back. `--skip-kis-ws` is
unaffected.

## Before you flip sessions: the tool surface narrows

A session on its dedicated server sees only its lane's manifest, further
filtered by the unchanged `LIVE_ALLOWED_TOOLS`. At the time of writing these
tools named in the live playbooks (`live/CLAUDE.md` and the lane prompts in
auto_trader-operator) are not served by the lane's profile, so a session would
not reach them:

- live-kr: analysis_artifact_get, get_fx_rate, get_news, get_orderbook,
  get_trading_policy, session_bootstrap_pack
- live-us: get_earnings_calendar, get_fx_rate, get_news, get_top_stocks,
  get_trading_policy, screen_stocks, toss_get_order_history,
  toss_reconcile_orders
- live-crypto: analyze_stock_batch, get_fx_rate, get_news, get_trading_policy,
  toss_get_order_history, toss_reconcile_orders

The emergency-group tools each profile serves (cancel/modify/reconcile/
watch void) are not in `LIVE_ALLOWED_TOOLS`, so `dontAsk` keeps denying them,
exactly as today. Resolving either list is an operator decision (a live.yaml
PR or a playbook change); this runbook does not change them.

## Desk steps

Never print a token value. Where a command needs one, it reads it into a
shell variable and unsets it afterwards.

1. **Create three tokens and add them by name** to the deploy secrets file
   (`/root/at-run/.env.secrets`, or whatever `AT_SECRETS_ENV_FILE` points to)
   and to the operator session env (`/root/at-secrets/.env.operator-session`),
   same value in both files per name:

       MCP_LIVE_KR_AUTH_TOKEN
       MCP_LIVE_US_AUTH_TOKEN
       MCP_LIVE_CRYPTO_AUTH_TOKEN

   Use three distinct fresh values (for example from `openssl rand -hex 32`
   written straight into the file). Check presence without printing:

       for n in MCP_LIVE_KR_AUTH_TOKEN MCP_LIVE_US_AUTH_TOKEN MCP_LIVE_CRYPTO_AUTH_TOKEN; do
         grep -c "^${n}=." /root/at-run/.env.secrets /root/at-secrets/.env.operator-session
       done

   From the first deploy of this change on, `deploy-ncp-pull.sh` exits 78
   before pulling anything while any of the three is missing. To ship an
   unrelated deploy before the tokens exist, run it with
   `MCP_UNITS_SKIP=live-kr,live-us,live-crypto`.

2. **Confirm the ports are free on the host** (read-only):

       ss -ltnH | awk '{print $4}' | grep -E ':(8773|8774|8775)$' || echo free

3. **Install the versioned script and template** from the merged commit, as in
   `ncp-pull-deploy.md` (the script renders
   `ops/ncp/haproxy/haproxy.cfg.tmpl` from its own checkout):

       install -m 0750 scripts/deploy-ncp-pull.sh /root/at-run/deploy-ncp-pull.sh

4. **Dry run**, then deploy. The dry run lists `at-mcp-live-kr/-us/-crypto`
   with `current: absent` on the first rollout:

       /root/at-run/deploy-ncp-pull.sh --dry-run
       /root/at-run/deploy-ncp-pull.sh

   Order inside the run: API color, worker, scheduler and WebSocket
   monitors, the inactive MCP color, the fixed units (analysis-readonly ...
   kiwoom, then live-kr, live-us, live-crypto; each waits for its loopback
   `/health`), then HAProxy render and SIGHUP (this is the HAProxy reload;
   no separate step), the main and API route checks, the three live tailnet
   route checks, and only then the active-color write. The final digest
   table must show `MATCH` for all three live units.

5. **Read-only smoke** (tools/list on each endpoint, no tool calls), run from
   the deployed image with the deploy's own env files:

       image="$(cat /root/at-run/deployed-digest)"
       docker run --rm --network host \
         --env-file /root/at-run/.env.runtime --env-file /root/at-run/.env.secrets \
         "$image" /app/.venv/bin/python -m scripts.live_mcp_tools_list

   Expect one line per profile with `MATCH` (or `MATCH_GATES_OFF` when that
   unit's env has `ORDER_PROPOSALS_ENABLED` off, in which case the
   `order_proposal_*` tools are absent and a session could not create
   proposals: fix the env before flipping). Exit status 0 means all three
   passed; `MISMATCH` or `ERROR` exits 1. Plain liveness through HAProxy:

       for p in 8773 8774 8775; do curl -fsS -o /dev/null -w "$p %{http_code}\n" http://100.122.100.56:$p/health; done

6. **Render the three session client configs** on NCP, mode 0600, beside the
   operator checkout's `.mcp.json`. The server name must stay
   `auto_trader_local` (LIVE_ALLOWED_TOOLS is keyed on it):

       umask 077
       for spec in kr:8773:MCP_LIVE_KR_AUTH_TOKEN us:8774:MCP_LIVE_US_AUTH_TOKEN crypto:8775:MCP_LIVE_CRYPTO_AUTH_TOKEN; do
         IFS=: read -r market port name <<<"$spec"
         tok="$(sed -n "s/^${name}=//p" /root/at-secrets/.env.operator-session | tail -n 1)"
         printf '{"mcpServers":{"auto_trader_local":{"type":"http","url":"http://100.122.100.56:%s/mcp","headers":{"Authorization":"Bearer %s"}}}}\n' "$port" "$tok" >"/root/at-operator/.mcp.live-${market}.json"
         unset tok
       done
       ls -l /root/at-operator/.mcp.live-*.json

   (`printf` is a shell builtin, so the value never appears in a process
   list.) A follow-up operator-repo PR can add these three files to
   `scripts/render_mcp_configs.py`; until then rerun this after a rotation.
   The prefect switch refuses to spawn when the rep's file is missing, not a
   regular file, readable by group/others, not JSON, still contains a
   `${...}` placeholder, defines any server other than exactly
   `auto_trader_local`, points at a different URL than its market's
   frontend, or lacks a bearer header.

7. **Flip the prefect switch** (robin-prefect-automations
   `KR_LIVE_MCP_MODE`; unset or `shared` = today's behavior, `dedicated` =
   per-market server). Worker-wide, durable across deployment
   re-registration:

       echo 'KR_LIVE_MCP_MODE=dedicated' >>/root/at-secrets/.env.prefect-worker
       systemctl restart at-prefect-worker.service

   The next spawned rep passes `--mcp-config /root/at-operator/.mcp.live-<market>.json
   --strict-mcp-config` (KR reps and `smoke` use live-kr, `us-2235`
   live-us, the four `crypto-*` reps live-crypto). Sessions already running
   are not affected. To canary one rep first, add
   `KR_LIVE_MCP_MODE=dedicated` to only that deployment's job-variable env
   in the Prefect UI (for example `weekday-crypto-0220`) instead; a later
   re-registration of the kickoff deployments resets job variables, so use
   the worker env for the lasting switch. Run the `smoke` rep once to
   confirm a session starts, then watch the first real rep.

## Rollback

- **Sessions only** (most cases): remove the `KR_LIVE_MCP_MODE` line (or set
  it to `shared`) and `systemctl restart at-prefect-worker.service`. The next
  spawn reads `.mcp.json` again. The live containers can keep running
  unused.
- **A failed deploy** rolls itself back (containers, HAProxy config, color
  files, digest records). To deploy other changes while a live unit is being
  investigated, run with `MCP_UNITS_SKIP=live-kr,live-us,live-crypto`
  (skipped units keep their current container and image).
- **Previous image**: `/root/at-run/deploy-ncp-pull.sh --rollback` redeploys
  the previous digest to every unit, the live units included.
- **Removing the units entirely**: first roll sessions back as above, then
  `docker rm -f at-mcp-live-kr at-mcp-live-us at-mcp-live-crypto`, then
  install and run the deploy script from a commit without them so the
  rendered HAProxy config drops the three frontends. A script that predates
  this change does not know these containers and would leave them running.
- **Tokens**: after the units are gone, delete the three names from both env
  files and remove `/root/at-operator/.mcp.live-*.json`.
