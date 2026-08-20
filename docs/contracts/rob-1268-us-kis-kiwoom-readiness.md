# ROB-1268 — US KIS / Kiwoom lane lifecycle-recovery contract

## Scope and authority

This is a contract-only artifact (J5C).  It consumes the signed registry and
the merged J2B/J3A ports without changing them, and it authorizes no broker,
network, database, scheduler, deployment, credential, or account operation.
It creates no policy, assigns no writer, proposes no cadence, and grants no
canary.

It covers exactly two lanes: `us.kis.mock` and `us.kiwoom.mock`.

Its single subject is the **lane-native lifecycle recovery contract** required
before either lane could become `AUTO_ENABLED`.  Recording that contract is not
a request to enable anything; activation is a separate approval outside this
document.

### Relationship to ROB-1266 (READ ONLY)

`docs/contracts/rob-1266-us-readiness.md` is the merged J5A contract and is the
authority for these two lanes' registry records and capability gaps.  This
document **references and quotes it; it does not re-render, restate, redefine,
or modify it.**  Registry facts are cited by path and section number rather
than reproduced, so that no second rendering of the same fact can drift from
the first.  Where this document and ROB-1266 disagree, **ROB-1266 is
authoritative** and this document is the defect.

Where this document and the registry disagree, **the registry is
authoritative** and this document is the defect.

`role` is a purpose-only registry value.  It is not execution authority.  The
two lanes' roles, statuses, writer flags, and owner fields are recorded in
`docs/contracts/rob-1266-us-readiness.md` §8 and are not repeated here.

## 1. Document-level fixed tokens

```text
SIGNED_SOURCE = app/services/mock_lane_registry.py::CANONICAL_LANE_REGISTRY @ e057941425d2ea7d35a36ebf6074a6c70eba3013
LINEAGE_SOURCE = app/services/mock_integration/lineage.py @ 094ab2d59d6f2bf5fc3df4efa43bb5d412221ffd
COORDINATION_SOURCE = app/services/mock_integration/coordination.py @ 03beecc5f53e636c352ddf0527aa3d98ddc7bd61
US_READINESS_CONTRACT = docs/contracts/rob-1266-us-readiness.md @ ddf4895ece2ca9dff8daf1a04fa7d6143f43c899
LIFECYCLE_RULE_SOURCE = gptpro-interim-verdict-20260816.md @ 233c69805f02791c327b3415ff4bc75eaf3d72bfbc3c284713fa995a96199b34
```

Each token appears exactly once in this document.

## 2. The lifecycle rule being applied

The controlling rule is quoted verbatim from `LIFECYCLE_RULE_SOURCE`:

> The common coordination layer does not own broker-specific retry,
> readback, or manual-resolution queues.
>
> Before a lane can become AUTO_ENABLED, its lane contract must identify:
>
> - exactly one recovery owner;
> - the trigger that rediscovers surviving durable claims after restart;
> - the authoritative broker readback operation;
> - the lane-native evidence written for ACK, unknown, reject, expiry,
>   partial fill, cancel, and terminal reconciliation;
> - the condition for exact release_if_matches;
> - the operator-visible blocked state when authoritative recovery is not
>   possible.
>
> A lane missing any of these remains
> AUTO_READY_BLOCKED_BY_LIFECYCLE.

Two consequences are binding on this document:

1. **No broker-specific retry, readback, or manual-resolution queue may be
   added to the common coordination layer.**  J3A already refuses to own one:
   `app/services/mock_integration/coordination.py` records that its held-lease
   map "is **not** a retry queue, **not** a durable state store, and it has no
   TTL, janitor, takeover, automatic retry, claim deletion, or scheduler"
   (`_HeldCoordination`), and that "there is deliberately no recovery API in
   this epoch" (`_release_guarded`).  This contract adds nothing there.
2. **`AUTO_READY_BLOCKED_BY_LIFECYCLE` is recorded on a dedicated axis, not on
   `lane_status`.**  `_SIGNED_LANE_STATUS_ALLOWLISTS` in `SIGNED_SOURCE` admits
   a single status for each of these two lanes, so writing the lifecycle verdict
   into `lane_status` would violate the signed registry.  That admitted status is
   **not restated here**: it is recorded once, by ROB-1266 §8, and this document
   neither repeats nor pins it.  The verdict is carried by
   `lifecycle_recovery_owner_status` in §4, and `lane_status` is left untouched.

