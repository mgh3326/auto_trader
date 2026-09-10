# B0X portability installation contract (future installer only)

This is a command-and-expected-result contract, not an installation record.
Task 191 performs no host access, deployment, registration, source enablement,
route/alias write, event emission, strategy loop, broker/account access, order,
cancel, checkpoint acknowledgement, or shutdown.

Required installer inputs are exact absolute values for:

- auto_trader code worktree and tested head/image digest;
- isolated operator policy-table Git worktree, expected branch, existing Git
  writer identity source, and existing credential source;
- policy output (`<selected-operator-checkout>/policy-tables` exactly), env-file
  path by name/path only, `uv`, `panewire`, and inbox;
- B0X lane/host/pane binding sources and unit/config manifest hashes;
- the captured legacy Prefect source revision and the approved Prefect
  lane-event binding-manifest source; the installed six records remain on that
  legacy revision until cutover;
- the installer-owned absolute ingress binding/config path, state database,
  stable lock, code checkout, Python executable, credential env-file reference,
  one-shot service/timer targets, finite service timeout, OS owner/group/modes,
  and the existing handoffkeep HTTP base/credential source and two allowed
  credential **key names** (never values);
- the sink-route readback source and exact future lane, sink record, unique host
  owner, route receipt, backlog count/range, `start_after_id`, `activation_at`,
  source binding epoch, reviewed code head, binding install receipt, and the
  source-off/no-emit quiescence receipt captured before selecting the floor.
  These are private installer inputs and must not be copied into the public PR;
- the installer-owned absolute `b0x-dispatch/v1` binding path, the same durable
  state DB supplied to its CLI, its stable host-local dispatch lock, exact
  auto_trader and Prefect roots/heads/interpreters, isolated policy checkout
  identity and approved ref, approved non-table tree hash,
  table/output/observation/receipt roots, and each
  runner's distinct environment **reference path** and lower writer-lock
  namespace. The binding also requires the approved account projection and
  canonical hash plus install, owner-fence, route-sink, source-owner,
  no-other-host-owner, and ready receipts. None of these private values belongs
  in this public repository;

Public mode placeholders accept only `0600` or read-only-group `0640`; the
rendered binding, state database, and stable lock must each match its declared
owner/group/mode exactly. The code never broadens permissions.
- captured `current`, intended `target`, and `rollback=current` for
  `b0x_source_executable_alias`, `b0x_consumer_executable_alias`, and
  `b0x_lane_frozen_route`, plus the `b0x_source_dry_run_guard`. The latter's
  current and rollback values retain `--dry-run`; its target may remove the
  guard only after HTTP ingress and business-dispatch readbacks are complete
  and a separate cutover is approved.

Never create/copy a token or broaden permissions. Checkout creation and every
operational Git write are installer-owned. The build receipt must bind the
auto_trader root/HEAD, selected operator root/Git-dir/HEAD, confined output,
exact build-output manifest paths, and each artifact hash. Before commit,
require an empty index and unchanged receipt artifacts; stage only exact
receipt-bound market paths. For crypto, the builder uses
`--preserve-latest-pointer`, and `policy-tables/latest-crypto.json` remains
untouched and slot-worker owned. A push race leaves the local commit intact for
explicit installer retry; it never rebases or stages another market.

Install/readback order:

1. Verify the exact code, image, unit, and config hashes offline.
2. Render the six producer `.service.in` templates, the public ingress
   config/service template, and `config/b0x_dispatch_binding.json.in` into an
   installer-owned private stage using exact validated inputs. Verify rendered
   hashes. Producer `ExecStart` still contains `--dry-run`; both bindings stay
   draft/off. Rendering is neither installation nor source arming.
3. If separately authorized, install the staged units disabled and read back
   their exact bytes plus environment **key names only**. Require
   `ingress_enabled=false`, `dispatch_enabled=false`, and
   `source_enabled=false`; require the five-second inactive timer cadence,
   `Persistent=false`, a finite service timeout, and the stable lock path.
   Write no route or alias in this code task.
