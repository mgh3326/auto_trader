# H5-LS-ENV-v1 NCP manual playbook (demo account only)

Copy-paste procedure for the operator-started H5 Futures Demo runner on NCP:
account truth gate, alert-path check, start, T0 record, observation, planned
stop, incident response and rollback. Task 1251 (parent hk 1122). Companion to
[binance-h5-demo.md](binance-h5-demo.md), which describes what the runner does.

**This file grants no permission.** Merging it does not start H5. The separate
operator contract (`mock/contracts/h5-ls-env-v1.md` in auto_trader-operator)
and an explicit operator approval of the start are still required, and the
24h stop-observation and fault-notification owner must be named before the
start in §5.
Nothing here is for the live Binance host, the Spot Demo, testnet or any other
account: the runner talks to `https://demo-fapi.binance.com` only.

## 1. Rules that never bend

1. **The demo flags live on the one-off containers only.** Pass
   `BINANCE_H5_DEMO_ENABLED`, `BINANCE_FUTURES_DEMO_ENABLED` and
   `BINANCE_H5_ALERT_ENABLED` as `-e NAME=true` on the specific `docker run`
   that needs them (§4–§5). `docker run` applies `-e` after `--env-file`, so the
   value in the file loses; the runner refuses to start when it is not exactly
   `true`.
2. **Never edit `/root/at-secrets/.env.api`, or any other env file, to enable
   them.** Shared units (`at-api`, `at-worker`, the MCP units) read that file
   and must keep seeing the flags off. §3 records the file hash before and §8
   compares it after.
3. **No live broker key is needed or wanted.** The containers read the shared
   env file for `DATABASE_URL`, the Futures Demo key pair and
   `DISCORD_WEBHOOK_ALERTS` only. H5 code never references a live broker
   credential setting (pinned by a test). Do not add `--env-file` for any other
   file and do not put a secret value on a command line.
4. **No supervisor, scheduler or auto-restart.** No `--restart`, no `-d`, no
   cron entry, no systemd unit, no TaskIQ or Prefect registration. The runner
   lives in a foreground tmux pane that a person started.
5. **Stop order is watcher, then runner** (§7), and the runner is stopped with
   Ctrl-C. `docker stop` is a SIGTERM and raises the `stopped` alert by design.

## 2. Variables

Paste this block in every new shell or tmux pane before any command below.

```bash
image="$(cat /root/at-run/deployed-digest)"
env_file=/root/at-secrets/.env.api
```

`deployed-digest` is the image `scripts/deploy-ncp-pull.sh` last promoted (see
[ncp-pull-deploy.md](ncp-pull-deploy.md)). The deploy script manages only its
own named units. `at-h5-demo` and `at-h5-watch` are not among them, so a deploy
neither stops nor replaces them; a runner keeps the image it started with
until you stop it.

## 3. Preflight

All five checks must pass. Any failure means stop and report; do not improvise.

1. The deployed image contains all three scripts (a missing one means this
   change is not deployed yet):

```bash
docker run --rm --network host --env-file "$env_file" "$image" /app/.venv/bin/python -m scripts.binance_h5_demo --help
docker run --rm --network host --env-file "$env_file" "$image" /app/.venv/bin/python -m scripts.binance_h5_truth_gate --help
docker run --rm --network host --env-file "$env_file" "$image" /app/.venv/bin/python -m scripts.binance_h5_heartbeat_watch --help
```

**KNOWN BLOCKER (image):** `Dockerfile.api` copies `app`, `research_contracts`,
`scripts` and other roots but not `research/`, and all three scripts import
`research.nautilus_scalping.rob974_features` through the H5 modules. In an image
built from it each command above ends with `ModuleNotFoundError: No module named
'research'` before argparse runs. Do not work around it (no bind mount, no
`PYTHONPATH`, no run from a checkout). Stop and report to the director: the fix is
a Dockerfile change and a new deployed digest. A test pins this paragraph to the
Dockerfile and requires its removal once the image ships `research`.

2. The shared env file does not enable the lane. Expect only `=false` lines or
   no output; any `=true` means someone enabled it globally, so stop:

```bash
grep -E '^(BINANCE_H5_DEMO_ENABLED|BINANCE_FUTURES_DEMO_ENABLED|BINANCE_H5_ALERT_ENABLED)=' "$env_file"
```

3. Record the env file hash (compare in §8). The hash is not a secret:

```bash
sha256sum "$env_file"
```

4. No runner or watcher container is left over (expect no output):

```bash
docker ps -a --filter name=at-h5 --format '{{.Names}} {{.Status}}'
```

5. **Alert path test.** One test message goes to the ops Discord channel. Expect
   the line below, exit status 0, and a Discord message titled
   `H5 알림 채널 테스트 (장애 아님)`. `"delivered": false` means the webhook is
   unset or failing: fix it first, because §5 starts nothing without a working
   alert path.

```bash
docker run --rm --network host --env-file "$env_file" -e BINANCE_H5_ALERT_ENABLED=true "$image" /app/.venv/bin/python -m scripts.binance_h5_heartbeat_watch --send-test
```

