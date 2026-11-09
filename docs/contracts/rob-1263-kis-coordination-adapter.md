# ROB-1263 J3B — KIS mock coordination adapter

The KIS mock lane's adapter over the merged ROB-1262 (J3A) coordination port.

- Source: `app/services/kis_mock_runner/singleton.py`
- Tests: `tests/services/mock_integration/test_kis_coordination_adapter.py`
- Consumes (read/import only): J2A `mock_lane_registry`, J2B
  `mock_integration/lineage` + `brokers/client_order_ids`, J3A
  `mock_integration/coordination`, and the existing `order_send_intent_service`.

J3B opens no socket of its own, signs nothing, loads no credential value,
registers no scheduler. The ROB-1263 evidence amendment adds two lane-native
append-only tables; operators alone apply its additive migration.

---

## 1. What this lane owns, and what it must not touch

J3A owns the PostgreSQL advisory key math, the COMMIT probe, ownership
reconstruction from `pg_locks`, partial-acquisition rollback, unlock order and
count, the binary reservation adapter, cancellation shielding, and its eight
reason codes. None of that is copied, reimplemented, or wrapped here — a static
test fails the build if any of it reappears in this file.

J3B owns exactly three things:

1. **which keys the KIS lane supplies** — the canonical physical-account key and
   the pre-existing legacy compatibility key;
2. **the real transport identity check** that J3A explicitly delegates to the
   lane (`rob-1262-coordination-port.md` §8);
3. **the lane-native recovery contract** in §6 below.

## 2. The dual keyset

```
physical_key = physical_account_scope_for_entry(entry).advisory_key   # J3A
legacy_key   = account_mode_advisory_key("kis_mock")                  # pre-existing
```

The legacy function is *reused verbatim*, never re-hashed or renamed. J3B hands
the set `{physical_key, legacy_key}` to J3A via `additional_advisory_keys`; J3A
de-duplicates it, orders it globally by numeric value, acquires it, rolls a
partial acquisition back, and unlocks in exact reverse order. The lane asserts
after the fact that the returned grant proves the **whole** set: a single-key
acquisition that still returned would mean an old legacy-only build could have
been writing concurrently.

**Both keys stay for this entire DAG.** Removing the legacy key is forbidden
until a separately approved proof that every deployed old process has
terminated.

## 3. The boolean is gone

`_ACTIVE_WRITER_LEASE` used to hold a bare `bool`, and nested calls skipped
acquisition on it. A boolean can outlive the thing it described — a lease
released without leaving its context leaves `True` behind — and every later
mutation then reached HTTP holding nothing.

It now holds a typed `_WriterAuthority` whose liveness is **recomputed on every
read** from the object that actually holds authority: the J3A grant, or a lease
whose `acquired` flag is still true. Re-entrancy additionally requires an exact
account-mode match and every required key. `KISMockCoordinationGrant` is frozen,
names both keys, and exposes exactly one capability — `assert_owned()`, the right
to *see* ownership, never to act on it.

## 4. The send boundary (J3A §8's delegation)

**Where it is wired.** `DomesticOrderClient._guard_kis_mock_writer` is the KRX
wire boundary for all three catalogued mutations. On the `is_mock=True` branch it
enters `kis_mock_mutation_authority`, and **when a coordination grant is held**
that helper composes `build_kis_mock_send_boundary_hook(...)` into the method's
`pre_send_hook`. The transport fires that hook immediately before every real HTTP
attempt, token-refresh and throttle re-sends included, so the check is per-POST
rather than once per callback.

> Round 1 of this document claimed the gate ran before *every* real KIS mock
> mutation attempt while no production caller constructed it. That claim was
> false and is corrected here. The scope is stated exactly: **coordinated sends
> get the gate; uncoordinated legacy sends do not, and are not AUTO evidence.**

When the hook fires, `assert_kis_mock_send_boundary` requires all of:

| # | Condition |
|---|---|
| 1 | the actual `KISClient._is_mock_client` is `True` |
| 2 | the actual resolved netloc is `openapivts.koreainvestment.com:29443`, via the J2A `assert_mock_only_endpoint` guard (which also rejects the live host list) |
| 3 | the actual settings view is the mock credential namespace **and** its own base URL resolves to that same host |
| 4 | the secret-free actual account fingerprint equals the exact canonical J2A `physical_account_id` |
| 5 | the grant still describes this exact lane |
| 6 | the full dual-key authority is still owned *right now* |

