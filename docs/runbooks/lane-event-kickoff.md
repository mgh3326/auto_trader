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
An operator may provide a different safe relative `.md` path with `--playbook`.
The event ID is always `kickoff-<slot>-<KST-date>`. In particular, `us-2235`
uses the KST calendar date, not its UTC date; that agrees with the prior
Prefect `market_closed_reason` convention.

## Environment and outputs

| Variable | Default | Purpose |
| --- | --- | --- |
| `LANE_EVENT_KICKOFF_ENABLED` | `false` | Only lowercase `true` permits emission. Disabled mode is an exit-0 dry run. |
| `LANE_EVENT_EMIT_BIN` | `panewire` | Host `panewire` executable visible inside the unit container. |
| `LANE_EVENT_EMIT_HOST` | local hostname | Producer host sent to `panewire`. |
| `LANE_EVENT_EMIT_PANE` | empty | Producer pane. Empty or `-` means no pane, so `--pane` is omitted. |
| `LANE_EVENT_EMIT_INBOX_ROOT` | `~/work/herdr-inbox` | Inbox root used by the producer. NCP units override this. |
| `LANE_EVENT_EMIT_TIMEOUT_S` | `3` | Emitter timeout in seconds; values at or below one are rejected with exit 2. |
| `LANE_EVENT_KICKOFF_LANE_KR` | unset | Admiral-supplied destination for KR slots. |
| `LANE_EVENT_KICKOFF_LANE_CRYPTO` | unset | Admiral-supplied destination for crypto slots. |
| `LANE_EVENT_KICKOFF_LANE_US` | unset | Admiral-supplied destination for the US slot. |

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

A duplicate is successful: the durable `(lane, event_id)` record already
exists and the CLI exits 0. All other emitter failures exit 1 with `reason` in
the JSON. `--dry-run`, a disabled gate, unsafe arguments, and text over 2048
bytes never start the emitter.

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

## Cutover and resident-session discipline

Run Prefect kickoff and NCP kickoff timers on different days. Enabling both
for a slot creates two sessions that can execute the same work. The admiral's
one-day parallel plan is: validate resident lanes and disabled timer output,
pause the relevant Prefect kickoff deployments, enable the NCP timers, and
observe the next slot. To reverse the change, disable the NCP timers first,
then unpause Prefect.

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