```text
{"delivered": true, "event": "alert_test"}
```

## 4. Account truth gate (read-only, must PASS before start)

Reads the Futures Demo account with signed GETs only, the H5 state tables and
the shared Binance Demo ledger. It sends no order and writes no row. It needs
the two demo flags and `--confirm-demo`, like the runner.

```bash
docker run --rm --network host --env-file "$env_file" -e BINANCE_H5_DEMO_ENABLED=true -e BINANCE_FUTURES_DEMO_ENABLED=true "$image" /app/.venv/bin/python -m scripts.binance_h5_truth_gate --confirm-demo
```

Expect exit status 0 and one JSON line containing `"verdict": "PASS"` with six
passing checks. Exit status 2 prints `"verdict": "FAIL"`: do not start. A failed check
names its cause in `detail` (for example `foreign account asset exposure`).
Paste the whole line into the start record either way.

| check | passes when | on FAIL |
| --- | --- | --- |
| `account_isolated_1x` | account trades, single-asset margin, no foreign asset, BTCUSDT/ETHUSDT/SOLUSDT isolated 1x BOTH | set margin and leverage in the Binance demo UI; this lane never changes them |
| `one_way_position_mode` | one-way mode | switch to one-way in the demo UI |
| `positions_flat` | every position is zero | something else holds a position (the scalping bot or a manual trade); the lane needs a flat account for attribution |
| `no_open_orders` | no open order | cancel it by hand in the demo UI after finding its owner |
| `h5_state_empty` | no active H5 signal, no unresolved intent | a previous run left state; this is not a first start (see §9) |
| `demo_ledger_no_open_roots` | no open root in `binance_demo_order_ledger` | an earlier lane left an open lifecycle; resolve it through its own runbook |

## 5. Start

Approval to start must already be on record. Start the runner first, then the
watcher, each in its own tmux session.

**Runner.** Open the session, paste the §2 variables, then the command:

```bash
tmux new-session -s h5-runner
```

```bash
docker run --name at-h5-demo --network host --env-file "$env_file" -e BINANCE_H5_DEMO_ENABLED=true -e BINANCE_FUTURES_DEMO_ENABLED=true -e BINANCE_H5_ALERT_ENABLED=true "$image" /app/.venv/bin/python -m scripts.binance_h5_demo --loop --confirm-demo
```

Detach with Ctrl-b d. The container has a fixed name, so a second runner cannot
start while the first exists. There is no `--rm`: the logs of a dead runner
stay available to `docker logs` until you remove the container.

**Watcher.** After the runner printed its first tick line (§6), in a second
session:

```bash
tmux new-session -s h5-watch
```

```bash
docker run --name at-h5-watch --network host --env-file "$env_file" -e BINANCE_H5_ALERT_ENABLED=true "$image" /app/.venv/bin/python -m scripts.binance_h5_heartbeat_watch --loop --miss-minutes 10 --poll-seconds 60
```

The watcher reads `review.binance_h5_lane_state.updated_at`, which the runner
stamps at the start of every tick, and needs no Binance credential. It alerts
once when the stamp is older than 10 minutes. It is the only path that reports
a SIGKILL, an OOM kill or a lost host.

## 6. Verify the start, record T0 and the correlation id

```bash
docker ps --filter name=at-h5 --format '{{.Names}} {{.Status}}'
docker logs --tail 5 at-h5-demo
docker logs --tail 3 at-h5-watch
```

Both containers `Up`. The runner prints one JSON line per minute, for example
`{"decision_ts": 1790000000000, "detail": null, "event": "no_entry", "signal_keys": []}`.
An `event` of `blocked`, `entry_uncertain` or `close_uncertain` is a failure:
go to §9 now. The watcher prints `"verdict": "ok"` lines.

Prove the flags are scoped to these containers. The first command shows three
`=true` lines; the second shows `=false` or nothing; the hash equals §3:

```bash
docker inspect at-h5-demo --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^(BINANCE_H5_DEMO_ENABLED|BINANCE_FUTURES_DEMO_ENABLED|BINANCE_H5_ALERT_ENABLED)='
docker inspect at-worker --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^(BINANCE_H5_DEMO_ENABLED|BINANCE_FUTURES_DEMO_ENABLED|BINANCE_H5_ALERT_ENABLED)='
sha256sum "$env_file"
```

**T0.** The contract defines T0 as the first operator-started H5 runner
process. Record the container start time immediately; it is also the
`--t0` value for `scripts/binance_h5_weekly_score.py`. Never redefine T0 after a
failed or restarted run; a changed T0 is a director decision.

```bash
docker inspect -f '{{.State.StartedAt}}' at-h5-demo
docker inspect -f '{{.Config.Image}}' at-h5-demo
```

**Correlation id.** The lane identity is `H5-LS-ENV-v1` and its correlation
prefix is `binance-h5:`. Per-signal ids exist only once a signal does: each tick
line with an `entry_sent` event lists `signal_keys`, and the correlation id of
one key is `binance-h5:` plus the first 24 hex characters of its SHA-256:

