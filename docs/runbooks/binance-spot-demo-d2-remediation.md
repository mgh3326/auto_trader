# D2 one-shot remediation — `d2_remediation_single`

`operator_contract.yaml` names a writer for the
`binance-demo-remediation-20260820` exception. This runbook covers the code that
answers to that name.

**Scope of this document.** It describes the writer and how to rehearse it. It
does **not** authorize execution. Execution requires a separate operator
re-sign, and the runbook cannot supply one.

## 1. What it is

| | |
|---|---|
| Writer | `app/services/brokers/binance/spot_demo/d2_remediation_single.py` |
| CLI | `scripts/binance_spot_demo_d2_remediation.py` |
| Contract | `~/work/herdr-inbox/contract-d2-binance-remediation-v2.2-20260820.md` |
| Seal | r7 attempt-2, `pre_snapshot_hash=sha256:5ba70a81…3e6b898` |
| Surface | `binance_spot_demo_remediation_only` |
| Canary/strategy use | forbidden |

It can express exactly three orders:

```text
BTCUSDT  SELL LIMIT 0.00015000    @ 69266.01000000
ETHUSDT  SELL LIMIT 0.00520000    @  2248.56000000
USDCUSDT SELL LIMIT 5000.00000000 @     1.00072000
```

There is no fourth, no BUY, no MARKET, and no flag that selects a symbol,
quantity, or price. The set lives in `D2_BOUND_ORDERS`; the sealed payload is
re-read at runtime and must match it exactly.

## 2. Why a second entry point exists

`scripts/binance_spot_demo_smoke.py` is the ROB-298 BUY round-trip and does not
wire a SELL `--confirm`, so it cannot express these operations. The 2026-08-20
execution attempt correctly refused to reach around it with an ad-hoc script,
because that would have created an unreviewed execution surface. This module is
the reviewed alternative.

`tests/services/brokers/binance/spot_demo/test_spot_demo_submit_callers.py`
enumerates every caller of `BinanceSpotDemoExecutionClient.submit_order` and
fails on an unlisted one. Adding an execution surface is a reviewed change.

## 3. Inherited boundaries — none relaxed

| Boundary | Owner | State after this change |
|---|---|---|
| Host pinned to `demo-api.binance.com` | ROB-298 transport | unchanged; the writer re-asserts it before composing |
| `submit_order(..., confirm=True)` per-call gate | ROB-298 execution client | unchanged; the writer's own default is `confirm=False` |
| `BINANCE_SPOT_DEMO_ENABLED` | ROB-298 | unchanged, still default-off |
| Ledger writes via `BinanceDemoLedgerService` | ROB-298 | unchanged; the repository is never imported here |
| Lifecycle state machine | ROB-298 | unchanged; the writer only uses legal transitions |

Added on top (all tightening):

* `D2_REMEDIATION_SINGLE_ENABLED` — a second, independent, default-off gate.
* A closed three-order set bound to one `pre_snapshot_hash`.
* A J3A lease precondition, re-attested immediately before every submit.
* One dispatch per `client_order_id`, enforced by a claim set.
* Two independent post-dispatch proof epochs.

## 4. Rehearsal

`--plan-only` is pure: it binds the seal and prints the three request payloads
with no HTTP, no DB, no lease, and no signing. It needs no env gate because it
touches nothing.

```bash
uv run python -m scripts.binance_spot_demo_d2_remediation \
  --plan-only \
  --sealed-payload <path>/r7-snapshot/attempt-2/binding-payload-proposed.json
```

`--dry-run` walks the whole path — both env gates, host re-assertion,
credential-fingerprint match, writer freeze, seal verification, J3A lease
acquisition and attestation, **pre-dispatch account truth**, request
composition, and the non-mutating `POST /api/v3/order/test` shape check — and
stops immediately before the signed POST. It needs both gates armed and a
reachable database.

```bash
BINANCE_SPOT_DEMO_ENABLED=true D2_REMEDIATION_SINGLE_ENABLED=true \
uv run python -m scripts.binance_spot_demo_d2_remediation \
  --dry-run --sealed-payload <path>/binding-payload-proposed.json
```

There is no flag to skip the order-shape check. It was optional in the first
revision, which meant the mode that sends real orders could also be the mode
that skipped the only broker-side proof that the sealed filters still describe
the market.

## 5. Execution — refused, at runtime, on every registered payload

`--confirm` is the only mode that reaches `submit_order(..., confirm=True)`.
For a caller entering through this module's functions, it is refused — not by
convention but by checks that run, each named with where it lives:

| Gate | Enforced at | Current state |
|---|---|---|
| Payload bytes hashed before parsing; unknown digest refused | `load_sealed_authority` | one registered digest |
| Every registered digest is `dispatch_authorized=false`, and import fails if not | `_assert_closed_order_set` | holds |
| `operator_authorization` non-null | `SealedAuthority.dispatch_block_reasons` | `null` |
| `expiry` present and not past, read from the real clock | `SealedAuthority.dispatch_block_reasons`, `D2RemediationSingleWriter.dispatch_block_reasons` | absent |
| `mutation_authorized` on all three rows | `SealedAuthority.dispatch_block_reasons` | `false` on all three |
| Sealed credential fingerprint = signed J2A registry | `load_sealed_authority`, `assert_registry_credential_fingerprint` | matches |
| Live client credential fingerprint = sealed | `D2RemediationSingleWriter._assert_client_account` | checked per run |
| Account-wide writer freeze | `assert_writer_freeze` | in force |
| Both env gates read only `os.environ` | `d2_remediation_enabled` | both off by default |
| Claim committed before any send, then read back | `D2RemediationSingleWriter._dispatch_one`, `BinanceDemoLedgerService.commit_planned_claim` | enforced |
| Broker identity / price / time-in-force echo | `D2RemediationSingleWriter._assert_echo`, `broker_identifier_problem` | enforced |