## 3. Record shape

Each lane is recorded as a `### LANE <lane_id>` heading followed by exactly two
fenced blocks, in this order:

1. a **LIFECYCLE** block whose first line is `# LIFECYCLE`, and
2. an **EVIDENCE** block whose first line is `# EVIDENCE`.

- The LIFECYCLE block's key set is exactly:
  `lane_id`, `lifecycle_recovery_owner_status`, `recovery_owner`,
  `restart_rediscovery_trigger`, `authoritative_readback_operation`,
  `release_if_matches_condition`, `operator_visible_blocked_state`,
  `unmet_lifecycle_items` — eight keys, no extras, no omissions.
- The EVIDENCE block's key set is exactly the seven evidence kinds named by the
  rule in §2: `ack`, `unknown`, `reject`, `expiry`, `partial_fill`, `cancel`,
  `terminal_reconciliation` — seven keys, no extras, no omissions.

Each body line is `<key> = <rendered value>`.  Keys do not repeat within a
block.  Every value begins with one of exactly three states:

- `PRESENT` — a merged in-repo anchor satisfies the item for this lane.
- `PRESENT_CONSTRAINED` — an anchor exists but carries a recorded limitation
  that stops it from satisfying the item unaided.
- `ABSENT` — no merged anchor satisfies the item for this lane.

`PRESENT_CONSTRAINED` and `ABSENT` both count as **unmet** for the purposes of
the rule in §2.  A constrained item is not a partial credit.

## 4. Lane records

### LANE us.kis.mock

```text
# LIFECYCLE
lane_id = us.kis.mock
lifecycle_recovery_owner_status = AUTO_READY_BLOCKED_BY_LIFECYCLE
recovery_owner = ABSENT (this lane's unmet-binding list is owned by rob-1266 §8 and records no bound owner there; that list is not restated here)
restart_rediscovery_trigger = ABSENT (query exists as OrderSendIntentReservationPort.list_reservations in COORDINATION_SOURCE; no lane-native caller is merged and J3A states there is deliberately no recovery API in this epoch)
authoritative_readback_operation = PRESENT_CONSTRAINED (KISOverseasOrders.inquire_daily_order_overseas(is_mock=True) dispatching VTTS3035R; not order-id keyed because the overseas order_number filter is ignored, and open-order truth is separately unavailable per rob-1266 §5.1)
release_if_matches_condition = PRESENT (DurableSendClaimAdapter.release_with_terminal_evidence gated by _terminal_evidence_authorizes in COORDINATION_SOURCE)
operator_visible_blocked_state = PRESENT (unreleased_authority_holds() in COORDINATION_SOURCE, with reason code terminal_evidence_required)
unmet_lifecycle_items = (recovery_owner, restart_rediscovery_trigger, authoritative_readback_operation, lane_native_evidence)
```

```text
# EVIDENCE
ack = ABSENT (no lane-separable table; see §5.1)
unknown = ABSENT (no lane-separable table; see §5.1)
reject = ABSENT (no lane-separable table; see §5.1)
expiry = ABSENT (no lane-separable table and no expiry status; see §5.1)
partial_fill = ABSENT (no lane-separable table and no remainder column; see §5.1)
cancel = ABSENT (no lane-separable table; see §5.1)
terminal_reconciliation = ABSENT (no lane-separable table; see §5.1)
```

### LANE us.kiwoom.mock

```text
# LIFECYCLE
lane_id = us.kiwoom.mock
lifecycle_recovery_owner_status = AUTO_READY_BLOCKED_BY_LIFECYCLE
recovery_owner = ABSENT (this lane's unmet-binding list is owned by rob-1266 §8 and records no bound owner there; that list is not restated here)
restart_rediscovery_trigger = ABSENT (query exists as OrderSendIntentReservationPort.list_reservations in COORDINATION_SOURCE; no lane-native caller is merged and J3A states there is deliberately no recovery API in this epoch)
authoritative_readback_operation = PRESENT_CONSTRAINED (KiwoomUSAccountClient.get_today_orders api-id ust21510 and get_open_orders api-id ust21050 on path /api/us/acnt; constrained because Market.US_EQUITY is absent from BROKER_CAPABILITIES[Broker.KIWOOM] per rob-1266 §5.3)
release_if_matches_condition = PRESENT (DurableSendClaimAdapter.release_with_terminal_evidence gated by _terminal_evidence_authorizes in COORDINATION_SOURCE)
operator_visible_blocked_state = PRESENT (unreleased_authority_holds() in COORDINATION_SOURCE, with reason code terminal_evidence_required)
unmet_lifecycle_items = (recovery_owner, restart_rediscovery_trigger, authoritative_readback_operation, lane_native_evidence)
```

