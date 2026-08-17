# ROB-1264 J3C — Kiwoom coordination adapter

Thin Kiwoom lane adapter around the merged J3A coordination port, J2A
registry, and J2B lineage. This document is the lane-native recovery
contract. It does not copy J3A PostgreSQL SQL, key math, reservation,
cancellation shielding, or reason enums.

- Adapter: `scripts/b0x/kr/kiwoom_ordering.py`
- Cycle wiring: `scripts/b0x/kr/kiwoom_cycle.py`
- Submit/ACK extraction: `scripts/b0x/kr/kiwoom.py`
- Attribution: `scripts/b0x/kr/kiwoom_attribution.py`
- Tests: `tests/services/mock_integration/test_kiwoom_coordination_adapter.py`

Activation to `AUTO_ENABLED` is **out of scope**. Missing any recovery
item below leaves this lane at `AUTO_READY_BLOCKED_BY_LIFECYCLE`.

---

## Lane-native recovery owner (activation precondition)

The common coordination layer does not own broker-specific retry,
readback, or manual-resolution queues.

Before a lane can become AUTO_ENABLED, its lane contract must identify:

- exactly one recovery owner;
- the trigger that rediscovers surviving durable claims after restart;
- the authoritative broker readback operation;
- the lane-native evidence written for ACK, unknown, reject, expiry,
  partial fill, cancel, and terminal reconciliation;
- the condition for exact release_if_matches;
- the operator-visible blocked state when authoritative recovery is not
  possible.

A lane missing any of these remains
AUTO_READY_BLOCKED_BY_LIFECYCLE.

### C3-1 Recovery owner — exactly one

`scripts.b0x.kr.kiwoom_ordering.KiwoomCoordinationAdapter`

Machine constant: `KIWOOM_RECOVERY_OWNER`.
No second owner is named. J3A does not own Kiwoom retry, readback, or
manual-resolution queues.

### C3-2 Restart trigger

`process_restart_rediscovers_durable_j2b_claims_for_physical_account`

On restart the adapter reloads durable J2B `OrderAttempt` rows for the
canonical J2A `physical_account_id` and runs `restart_disposition`.

### C3-3 Authoritative broker readback

`kt00007` — exact row keyed by known native `broker_order_id`.

`kt00009`, local JSONL, cycle artifacts, and wall clock are diagnostic
only.

### C3-4 Lane-native evidence (seven kinds)

Exact `record_lane_evidence("<kind>", ...)` call sites in
`KiwoomCoordinationAdapter` (`scripts/b0x/kr/kiwoom_ordering.py`).
`LANE_EVIDENCE_KINDS` is the closed set. A missing kind keeps the lane
`AUTO_READY_BLOCKED_BY_LIFECYCLE`.

| Kind | Lane-native write |
|---|---|
| ACK | `submit_coordinated` callback after a non-blank `ord_no` |
| unknown | blank `ord_no` in `submit_coordinated`; `apply_restart_disposition` when the claim is uncorrelated; `record_native_broker_truth` for `kt00007` state `unknown` |
| reject | `record_native_broker_truth` when `kt00007` normalizes to `rejected` |
| expiry | `record_native_broker_truth` when `kt00007` normalizes to `expired` (local clock is not expiry) |
| partial fill | `record_native_broker_truth` when `kt00007` normalizes to `partial` |
| cancel | `cancel_attributed` callback after the cancel POST |
| terminal reconciliation | `release_if_matches_terminal` after `DurableSendClaimAdapter.release_with_terminal_evidence` |

### C3-5 Exact `release_if_matches` condition

Release is permitted only after:

- A. pre-transport authoritative `NOT_CREATED` evidence, or
- B. an exact attributed broker-native terminal fact persisted through
  the J2B/native lifecycle port,

and only with exact reservation ownership/token/key match, presented as
`TerminalClaimEvidence` to
`DurableSendClaimAdapter.release_with_terminal_evidence`.

Wall-clock DAY expiry, missing `kt00007` row, journal absence, and local
cycle completion cannot release.

### C3-6 Operator-visible blocked state

`AUTO_READY_BLOCKED_BY_LIFECYCLE`

Recorded on the cycle as `lane_lifecycle_status` when coordination,
authoritative recovery, or required P&L/account truth is unavailable.
A numeric `0` is never substituted for an unreadable P&L.

### C3-7 Lifecycle status

This lane's status remains `AUTO_READY_BLOCKED_BY_LIFECYCLE`.
This job does not activate recurring execution.

---

## Identity and grant

Canonical identity is J2A `LaneRegistryEntry.physical_account_id`.
Caller-derived identifiers, artifact-root paths, and
`account_identity_summary()["fingerprint"]` are diagnostic only and are
rejected when offered as identity authority.

`AccountWriterLease` remains a host-local flock. Its
`canonical()["authorizes_send"]` is `false`. A held flock without a J3A
grant makes zero transport calls.

Each POST and each cancel/reduce reasserts `CoordinationScope.assert_owned()`
immediately before the mutation.

---

## Native client ID

Kiwoom attempts use:

- `broker_client_id_target = None`
- `broker_client_order_id = None`
- non-blank J2B internal idempotency
- `BrokerClientIdTarget` enum unchanged

After ACK the exact Kiwoom order number is persisted as J2B
`broker_order_id` **before** `own-orders.jsonl` is appended.

---

## Crash / restart

| Window | Disposition |
|---|---|
| Pre-send + authoritative `NOT_CREATED` | matched cleanup allowed |
| POST then crash before durable ACK | `unknown_pending_reconcile`, account-wide block, repost 0 |
| Durable ACK then crash before JSONL | recover by exact J2B native id + `kt00007` |
| JSONL missing/corrupt | never "no owned orders" while a durable claim exists |
| `kt00007` unreadable or exact row absent | unknown + hold |

---

## Transport gate (before every real send)

1. actual client `type(...) is KiwoomMockClient`
2. `client._base_url ==` exact mock constant
3. secret-free fingerprint maps to J2A `physical_account_id`
4. registry lane/profile/mode/endpoint match
5. J3A grant still owned

A live-character client behind a mock-looking caller fails before
transport. `app/services/brokers/kiwoom/client.py` is not modified.

---

## C5 (TaskGroup / `asyncio.timeout` cancellation-count)

J3A left C5 as **UNKNOWN**. J4-V confirmed that status.

J3C does not introduce `asyncio.TaskGroup` or `asyncio.timeout`.
Cancellation shielding is consumed from J3A
`coordinate_mock_order_mutation` retained tasks. This adapter cannot
independently certify J3A's cancellation-count, so **C5 remains
UNKNOWN** with that evidence. It is not treated as "not applicable".
