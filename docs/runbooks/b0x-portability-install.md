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
  path by name/path only, `uv`, `panewire`, inbox, and consumer SQLite path;
- B0X lane/host/pane binding sources and unit/config manifest hashes;
- the captured legacy Prefect source revision and the approved Prefect
  lane-event binding-manifest source; the installed six records remain on that
  legacy revision until cutover;
- an approved destination-side delivery adapter identity and executable hook
  that supplies the exact `lane`, producer `event_id`, and text to the durable
  consumer, plus its independent delivery/consumption readback source;
- captured `current`, intended `target`, and `rollback=current` for
  `b0x_source_executable_alias`, `b0x_consumer_executable_alias`, and
  `b0x_lane_frozen_route`, plus the `b0x_source_dry_run_guard`. The latter's
  current and rollback values retain `--dry-run`; its target may remove the
  guard only after the delivery blocker is resolved and a separate cutover is
  approved.

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
2. Render the six public `.service.in` templates into an installer-owned private
   stage using exact validated absolute inputs. Verify the rendered hashes;
   each rendered `ExecStart` must still contain `--dry-run`, so rendering is not
   installation or source arming. Install the six paired service/timer drafts
   only after a separately approved delivery adapter exists, with every timer
   disabled and `Persistent=false`; write no route or alias yet.
3. Read back unit bytes and environment **key names only**. Require
   `LANE_EVENT_KICKOFF_B0X_ENABLED=false` and
   `B0X_LANE_EVENT_CONSUMER_ENABLED=false`.
4. Capture the real MCP `tools/list`; it must be exactly the seven task-191
   shadow functions and pass the deny matrix.
5. Only after separate approval, collect three distinct full KST shadow days.
6. Freeze/verify; wait for the current legacy slot; record every queued,
   failed, held, and unconsumed event. Stop here while the delivery-ingress
   blocker below remains unresolved.
7. Only after that blocker is resolved and separately approved, disable the six
   legacy Prefect sources, read back legacy disabled, then enable consumer
   followed by source. Never enable both source owners.
8. Prove the exact manifest/symlink source is stable, write the checkpoint,
   prove successor consumption, and only then shut down the Mac owner.

Transport exit 0/duplicate proves only producer durability. Required readback
also includes the consumer row keyed by `(lane,event_id)` and its disposition;
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

## Unresolved destination-delivery blocker

The current verified panewire contract persists a source-side producer record,
then delivers a one-line prompt to the destination pane. It exposes no
destination-side delivered-event file, subscription, or executable hook. The
one-shot `scripts.b0x_lane_event_consumer` can validate a caller-supplied real
producer record shape, but no service can honestly claim that panewire delivery
invokes it. Scanning the producer `events-lane` directory would be incorrect:
that record proves producer persistence, not destination delivery.

Consequently `app.services.b0x_lane_consumer.production_consumer_path_readiness`
returns blocked, and both source gates must remain false. AC3 source-to-consumer
wiring is unresolved in task 191. Resolution requires an approved change or
installer-supplied existing mechanism at the panewire/herdr destination
boundary that preserves `(lane,event_id)`, invokes the consumer once, and keeps
the pane-delivery acknowledgement distinct from the consumer SQLite receipt.
No such mechanism is invented or installed by this PR.

Broader KR/crypto/US resident redesign is `NEEDS_INPUT`: task 191 adds only the
B0X source/consumer and seven-capability observation profile. Existing task
#164 gates remain independent.
