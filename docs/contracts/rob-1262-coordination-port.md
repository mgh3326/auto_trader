# ROB-1262 J3A — coordination port (lease / fence)

Broker-neutral coordination for one **physical** mock/paper/demo account:
a PostgreSQL session advisory lease, a durable binary send reservation,
lane-owned durable evidence ports, and an injected mutation callback.

- Source: `app/services/mock_integration/coordination.py`
- Tests: `tests/services/mock_integration/test_coordination.py`
- Consumes (read/import only): J2A `mock_lane_registry`, J2B
  `mock_integration/lineage` + `brokers/client_order_ids`, and the existing
  `order_send_intent_service`.

J3A imports **no broker transport**, opens no socket, signs nothing, and loads no
credential value. It registers no scheduler, adds no model and no migration.

---

## 1. This is NOT broker-enforced fencing

> **The J3A physical-account lease is process coordination only. It is NOT
> broker-enforced fencing.**

Holding the lease proves that no *other holder of this same lease* is mutating
the account. It proves nothing about the broker. No broker below accepts a
fencing token, so a stale deployment, an out-of-repo process, or a human at a
broker console reaches the same account without ever contending for this lease.

"The lease is held, therefore the broker will reject everyone else" is the most
expensive available misreading of this module.

### Lane matrix

Every canonical J2A lane, with no exception. A lane that later gains real
broker-side fencing must add its own evidence rather than reinterpreting a row.

| Lane | Fencing |
|---|---|
| `kr.kis.mock` | `not_broker_enforced` |
| `kr.kiwoom.mock` | `not_broker_enforced` |
| `us.kis.mock` | `not_broker_enforced` |
| `us.kiwoom.mock` | `not_broker_enforced` |
| `us.alpaca.paper.default` | `not_broker_enforced` |
| `us.alpaca.paper.lab` | `not_broker_enforced` |
| `crypto.binance.spot_demo.canonical` | `not_broker_enforced` |
| `crypto.binance.spot_demo.b0x_sidecar` | `not_broker_enforced` |
| `crypto.alpaca.paper.default` | `not_broker_enforced` |
| `crypto.alpaca.paper.clean` | `not_broker_enforced` |
| `crypto.upbit.shadow` | `not_broker_enforced` |
| `crypto.binance.futures_demo` | `not_broker_enforced` |

The same table is machine-readable as `coordination.LANE_FENCING_MATRIX`.

---

## 2. Identity and scope

The lock scope is derived from the canonical J2A `physical_account_id` of a
validated, identity-known registry entry. A caller-supplied scope is never
accepted, and the raw identifier is never logged, serialized, or stored.

```python
d = sha256(b"mock-physical-account-v1\0" + physical_account_id.encode("utf-8")).digest()
claim_account_scope = "mockpa:v1:" + d.hex()
advisory_key        = int.from_bytes(d[:8], byteorder="big", signed=True)
```

Two logical lanes on one physical account derive the same scope and key. Unknown
or blank identity fails before any lease, persistence, reservation, or callback.

J2A's `PolicyBinding`, exact lane guards, and reject literals are **consumed, not
redefined**. `assert_lineage_registry_binding` then `assert_entry_execution_ready`
both run before anything else.

---

## 3. The advisory lease

Authority is a **dedicated** connection holding a session-level
`pg_try_advisory_lock(bigint)`. There is **no TTL**, no heartbeat, no automatic
takeover, and **no file-lock fallback** — the existing KIS PID file remains a
later J3B *diagnostic* seam, never authority.

Acquisition:

1. verify the authority can prove backend-session termination;
2. read and retain `pg_backend_pid()`;
3. `pg_try_advisory_lock` each key in deterministic global numeric order
   (de-duplicated first) — `false` ⇒ `lease_contended`;
4. **explicit COMMIT**;
5. in the new transaction, re-read the PID and prove every exact `pg_locks` row.

A row proves ownership only when *all* hold: `locktype='advisory'`,
`mode='ExclusiveLock'`, `granted`, database oid, retained PID, `objsubid=1`, and
the **reconstructed signed high/low 32-bit halves**. `objid` is never compared to
the signed key directly — for a negative key it never matches.

Ownership is re-proven — PID plus exact-row — immediately before every mutation;
there is no heartbeat, so an idle lease is never assumed. The lane does not hold a
grant to do that with: it calls `await scope.assert_owned()` on the
`CoordinationScope` it is handed (see §5.1).

Multi-key: partial-acquire rolls back every acquired key in reverse order;
release unlocks in reverse order and counts only keys PostgreSQL confirmed.