```text
# EVIDENCE
ack = ABSENT (no Kiwoom order ledger exists; see §5.2)
unknown = ABSENT (no Kiwoom order ledger exists; see §5.2)
reject = ABSENT (no Kiwoom order ledger exists; see §5.2)
expiry = ABSENT (no Kiwoom order ledger exists; see §5.2)
partial_fill = ABSENT (no Kiwoom order ledger exists; see §5.2)
cancel = ABSENT (no Kiwoom order ledger exists; see §5.2)
terminal_reconciliation = ABSENT (no Kiwoom order ledger exists; see §5.2)
```

## 5. Evidence-surface findings

### 5.1 `us.kis.mock` — the only KIS mock ledger is not lane-separable

`KISMockOrderLedger` (`app/models/review.py`, table `kis_mock_order_ledger`)
is the sole KIS mock order ledger in the tree.  Its table arguments pin
`account_mode = 'kis_mock'` and `broker = 'kis'` as CHECK constraints, and it
carries no market, venue, or lane column.  `kr.kis.mock` and `us.kis.mock`
would therefore write rows that are **indistinguishable at the constraint
level**; the `currency IN ('KRW','USD')` CHECK permits both currencies in the
same table.

Consequently the table cannot serve as *lane-native* evidence for
`us.kis.mock`: a row found there cannot be attributed to this lane rather than
the KR lane without a discriminator that does not exist.  This is recorded as
absence of lane-native evidence, **not** as a claim that the KR lane's evidence
is defective for its own lane.

Two further shape limits are recorded, and they hold regardless of the
attribution problem above:

- `status` is CHECK-constrained to `('accepted','rejected','unknown')` — three
  values that cover ACK, reject, and unknown but express neither expiry, nor
  cancel, nor terminal reconciliation.
- There is no remainder or filled-quantity column, so a partial fill cannot be
  distinguished from a full fill by this table.

Creating, altering, or backfilling any table is outside this contract.

### 5.2 `us.kiwoom.mock` — no order ledger exists at all

No Kiwoom order ledger model exists.  The only occurrence of `kiwoom` in
`app/models/review.py` is the `account_mode` CHECK list of
`trade_retrospectives`, which is a retrospective-scoped table and not an order
lifecycle ledger.  All seven evidence kinds are therefore absent for this lane
by construction, not by omission of a lookup.

### 5.3 `us.kis.mock` shares a credential namespace with `kr.kis.mock`

In `SIGNED_SOURCE`, `LANE_CREDENTIAL_NAMESPACES` maps `kr.kis.mock` and
`us.kis.mock` to **the same string**, and `LANE_ALLOWED_HOSTS` maps them to
**the same host tuple**.  The two Kiwoom lanes are mapped apart on the same
axis.  The values themselves are registry-owned and are deliberately not copied
into this document; the recorded fact is the **equality relation** between the
two KIS rows, which is what the prohibition below rests on.

ROB-1266 §6 establishes that the four *US* lanes are declared under distinct
namespaces; that statement is scoped to those four rows and does not speak to
the KR↔US KIS pair.  This document records the pair as a **declared
collision**: the two lanes are not merely unproven-separate, they are declared
under one namespace and one host, while ROB-1266 §8 records both as having no
physical account and an unproven identity.

This is the direct basis for the concurrent-writer prohibition in §6.4.  It is
recorded as a fact about declarations; no physical-account measurement exists
and none is claimed.

## 6. Pre-submit invariants

These are contract rules for any future lane implementation.  Nothing here
grants a send; every rule is a refusal.  Each rule is stated so that its
violation is detectable before broker I/O.

### 6.1 Open-order truth is required before any KIS submit