4. Capture the real MCP `tools/list`; it must be exactly the seven task-191
   shadow functions and pass the deny matrix.
5. Only after separate approval, collect three distinct full KST shadow days.
6. Validate the fixed dispatch registry and its private binding/readbacks,
   including US `ready=false` until task #164 supplies its approved output,
   environment, writer-fence, and no-incomplete-attempt inputs. Freeze/verify;
   wait for the current legacy slot; record every queued, failed, held, and
   unconsumed event. Code existence is not operational readiness.
7. Only after all private ingress/dispatch binding and owner readbacks are
   separately approved: keep the new source off and prove no emit is in
   progress; capture the backlog and fixed activation floor/time/epoch/head/
   receipts; prove the route is a uniquely owned sink; disable and read back
   all six legacy Prefect sources; then enable ingress/dispatch and finally the
   new source. Never enable both source owners, and never convert an existing
   Mac lane to sink.
8. Prove the exact manifest/symlink source is stable, write the checkpoint,
   prove successor consumption, and only then shut down the Mac owner.

Transport exit 0/duplicate proves only producer durability. HTTP GET success
proves only polling. Required readback also includes the ingress observation
and consumer row keyed by `(lane,event_id)` plus its disposition;
harvest must read `observed_harvest_no_cycle` with `cycle_created=false`.
First delivery is eligible only in its exact KST source minute (seconds within
that minute are accepted). A prior date, a prior tick on the same date, or any
late delivery is durably `preserved_unconsumed_out_of_window` with
`cycle_created=false` and zero additional kickoffs. A later retransmission,
including after a KST boundary, returns that same composite receipt and never
promotes it.

## Seven-capability shadow boundary

The `shadow-replay` profile is closed to exactly
`canonical_session_context_read`, `source_event_observe`,
`emitted_trigger_playbook_read`, `identical_input_snapshot_read`,
`shadow_report_write`, `deterministic_replay_compare`, and
`raw_difference_artifact_write`. There are no aliases. The report writer binds
one canonical context, source event, event-selected playbook, available
identical-input snapshot, code hash, and distinct live/shadow artifact, owner,
and provenance identities. Comparison accepts exactly `permission`, `account`,
`target`, `intent`, `quantity_or_price_band`, and `guard_decisions`; every
deterministic mismatch is unexplained and cannot be relabelled as temporal or
prose. The raw-difference writer recomputes the comparison binding from the
supplied live/shadow snapshots, requires exact live/shadow raw values and the
fixed exclusion vocabulary, and creates a new confined artifact. Every handler
returns `dry_run=true`, `orders=[]`, and `actions=[]`.

## Implemented HTTP ingress and fixed default-off business dispatch

The public production entry is
`b0x-lane-event-consumer.service.in` ->
`python -m scripts.b0x_lane_event_poller --binding <absolute-config>
--state-db <absolute-db> --once` -> an HTTP GET adapter -> the same-transaction
durable consumer. The only remote method/path is:

```text
GET /v1/relay/events?lane=<binding.lane>&kind=lane.event&undelivered=false&after_id=<sweep-cursor>&limit=200
```

The poller accepts exactly the authoritative 20-field handoffkeep
`store.RelayEvent` JSON shape, with the separately approved optional
`truncated=false` marker. It maps HTTP `id` to `delivery_id` and `kind` to
consumer `type`, treats the row's `text` strictly as closed JSON data, and
never updates the hub's delivery ledger. It records the raw UTF-8 byte
count/hash and hub metadata but not raw text. `delivered_at=null` and the Go
zero-value `delivered_to=""` sentinel are valid undelivered evidence; a
nonempty `delivered_to` must match the attested sink record. HTTP/ingress/
business/queue/start/terminal states are separate in JSON and readback.

