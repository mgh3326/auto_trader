# Fill-event handoff

This NCP timer transfers sanitized, evidence-backed fill rows into the market
operator briefing. It never calls a model or a broker/order/proposal/watch
mutation surface. Durable `session_context` is canonical; pane delivery and
early kickoff are best effort only.

## Install

After deploying an image containing this revision, on NCP install the versioned
units, create `/var/lib/fill-event-handoff` mode 0700 owned by uid/gid 10001,
then enable the timer:

```bash
install -m 0644 ops/ncp/systemd/fill-event-handoff.{service,timer} /etc/systemd/system/
install -d -m 0700 -o 10001 -g 10001 /var/lib/fill-event-handoff
systemctl daemon-reload
systemctl enable --now fill-event-handoff.timer
```

`fill-event-handoff.service` reads the image digest already selected by
`/root/at-run/deployed-digest`; it runs that image with host networking and
bind-mounts only the state directory. The container process runs as `appuser`
uid 10001 (`Dockerfile.api` line 63), so the state directory must be owned by
10001:10001 — a root-owned mode-0700 directory raises `PermissionError` on the
first state write. The numeric IDs are required because `appuser` exists only
inside the image, not on the NCP host. Deployment remains an operator action.

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

## Watch-event kicks (#865)

The same `FILL_HANDOFF_KICK_ENABLED` switch governs delivered watch events.
Delivered `investment_watch_events` rows are still bundled into lane events by
the bundle runner exactly as before — the watch pass below only decides
whether an event may also consume an early kickoff slot. It runs inside the
same `FillHandoffRunner` poll, after the fill loop, and draws from the *same*
`kick_days`/`cooldowns` state, so fill kicks plus watch kicks together can
never exceed `FILL_HANDOFF_KICK_DAILY_CAP` per market per KST day.

A delivered watch event is kick-eligible only when **all** of:

- `action_mode == "approval_required"` — exact canonical spelling only
  (`notify_only`, `preview_only`, `auto_execute_mock`, missing or
  unrecognized modes are queue-only; a case-variant or whitespace-padded
  spelling is `action_mode_malformed`, never an authorization);
- `intent == "buy_review"` (exact spelling) **or** the source alert's
  `max_action.side` is present (the alert row is LEFT JOINed for
  `max_action`; a deleted alert or a non-mapping value classifies
  `max_action_unavailable` — never a guess);
- the market is inside its tradable session at evaluation time (XKRX/XNYS
  trading minutes for kr/us; crypto is always tradable) — a clock input
  that is not a timezone-aware datetime fails closed for every market;
- `delivered_at` is fresher than the 24-hour dedupe window — an older row
  is `stale_event`, rides the next regular rep, and can never kick, which
  also means an expired `watchkick:<id>` replay mark cannot reopen a
  crash-replay double-kick.

Ladder fires of the same `(market, symbol)` within one poll are a single kick
candidate: the first eligible event in delivery order attempts the gate, and
later eligible rungs are recorded `queue_only`/`ladder_grouped`. Bundle
dedupe stays per `idempotency_key`, so every rung still appears in the bundle
payload; only the kick candidacy groups by symbol. Each event's classification
is recorded in `watch_decisions` with `event_id`, `market`, `symbol`,
`action_mode`, `filter` (classifier reason), `class`
(`kick`/`queue_only`/`capped`), `reason`, `flow_run_id`, and a `dry_run` flag
under `--dry-run`; `watch_kicked` counts real kickoffs and `watch_errors`
records read/seed failures (`watch_read_failed`, `watch_high_watermark_failed`,
`watch_cursor_corrupt`, `event_malformed`). The Prefect `date_tag` is
`YYYYMMDD-watch<event_id>`.

The kick pass keeps its own `(delivered_at, event_id)` cursor under
`watch_kick_watermark`/`watch_kick_delivered_at` in `state.json`, independent
of the bundle runner's watch cursor. On a fresh state file — or an upgrade
onto a pre-#865 one — the cursor seeds to the delivered high-water mark and
processes nothing, so historical watch fires never replay as kicks. The watch
re-judgement spawner (`app/services/watch_trigger_repricing`, ROB-1286/1304)
is **not** armed, scheduled, or called by this path; watch kicks reuse the
existing Prefect deployment kickoff surface only.

## State and recovery

`state.json` is atomically replaced under `fcntl.flock`. It retains a monotonic
watermark, 24-hour `(broker, broker_order_id, side, filled_qty, filled_price)`
dedupe evidence, market kickoff cooldowns, per-market `kick_days` daily
counters, and the watch-kick cursor pair
`watch_kick_watermark`/`watch_kick_delivered_at`. A missing state file is an
installation boundary: the first ordinary `--once` run records the current
maximum ledger id and delivered watch mark and processes zero historical rows.
It never backfills the ledger or the watch log by default.

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

## Bundled runner shadow mode (#931)

`scripts/fill_handoff_bundle.py` is the bundled pull that supersedes the
per-row handoff above: it reads fill rows and delivered watch rows in one
pass, groups them into per-market bundles, runs the BROKER_RISK detector, and
classifies kicks. `FILL_EVENT_HANDOFF_SHADOW=1` runs that entire evaluation
path while recording what it *would* have done — no lane events, no Telegram
risk pushes, no herdr messages, no Prefect flow runs, nothing external.
Operator approval Q-70 (2026-09-29) calls for two weeks of shadow observation
before the bundled handoff is enabled.

Shadow wins over `FILL_EVENT_HANDOFF_ENABLED`: with both set the transports
are still forced to the Null implementations and `TradeNotifierRiskPush` is
never even constructed. `--since-fill-id`/`--since-watch-id` behave as in the
enabled path.