Any mismatch raises before network I/O. `is_mock=True` passed to
`order_korea_stock`, a mock-labelled registry row, a mock TR id, and admission to
the ROB-892 VTS distributed gate are each insufficient: the VTS gate is a
rate/serialization scope, not a live-host rejection.

### The fingerprint derivation is J3B-owned

```
kismock:v1:sha256(b"kis-mock-account-v1\0" + app_key + b"\0" + account_digits)
```

Non-reversible and credential-free, so the value is safe to store in the
registry, log, and compare. It is defined here because B-4 requires the mapping
and no upstream job supplies one for KIS. An operator binding a real
`physical_account_id` for `kr.kis.mock` must use exactly this derivation.

## 5. Reservation semantics (corrected)

`review.order_send_intents` is a binary reservation, not a lifecycle store.

The previous KIS behaviour released a `kis_mock` mirror reservation after any
`httpx.RequestError` and advertised `retry_allowed=True`, reasoning that mock
money carries no risk. That is the wrong axis: the risk is a duplicate order at
the broker and a lineage that can no longer say which send produced it.

Now:

- only an explicit pre-send block, or a transport tracker in `NOT_CREATED`,
  releases before broker contact — and then only through
  `release_if_matches(account_scope, row_id, idempotency_key, side)`. The row id
  observed at reservation time is retained for exactly this reason: releasing by
  `(scope, key)` alone deletes whatever row currently carries that key, so a
  stale failure path could remove a *replacement* reservation made by a later
  attempt or a reconciler. The unrestricted `release` is not reachable from the
  send path;
- a timeout, cancellation, or provider ambiguity **after** the send boundary is
  `unknown_pending_reconcile`: the claim is retained, the physical account is
  blocked from a same/conflicting submit, and `retry_allowed=True` is never
  returned;
- a **missing** outcome tracker proves less than an ambiguous one, so it holds too;
- KIS ledger conservative/local-clock expiry, a missing pending row, a soft
  cancel, and a DB read failure are none of them broker terminal evidence;
- release happens only through
  `DurableSendClaimAdapter.release_with_terminal_evidence`, after native/J2B
  persistence and an exact owner match.

KIS `broker_client_id_target` and `broker_client_order_id` remain `None`; the
internal idempotency key is generated and persisted by J2B. An accepted `ODNO` is
persisted only as `broker_order_id`, and a blank ODNO is not an acknowledgement.
The `BrokerClientIdTarget` enum is unchanged.

## 6. Lane-native recovery ownership — an activation precondition

The common coordination layer does not own broker-specific retry, readback, or
manual-resolution queues. Machine-readable as
`singleton.KIS_MOCK_LANE_RECOVERY_CONTRACT`.

| Element | This lane's answer |
|---|---|
| **recovery owner** (exactly one) | the designated operator-run KIS mock reconciler in `app/mcp_server/tooling/kis_mock_ledger.py` (`KISMockLifecycleService`), using its dispatch-store adapter |
| **restart trigger** | an unscoped operator-invoked reconciliation over all unreleased `review.order_send_intents` rows whose `account_scope` is this physical account's claim scope |
| **authoritative broker readback** | KIS `inquire_daily_order_domestic`, keyed by the exact attributed ODNO — never symbol/side/quantity/time proximity |
| **exact `release_if_matches` condition** | `release_with_terminal_evidence` with `TerminalClaimEvidence(lane_native_terminal_evidence, account_position_reconciled, remainder_known)`, or a proven `authoritative_absence_proven` + `account_position_reconciled`; the underlying `OrderSendIntentService.release_if_matches` is never called directly |
| **operator-visible blocked state** | `AUTO_READY_BLOCKED_BY_LIFECYCLE` |

**Lane-native evidence — all seven required** (`KIS_MOCK_LANE_EVIDENCE_KINDS`):
`ack`, `unknown`, `reject`, `expiry`, `partial_fill`, `cancel`,
`terminal_reconciliation`.

### Evidence amendment (ROB-1263, task 2668): implemented, activation pending

