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
2. Render the six producer `.service.in` templates and the public ingress
   config/service template into an installer-owned private stage using exact
   validated inputs. Verify rendered hashes. Producer `ExecStart` still
   contains `--dry-run`; the ingress binding remains `DRAFT_NOT_INSTALLED`.
   Rendering is neither installation nor source arming.
3. If separately authorized, install the staged units disabled and read back
   their exact bytes plus environment **key names only**. Require
   `ingress_enabled=false`, `dispatch_enabled=false`, and
   `source_enabled=false`; require the five-second inactive timer cadence,
   `Persistent=false`, a finite service timeout, and the stable lock path.
   Write no route or alias in this code task.
4. Capture the real MCP `tools/list`; it must be exactly the seven task-191
   shadow functions and pass the deny matrix.
5. Only after separate approval, collect three distinct full KST shadow days.
6. Freeze/verify; wait for the current legacy slot; record every queued,
   failed, held, and unconsumed event. Stop here while the approved
   queued-cycle business-dispatch entry point remains unresolved.
7. Only after that entry point and all private binding/readbacks are separately
   approved: keep the new source off and prove no emit is in progress; capture
   the backlog and fixed activation floor/time/epoch/head/receipts; prove the
   route is a uniquely owned sink; disable and read back all six legacy Prefect
   sources; then enable ingress/dispatch and finally the new source. Never
   enable both source owners, and never convert an existing Mac lane to sink.
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

## Implemented HTTP ingress and unresolved business dispatch

The public production entry is
`job-b0x-lane-event-poller.service.in` ->
`python -m scripts.b0x_lane_event_poller --binding <absolute-config>
--state-db <absolute-db> --once` -> an HTTP GET adapter -> the same-transaction
durable consumer. The only remote method/path is:

```text
GET /v1/relay/events?lane=<binding.lane>&kind=lane.event&undelivered=false&after_id=<sweep-cursor>&limit=200
```

The poller maps HTTP `id` to `delivery_id` and `kind` to consumer `type`, treats
the row's `text` strictly as closed JSON data, and never updates the hub's
delivery ledger. It records the raw UTF-8 byte count/hash and hub metadata but
not raw text. `delivered_at=null` is valid; a non-null `delivered_to` must match
the attested sink record. HTTP/ingress/business/queue/start/terminal states are
separate in JSON and readback.

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

No approved production runner currently consumes `queued_cycle` rows. The
repository search found persistence/readback and test helpers only, not a
fixed strategy runner entry point. Therefore
`production_consumer_path_readiness()` reports HTTP ingress wired but overall
readiness false, dispatch wired false, and source enablement prohibited. A
queued row is not dispatch started or terminal evidence. This exact
business-dispatch gap is `NEEDS_INPUT`; this PR does not invent a generic argv,
prompt, model, or strategy dispatcher and does not install or activate ingress.

Broader KR/crypto/US resident redesign is `NEEDS_INPUT`: task 191 adds only the
B0X source/consumer and seven-capability observation profile. Existing task
#164 gates remain independent.
