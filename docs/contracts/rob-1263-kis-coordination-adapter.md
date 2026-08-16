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

Immediately before **every** real KIS mock mutation HTTP attempt — re-sends
included, which is why it is wired as the transport's `pre_send_hook` rather
than run once per callback — `assert_kis_mock_send_boundary` requires all of:

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
  releases before broker contact — and only the exact matched reservation;
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

## 9. Known gap this job could not close inside its fence

B-2 also requires that no low-level `is_mock=True` place/cancel/modify reach
HTTP *merely because an env gate is false*. Today `_guard_kis_mock_writer`
enforces the writer singleton only when `KIS_MOCK_RUNNER_ENABLED` is truthy; with
the gate false the guard is a no-op and a manual mock mutation proceeds with no
distributed authority at all.

That half is **not closed here**, for two reasons that are themselves contract
boundaries: the job forbids env-gate changes, and arming the guard
unconditionally would change existing manual ownership semantics and require
edits to test files outside the J3B write fence. The boolean half of the same
sentence *is* closed (§3).

This gap is one of the reasons the lane remains
`AUTO_READY_BLOCKED_BY_LIFECYCLE`, and it needs an ownership amendment before it
can be fixed.
