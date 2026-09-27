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

## Kick priority filter and daily cap

Every fill is still queued as a `session_context` `open_question` before any
delivery decision; the filter below only changes whether an early Prefect
kickoff is attempted. With `FILL_HANDOFF_KICK_ENABLED` unset or false the
runner behaves exactly as before — no ledger position read, no filter refs, no
cap state.

When kicks are enabled, a fill is kick-eligible only when it is one of:

- `sell_full_exit` — a sell whose ledger position after the fill is zero;
- `buy_new_position` — a buy whose ledger-proven position before the fill is
  exactly zero (a proven-negative balance is an inconsistent ledger view, so
  it classifies `position_unproven` and never kicks);
- `partial_fill_ge_25pct` — a fill covering at least
  `FILL_HANDOFF_KICK_MIN_POSITION_FRACTION` (default `0.25`) of the position
  before the fill.

Queue-only classes are `parking_etf` (configured parking symbols),
`small_dca_buy` (buy notional below the per-currency floor),
`buy_add_below_25pct`, `sell_partial_below_25pct`, `position_unproven` (the
ledger has no earlier rows for the fill's exact key, so zero cannot be proven
— a proven-negative balance is likewise unproven rather than flat),
`position_read_failed` (the position read itself failed — never a guess),
`fill_malformed` (bad side/quantity/notional, a missing or timezone-naive
`filled_at`, or a magnitude that overflows decimal arithmetic),
`unsupported_market` (the fill's market or instrument type is outside
kr/us/crypto — e.g. a `forex` row cannot be an `open_question`, so it is
recorded under `skipped` and the watermark still advances rather than wedging
the batch), and `classification_failed` (the classifier itself raised). A row
that cannot even be sanitized lands in `skipped` with `sanitize_failed` for the
same reason. Position facts come from an execution-ledger read of net
signed quantity strictly before the fill, keyed by
broker/account-mode/venue/instrument-type/symbol/currency and ordered by
`(filled_at, id)`; opening-lot `manual_import` rows count.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `FILL_HANDOFF_KICK_DAILY_CAP` | `2` | Maximum Prefect kickoffs per market per KST day, on top of the cooldown and rep-window rules. `0` disables kicks but keeps the classification refs. |
| `FILL_HANDOFF_KICK_PARKING_SYMBOLS` | `SGOV,BIL,459580,357870` | Comma-separated queue-only parking symbols. |
| `FILL_HANDOFF_KICK_SMALL_BUY_NOTIONAL` | `{"KRW":5000,"USD":5}` | JSON currency-to-notional map; buys below the floor for their currency are queue-only DCA. |
| `FILL_HANDOFF_KICK_MIN_POSITION_FRACTION` | `0.25` | Minimum fraction of the pre-fill position a partial fill must cover to kick. |

At most `FILL_HANDOFF_KICK_DAILY_CAP` kicks fire per market per KST day; the
counter lives in `state.json` under `kick_days` and resets on the KST date
change. An eligible fill over the cap records class `capped` and keeps its
open question for the next regular rep, which consumes the queue as before.
The cap slot and cooldown are persisted to `state.json` *before* the Prefect
`create_flow_run` call, so a crash mid-issue over-counts rather than exceeds
the cap; an ambiguous failure keeps the reservation, while a completed
response without a run id releases it.

Each queued fill records `refs.kick_filter_class` (`kick`/`queue_only`),
`refs.kick_filter_reason` (the class or deferral reason), and
`refs.position_before`/`refs.position_after` when a position was read. The run
JSON gains a `decisions` list (only when kicks are enabled) with one entry per
fill: `class` (`kick`/`queue_only`/`capped`), `reason`, and `flow_run_id` for
actual kickoffs. This feeds the two-week measurement of queue count, kick
count, consumption delay, and unresolved count.

## State and recovery

`state.json` is atomically replaced under `fcntl.flock`. It retains a monotonic
watermark, 24-hour `(broker, broker_order_id, side, filled_qty, filled_price)`
dedupe evidence, market kickoff cooldowns, and per-market `kick_days` daily
counters. A missing state file is an
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
