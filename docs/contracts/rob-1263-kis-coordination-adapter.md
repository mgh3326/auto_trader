# ROB-1263 J3B — KIS mock coordination adapter

The KIS mock lane's adapter over the merged ROB-1262 (J3A) coordination port.

- Source: `app/services/kis_mock_runner/singleton.py`
- Tests: `tests/services/mock_integration/test_kis_coordination_adapter.py`
- Consumes (read/import only): J2A `mock_lane_registry`, J2B
  `mock_integration/lineage` + `brokers/client_order_ids`, J3A
  `mock_integration/coordination`, and the existing `order_send_intent_service`.

J3B opens no socket of its own, signs nothing, loads no credential value,
registers no scheduler, adds no model and no migration.

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
| **recovery owner** (exactly one) | the operator-run KIS mock reconciler in `app/mcp_server/tooling/kis_mock_ledger.py` (`KISMockLifecycleService`) |
| **restart trigger** | operator-invoked reconciliation over `review.order_send_intents` rows whose `account_scope` is this physical account's claim scope and whose lineage has no durably attributed `broker_order_id` |
| **authoritative broker readback** | KIS `inquire_daily_order_domestic`, keyed by the exact attributed ODNO — never symbol/side/quantity/time proximity |
| **exact `release_if_matches` condition** | `release_with_terminal_evidence` with `TerminalClaimEvidence(lane_native_terminal_evidence, account_position_reconciled, remainder_known)`, or a proven `authoritative_absence_proven` + `account_position_reconciled`; the underlying `OrderSendIntentService.release_if_matches` is never called directly |
| **operator-visible blocked state** | `AUTO_READY_BLOCKED_BY_LIFECYCLE` |

**Lane-native evidence — all seven required** (`KIS_MOCK_LANE_EVIDENCE_KINDS`):
`ack`, `unknown`, `reject`, `expiry`, `partial_fill`, `cancel`,
`terminal_reconciliation`.

### Current status: `AUTO_READY_BLOCKED_BY_LIFECYCLE`

This lane does **not** satisfy the six elements today, and
`coordinate_kis_mock_mutation` fails closed with
`kis_mock_lifecycle_ports_unavailable` before any lease, claim, or callback when
they are absent. Two independent blockers:

1. **No durable dispatch-evidence store exists.** J3A forbids
   `review.order_send_intents` as a `DispatchEvidencePort` target (it is a binary
   reservation, not a state store), and a new durable store would require a
   migration — which this job's boundary sets to zero. The lane therefore cannot
   durably record `unknown` / `partial_fill` / `terminal_reconciliation` as typed
   evidence.
2. **The canonical `kr.kis.mock` registry row has no bound identity.**
   `physical_account_id` is `None`, `identity_status` is `UNKNOWN`, `writer` and
   `auto_order_enabled` are `False`, and `activation_status` is `BLOCKED`, so
   `assert_entry_execution_ready` and `physical_account_scope_for_entry` both
   reject it. The coordinated AUTO path is structurally unreachable in production
   until an operator binds an identity — which is a separate, approval-gated
   decision.

Nothing here claims `AUTO_ENABLED`, and no runtime canary is in scope.

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

The route provider is `None` in production, for the two independent reasons in
§6. Installing one is a separate, approval-gated decision; it is a single
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