```bash
printf '%s' "$signal_key" | sha256sum | cut -c1-24
```

Write into the hk record named in the hk 1122 thread: T0 (UTC), the image
reference above, the exact truth-gate line, the env file hash before and after,
the alert test output, the lane identity and prefix above, and later each
signal key with its correlation id. Then notify strategy-lab: T0 starts the
five-trading-day error-free signal count of #917.

## 7. Observation and planned stop

For the first 24 hours the observation owner named before the start watches Discord and, at least at
each coin session, runs:

```bash
docker logs --since 1h at-h5-demo
docker logs --tail 3 at-h5-watch
```

**H5 has no exchange-side stop.** While a position is open, the runner is the
only thing that closes it at -5%, -3%, six bars or 24 hours. A dead, hung or
blocked runner means the stop is not being observed.

Planned stop, in this order:

1. `tmux attach -t h5-watch`, Ctrl-C. The watcher exits 0.
2. `tmux attach -t h5-runner`, Ctrl-C. The runner exits 130 and sends no alert.
3. Check and clean up:

```bash
docker inspect -f '{{.State.ExitCode}}' at-h5-demo
docker rm at-h5-demo at-h5-watch
tmux kill-session -t h5-watch
tmux kill-session -t h5-runner
```

4. Run the truth gate (§4). If it shows an open H5 position the runner is the
   only thing that can manage it: restart per §9, or close it by hand in the
   demo UI. Do not leave it unattended.

Stopping the runner first leaves the watcher running; it will send one
`heartbeat_missed` alert after 10 minutes. That is expected, not an incident.

## 8. Rollback

Rollback is the planned stop in §7 plus one check; there is nothing to revert in
any env file, unit or schedule, because none was touched.

```bash
sha256sum "$env_file"
```

The hash must equal the §3 value. The H5 tables and ledger rows stay as audit
evidence; nothing here drops or edits them. Do not set a new T0 or delete H5
state to "start clean": that decision belongs to the director.

## 9. Incident response

Each alert names its kind. All three mean stop observation is degraded.

| kind | meaning | first look |
| --- | --- | --- |
| `stopped` | the runner ended and the operator did not ask for it (an exception escaped, SIGTERM, cancellation) | `docker ps -a --filter name=at-h5-demo`, `docker logs --tail 50 at-h5-demo`, exit code |
| `error` | a tick ended `blocked`, `entry_uncertain` or `close_uncertain`; the runner keeps retrying each minute but entries, and often holding management, are suspended | `docker logs --tail 20 at-h5-demo` |
| `heartbeat_missed` | no tick stamp for 10 minutes: the runner is dead or hung | `docker ps --filter name=at-h5 --format '{{.Names}} {{.Status}}'`, then the logs |

Steps, in order:

1. Run the truth gate (§4). After an incident it normally FAILs on
   `positions_flat` or `h5_state_empty` when H5 holds a position; read the
   detail. Foreign exposure or an unknown order means stop and report.
2. If the log or state shows `entry_uncertain`, `close_uncertain` or
   `uncertain H5 exposure`, do **not** restart. The runner blocks new, repeated
   and duplicate orders until broker evidence resolves the response. Report to
   the director with the log lines.
3. If the runner is only dead or hung and nothing is uncertain, remove the dead
   container and start again from §5 (the runner reconciles positions, open
   orders and its own client order ids before acting; a not-found response never
   triggers a resend). A hung runner that ignores Ctrl-C: `docker stop at-h5-demo`,
   then `docker kill at-h5-demo` if it is still `Up`.
4. Record what happened in the hk record. Keep T0.

An alert burst is bounded: one message per failure episode per kind (tick
errors are one episode per tick event, so alternating exception classes inside
one outage stay one message), a reminder at most every six hours while it
persists, and nothing when `BINANCE_H5_ALERT_ENABLED` is not exactly `true`.
Tick alerts are sent in the background, so a slow webhook never delays the next
tick.

## 10. Known limits

- The watcher is not itself watched. If both it and the runner die, no alert
  is sent. The `docker logs` check at each coin session is the backstop.
- The heartbeat is the stamp written at the start of each tick. It proves recent
  NAV and state activity, not completed stop management; a blocked tick can still
  advance it, which is why `error` is a separate alert. A tick that legitimately
  runs longer than the miss window would raise a false `heartbeat_missed`. The
  longest tick is not measured here: watch the first 4h-boundary tick (33
  history page reads) and widen `--miss-minutes` if needed.
- The watcher's own read has a 15 second deadline; a stalled read is reported as
  `unreadable`, not left waiting.
- No MCP tool reports H5 runner health to the coin session, so the operator
  routine cannot read it yet. A read-only tool over `review.binance_h5_lane_state`
  is the follow-up from task 1251.
- The runner polls once a minute and sees one-minute extremes only; a stop can
  slip past its threshold (see [binance-h5-demo.md](binance-h5-demo.md)).
- An exchange-side hard stop is tracked separately as #1076 (E2) and is not
  part of this playbook.
