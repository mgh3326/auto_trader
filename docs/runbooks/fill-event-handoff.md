# Fill-event handoff

This NCP timer transfers sanitized, evidence-backed fill rows into the market
operator briefing. It never calls a model or a broker/order/proposal/watch
mutation surface. Durable `session_context` is canonical; pane delivery and
early kickoff are best effort only.

## Install

After deploying an image containing this revision, on NCP install the versioned
units, create `/var/lib/fill-event-handoff` mode 0700, then enable the timer:

```bash
install -m 0644 ops/ncp/systemd/fill-event-handoff.{service,timer} /etc/systemd/system/
install -d -m 0700 /var/lib/fill-event-handoff
systemctl daemon-reload
systemctl enable --now fill-event-handoff.timer
```

`fill-event-handoff.service` reads the image digest already selected by
`/root/at-run/deployed-digest`; it runs that image with host networking and
bind-mounts only the state directory. Deployment remains an operator action.

The unit layers `/root/at-secrets/.env.api` first, then the mode-0600
`/root/at-secrets/.env.fill-handoff` override. The API file supplies the
standard required application Settings; the handoff file names only these
handoff values:
`DATABASE_URL`, `FILL_HANDOFF_STATE_DIR`, `FILL_HANDOFF_HERDR_TARGETS`,
`PREFECT_API_URL`, `FILL_HANDOFF_KICK_ENABLED`,
`FILL_HANDOFF_KICK_COOLDOWN_S`, `FILL_HANDOFF_KICK_DEPLOYMENTS`, and
`DISCORD_FILL_HANDOFF_WEBHOOK`. Do not put values in this repository.

The Docker invocation uses both files in that order, so a handoff-specific
value overrides `.env.api` without omitting the Settings values required while
the application imports. The timer first runs two minutes after boot, then
once per service activation interval.

Kickoff is disabled unless `FILL_HANDOFF_KICK_ENABLED=true`. Deployment mapping
is JSON, for example `{"crypto":"weekday-crypto-1420"}`. The service queries
Prefect by that name, then creates a run with the next REPS-derived rep and a
`YYYYMMDD-fill<ledger_id>` tag. Normal rep windows are the 30 minutes beginning
at every REPS start; no early kickoff occurs inside one. The default cooldown is
3600 seconds per market.

## State and recovery

`state.json` is atomically replaced under `fcntl.flock`. It retains a monotonic
watermark, 24-hour `(broker, broker_order_id, side, filled_qty, filled_price)`
dedupe evidence, and market kickoff cooldowns. A missing state file is an
installation boundary: the first ordinary `--once` run records the current
maximum ledger id and processes zero historical rows. It never backfills the
ledger by default.

For an intentional continuity seed from the retired Mac poller, its last
known watermark was `54646`. Before enabling the timer, run the same selected
image once with the same two `--env-file` arguments and state bind mount, but
append `--once --since-ledger-id 54646`; only rows with a later ledger id are
eligible. Use `--dry-run` to inspect that invocation without changing context,
state, pane delivery, kickoff, or Discord. Do not delete `state.json` as a
dedupe recovery shortcut: a websocket/reconciler pair has distinct ledger ids,
so preserve the 24-hour state evidence or seed a replacement watermark first.

Check a run with `journalctl -u fill-event-handoff.service -n 100`. A pane or
Prefect failure must not be repaired by deleting context entries; inspect the
next market briefing instead.