`app/services/kis_mock_runner/evidence_store.py::KISMockEvidenceStore` supplies
lineage persistence, dispatch evidence and the account uncertainty gate through
`store.ports()`. The additive migration `20261102_rob1263_kis_evidence` creates
only lane-native objects: `review.kis_mock_dispatch_lineage` and
`review.kis_mock_dispatch_evidence`, their constraints, sequence and triggers.
It changes no existing table or `review.order_send_intents` semantics. Both new
tables reject UPDATE/DELETE/TRUNCATE; downgrade refuses retained evidence.

The service commits initial lineage before the common coordinator reserves a
claim. ACK attachments and dispatch outcomes are append-only. The common
`DispatchEvidence` vocabulary maps acknowledged definitive results to `ack`;
every other outcome maps to `unknown`, never inferred rejection. The manual
reconciler now records exact-order native readback through
`record_native_evidence`. Its guarded release path calls
`release_with_recorded_terminal_evidence` only when the common terminal gate
is satisfied. The position proof amendment below defines what proves
`account_position_reconciled`.
Each event is unique on `(order_attempt_id, kind, broker_evidence_key)`; the
broker evidence key is stored as a domain-separated digest. Same-key replay is
a no-op only when the exact payload matches; conflicting evidence is refused.
Native evidence cannot use the reserved coordinator keys `dispatch` and
`lineage-ack`, so a native event cannot impersonate a dispatch outcome. Readers
also require dispatch kind `ack`/`unknown` and an exact integer `claim_row_id`
matching the current claim; malformed or foreign-claim dispatch data holds the
account and cannot authorize release.

Fresh-session `rediscover_unreleased_claims()` reads surviving binary claims
and their attributed evidence. **Every surviving claim blocks the account**,
including a definite ACK and a terminal readback, until it is released by a
later proof-backed workflow. An ACK supplies an exact ODNO for
`readback_by_exact_odno`; it never erases an earlier unknown. This reader sends
only that attributed ODNO to the operator-provided `inquire_daily_order_domestic`
reader and requires the returned row's `odno` to match exactly. No ODNO means
no readback callback; symbol/side/quantity/time candidates are not a fallback.
The operator reader must return one authoritative row, not a proximity match.
The adapter checks the actual mock client, host and account fingerprint before
the read. The exact claim creation and matching post-send ACK must fall on the
same KST day; if they span days or cannot be bound, recovery retains the claim without
a read. The broker query uses that one day for both start and end. A returned
row must be on that same day and within the claim-to-ACK time window; later ODNO
reuse is ignored. Exactly one row on the day is required, even when duplicates
are byte-identical. It requires an exact order row, valid order date/time, matching
plan terms, and consistent filled and remaining quantities. Missing, conflicting
or failed reads are reported as uncertain; partial orders remain held and are
reported as partial. Neither can release.

The existing `kis_mock_reconciliation_run` invokes dispatch recovery only when
the operator requests an unscoped run. Preview is read-only; evidence writes
require `dry_run=False, confirm=True, record_dispatch_evidence=True`; the last
flag defaults to false. An unscoped holdings-only apply does not append dispatch
evidence. An exact full fill with the explicit opt-in records a
`terminal_reconciliation` event with `lane_native_terminal_evidence=True`,
`remainder_known=True`, and **`account_position_reconciled=False`**. Without a
position proof its exact claim row ID is reported as
`retained: position proof unavailable`. The current holdings baseline, holdings
listing and mock order ledger are not position proof. The same evidence key is
idempotent on repeat. Missing ODNO is never guessed. There is no recovery schedule
or automatic replay. Absence is never inferred as terminal. A proof-backed release
appends new complete evidence; the existing event cannot be upgraded in place. An empty claim result is reported as clear only after the actual mock
client's fingerprint is checked against the configured physical account.

`release_with_recorded_terminal_evidence` requires a committed
`terminal_reconciliation` event and the dispatch's exact claim row/scope/key/side,
then delegates to the existing `release_with_terminal_evidence` AND gate.
`partial_fill`, `cancel`, `expiry`, `reject` and local/soft cancel alone never
release. The store currently records terminal reconciliation only for an
attributed exact ODNO; absent-ODNO ambiguity remains held for the recovery owner.
The common authoritative-absence predicate is unchanged and is not inferred
from missing store rows, elapsed time or a local ledger state.