The canonical future installed basenames are
`b0x-lane-event-consumer.service` and `b0x-lane-event-consumer.timer`. The
public timer is named accordingly and its `Unit=` target is exactly
`b0x-lane-event-consumer.service`. Installer readback must prove both basenames
and timer-target/service-basename equality; this code does not install or start
either unit.

The older `scripts.b0x_lane_event_consumer` direct event-file entry remains
default-off for compatibility but is write-blocked even if its legacy gate is
set. It cannot bypass the binding, activation checks, stable lock, HTTP row
receipt, or same-transaction cursor contract.

Every complete sweep restarts from the installer-recorded fixed
`start_after_id`; only a bounded partial sweep resumes its committed local
cursor. A stable POSIX advisory lock is a host-local fence, not a distributed
lease. Installer readback must separately prove one host owner. Row receipt,
digest/metadata, business disposition, and scan progress commit together in
SQLite, while composite `(lane,event_id)` remains the business identity.

The fixed production entry is `python -m scripts.b0x_lane_event_dispatcher
--binding <absolute-path> --state-db <absolute-path> --once`; its read-only
counterpart adds `--readback --lane <lane> --event-id <event_id>`. It selects
only KR `scripts.run_b0x_kr_kiwoom_cycle`, US `scripts.run_b0x_us_cycle`, crypto
`scripts.run_b0x_cycle --lane shadow`, the Prefect policy-table handler, or the
no-process harvest disposition. Event text supplies none of argv, paths,
environment, account, owner, or policy inputs. Each cycle child receives a
minimal non-credential process environment plus only its binding-selected
`ENV_FILE` path; the dispatcher never opens that file or persists its contents.
US never inherits another runner's reference or ambient credential key.

The dispatch claim is the upper event-start authority. The existing per-lane
writer lock is the lower defence, acquired in that order against the identical
output namespace. Busy or unknown lower-lock state starts zero cycles. SQLite,
the stable host-local `flock`, and an installer receipt do not claim a
distributed lease. The fixed policy path performs dirty check, fetch, detached
approved-ref checkout, non-table tree hash comparison, pointer/readlink-to-
same-ref-blob comparison, and table hash capture. Policy preflight, post-build,
and post-commit heads are separate
attempt evidence rather than a permanent install-time policy HEAD pin. Crypto
non-fast-forward is STOP/ESC with artifacts preserved, `push_reapplications=0`,
and no automatic cycle restart.

Claim time remains the exact source KST minute; hub receipt and ingress
processing retain their separate exact-minute gates. Process start and cycle
observation timestamps are recorded separately and do not create a grace or
catch-up window. A committed claim with an ambiguous start/outcome becomes
`unknown_preserved` and is never automatically retried.

Typed readback exposes the durable queue disposition separately from claim,
per-stage process evidence, cycle artifact observation, and verified terminal
type. It binds `(lane,event_id)`, attempt, binding/source/runner, payload hash,
owner epoch, executable/argv hash/cwd/installed head/PID/start identity and
process timestamps, plus artifact path/hash/bytes/table hash/cycle id when
present. It rejects an artifact replaced by a symlink even when its target has
identical bytes. Raw stdout, stderr, environment values, and headers are never
stored. `success_observed`, `zero_order_observed`, `failed_preserved`, and
`unknown_preserved` remain distinct; a pre-table zero-order may omit the table
hash only when its real record has a validated reason and explicit no-action
evidence.

`production_consumer_path_readiness()` therefore reports both ingress and
fixed dispatch code wired, while overall readiness and source enablement remain
false: the private install/account/owner receipts and all three gates are still
unprovided/default-off. The future Prefect five-second deployment contract is
paused with an inactive schedule and zero retries; it is not registered here.
A queued row is not a process start, an observed cycle is not by itself a
verified terminal, and no code merge is installation or activation.

Broader KR/crypto/US resident redesign is `NEEDS_INPUT`: task 191 adds only the
B0X source/consumer and seven-capability observation profile. Existing task
#164 gates remain independent.
