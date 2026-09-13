# Resident-lane kickoff events

`scripts.lane_event_kickoff` injects one durable `lane.event` notification into
an already-running operator lane. It does not create a session, contact a
broker, access the database, call Prefect, or make HTTP requests.

```sh
python -m scripts.lane_event_kickoff \
  --lane lane-a --slot 0905 --playbook prompts/kr-open-trade.md
```

The accepted slots, their KST timer times, weekday restrictions, and default
playbooks are the single `scripts.lane_event_kickoff.KICKOFF_SLOTS` mapping.
An operator may provide a different safe relative `.md` path for the original
eleven slots; each B0X source requires its frozen mapped playbook. The original
event ID remains `kickoff-<slot>-<KST-date>`. In particular, `us-2235`
uses the KST calendar date, not its UTC date; that agrees with the prior
Prefect `market_closed_reason` convention.

Six additive B0X sources mirror the legacy Prefect records exactly:

| Source | KST schedule | Disposition |
| --- | --- | --- |
| `b0x-table-kr` | 07:45 weekdays | policy-table build, no cycle |
| `b0x-table-us` | 22:00 weekdays | policy-table build, no cycle |
| `b0x-nudge-kr` | 09:05 weekdays | cycle kickoff |
| `b0x-nudge-us` | 22:35 weekdays | cycle kickoff |
| `b0x-nudge-crypto` | 01/05/09/13/17/21 daily | tick-scoped cycle kickoff |
| `b0x-harvest` | every hour at :13/:43 | tick-scoped observation only, never a cycle |

Tick-scoped IDs append `-T<HHMM>` to the KST date. An off-schedule tick fails
closed, and `Persistent=false` forbids implicit catch-up.

## Environment and outputs

| Variable | Default | Purpose |
| --- | --- | --- |
| `LANE_EVENT_KICKOFF_ENABLED` | `false` | Only lowercase `true` permits emission. Disabled mode is an exit-0 dry run. |
| `LANE_EVENT_KICKOFF_B0X_ENABLED` | `false` | Second gate required only for every B0X source. |
| `LANE_EVENT_EMIT_BIN` | `panewire` | Host `panewire` executable visible inside the unit container. |
| `LANE_EVENT_EMIT_HOST` | local hostname | Producer host sent to `panewire`. |
| `LANE_EVENT_EMIT_PANE` | empty | Producer pane. Empty or `-` means no pane, so `--pane` is omitted. |
| `LANE_EVENT_EMIT_INBOX_ROOT` | `~/work/herdr-inbox` | Inbox root used by the producer. NCP units override this. |
| `LANE_EVENT_EMIT_TIMEOUT_S` | `3` | Emitter timeout in seconds; values at or below one are rejected with exit 2. |
| `LANE_EVENT_KICKOFF_LANE_KR` | unset | Admiral-supplied destination for KR slots. |
| `LANE_EVENT_KICKOFF_LANE_CRYPTO` | unset | Admiral-supplied destination for crypto slots. |
| `LANE_EVENT_KICKOFF_LANE_US` | unset | Admiral-supplied destination for the US slot. |
| `LANE_EVENT_KICKOFF_LANE_B0X` | unset | Installer-supplied destination for all B0X sources. |

The pre-existing fill-handoff CLI also understands the five
`LANE_EVENT_EMIT_*` settings. Its deployment-specific `FILL_HANDOFF_EMIT_BIN`,
`_HOST`, `_PANE`, `_INBOX_ROOT`, and `_TIMEOUT_S` settings win over their
shared counterparts independently; existing deployments that set only
`FILL_HANDOFF_EMIT_*` retain their behavior. Kickoff deliberately never reads
the `FILL_HANDOFF_*` namespace.

Every invocation prints one sorted JSON line. Typical results are:

```json
{"date":"2026-09-05","dry_run":false,"duplicate":false,"emitted":true,"enabled":true,"event_id":"kickoff-0905-2026-09-05","lane":"lane-a","playbook":"prompts/kr-open-trade.md","slot":"0905"}
{"date":"2026-09-05","dry_run":false,"duplicate":true,"emitted":false,"enabled":true,"event_id":"kickoff-0905-2026-09-05","lane":"lane-a","playbook":"prompts/kr-open-trade.md","slot":"0905"}
{"date":"2026-09-05","dry_run":false,"duplicate":false,"emitted":false,"enabled":true,"event_id":"kickoff-0905-2026-09-05","lane":"lane-a","playbook":"prompts/kr-open-trade.md","reason":"timeout","slot":"0905"}
```

A duplicate is a successful **transport** result: the durable
`(lane, event_id)` producer record already exists and the CLI exits 0. JSON
reports `transport_acknowledged`; `consumer_execution_evidence` remains null.
Only the consumer's durable disposition proves execution. All other emitter
failures exit 1 with `reason`. `--dry-run`, either disabled B0X gate, unsafe
arguments, and text over 2048 bytes never start the emitter.

## NCP unit boundary

The admiral owns panewire lane registration, resident-session startup, and the
values of the three lane variables above. The operator lanes might be described
as `opa-kr`, `opa-crypto`, and `opa-us` in discussion only; the deployed names
come from `lanes.json` and are not committed in unit `ExecStart` values.

The 11 `job-kickoff-<slot>.service` units are dedicated Docker invocations, not
`at-job.sh` jobs. This is necessary because the ordinary runner has no bind
mounts for the producer binary, daemon inbox, or daemon socket.

