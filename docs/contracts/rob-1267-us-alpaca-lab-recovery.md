# ROB-1267 — US Alpaca paper *lab* lane lifecycle-recovery contract

## Scope and authority

This is a contract-only artifact (J5B).  It consumes the signed registry and
the merged J5A contract without changing them, and it authorizes no broker,
network, database, scheduler, deployment, credential, or account operation.
It creates no policy, assigns no writer, proposes no cadence, and grants no
canary.  It moves no lane to an enabled activation state.

It covers exactly one lane: `us.alpaca.paper.lab`.

Its single subject is the **lane-native lifecycle recovery contract** for that
lane, plus the two pre-submit boundary invariants recorded in §6.  Recording
the contract is not a request to enable anything; activation is a separate
approval outside this document.

`us.alpaca.paper.default` is deliberately **not** given a record here.  §6.1
records why the two lanes may never be collapsed into one callback or one
ledger instance, which is the only thing this document has to say about the
default lane.

### Relationship to ROB-1266 (READ ONLY)

`docs/contracts/rob-1266-us-readiness.md` is the merged J5A contract and is the
authority for this lane's registry record — its role, its statuses, its writer
and owner fields, its namespace, its hosts, and its unmet-binding list.  This
document **references and cites it; it does not re-render, restate, redefine,
or modify it.**  Registry facts are cited by path, section, and line rather
than reproduced, so that no second rendering of the same fact can drift from
the first.  Where this document and ROB-1266 disagree, **ROB-1266 is
authoritative** and this document is the defect.  Where this document and the
registry disagree, **the registry is authoritative** and this document is the
defect.

That citation discipline is machine-enforced: `test_g5` in
`tests/services/test_alpaca_paper_lab_recovery_contract.py` pulls this lane's
distinctive registry axis values live from `SIGNED_SOURCE` and fails if any of
them is transcribed into this file.

**This lane's lifecycle verdict is one of those already-recorded facts.**  J5A
records it on the lane's own status axis at
`docs/contracts/rob-1266-us-readiness.md:372`.  Unlike the two KIS/Kiwoom lanes
of ROB-1268 — whose signed allowlist admits only a different status, so the
lifecycle verdict needed a separate axis to live on — this lane's signed status
*is already* the lifecycle-blocked one.  Writing it again here, on any axis,
would be a second independent recording of a fact ROB-1266 owns.  Therefore
this document **carries no lifecycle status value at all.**  It cites the line
that holds it and contributes only what J5A does not have: which specific rule
items are unmet, and on what merged evidence.

## 1. Document-level fixed tokens

```text
SIGNED_SOURCE = app/services/mock_lane_registry.py::CANONICAL_LANE_REGISTRY @ e057941425d2ea7d35a36ebf6074a6c70eba3013
US_READINESS_CONTRACT = docs/contracts/rob-1266-us-readiness.md @ ddf4895ece2ca9dff8daf1a04fa7d6143f43c899
LIFECYCLE_RULE_RENDERING = docs/contracts/rob-1268-us-kis-kiwoom-readiness.md#2-the-lifecycle-rule-being-applied @ 6db485f2adfc6fb1861c61285cb4e7c2255c0042
```

Each token appears exactly once in this document.

The implementation modules cited throughout §4 and §6 are deliberately **not**
pinned to a commit.  They are live, currently-edited surfaces; a commit pin on
them would be stale on the next merge and would invite exactly the drift this
document is trying to avoid.  They are cited by path plus the named symbol that
carries the behaviour, and `test_g13` fails if any cited symbol disappears.

## 2. The lifecycle rule being applied

The controlling rule is the same six-item rule J5C already renders verbatim, at
`LIFECYCLE_RULE_RENDERING`.  It is **not re-rendered here**: a second in-repo
rendering of the same upstream text is a second thing to keep in sync, and
keeping only one rendering is the whole point of §"Relationship to ROB-1266"
above.  What this document reuses is the rule's item set, because those are the
record keys of §4:

`recovery_owner`, `restart_rediscovery_trigger`,
`authoritative_readback_operation`, `lane_native_evidence`,
`release_if_matches_condition`, `operator_visible_blocked_state`.

`lane_native_evidence` is itself decided by the seven evidence outcomes the
rule names, which are the record keys of the EVIDENCE block in §4.