Identity configuration is explicitly consumed by the route installer via
`registry_binding.kis_mock_registry_from_environment()`. The optional
`KIS_MOCK_PHYSICAL_ACCOUNT_FINGERPRINT` holds only a fingerprint using §4's
existing derivation. The helper binds **only** the canonical `kr.kis.mock` row;
it rejects malformed or conflicting pins and leaves every other row unchanged.
The send boundary still compares the actual mock client's fingerprint, host
and namespace to this binding before every POST. No full account number or
credential is placed in configuration examples or durable evidence.

**Identity alone does not authorize activation.** With no fingerprint the row
remains UNKNOWN/BLOCKED. With a fingerprint but no activation opt-in it becomes
identity-known and physical scope derivation is available, while execution
readiness remains BLOCKED.

### Default-off single-run E3 activation configuration (required for the run)

`KIS_MOCK_E3_ACTIVATION_ENABLED` is required for the E3 run, defaults off, and
accepts only the exact string
`true` as opt-in. `e3_activation.E3_ACTIVATION_CONTRACT` records the approved
configuration, version `kis-mock-e3-activation/v1`. The operator sets the flag
only on the foreground route process for the single operator-present run; this
PR changes no host environment and installs no route.

| Binding | Approved E3 value |
|---|---|
| Lane / account | `kr.kis.mock` / `kis_mock` only |
| Quantity per intent | exactly 1 share |
| Notional per intent | `max_notional_krw < 100000` (whole KRW: at most 99,999) |
| Intent expiry | at most 30 minutes after authorization |
| Working orders | at most 1 per intent |
| Scope | one intent in the single E3 run |
| Owner | E1 gateway route (`POST /execution-intents/children`), operator present, foreground only |
| Writer | gateway; the registry `writer` flag becomes true only with explicit activation |
| Auto | true only while the explicit activation setting is on |
| Canary | the single operator-present run in `docs/runbooks/execution-intent-gateway.md#run-one-mock-order` |

With opt-in plus a matching mock identity, the configuration helper supplies
these approved policy/cap/owner/canary labels, `activation_status=ENABLED`,
`writer=true` and `auto_order_enabled=true`. The policy binding is the activation
contract version and the SHA-256 of its canonical projection, not a substituted
trading-decision policy. Existing `assert_entry_execution_ready` and physical
scope guards then pass unchanged. The coarse registry fields describe the
single-intent E3 scope: `max_order_notional=99999`,
`max_orders_per_session=1`, `max_open_orders=1`, limit/DAY only, reconcile required,
manual scheduler owner and gateway timing owner. These registry fields are
configuration labels; the operator must store the E1 intent with the per-intent
bounds above, which the existing E1 journal/decision path enforces. The broader
E1 schema and locked runner envelope are unchanged. Readiness is not an E3
result or a replacement for intent validation.

Without opt-in, including when re-evaluating a previously activated effective
row, the helper rebuilds from the canonical row and re-applies only a currently
configured identity. E3 policy/cap/owner/canary labels and their missing-binding
state return to canonical defaults; writer and auto are false and activation
is BLOCKED. Removing the fingerprint too restores UNKNOWN identity. Opt-in without
an identity, with a conflicting pin, or with live/noncanonical account, namespace
or host metadata is refused. The unchanged send boundary still checks the
actual client's identity and transport before every POST. Every other registry
row stays unchanged. Canonical rows remain default-off in code.

This is a bounded manual configuration: the lane status remains observational,
so the existing recurring-authority guard still refuses recurrence. It adds no
scheduler, server, canary execution, runtime provider installation or automatic
activation. Existing route enablement, per-call confirm, attribution, kill state,
coordination, follow-up and broker gates all remain in force.

After schema application and explicit port construction, lifecycle readiness is
`AUTO_READY_PENDING_ACTIVATION`; absent ports still report
`AUTO_READY_BLOCKED_BY_LIFECYCLE`. This is not `AUTO_ENABLED`. The production
route provider remains unset: this amendment neither installs it nor runs a
runtime canary, migration, broker request or scheduler. The implemented readback
adapter alone does not authorize installation: hk 2690's independently verifiable
account-position proof and review remain required before route installation.

### Position proof amendment (hk 2690): what proves `account_position_reconciled`