### Release, and what does not count as one

Release is exactly two things:

- an **attested** owner/key-matched `pg_advisory_unlock` whose boolean is `true`
  for every key, then close; or
- a **positive termination receipt** bound to the exact backend PID and owner
  token.

`close()` and a pool return are **never** termination. An ambiguous driver error
is not a receipt. When neither can be proven, the lease is not marked released
and an auditable `UnreleasedAuthorityHold` is recorded.

A single `pg_advisory_unlock` returning true is **not** proof either. Session
advisory locks stack per backend, so acquisition first proves this backend holds
none of the target keys, and release re-reads the same backend afterwards to prove
every row is gone. Only that pair survives re-entrancy.

Release is cancellation-safe: it delegates to one retained inner task, captures
outer cancellation, and re-raises only after a safe definite outcome. Every
failure inside the critical section — a `CancelledError` included — means the
outcome is *unknown*, never that the lock was released.

Whether a failed release can be retried at all depends on who owns it, and
`UnreleasedAuthorityHold.recoverable_in_process` is recomputed on every read to
report exactly that — a hold called recoverable when nothing can reach it is the
same kind of false report as one called held after it was released.

| Owner state | Evidence | `recoverable_in_process` | Why |
|---|---|---|---|
| unsealed standalone lease | durable-true | `True` | the owner still holds its exact private lease/grant and may retry |
| unsealed standalone lease | durable-false | `False` | release is refused for every caller, the owner included |
| coordination-sealed lease | durable-true | `False` | a sealed lease is never unsealed after a failed release |
| coordination-sealed lease | durable-false | `False` | both blockers apply |
| partial-acquisition rollback | — | `False` | there is no owning lease at all |

The flag is the negation of the two things that block a release: a permanent
coordination seal, and missing durable evidence. There is no in-process recovery
API in this epoch, and the presence of private machinery is not a supported retry
path. Stale or foreign grants are always rejected.

---

## 4. Durable claim

`review.order_send_intents` is a **binary reservation only** — not a lifecycle
store, not a hold store, not a broker-order-id store, not a retry queue. J3A
adapts the existing `OrderSendIntentService` and never edits it into a state
machine. The unrestricted `release` is not part of the consumed port.

An existing reservation on the physical account is the account block. It is
removed only by an evidence-gated `release_if_matches` after lane-native terminal
evidence **plus** account/position reconciliation. Unknown, anomaly, rejection
without proven absence, and partial fills with an unknown remainder all retain
it. Nothing releases a claim automatically, and there is no clock in this module
to make that possible.

---

## 5. Order of operations

1. canonical J2A lane/identity/policy validation
2. require **both** lane-supplied durable write ports
3. persist the immutable envelope — a cancellation observed here aborts before
   any lease, reservation, or send
4. acquire the physical-account lease
5. check account-wide unresolved reservations
6. reserve and COMMIT the binary claim
7. re-assert lease ownership and re-run the lane guards
8. register the strong held-coordination handle and seal the lease, then invoke
   the callback with its `CoordinationScope` (no `await` between those two)
9. persist the ACK/uncertainty envelope **and** the typed dispatch evidence —
   both retained against cancellation, forming a single AND gate
10. release the lease and drop the handle **only if** step 9 closed

The durable claim is never released here.

### 5.1 The callback interface — what J3B/J3C must do

```python
type MockMutationCallback = Callable[
    [CoordinationScope], Awaitable[MutationCallbackResult]
]
```

The callback takes **one argument**. `CoordinationScope` exposes exactly one
coroutine and nothing else:

```python
await scope.assert_owned()
```

It carries no lease, grant, connection, backend PID, owner token, advisory key,
hold id, release, or termination — the right to see, never the right to act. Each
call re-proves ownership of the exact lease **and** re-checks the pinned canonical
registry entry, so a registry mutated mid-flight fails here too.

**The obligation is per send, not per callback.** A lane must
`await scope.assert_owned()` immediately before **every** POST in a same-cycle
batch, and before **every** supported cancel or reduce — in particular after any
intervening `await` (account truth, token refresh, rate limiting). The
coordinator's single assertion before the callback is deliberately *not*
sufficient: ownership can be lost in that interval, and a failure discovered at
release is discovered after the orders are already out.

The scope stops working when its coordinated section ends, so a captured scope
raises rather than asserting against a finished lease. A lane must also not move
the send into a detached task: a compliant callback awaits `assert_owned()` inside
the callback task the coordinator retains.

### Cancellation