One consequence is binding on this document: **no broker-specific retry,
readback, or manual-resolution queue is added anywhere by this work.**  This
document adds no runtime module and no scheduler entry, and the tests it ships
inject their own doubles rather than reaching a broker, a database, or a
credential.

## 3. Record shape

The lane is recorded as a `### LANE <lane_id>` heading followed by exactly two
fenced blocks, in this order:

1. a **LIFECYCLE** block whose first line is `# LIFECYCLE`, and
2. an **EVIDENCE** block whose first line is `# EVIDENCE`.

- The LIFECYCLE block's key set is exactly: `lane_id`,
  `lifecycle_status_authority`, `recovery_owner`,
  `restart_rediscovery_trigger`, `authoritative_readback_operation`,
  `release_if_matches_condition`, `operator_visible_blocked_state`,
  `unmet_lifecycle_items` — eight keys, no extras, no omissions.
- The EVIDENCE block's key set is exactly the seven evidence outcomes named by
  the rule in §2: `ack`, `unknown`, `reject`, `expiry`, `partial_fill`,
  `cancel`, `terminal_reconciliation` — seven keys, no extras, no omissions.

Each body line is `<key> = <rendered value>`.  Keys do not repeat within a
block.  `lane_id`, `lifecycle_status_authority` and `unmet_lifecycle_items` are
bookkeeping keys and carry no state.  Every other value — the five rule items
and all seven evidence outcomes — begins with one of exactly three states:

- `PRESENT` — a merged in-repo anchor satisfies the item for this lane.
- `PRESENT_CONSTRAINED` — an anchor exists but carries a recorded limitation
  that stops it from satisfying the item unaided.
- `ABSENT` — no merged anchor satisfies the item for this lane.

`PRESENT_CONSTRAINED` and `ABSENT` both count as **unmet**.  A constrained item
is not partial credit.

Every state below is *derived from merged code at test time*, never read back
out of this document.  `test_g8`/`test_g9` recompute each state from the
registry and the implementation modules and fail if the value written here
disagrees; `test_g10` recomputes `unmet_lifecycle_items` the same way.  A state
cannot be improved by editing this file.

## 4. Lane record

### LANE us.alpaca.paper.lab