Operator decision `decision/2026-11-09/rob1474-mock-experiments-go` Q3. This replaces the
statements above that no source proves position, for recovery run with the existing
`record_dispatch_evidence=True` opt-in. The route provider stays uninstalled.

**Baseline** is the exact-account holding of the plan symbol, read through the same complete
balance reader, and committed once per order attempt to the append-only
`review.kis_mock_position_baseline` **before** the claim row exists. The read must be at most 300
seconds older than the claim. The installed route must call
`KISMockEvidenceStore.record_position_baseline` before reserving; until then no baseline exists
and every claim is retained as `position_baseline_unavailable`. The ledger's
`holdings_baseline_qty` is not a baseline.

| # | Invariant | Input | Transition | Enforcement | Dependency | Observable result |
|---|---|---|---|---|---|---|
| I1 | A balance read is complete only if every page is `rt_cd=0` with list `output1` and a non-empty `output2`, and the last `tr_cont` header is present and `D`, `E` or empty | KIS mock `VTTC8434R` pages | continuation, page cap or missing header → unproven | `position_proof.parse_balance_pages` | KIS `tr_cont` signal (ROB-268) | `position_reason=position_read_truncated` / `position_read_malformed` |
| I2 | A failed, missing, malformed, stale or duplicated read is unproven, never zero. Only a complete read that omits the symbol means 0, because KIS omits empty holdings | reader result | anything else → retained | `parse_balance_pages`, `reconcile_position`, `_prove_position` | I1 | claim retained with a closed `position_reason` |
| I3 | The baseline predates the exact claim on one DB clock, matches the plan symbol, and is immutable | baseline row, claim `created_at` | otherwise → unproven | `load_position_baseline`, table triggers | caller records it before reserving | `position_baseline_unavailable` / `position_baseline_stale` |
| I4 | Observed = baseline + fills (buy) or baseline − fills (sell), with fills read from committed events of this attempt | baseline, events, read | mismatch → retained | `reconcile_position`, `attributed_filled_quantity` | I2, I3 | `position_mismatch` |
| I5 | Cancel is terminal only on the attributed row: `cncl_yn=Y`, `cncl_cfrm_qty` equals the unfilled quantity, `rmn_qty=0` | exact ODNO row | else `accepted`/`partial_fill`, or uncertain | `_classify_readback` | live-observed fields; mock unmeasured | `readback=terminal_cancel`, kind `cancel` |
| I6 | Expiry needs the ROB-671 accept-session deadline passed **and** `rjct_qty` equal to the unfilled quantity with `rmn_qty=0`; time alone is nothing | exact ODNO row, clock | otherwise not terminal | `_classify_readback` | `kr_day_order_expiry` | `readback=terminal_expiry`, kind `expiry` |
| I7 | Zero-fill terminal before the deadline needs `tot_ccld_qty=0`, `rjct_qty=ord_qty`, `rmn_qty=0`, and still needs position proof | exact ODNO row | else not terminal | `_classify_readback` | I9 | `readback=zero_fill_terminal`, kind `reject` |
| I8 | Evidence binds the exact attributed ODNO on the claim's KST day inside the claim-to-ACK window; reuse on another day never matches, absence is never evidence | row, lineage | otherwise uncertain | `_classify_readback`, `readback_by_exact_odno` | ROB-1461 ODNO reuse rule | `readback_incomplete_or_inconsistent` |
| I9 | Release = terminal evidence AND position proof AND the common AND gate AND exact claim owner, deleting exactly one row | all above | any miss → retained | `_recover_one`, `release_with_recorded_terminal_evidence`, `release_with_terminal_evidence` | existing gate unchanged | `status=released` only then |
| I10 | Position proof is attributable only when this is the sole surviving claim on the account | rediscovered claims | otherwise retained | `recover_kis_mock_dispatch_claims` | account uncertainty gate | `position_other_claim_unresolved` |
| I11 | The store refuses `account_position_reconciled=True` unless the attached proof recomputes against the stored baseline and the fills committed for this attempt; read freshness stays a producer check | evidence, baseline | otherwise conflict, no write | `record_native_evidence` | I3, I4 | `KISMockEvidenceConflict` |
| I12 | Proof-backed release appends a new event; an earlier unproven event is never upgraded | evidence key | new key binds order and balance digests | `_append` uniqueness | existing append-only tables | one additional `terminal_reconciliation` |