`asyncio.shield` alone is not enough: it lets the caller raise while the socket
write continues, after which a naive `finally` would surrender the lease on an
order that may already be at the broker. The inner task is retained and awaited
to a definite result — completion or durable uncertainty — before any cleanup.

### Held coordination

The handle is created the instant the reservation commits and before the callback
task exists, and is held by a module-owned strong-reference map keyed only by an
opaque hold id. It is a lifetime and safety guard: **not** a retry queue, not a
durable state store, with no TTL, janitor, takeover, automatic retry, claim
deletion, or scheduler. Entries are removed only when both post-send durable
writes succeeded *and* release reached a proven outcome. Process death may end
the ephemeral session, but the durable reservation survives and keeps blocking a
successor.

When the AND gate does **not** close, the lease is marked with an authority hold
carrying `durable_evidence_written=False`, and from that moment the lease is
unreleasable in this process by any caller, the owning lease included.
`held_coordination(hold_id)` returns a capability-free `HeldCoordinationSnapshot`
— no lease, no grant, no connection, no PID, no owner token, nothing callable —
so introspection can *see* a stuck lease and has no way to release it. There is deliberately no recovery API: surrendering the advisory lock
while nobody knows whether an order went out would let an old or non-claim-aware
writer mutate the account concurrently. The coordination handle and the authority
hold share one opaque id, so an operator follows a single thread.

### Resolving a hold

The opposite failure matters too: reporting a resolved hold as unresolved is as
much a defect as releasing early. A hold recorded because the *release itself*
failed (`durable_evidence_written=True`) is cleared — atomically, and only its own
entries — the instant a retry proves a full reverse unlock plus row absence, or a
positive backend-termination receipt. Cleanup happens **before** the fallible
`close()`, so a pool-return error cannot strand an authority that is provably
released. A foreign or stale grant clears nothing, an ambiguous or failed retry
clears nothing, and one lease's hold never touches another's: every delete
compares the stored owner and connection, not just the id.

`unreleased_authority_holds()` and `held_coordinations()` answer "what is
unresolved right now"; `authority_hold_history()` keeps the immutable record.
All three are **capability-free**: they carry no connection, no lease, no grant,
no backend PID, no owner token, and nothing callable. Public here means the right
to *see*; the right to *release* is module-private without exception, because a
PID paired with an owner token is by itself enough to terminate the backend whose
lock is the safety property.

### Strength of this guarantee

Like the repository's Kiwoom read-only lane, this is **accident prevention plus
static detection — not structural impossibility**. One line reaching into a
private attribute still reaches the capability. The boundary is drawn so that such
a line has to be written deliberately, and so that a reviewer can see it.

---

## 6. Dispatch evidence

`DispatchEvidence` is a typed immutable record carrying intent / plan / attempt /
cycle correlation, so an unknown is legible at rest rather than inferred from a
missing field. Kinds — an *evidence* vocabulary, never a reason code:

| Kind | Meaning |
|---|---|
| `acknowledged` | definitive, with a broker order id attached via J2B |
| `definitive_without_broker_id` | definitive, no id reported |
| `lane_reported_uncertain` | the lane could not tell |
| `callback_failed` | the callback raised — the write may still have landed |
| `ack_attachment_failed` | J2B rejected the broker id — also uncertain |

`outer_cancellation_requested` is orthogonal and never overwrites the kind:
cancelling the caller says nothing about what the transport did.

---

## 7. Reason codes

Exactly eight, J3A-owned. `lineage_persistence_unavailable` reuses J2B's literal;
J2B's `LineageReasonCode` is never modified or overloaded.

`lock_authority_unavailable` · `lease_contended` · `lease_lost` ·
`lease_event_loop_mismatch` · `durable_claim_conflict` ·
`lineage_persistence_unavailable` · `terminal_evidence_required` ·
`claim_followup_not_authorized`

---

## 8. Ownership boundaries

J3A provides a **broker-neutral** ordered keyset primitive. It does not choose or
supply any KIS key, does not edit KIS singleton or mutation call sites, and does
not own old-legacy-only compatibility — those belong to J3B, which supplies the
exact KIS physical + legacy keyset and retains both for the whole DAG.

J3A also does not claim host/profile coupling. It validates canonical J2A
lane/identity/policy and coordinates an injected, fake-tested callback; verifying
the real client host and profile at the send boundary is J3B/J3C's job.

`describe_claim_followup` reports **capability only**. It never authorizes a
cancel or reduce, never releases a claim, and cannot be constructed as if it did.