If `open_order_count` is absent, `None`, or the inquiry errored, the lane must
stop before broker I/O.  A missing count is *unknown* and is never read as
zero.  For `us.kis.mock` this is presently unsatisfiable at all, because the
overseas pending-order inquiry raises for the mock lane (rob-1266 §5.1), so the
only contract-conforming outcome for this lane today is *stop*.

### 6.2 A writer requires masked physical identity

`writer` may not be set true for either lane while ROB-1266 §8 records that
lane as having no physical account and an unproven identity.  Masked physical
account fingerprint evidence is the precondition; no such evidence exists
today, and namespace or profile distinctness is a declaration rather than a
measurement (rob-1266 §6).

### 6.3 Kiwoom US keeps its recorded role

`us.kiwoom.mock` keeps for this epoch exactly the role ROB-1266 §8 records for
it; that value is registry-owned and is not restated here.  Rewriting that role
— in particular promoting the lane to an automatic mirroring role — requires a
separate approved autonomous policy and is not authorized here or by
ROB-1266 §5.3.

### 6.4 KR and US KIS may not hold concurrent writers

While it is unproven that `kr.kis.mock` and `us.kis.mock` resolve to different
physical accounts, the two may not hold writers concurrently.  §5.3 records
that they are declared under one credential namespace and one host, which
strengthens rather than weakens this prohibition.  Proof of separation requires
masked physical-account fingerprint evidence that does not exist.

### 6.5 Replay across process restart is forbidden

Re-running the same immutable send lineage after a process restart must produce
zero new broker POSTs.  The durable binary claim is the mechanism: J3A records
that "process death may end the ephemeral DB session, but the durable binary
reservation survives and keeps preventing a replay of that exact send lineage"
(`_HeldCoordination` in `COORDINATION_SOURCE`).  A successor process that
cannot observe the claim must stop rather than assume absence.

### 6.6 A stale or lost fencing token forbids submit

A submit may not proceed under a stale, lost, or foreign lease grant.
`COORDINATION_SOURCE` refuses this structurally: `AdvisoryLeaseGrant` ownership
is checked by `assert_owned`, and lease loss is reported with the
`lease_lost` reason code.  Neither an expired nor a foreign grant may be
adopted.

### 6.7 KIS and Kiwoom lifecycles are independent

A KIS failure may not cancel, roll back, unwind, or otherwise mutate a Kiwoom
plan, and the reverse likewise.  The two lanes have separate credential
namespaces, separate hosts, separate readback operations (§4), and separate
native order identities; they share no transaction.  Cross-lane compensation is
not a defined operation in this contract.

### 6.8 Currency is exact; no FX or parity

Both lanes carry the single quote currency ROB-1266 §8 records for them; that
value is registry-owned and is not restated here.  An intent denominated in a
different currency may never be rendered into a plan denominated in the lane's
currency, and no FX rate, parity, or conversion may be applied.
J2B already fails such a request closed with the exact literals
`currency_conversion_not_authorized` and `lane_quote_currency_mismatch`
(`LINEAGE_SOURCE`); this contract adds no conversion path and no rounding
tolerance that would obscure one.  A lane's own quote currency is never
treated as interchangeable with any other currency.

## 7. What this document does not do

- It does not modify `app/services/mock_lane_registry.py`,
  `app/services/mock_integration/**`, any broker adapter, any model, any
  migration, or any other production module.  A registry disagreement is fixed
  by a serial J2A follow-up, not from here.
- It does not modify `docs/contracts/rob-1266-us-readiness.md`, which is READ
  ONLY for this job, and it does not re-render any value that document owns.
- It does not change `lane_status`, `activation_status`, `role`, `writer`,
  `auto_order_enabled`, or `scheduler_owner` for any lane.  An absent
  `scheduler_owner` means **owner absent; mutation-ineligible; no authority to
  bind one downstream**, and is not an alternative spelling of `DISABLED`.
- It does not create, approve, or imply a policy, a cap, a cadence, a canary,
  or a scheduler registration.
- It does not add a retry queue, readback queue, or manual-resolution queue to
  the common coordination layer, and it does not name a recovery owner that
  does not exist.
- It does not move either lane toward `AUTO_ENABLED`.  Recording the lifecycle
  contract is the deliverable; activation is a separate approval.