**Still not proof:** the ledger holdings baseline, `fetch_my_stocks` (it hides truncation and
filters zero rows), a local or soft cancel, elapsed time, an absent order row, and a partial
fill with a working remainder. Whether the KIS mock server populates `cncl_yn`,
`cncl_cfrm_qty`, `rjct_qty` and the `tr_cont` header as the live server does is unmeasured; if it
does not, claims stay retained.

## 7. Follow-up mutations

`describe_claim_followup` reports **capability only**. A cancel or reduce is
permitted only when the merged common capability is present **and** an exact
attributed native order id **and** a known broker remainder **and** fresh guards
**and** current grant/claim ownership all hold. Otherwise the lane holds and
makes zero mutation calls.

`authorize_kis_mock_claim_followup` no longer accepts a default that supplies
`lease_ownership_verified=True`. It requires a **live** grant, **re-asserts** it
at authorization time (ownership can be lost between the ledger read and the
decision), and requires that grant to own *this exact* durable claim
(`grant.owns_claim(account_scope=..., idempotency_key=...)`).

> 🔴 **Operator-visible consequence.** While this lane is
> `AUTO_READY_BLOCKED_BY_LIFECYCLE` there is no grant and no durable claim for a
> ledger row to own, so **`cancel_order(account_mode="kis_mock")` now fails
> closed** with `reason_code="claim_followup_not_authorized"` and makes zero
> broker calls. That is the contracted outcome of B-6, not an incidental
> regression — but it removes a working operator action, and it stays removed
> until the lane is coordinated. Unblocking it needs the same two decisions §6
> lists, plus a ledger row that carries its J2B claim identity.

Corrected here: the KIS mock cancel path used to substitute `quantity = ... or 1`
when the ledger quantity was unusable — an invented broker instruction — and it
soft-cancelled the ledger row when the forwarding org number was missing. Both
are gone. A soft cancel now carries `terminal_evidence: False` and
`claim_released: False`: it is a local bookkeeping note about an order that may
still be live at the broker, not terminal evidence and not a claim release.

### Amendment: kis_mock gateway-origin orders (ROB-1461)

The desk decided this in `at-e3b-decision-1`: option A, scoped. It amends this section and §6 for **one origin only**: a kis_mock order sent by the execution intent gateway (ROB-1440). Everything above stays in force for every other order.

**What changes.** For a gateway-origin order, the durable claim is the gateway journal row in `review.execution_child_requests`, and the lifecycle owner is `KisMockForwarder`. It mints the capability with `issue_kis_mock_followup_capability`, using a new short lease. It holds the receipt only for that one amend or cancel call. No migration is needed.

**What the receipt binds to.** `followup_refusal` checks all of these before it mints:

| Binding | Check |
|---|---|
| Account | The intent's account is in `MOCK_ACCOUNTS` |
| Intent | The journal belongs to this intent |
| Target key | The target idempotency key is in that journal |
| Broker order id | That row is a submit or amend that was not refused, and its journaled order id is exactly the one this call names |
| Ledger correlation | The `kis_mock_order_ledger` row for that order id carries `intent-gateway:<root key>`, where the root is the submit of that journal lineage |
| Terms | The ledger symbol, side and instrument match the intent |
| Open order | The ledger state is `submitted`, `accepted`, `pending` or `fill`, and the remainder is known and above zero |

The issuer reads the journal row again inside the lease. If the row no longer matches, nothing is sent.

**What does not change.**

- **Other origins stay closed.** An order from J2B, the coordinated lane, a mirror or an operator has no `intent-gateway:` correlation for a journal lineage, so it cannot mint. `cancel_order(account_mode="kis_mock")` called directly on such an order still fails closed with zero broker calls. `tests/services/execution_intent_gateway/test_followup_capability.py` pins this, along with the non-mock case.
- **The route stays uninstalled.** This PR does not call `set_kis_mock_coordination_route_provider`. The §6 evidence amendment supplies ports and identity configuration, but schema application, signed activation bindings and route installation remain operator preconditions. While no route is installed, `run_kis_mock_send` refuses every submit, so no gateway order has a broker order id, and E1 refuses every amend and cancel with `target_not_working`. So the mint cannot reach the broker until an operator installs a route, which remains a separate, approval-gated decision.
- **`authorize_kis_mock_claim_followup` is unchanged.** It still requires a live grant. The gateway does not use it, because a gateway follow-up runs outside `coordinate_kis_mock_mutation` and has no grant.
- **The verifier is unchanged.** `verify_kis_mock_followup_capability` checks the receipt as before.