Symbol names rather than line numbers on purpose: a line citation is a claim
that goes stale on the next edit without anyone noticing, and
`test_the_runbook_gate_table_names_symbols_that_exist` checks every name in the
"Enforced at" column against the live modules.

`--dry-run` prints the whole blocker list; `--confirm` refuses on it *before*
opening a client, a lease, or a database session. Both env gates being armed
changes none of this.

Authorizing dispatch takes two separate things, in this order:

1. the operator's fresh re-sign bound to the exact r7 `pre_snapshot_hash`,
   which produces a **different file with a different digest**; and
2. a reviewed change that registers that digest with
   `dispatch_authorized=True` — which also has to update the import-time
   tripwire in `_assert_closed_order_set` that currently asserts no such entry
   exists.

Neither step is something this repository can do to itself.

## 5a. The durable replay fence

**Record, then execute.** The claim row is committed in its own transaction
*before* any broker call, and read back through a second, independent
transaction to prove it landed. Only then is anything sent.

The earlier ordering was the other way round: the claim was written into the
CLI's transaction and committed after `execute()` returned, so a process that
died between the signed POST and its own commit left no row at all — and the
restart, seeing nothing, re-sent. Measured on a throwaway database with a child
killed by `os._exit(9)` immediately after the POST:

```text
old ordering:  fence_row_after_kill = None       -> restart would re-send
new ordering:  fence_row_after_kill = 'planned'  -> restart goes to readback
```

The read-back is deliberately *not* done through the writing session. A session
can see its own flushed-but-uncommitted rows, so reading there would call a
flush durable. Going out to a separate transaction means a state that comes
back has survived a commit and is visible to any other process — which is also
why a ledger whose writes go nowhere fails here, before the broker is touched.
Requiring the ledger's *type* was never the same as requiring its *effect*.

The `client_order_id` is derived from the seal and the order
(`d2rem-<24 hex>`), not minted per run. Three consequences:

- a restarted process computes the same id, so the committed claim is found;
- Binance rejects a duplicate `newClientOrderId` while the original is live, so
  there is a second fence outside this repository;
- `uq_binance_demo_ledger_open_root` is unique on `(product, instrument_id)`
  across the open lifecycle states, so the database itself refuses a second
  open root for the same instrument — a third fence, free.

A ledger row with no matching broker record is **unresolvable** and halts the
run. "Never arrived" and "arrived then vanished" look identical from here, and
only one of them is safe to act on.

## 6. Failure behaviour

* **Ambiguous submit** (timeout, reset, unclear response): one bounded
  readback of the order by its client id. Never a re-send — a duplicate order
  is the worst available outcome, so an unproven result is recorded as an
  anomaly and the run halts before the next order.
* **Broker echo mismatch**: a response describing a different symbol, side,
  type, quantity, **price**, or **timeInForce** is a failure, not cosmetic
  drift. So is one whose **`clientOrderId`** is not the deterministic id we
  sent, or which carries no **`orderId`**: matching contents are not identity,
  and the shared Demo account can hold another order with the same symbol,
  side, quantity, and price. A response that omits any of these has not proved
  the sealed values — absence is a failure, not a pass.
* **Account drift**: a resting order this one-shot did not place, or any
  balance that differs from the seal, stops the run before the first POST.
  This writer has no cancel path, so clearing a foreign order is an operator
  action.
* **Lease lost**: refusal before the transport. Acquisition-time success is
  never carried forward, and the lease must be a real
  `PostgresAdvisoryKeysetLease` — a duck-typed stand-in is rejected at
  construction.
* **Unreleased lease**: the CLI releases the lease last, after the proof
  epochs, and exits 2 if the release could not be *proven*. A stuck lease is
  reported with its hold id rather than swallowed. The client close and the
  lease release sit in **separate** try/finally layers, so a raising `aclose()`
  cannot skip the release — that was the one path where the lease was most
  likely to be stuck and least likely to be reported.

## 7. Known limits

* **The J3A lease is not broker-enforced fencing.** Binance never sees it. An
  operator at a console or an out-of-repo process reaches the same shared Demo
  account without contending for it. That is why the writer also re-attests per
  submit and reads the *account-wide* open-order book before dispatching and in
  both proof epochs — and why none of those closes the hole, only narrows it.
* 🔴 **Threat model — confirmed by the operator, not pending.** These gates
  cover **accidental misuse**: a wrong parameter, a doctored or unregistered
  payload file, an account that has drifted from the seal, a malformed or
  foreign broker response, and a re-send after a crash. They do **not** cover
  **deliberate forgery by code running in the same process**, and that is
  accepted as a documented limit rather than treated as a gap to close.

  Concretely, an in-process caller can import `_AUTHORITY_TOKEN` and build a
  `SealedAuthority` that never saw a file, construct a real
  `PostgresAdvisoryKeysetLease` over a fabricated lock authority, subclass
  `BinanceDemoLedgerService`, and rebind `os.environ` or any module constant.
  It can also skip this module entirely and call
  `BinanceSpotDemoExecutionClient` directly. No writer-level check closes any
  of that in Python, and none in this repository claims to — the source
  docstrings state the same limit.
* **Balance drift is checked by exact equality**, so any movement on the three
  sealed assets since the snapshot stops the run. That is deliberate — the seal
  either describes the account or it does not — but it means a stale seal fails
  loudly rather than adapting.
* **A resting SELL LIMIT is not a fill.** The writer records `submitted` and
  proves the resting state; it does not wait for or assert a fill.
* **No scheduler registration.** CLI-only, operator-driven, one shot.