### Desk command (NCP)

Create the dedicated state directory, then run the same selected image on a
five-minute cadence during KR/US regular hours (crypto rows evaluate whenever
the run fires; the cadence only decides freshness):

```bash
install -d -m 0700 -o 10001 -g 10001 /var/lib/fill-handoff-bundle-shadow
image="$(cat /root/at-run/deployed-digest)"
docker run --rm --network host \
  --env-file /root/at-secrets/.env.api \
  --env-file /root/at-secrets/.env.fill-handoff \
  -e FILL_EVENT_HANDOFF_SHADOW=1 \
  -v /var/lib/fill-handoff-bundle-shadow:/var/lib/fill-handoff-bundle-shadow \
  "$image" /app/.venv/bin/python -m scripts.fill_handoff_bundle
```

The state directory is bind-mounted into the container, which runs as
`appuser` uid 10001 (`Dockerfile.api` line 63), so it must be owned by
10001:10001 — a root-owned mode-0700 directory raises `PermissionError` on the
first state write. Use the numeric IDs: `appuser` exists only inside the
image, not on the NCP host.

A five-minute systemd timer or cron entry wrapping that invocation is the
cadence lever; this change registers no scheduler.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `FILL_EVENT_HANDOFF_SHADOW` | unset (off) | `1`/`true`/`yes`/`on` enables the observation-only shadow. Wins over `FILL_EVENT_HANDOFF_ENABLED`. |
| `FILL_HANDOFF_BUNDLE_SHADOW_STATE_DIR` | `/var/lib/fill-handoff-bundle-shadow` | Shadow state directory; `--state-dir` overrides. |
| `FILL_EVENT_HANDOFF_ENABLED` | unset (off) | Live gate; under shadow it only shows up in the outcome JSON as `enabled`. |
| `FILL_HANDOFF_LANES` | unset (empty map) | Same JSON market-to-lane map; shadow parses it identically but delivers nothing. |
| `FILL_HANDOFF_KICK_*`, `PREFECT_API_URL` | as documented above | Shadow mirrors the same knobs so `kick`/`queue_only`/`capped` counts answer "what the live gate would have done". These envs are only parsed under shadow, so a malformed kick value can never break a production run. |

Shadow keeps its own `state.json` in the shadow directory — production
watermarks are never advanced by a shadow run, and three fail-closed guards
keep the worlds apart before the state file is even opened for writing:
shadow refuses `/var/lib/fill-handoff-bundle`, `/var/lib/fill-event-handoff`,
and whatever `FILL_HANDOFF_BUNDLE_STATE_DIR`/`FILL_HANDOFF_STATE_DIR`
currently resolve to; shadow refuses a directory whose existing state was
written by a non-shadow run (no `shadow` marker); the production runner
refuses a directory already marked `shadow`. A mispointed run is a loud
failure, not a merged cursor.

### Reading the counts

Every shadow run of the desk command emits exactly one structured counts
line on **stderr** (the runner also logs it at INFO on the
`app.services.fill_event_handoff.bundle` logger for embedded callers) —
`journalctl` and docker logs both capture stderr:

```text
fill_handoff_bundle_shadow {"bundles_formed":{"opa-crypto":1},...}
```

The same counts appear under `shadow` in the run's JSON output on stdout,
and `transport` reports `shadow`. Under shadow the top-level counters
(`fill_bundles`, `watch_bundles`, `risk_pushes`, `stall_notices`) are
would-be counts — the sends never happen.

- `fills_read` / `watches_read` — rows evaluated this run, including the
  lookback re-read window; this is scanned volume, not new rows.
- `bundles_formed` — per-lane count of `fill-bundle`/`watch-bundle` payloads
  that would have been emitted as `lane.event`s.
- `lane_sends` — per-lane count of every would-be send, bundles plus the
  notice kinds counted under `notices`.
- `notices` — stall and seen-pressure lane notices that would have posted.
- `bundles_undeliverable` — per-market bundles that would have wedged live
  for a missing lane; shadow resolves those rows anyway so observation
  continues, and the market appears in `errors` as
  `fill_lane_missing:<market>`/`watch_lane_missing:<market>`.
- `duplicates_suppressed` — rows already inside the 24-hour dedupe window.
- `risk_judgements` — detector outputs evaluated; `risk_would_push` —
  evidenced judgements that would have pushed to Telegram; `risk_deduped` —
  repeat judgements suppressed by dedupe state.
- `kick` / `watch_kick` — `{kick, queue_only, capped, by_reason}` per the
  #825/#865 classes. A `kick` count is a Prefect flow run that would have
  been created; `queue_only`/`capped` carry the reason in `by_reason`
  (`buy_new_position`, `daily_cap`, `rep_window`, `cooldown`,
  `parking_etf`, `stale_event`, `ladder_grouped`, …). Fill kicks and watch
  kicks share the same shadow `kick_days`/`cooldowns` as the live gate.
- `watch_kick_rows` — delivered-watch rows the kick pass evaluated.
- `errors` — the run's error list (`fill_read_failed`,
  `watch_kick_seed_failed`, `watch_kick_read_failed`, …).

After two weeks, compare `kick`/`queue_only`/`capped` by reason against the
queue the desk actually consumed, and `risk_would_push` against the pushes
the desk would have wanted. Sustained nonzero `bundles_undeliverable` or
`errors` means enabling as-is would wedge or spam — fix the lane map or the
source before flipping `FILL_EVENT_HANDOFF_ENABLED`.