| Required container mount | Why it is required |
| --- | --- |
| `/root/pw-s2pilot/bin/panewire:/root/pw-s2pilot/bin/panewire:ro` | Runs the host-compatible producer binary. |
| `/root/pw-s2pilot/inbox:/root/pw-s2pilot/inbox` | Reaches the NCP daemon inbox namespace. |
| `"/root/Library/Application Support/panewire":"/root/Library/Application Support/panewire"` | Reaches the daemon socket at the same absolute path. |

The service sets `PANEWIRE_SOCKET`, `LANE_EVENT_EMIT_BIN`,
`LANE_EVENT_EMIT_INBOX_ROOT`, and `LANE_EVENT_EMIT_HOST`, and layers
`/root/at-secrets/.env.api` with the admiral-owned
`/root/at-secrets/.env.lane-kickoff`. Do not add `LANE_EVENT_EMIT_PANE`: this
producer has no pane. Giving a different inbox root is a trap: the daemon
namespace guard rejects it, leaving only a file that cannot be injected.

### Environment-file warning

Do not put `LANE_EVENT_EMIT_*` in `/root/at-secrets/.env.api`. The unit's
`docker run` places its pinned `--env LANE_EVENT_EMIT_*` arguments before that
`--env-file`, so a same-named API-file value overrides the pinned value. In
particular, a changed `LANE_EVENT_EMIT_INBOX_ROOT` is rejected by the daemon
namespace guard and silently degrades the kickoff to file-only delivery.
`fill-event-handoff.service` also reads `.env.api`; shared `LANE_EVENT_EMIT_*`
values leak into its path unless that deployment supplies the corresponding
`FILL_HANDOFF_EMIT_*` override.

Timers use KST `OnCalendar`, zero randomized delay, and `Persistent=false`.
A system started after a slot must not replay a stale kickoff after the market
window; its resident session could otherwise execute a past cycle. The units
do not use `flock` or `at-job.sh` summary JSON. Event-ID idempotency, rather
than overlapping-run locking, is the duplicate safeguard.

The destination consumer additionally checks a first delivery against the
exact KST source minute. It durably preserves prior-date, prior-tick, and late
records as `preserved_unconsumed_out_of_window`; none is caught up later.
Composite `(lane,event_id)` lookup precedes that eligibility decision, so a
retransmission after a clock boundary returns the original disposition with no
additional kickoff.

The six additive B0X services do **not** copy that host layout. Public files are
`job-kickoff-b0x-<source>.service.in` templates containing only closed render
tokens plus false source gates. `scripts.b0x.systemd_render` requires an
installer-owned exact checkout, env-file reference, executable, service user,
timeout, and private staging root; it read-only attests the real Git root/HEAD
and rejects relative, missing, wrong-kind, symlinked, unsafe-character,
incomplete, fake-Git-marker, or already-rendered targets without reading the
env file. Its receipt records the exact source HEAD and output hash. An
unrendered template is not a valid unit, and a rendered unit still has
`B0X_UNIT_TEMPLATE_RENDERED=false`, `LANE_EVENT_KICKOFF_ENABLED=false`, and
`LANE_EVENT_KICKOFF_B0X_ENABLED=false`, and its `ExecStart` retains an
unconditional `--dry-run`. Rendering neither installs nor enables it. Removing
that CLI guard is a separately approved runtime switch whose rollback restores
`--dry-run`; it is forbidden while HTTP ingress/business-dispatch activation
readbacks remain incomplete.
Exact operational inputs and rendered output hashes belong in the private
installer handoff and readback, not this repository.

## Cutover and resident-session discipline

The six legacy Prefect B0X sources and the new source must never both be live.
Install and read back the new units, sink route, consumer state/lock roots, and
all three false binding gates (`ingress_enabled`, `dispatch_enabled`, and
`source_enabled`) first. Collect three full KST days through a separately armed
observation-only path while the legacy owner alone remains live. At cutover,
freeze evidence, wait for the current legacy slot, record queued/unconsumed
dispositions, and stop while the approved queued-cycle dispatch entry point in
`b0x-portability-install.md` remains unresolved. Only a separately approved
resolution plus exact floor/time/head/install/sink receipts may continue with
disabling all six legacy sources, verifying them disabled, enabling ingress and
dispatch, then enabling the B0X source and proving one owner plus successor
consumption. Rollback freezes/disables the new source first, waits for its
active slot, restores captured route/alias targets and the dry-run guard, then
restores legacy.

The retirement checklist is admiral-only: pause then delete the eleven `KR Live
Session Kickoff` Prefect deployments, and handle its paused `manual-smoke`
deployment too; create a separate removal PR for
`krb1_headless`, `cycle_runner`, and the B0-X slot runner; and remove no timer
until the associated resident-lane cutover is accepted.

Resident sessions call `get_operating_briefing` again for every event, restart
once per day, and checkpoint then compact when their context becomes full.

### Intentional holiday regression

The former Prefect flow skips KRX and NYSE holidays using `KRX_HOLIDAYS` and
`NYSE_HOLIDAYS`. systemd knows only its calendar expression, so it injects a
kickoff event even on a holiday. This is intentional in this scope: it is one
notification line, not an order; the resident session reads its briefing and
calendar and ignores it (reporting `no_session_today` when applicable). Do not
silently add a second holiday calendar to this emitter.
