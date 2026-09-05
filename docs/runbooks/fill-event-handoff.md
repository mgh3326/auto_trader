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
standard required application Settings; the handoff file names these standard
handoff values (and the optional lane-event values described below):
`DATABASE_URL`, `FILL_HANDOFF_STATE_DIR`, `FILL_HANDOFF_HERDR_TARGETS`,
`PREFECT_API_URL`, `FILL_HANDOFF_KICK_ENABLED`,
`FILL_HANDOFF_KICK_COOLDOWN_S`, `FILL_HANDOFF_KICK_DEPLOYMENTS`, and
`DISCORD_FILL_HANDOFF_WEBHOOK`. Lane-event settings are described below. Do not
put values in this repository.

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

## `lane_event` mode

`FILL_HANDOFF_LANES` is optional. When it is unset or empty, fill handoff uses
the existing herdr pane discovery and Prefect behavior unchanged. When it is a
JSON mapping for a market, for example `{"crypto":"lane-a"}`, the runner first
persists the canonical `session_context` open question and then emits a durable
panewire `lane.event`. Only `crypto`, `kr`, and `us` are accepted mapping keys;
every lane value must be a valid panewire lane name.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `FILL_HANDOFF_LANES` | unset (empty map) | JSON market-to-lane map; enabling one market does not enable the others. |
| `FILL_HANDOFF_EMIT_BIN` | `panewire` | Panewire executable to invoke. |
| `FILL_HANDOFF_EMIT_HOST` | `socket.gethostname()` | Producer host name supplied to panewire. |
| `FILL_HANDOFF_EMIT_PANE` | empty | Optional producer pane; empty omits `--pane`. |
| `FILL_HANDOFF_EMIT_INBOX_ROOT` | `~/work/herdr-inbox` | Panewire inbox root. |
| `FILL_HANDOFF_EMIT_TIMEOUT_S` | `3` | Subprocess timeout in seconds; it must be greater than 1. |

The invocation has a fixed token order. With no pane it is:

```sh
panewire emit --kind lane.event --lane lane-a --event-id <event_key> --text <text> --host host-a --inbox-root <root> --timeout 2s
```

With a producer pane, `--pane w1:p1` is placed immediately after `--host
host-a` and immediately before `--inbox-root`. `event_key` is the event ID:
it is the stable idempotency key shared with the durable fill handoff, so this
runner never generates a new UUID for a retry.

A return code of 0 is delivered, including panewire's file-only success when
its local daemon is unavailable. A return code of 2 is a successful duplicate
only when stderr says `duplicate event_id`; that means the durable event was
already recorded and must not be sent to herdr again. Other emit failures add
one reason to the JSON `fallback` list and retain the original order:

```text
panewire emit failure → herdr pane discovery/direct prompt → Prefect kickoff when no pane is found
```

`fallback` reasons are `binary_not_found`, `timeout`, `usage`, `os_error`,
`exit_<rc>`, `empty_text`, `invalid_lane`, and `invalid_event_id`. Repeated
reasons are intentionally retained so operators can see repeated failures.

Example normal output (key ordering follows the CLI's sorted JSON output):

```json
{"duplicate":0,"durable":1,"fallback":[],"kicked":0,"pushed":1}
```

For `--dry-run`, lane emission is skipped along with context, state, pane,
kickoff, and Discord writes; its output can therefore be:

```json
{"duplicate":0,"durable":0,"fallback":[],"kicked":0,"pushed":0}
```

Do not set `FILL_HANDOFF_EMIT_TIMEOUT_S` to one second or less. The panewire
`--timeout` is deliberately one second shorter than the subprocess timeout, so
panewire returns after its file write before this process can fall back to
herdr and risk dual delivery.

NCP timer installation, environment configuration, and lane registration are
outside this PR and remain operator actions.

Check a run with `journalctl -u fill-event-handoff.service -n 100`. A pane or
Prefect failure must not be repaired by deleting context entries; inspect the
next market briefing instead.