**Remaining risk.** A journal row is a binary claim. It is not the typed dispatch evidence §6 asks for, so it records no `partial_fill` or `terminal_reconciliation`. The gateway journal and the ledger are the only evidence for these orders. An installed route that later writes J2B claim identity onto the ledger row needs its own review of this amendment, because `_kis_mock_expected_claim` would then compare that identity with this receipt and refuse a mismatch.

## 8. C5 — carried forward, still `UNKNOWN`

J3A deliberately left the `TaskGroup` / `asyncio.timeout` cancellation-count
question `UNKNOWN`, and J4-V confirmed that state through five rounds. J3B is a
recipient of that item and does **not** close it.

J3B cannot resolve a question about J3A's internal retained-task and
cancellation-count semantics from outside the primitive; asserting otherwise from
the lane would be exactly the kind of claim-beyond-evidence this program has
rejected eleven times. What the lane *can* do, and does, is guarantee it
introduces no new instance of the unknown: a static test fails if the adapter
references `asyncio.TaskGroup`, `asyncio.timeout`, or `asyncio.wait_for` anywhere,
so no coordinated section is ever wrapped in either construct by this lane.

**C5 status: `UNKNOWN`, unchanged. Owner: J3A/J4-V, not J3B.**

## 9. B-2's env-gate clause — closed

B-2 also requires that no low-level `is_mock=True` place/cancel/modify reach HTTP
*merely because an env gate is false*. Round 1 left this open on the argument
that arming the guard unconditionally would break tests outside the write fence.

**That argument was wrong, and it was measured rather than assumed the second
time.** Arming the mock branch for every `is_mock=True` mutation breaks nothing:
the full mock-order regression set (61 files) stays green. The guard no longer
consults `KIS_MOCK_RUNNER_ENABLED` at all.

`KIS_MOCK_RUNNER_ENABLED` still arms the runner and is otherwise untouched; what
changed is that it can no longer switch this safety guard *off*.

The live branch is unaffected by construction: `is_mock=False` returns from the
wrapper before any lane code runs, so there is no path by which J3B observes or
alters a live request.

## 10. The coordinated production route

`order_execution._execute_and_record` calls `run_kis_mock_send(...)` for every
`equity_kr` mock send. That function asks
`resolve_kis_mock_coordination_route(...)`:

- **route present** → the entire send, transport included, runs inside
  `coordinate_kis_mock_mutation`, inheriting J3A's ordering: lineage persist →
  lease → uncertainty gate → binary claim → re-assertion → callback → retained
  durable writes → conditional release;
- **no route** → the lane is `AUTO_READY_BLOCKED_BY_LIFECYCLE` and **nothing is
  sent**: `run_kis_mock_send` raises. An earlier revision sent anyway and only
  withheld the AUTO label, which takes the label off a bypass rather than
  closing it; the adapter is the final enforcement point, so the send does not
  happen.

The route provider remains `None` in production. The §6 evidence amendment
supplies the store and configurable fingerprint; schema application and signed
activation bindings are still operator preconditions. Installing one is a separate, approval-gated decision; it is a single
`set_kis_mock_coordination_route_provider(...)` call, deliberately not made here.

## 11. Known unguarded surface — the US (`us.kis.mock`) lane

`app/services/brokers/kis/overseas_orders.py` exposes three POST sites —
`order_overseas_stock`, `cancel_overseas_order`, `modify_overseas_order` — that
accept `is_mock=True` and reach the transport with **no coordination authority
at all**. `us.kis.mock` is a canonical J2A lane on the same
`openapivts.koreainvestment.com:29443` host, so by the §86 boundary statement
this is an open KIS mock mutation boundary.

It is **not closed here**. J3B's scope is the KIS mock **KR** lane, and that
module is outside this job's write fence; closing it is a separate job
(operator §87 ④). The KR enumeration test discovers POST sites structurally, so
a new KR bypass fails the build; the US sites are enumerated only so that a new
one cannot appear unnoticed, and their guard state is deliberately not asserted
— a test that pinned "unguarded" would defend the defect.