```text
# LIFECYCLE
lane_id = us.alpaca.paper.lab
lifecycle_status_authority = US_READINESS_CONTRACT §8, heading `### LANE us.alpaca.paper.lab`, line 372 — this lane's lifecycle verdict is recorded there and is deliberately not restated on any axis of this document
recovery_owner = ABSENT (this lane's unmet-binding list is owned by US_READINESS_CONTRACT §8 and records no bound owner there; that list is not restated here)
restart_rediscovery_trigger = ABSENT (no owner exists to own one, and nothing merged rediscovers surviving claims at start-up: an execution row whose submit crashed mid-flight is deliberately *retained* rather than re-posted — see is_inflight_execution in app/services/alpaca_paper_ledger_service.py — and the only resolver, AlpacaPaperSubmitCoordinator._resolve_inflight in app/services/alpaca_paper_submit_service.py, runs a bounded poll inside a live duplicate submit call, not at process start)
authoritative_readback_operation = PRESENT (AlpacaPaperReconcileService._reconcile_one reads the single order back by key via broker.get_order_by_client_order_id in app/services/alpaca_paper_reconcile_service.py, and the candidate set it reads is lane-scoped because AlpacaPaperLedgerService.list_reconcile_candidates filters on the instance's pinned account_mode)
release_if_matches_condition = PRESENT_CONSTRAINED (a release predicate exists but is cancel-scoped, not recovery-scoped: alpaca_paper_cancel_order in app/mcp_server/tooling/alpaca_paper_orders.py releases the sell reservation only when its read-back both succeeds and normalizes to the canceled status, and keeps the hold otherwise; no recovery-contract release condition is designated for this lane and none is inferred here)
operator_visible_blocked_state = PRESENT (the manual_review closure in AlpacaPaperReconcileService._reconcile_one returns action noop_requires_manual_review with requires_manual_review true and a reason, for an unreadable broker answer, a missing order, an incomplete fill set, and a filled status carrying no fill quantity)
unmet_lifecycle_items = (recovery_owner, restart_rediscovery_trigger, release_if_matches_condition, lane_native_evidence)
```

```text
# EVIDENCE
ack = PRESENT (AlpacaPaperLedgerService.record_submit stamps broker_order_id and submitted_at onto the lane-pinned execution row claimed by claim_submit; see §5.1)
unknown = ABSENT (nothing lane-native is written for an unresolvable broker answer — the manual_review closure builds a return payload and performs no ledger write at all, so the row keeps its prior state and the only record of the unknown outcome is the ephemeral tool response; see §5.2)
reject = PRESENT (AlpacaPaperLedgerService.record_submit_failure books a deterministic broker rejection terminally, with a redacted bounded error_summary; see §5.1)
expiry = PRESENT_CONSTRAINED (an expired order is booked, but no lane-native terminal state distinguishes it: derive_lifecycle_state in app/services/alpaca_paper_ledger_service.py maps expiry, rejection and suspension to one and the same anomaly state, and only the raw order_status column separates them; see §5.3)
partial_fill = PRESENT (resolve_transition in app/services/alpaca_paper_reconcile_service.py forces the partially_filled broker status on a partial verdict so the ledger is never promoted past its evidence, and the booked quantity and average price are persisted; see §5.1)
cancel = PRESENT (AlpacaPaperLedgerService.record_cancel writes cancel_status and canceled_at, and derive_lifecycle_state books a canceled order terminally only when that cancel evidence is present, treating a cancel without evidence as an anomaly instead; see §5.1)
terminal_reconciliation = PRESENT (AlpacaPaperLedgerService.record_reconcile and record_final_reconcile write reconcile_status and reconciled_at, and the final-reconciled state is inside the terminal set that removes the row from the reconcile candidate query; see §5.1)
```

## 5. Evidence-surface findings

### 5.1 The Alpaca ledger *is* lane-separable, so most outcomes are lane-native

Unlike the two lanes of ROB-1268, this lane has a usable lane-native evidence
table.  `alpaca_paper_order_ledger` carries an `account_mode` column
(`app/models/review.py::AlpacaPaperOrderLedger`), and
`AlpacaPaperLedgerService` pins exactly one normalized account mode per
instance in its constructor and puts that mode into the predicate of every read
it performs — `get_by_client_order_id`, `get_by_id`, `list_recent`,
`list_reconcile_candidates`, and the sell-reservation queries alike — and into
the values of the row `claim_submit` inserts.

That is why five of the seven outcomes above come out `PRESENT`: the writer
exists *and* what it writes is attributable to this lane rather than to the
default one.  §6.1 records the invariant that keeps it that way.

### 5.2 The missing outcome is `unknown`, and it is missing by omission of a write

`AlpacaPaperReconcileService._reconcile_one` escalates four distinct
unresolvable conditions through one local `manual_review` closure: the broker
read raised, the broker returned no such order, the fill set could not be
proven complete, and the broker called the order filled while producing no fill
quantity.  Each of those is exactly the "unknown" outcome the rule names.

The closure updates the in-memory result dict and returns it.  **It performs no
ledger write.**  The row therefore stays in whatever non-terminal state it was
already in, indistinguishable from a row nobody has looked at yet, and the fact
that an authoritative readback was attempted and failed survives only in the
tool's return value.  A recovery owner arriving after a restart would have no
lane-native record that the condition had ever been observed.

This is recorded, not repaired.  Adding such a write is a runtime change to a
shared reconcile path used by the default and crypto lanes too, and it is not
in this document's scope.

### 5.3 Expiry, rejection and suspension collapse into one lifecycle state

`derive_lifecycle_state` maps all three of those broker statuses to the single
anomaly lifecycle state.  The distinguishing information is not lost — the raw
broker status is persisted on `order_status`, and `record_submit_failure`
additionally persists a bounded, redacted `error_summary` — but the lane's own
lifecycle vocabulary cannot tell an expiry from a rejection.

Rejection is nevertheless recorded `PRESENT` and expiry `PRESENT_CONSTRAINED`,
because rejection has a dedicated lane-native writer that stamps its own status
and summary, and expiry has none: an expired order reaches the ledger only
through the generic zero-fill terminalization path that
`AlpacaPaperReconcileService` shares with every other terminal status.

### 5.4 What C3-1, C3-2 and C3-5 would each require, and why none is filled in

None of the three unmet lifecycle items is filled in with an inferred value,
and no value for them is proposed here:

- **`recovery_owner`** would require a designated owner for this lane.  The
  registry itself records that this lane has no bound owner, in the
  unmet-binding list ROB-1266 §8 holds.  Naming one is an operator act; a
  contract cannot appoint it.
- **`restart_rediscovery_trigger`** would require something that runs at
  start-up and rediscovers surviving durable claims.  Nothing merged does that,
  and a trigger is a thing an owner owns — with no owner there is nobody to own
  one.  Inventing a trigger here would create an unowned automatic action,
  which is worse than the gap.
- **`release_if_matches_condition`** would require an exact match predicate
  under which recovery may release a held claim.  The nearest merged predicate
  is cancel-scoped, and the whole point of a recovery release condition is that
  it is *not* the ordinary cancel path.  Substituting the cancel predicate for
  it would look like a satisfied item while leaving the real one unwritten.

The value of this record is that these three stay unmet.

## 6. Pre-submit boundary invariants

Both invariants below are enforced by tests that this work ships, and both were
verified by mutation: the invariant's guard was removed in the working tree,
the test was observed to fail, and the guard was restored.

### 6.1 The lab lane and the default lane never collapse into one path

Three independent mechanisms keep them apart, and all three must hold:

1. **Profile routing.**  `profile_for_account_mode` in
   `app/services/alpaca_paper_account_modes.py` maps the lab account mode to
   its own broker profile, and `_service_for_account_mode` in
   `app/mcp_server/tooling/alpaca_paper_orders.py` constructs the broker
   service from that profile.  The three supported paper account modes map to
   three distinct profiles; no two share one.
2. **Ledger pinning.**  An `AlpacaPaperLedgerService` instance normalizes one
   account mode in its constructor and carries it in the predicate of every
   read and the values of every claim.  A lab-pinned instance cannot see, book
   against, or terminalize a default-lane row.
3. **Packet/coordinator binding.**  `AlpacaPaperSubmitCoordinator` verifies the
   packet's account mode against the mode it was constructed for, before any
   broker work, and rejects a mismatch with the packet layer's
   account-mode-mismatch code.  The rejection is symmetric: a default packet
   offered to a lab coordinator and a lab packet offered to a default
   coordinator are both refused, and neither reaches the broker.

`tests/mcp_server/test_alpaca_paper_lab_automated_boundary.py` covers all three.

### 6.2 Contaminated, foreign, or unlinked residue is a pre-submit stop

`scripts/b0x/us/cycle.py::run_us_cycle` checks the account state's
contamination flag *first* among its submission gates — ahead of the confirm
flag and ahead of the presence of an injected submitter — and on contamination
records a skip reason and leaves the submitted list empty.  No planned order is
handed to a submitter, so an injected seam is called zero times.

Contamination is not limited to an obviously foreign symbol.
`_attribute_positions` in `scripts/b0x/us/alpaca.py` adds a position to the
foreign set on *every* attribution failure — no `b0xu-` correlation at all, a
correlated execution whose fill quantity is unreadable, and a correlated
execution set whose signed quantity does not exactly equal the broker quantity
— recording a human-readable linkage failure alongside each one.  Because the
contamination flag is computed from the foreign sets, every linkage failure is
also a contamination, and an "unlinked residue" cannot slip through as merely a
note.

Independently, the seam itself is unwired by default:
`submit_planned_order` and `cancel_own_open_orders` in `scripts/b0x/us/alpaca.py`
raise rather than default to a broker, and an unconfirmed submit returns its
confirmation-required response without calling an injected submitter even when
one is present.

`tests/scripts/b0x/us/test_alpaca_lab_mutation_seam.py` covers all of these as
call-count assertions.

## 7. What this document does not do

- It does not move any lane to an enabled activation state, set `writer` or
  `auto_order_enabled` true, name a scheduler owner, arm an environment
  variable, or register a scheduler entry.  Nothing in this work does.
- It does not create, propose, or imply a policy, a cap, a cadence, a canary,
  an FX rule, or a physical-account identity.
- It does not name a recovery owner, invent a restart trigger, or designate a
  release condition; §5.4 records why each stays unwritten.
- It does not modify `US_READINESS_CONTRACT`, restate any registry axis value
  it owns, or record this lane's lifecycle status a second time.
- It does not add a retry queue, a durable state store, a janitor, a takeover,
  or a recovery API to any shared coordination layer.
- It does not change any runtime module.  This work ships one document, three
  test files, and the manifest lines those test files require.
