# ROB-1270 J6B — Binance Spot Demo canonical LIMIT composition

A separate LIMIT composition for `crypto.binance.spot_demo.canonical`, built on
the merged J2A registry, J2B lineage factory, and J3A coordination port. This
document is also **this lane's contract document** for the recovery-ownership
and single-currency requirements below; there is no second contract file.

- Composition: `app/services/brokers/binance/spot_demo/mock_auto_limit.py`
- Transport (read/reuse only): `app/services/brokers/binance/spot_demo/execution_client.py`
- Tests: `tests/services/brokers/binance/spot_demo/test_mock_auto_limit.py`

**No activation.** Activation to `AUTO_ENABLED` is out of scope for this job and
requires separate approval. Missing any recovery item below leaves this lane at
`AUTO_READY_BLOCKED_BY_LIFECYCLE` — and that verdict is **computed** by
`spot_demo_recovery_contract_gaps()`, not asserted in this document. See §6.7 for
what is unmet today.

**No scheduler, no canary, no send.** This job registers no TaskIQ / Prefect /
cron / launchd / systemd entry and dispatches nothing. A real order is J8's
bounded canary and nothing else.

---

## 1. What this is not

| Not this | Why it matters |
|---|---|
| The ROB-845 `BUY` / `MARKET` / notional-only paper adapter | That is a **frozen asset**. It is not edited, wrapped, or relabelled. `PAPER_BROKER_CAPABILITIES[Broker.BINANCE]` still declares `sides={"buy"}`, `order_types={"market"}`, `sizing_modes={"notional"}`, `time_in_force=frozenset()`. Byte identity is pinned in `test_mutant_04a_*`. |
| A registry change | `app/services/mock_lane_registry.py` is untouched. Mechanically narrowing the signed lane-status allowlist is J2A-owned work, not J6B's. |
| An `execution_client` extension | The ROB-298 client already supports LIMIT `price` + `timeInForce` and a default-`false` `confirm`. Nothing was added to it. |
| A B0-X sidecar promotion | Operator decision D2: the sidecar stays observation-only. The transition option is retired, so the "own-fill attribution proof" precondition never arises. |
| Alpaca crypto mutation wiring | Operator decision D1: no mutation profile is assigned this epoch. Both rows stay `NOT_READY` / `DISABLED` / `writer=false`. |

---

## 2. Signed lane row — consumed, not rewritten

| field | value |
|---|---|
| `lane_id` | `crypto.binance.spot_demo.canonical` |
| `role` (purpose only) | `PRIMARY_AUTO` |
| `lane_status` | `NOT_READY` |
| `activation_status` | `BLOCKED` |
| `scheduler_owner` | `DISABLED` (the explicit `SchedulerOwner.DISABLED` member) |
| `writer` / `auto_order_enabled` | `false` / `false` |
| `quote_currency` | `USDT` |
| `allowed_hosts` | `("demo-api.binance.com",)` |
| `physical_account_id` / `identity_status` | `None` / `UNKNOWN` |

`role` is a registry **purpose** value, not execution authority.

### `scheduler_owner`: absent ≠ disabled

The signed table carries two different facts in this column and they stay two
facts here:

| value | meaning | blocker string emitted |
|---|---|---|
| `SchedulerOwner.DISABLED` | an explicit decision, backed by in-repo evidence that Binance Demo has no scheduler registration | `scheduler_owner_disabled` |
| `None` | **owner absent** — no bind authority was ever assigned, which is why those rows also carry `MissingBinding.OWNER` | `scheduler_owner_absent` |

`spot_demo_activation_blockers()` branches on the two separately. Reporting
absence as disablement would tell a downstream reader that an ownership decision
had been made when none was. The canonical Binance row is `DISABLED`, so a test
that only exercises that row cannot see the difference; the `None` branch is
exercised directly against the signed Alpaca crypto rows
(`test_an_absent_scheduler_owner_is_not_a_spelling_of_disabled`,
`test_signed_rows_with_an_absent_owner_report_absence`).

### Terminal lane status

When the LIMIT lifecycle is complete, the **primary** terminal lane status for
this lane is exactly:

```
AUTO_READY_BLOCKED_BY_POLICY
```

There is no approved autonomous policy. The absent scheduler owner is real, but
it is a **secondary / activation** blocker reported separately by
`spot_demo_activation_blockers()`; it is never promoted to the primary status.
`AUTO_READY_BLOCKED_BY_SCHEDULER` is not selected here.

The merged registry's signed allowlist for this lane still admits three values
(`NOT_READY`, `AUTO_READY_BLOCKED_BY_POLICY`, `AUTO_READY_BLOCKED_BY_SCHEDULER`)
— a conservative superset the J2A verifier dispositioned as *not a violation*.
J6B binds the narrower choice by contract and by `SPOT_DEMO_PRIMARY_TERMINAL_
LANE_STATUS`, without touching the registry.

**Three different statuses are in play; do not conflate them.**

| status | who owns it | value today |
|---|---|---|
| the registry's `lane_status` field | J2A (signed, unmodified here) | `NOT_READY` |
| the §D primary *terminal* status this composition binds | J6B contract | `AUTO_READY_BLOCKED_BY_POLICY` |
| the §83 correction-3 lifecycle verdict, computed | `spot_demo_lifecycle_lane_status()` | `AUTO_READY_BLOCKED_BY_LIFECYCLE` (see §6.7) |

Reading `spot_demo_activation_blockers()` shows all three: the policy blocker at
index 0, the lifecycle verdict and its named gaps next, then the scheduler,
activation, and missing-binding blockers.

---

## 3. Why the mutation path is unreachable

Not "switched off" — **structurally unsatisfiable**. For this lane
`assert_entry_execution_ready` cannot pass under any configuration:

- `activation_status is ENABLED` → `_violates_signed_lane_restriction` fires,
  because the lane appears in `_SIGNED_LANE_STATUS_ALLOWLISTS` →
  `lane_signed_restriction_violation`;
- any other activation status → `lane_activation_not_enabled`.

`test_execution_ready_is_unsatisfiable_for_this_lane` proves this exhaustively
over every `ActivationStatus` member × `writer` × `auto` (24 combinations).

With the signed registry, `submit_limit_order(..., confirm=True)` therefore
raises `lane_binding_incomplete` with **zero HTTP requests and zero durable
claim attempts** (`test_submit_with_the_signed_registry_dispatches_zero_http`).

---

## 4. D6 sizing — operator-fixed, not selectable

```
LIMIT only.  MARKET and notional-only plans are refused before broker I/O.
quantity = floor_to_step(target_notional / limit_price)
```

- **Floor only.** Rounding up to reach a venue minimum would place a larger
  order than the decision authorized, so it is refused rather than adjusted.
- **Below `min_qty` or `min_notional` → no plan at all.** Not a smaller order,
  not a padded quantity — `LimitSizingBlocked`, whose `produces_plan` field is a
  structural `False` (no constructor argument).
- **Five provenance facts travel with the plan** in `tick_rounding`:
  `price_source`, `price_cutoff`, `step_size`, `step_version`, `rounding_delta`
  (plus `mode="floor_to_step"`). Dropping any one fails
  `assert_sizing_provenance_complete` before broker I/O.
- A negative `rounding_delta` is the round-up signature and is rejected as
  `sizing_round_up_forbidden`.

**Caps are not invented.** The registry reports `MissingBinding.CAP` and
`max_order_notional=None` for this lane, so the plan records
`risk_caps={"cap_binding": "missing"}` rather than a chosen number.

---

## 5. Single-currency DecisionIntent (§83 correction 2)

The controlling authority, reproduced verbatim:

```text
A DecisionIntent is single-currency.

ExecutionPlan fan-out from one DecisionIntent is permitted only to lanes
whose registry quote_currency exactly equals the intent's
target_notional_currency.

USD and USDT are distinct currencies. No parity, FX lookup, or implicit
conversion is authorized.

When the same policy is evaluated on USD and USDT venues, the system must
materialize separate sibling DecisionIntents, one per currency, and bind
them through an immutable common comparison or policy-decision correlation.

For crypto.alpaca.paper.*, AUTO_MIRROR means policy mirror, not
same-DecisionIntent currency conversion.
```

| item | binding |
|---|---|
| **C2-1** single-currency construction | `DecisionIntent` is frozen/strict with exactly one `target_notional_currency` (`app/schemas/execution_contracts.py:301-315`); the id is derived server-side by `MockLineageFactory`. |
| **C2-2** exact fan-out guard | Three-way exact equality: `MockLineageFactory.create_execution_plan` (`lineage.py:525-526`), `assert_lane_quote_currency` (`mock_lane_registry.py:1085-1097`), and this module's `assert_usdt_single_currency`. |
| **C2-3** no FX path | `test_c2_3_no_fx_parity_or_conversion_path_exists` — AST name scan **and** a token-stripped text scan of the executable surface, both empty, plus a self-check that the pattern does fire on a real conversion. |
| **C2-4** sibling binding | `SIBLING_BINDING_FOR_EXECUTION = PENDING`, consumed unchanged from the merged ROB-1269 contract. No key is named, synthesized, or persisted here, and no USD intent fans out to this USDT lane. |
| **C2-5** `AUTO_MIRROR` | Policy mirror only. Both Alpaca crypto rows are USD, `NOT_READY`, `DISABLED`, `writer=false`, `scheduler_owner=None` (owner absent — **not** a spelling of `disabled`). |

---

## 6. Lane-native recovery owner (§83 correction 3 — activation precondition)

The controlling authority, reproduced verbatim:

```text
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
```

### C3-1 Recovery owner — exactly one

`app.services.brokers.binance.spot_demo.mock_auto_limit.BinanceSpotDemoLimitComposition`

Machine constant: `SPOT_DEMO_RECOVERY_OWNER`. No second owner is named, and no
`TBD` appears. J3A owns no Binance-specific retry, readback, or
manual-resolution queue — `app/services/mock_integration/coordination.py`
contains no occurrence of "binance" at all (`test_common_layer_owns_no_binance_
retry_or_readback_queue`).

### C3-2 Restart trigger

`process_restart_rediscovers_durable_j2b_claims_for_physical_account`

Machine constant: `SPOT_DEMO_RESTART_TRIGGER`. The **executable** entry point is
`BinanceSpotDemoLimitComposition.rediscover_restart_claims`
(`SPOT_DEMO_RESTART_TRIGGER_ENTRYPOINT`), and each rediscovered claim is resolved
by `resolve_restart_claim`. Naming a trigger is not owning one, so the method
does the enumeration itself:

1. derive this lane's physical-account scope from the J2A entry — never from a
   caller-supplied string;
2. `list_reservations(account_scope=...)` on the lane's own reservation
   read-side port;
3. for each survivor, recompute the client order id from the claim's J2B
   idempotency key (`derive_attributed_client_order_id`);
4. authoritative readback → disposition → lane-native evidence.

`DurableSendClaimAdapter` deliberately exposes no listing surface — enumerating
survivors is lane work, not coordination work — so the trigger takes its own
`reservations` port. An **unbound** port is not silently substituted: it raises
`restart_claim_source_unavailable` and is reported as a computed recovery gap
(§6.7).

Two kinds of survivor are deliberately *not* acted on, and both leave an
`unknown` evidence row carrying `unknown_pending_reconcile` rather than being
silently skipped:

- a claim whose key this lane's lineage cannot reproduce — the physical account
  is shared, so the claim may belong to another writer; it is not even read back;
- an attributable claim whose symbol cannot be recovered — the readback needs a
  symbol, and guessing one would aim an authoritative query at the wrong market.

### C3-3 Authoritative broker readback

`GET /api/v3/order?origClientOrderId=...` via
`BinanceSpotDemoExecutionClient.get_order_status`.

Open-order listings (`GET /api/v3/openOrders`), account balances, local
evidence files, and the wall clock are **diagnostic only**. A Binance `-2013`
is the broker proving absence (`NOT_CREATED`); every other failure is
`UNREADABLE`, which is a held unknown, not an absence. An unrecognised native
status maps to `UNREADABLE`, never optimistically to a terminal state.

### C3-4 Lane-native evidence — seven kinds, none missing

`SpotDemoLaneEvidenceKind` is the closed set; `LANE_EVIDENCE_KINDS` is asserted
to be exactly these seven.

| Kind | Lane-native write site |
|---|---|
| ACK | `submit_limit_order._mutate` after a non-blank native `broker_order_id` |
| unknown | `submit_limit_order._mutate` when submit raises, and when the response carries no native order id; `resolve_restart_claim` for `UNREADABLE`, `OPEN` |
| reject | `resolve_restart_claim` when the readback normalizes to `REJECTED` |
| expiry | `resolve_restart_claim` when the readback normalizes to `EXPIRED` (the local clock is never expiry) |
| partial fill | `resolve_restart_claim` when the readback normalizes to `PARTIALLY_FILLED` |
| cancel | `cancel_limit_order` **only** on a broker-proven cancellation (`confirm=True` + a native status normalizing to `CANCELED`); `resolve_restart_claim` for `CANCELED` |
| terminal reconciliation | `resolve_restart_claim` for `FILLED` / `NOT_CREATED`, and again after `release_with_terminal_evidence` succeeds |

The evidence port is required **at construction**: a composition that cannot
write its own evidence cannot be built, let alone dispatch.

A dry-run cancel writes **nothing at all** — see §6a.

### C3-5 Exact `release_if_matches` condition

Release is permitted in exactly two shapes, and never without reconciliation:

- **A.** authoritative `NOT_CREATED` (broker-proven absence) **and**
  `account_position_reconciled`; or
- **B.** an attributed native terminal fact (`FILLED` / `CANCELED` / `REJECTED`
  / `EXPIRED`) **and** `account_position_reconciled` **and** `remainder_known`.

Everything else retains the claim. A missing readback row, an unparseable
status, an open or partially-filled order, and the passage of time cannot
release.

The release itself flows only through
`DurableSendClaimAdapter.release_with_terminal_evidence`, which re-checks the
exact booleans. `terminal_evidence_for()` returns a **default-constructed**
`TerminalClaimEvidence` — which authorizes nothing — for any disposition that
may not release; no flag is ever fabricated. The unrestricted
`release_if_matches` is never named in this module.

### C3-6 Operator-visible blocked state

`unknown_pending_reconcile` (`SPOT_DEMO_UNRECOVERABLE_STATE`), surfaced as
`RestartDisposition.operator_visible_state` and written into the lane evidence
payload. It is a state, not a log line.

### C3-7 Lifecycle status — computed, and currently blocked

`spot_demo_recovery_contract_gaps(entry, composition=...)` checks each of the six
items above against live objects — the registry entry, the transport class, the
J3A adapter, and the composition instance that actually holds the lane's ports —
and `spot_demo_lifecycle_lane_status()` resolves any unmet item to
`AUTO_READY_BLOCKED_BY_LIFECYCLE`. The enum member
`SPOT_DEMO_LIFECYCLE_BLOCKED_LANE_STATUS` is what that verdict *resolves to*, not
the verdict itself.

**Unmet today** (`spot_demo_recovery_contract_gaps(signed_entry)`):

| gap | why it is unmet | what would close it |
|---|---|---|
| `restart_trigger` | two independent causes: no production caller constructs this composition, so no `reservations` port is bound; **and** the signed row's `identity_status` is `UNKNOWN`, so `physical_account_scope_for_entry` raises and there is no scope to enumerate within | masked-fingerprint identity evidence for the Binance Demo account (J2A-owned) **and** a production wiring site |
| `lane_native_evidence` | the seven kinds exist and are written, but no production construction site binds a durable `SpotDemoLaneEvidencePort` — today only tests construct the composition | a production wiring site with a durable evidence store |

Both gaps have the same root: **this composition has no production caller.**
Building one is out of scope for J6B, which is why the honest state is a computed
`AUTO_READY_BLOCKED_BY_LIFECYCLE` rather than a claim of readiness. Status of the
remaining work: `pending` — owner J8 / a later activation job, not this one.

The verdict discriminates rather than always returning "blocked": given an owner
holding both ports and an identity-known entry, the gaps are empty and the
lifecycle status clears (`test_c3_7_a_fully_wired_owner_has_no_recovery_gaps`),
and each gap is reproducible on its own cause
(`test_c3_7_each_missing_recovery_item_is_reported_on_its_own`).

**Never reposted.** `RestartDisposition.repost` is a structural `False` with
`init=False`: no code path can construct a disposition that authorizes
re-sending an order whose outcome is unknown. This is proven exhaustively over
every readback outcome.

---

## 6a. Cancel — the same contract as a submit

A cancel is a `DELETE` against a real venue, not a lighter operation. It runs
under the same attribution, coordination, and uncertainty contract as a submit:

| step | what runs | why |
|---|---|---|
| fresh guards | `assert_spot_demo_transport`, lane check, `assert_mock_only_endpoint`, `assert_binance_domain_invariants` | the cancel path is not a side door around the registry invariants |
| **attribution** | `assert_client_order_id_attributed` (the id must be reproducible from this claim's J2B key) and `assert_claim_belongs_to_lane` (J2A-derived scope) | `confirm` decides whether a mutation is *sent*; it says nothing about whether the thing being mutated is ours |
| **coordination** | `CoordinationScope.assert_owned()`, then J3A's own `describe_claim_followup` predicate | that predicate requires an attributed native order id **and** a known remainder, so a cancel aimed at an order whose identity or remainder is unknown cannot even be described |
| pre-send re-assert | `CoordinationScope.assert_owned()` again, immediately before the `DELETE` | anything awaited in between can outlive the lease |
| **uncertainty** | a raised send records `unknown` + `unknown_pending_reconcile` and re-raises; a response that does not normalize to `CANCELED` is also `unknown` | ROB-298 §4 / ROB-395 evidence-first: the write may well have reached the broker, so silence is not an option |

**Dry run writes nothing.** Without `confirm=True` no `DELETE` is sent and **no
lane evidence row is created**, durable or otherwise;
`SpotDemoCancelDisposition.dispatched` is `False`. A durable `CANCEL` row asserts
that the broker cancelled the order — on a dry run it did not, and a later
reconciliation reading that row would conclude the order is gone while it is
still live at the venue.

Refusal reason codes: `cancel_not_attributed`,
`cancel_followup_capability_absent`. Both are disjoint from J3A's
`CoordinationReasonCode` vocabulary.

---

## 7. Transport gate — re-asserted before every send

1. `type(client) is BinanceSpotDemoExecutionClient` — exact type, because a
   subclass can override the transport;
2. the client's base-URL host passes `assert_spot_demo_host` (frozen
   `SPOT_DEMO_HOSTS = {demo-api.binance.com}`);
3. the registry's declared endpoint passes `assert_mock_only_endpoint`;
4. `CoordinationScope.assert_owned()` immediately before the send — the
   coordinator's single pre-callback attestation is not sufficient on its own;
5. `assert_spot_demo_transport` runs **again** inside the callback;
6. the registry writer-domain invariants (`assert_binance_domain_invariants`).

The cancel path runs the same list — see §6a.

Live (`api.binance.com`), retired testnet (`testnet.binance.vision`), live
futures (`fapi.binance.com`), Futures Demo (`demo-fapi.binance.com`), and suffix
spoofs all fail before any request is built. Where the ROB-298 transport factory
already refuses at construction, the guarantee simply lands one layer earlier;
either way the assertion is zero HTTP.

---

## 8. Adversarial mutant coverage (§F, twelve items)

| # | Claim | Kill point |
|---|---|---|
| 1 | Two writers in one Binance conflict domain | `assert_binance_single_writer_domain` — every Binance lane is one domain while identity is `UNKNOWN`. `test_mutant_01b_*` shows the registry's own `assert_single_writer` is *silent* here, which is why the stricter guard exists. |
| 2 | Sidecar writer / live recurring owner | `assert_sidecar_observation_only` (writer, auto, and scheduler-owner variants) |
| 3 | Alpaca crypto writer / profile / submit wiring | `assert_alpaca_crypto_unwired` + an AST import scan showing no Alpaca import exists |
| 4 | Frozen ROB-845 bytes or behaviour change | SHA-256 pin of the three files **and** the declared capability shape |
| 5 | MARKET / notional-only plan enters | `assert_limit_only_plan`, before broker I/O, zero HTTP asserted |
| 6 | Round-up to step | floor-only in `compose_limit_sizing`; negative `rounding_delta` rejected |
| 7 | Plan built below min qty / notional | `LimitSizingBlocked`, `produces_plan` structurally `False` |
| 8 | Missing source / cutoff / step-version / rounding-delta | `assert_sizing_provenance_complete`, parametrized over all five keys |
| 9 | USD treated as USDT | `compose_limit_sizing` currency check + `assert_usdt_single_currency` + the J2B factory reject |
| 10 | Blind repost after restart | `RestartDisposition.repost` structural `False`, exhaustive over every outcome |
| 11 | Live / testnet / futures host or client | `assert_spot_demo_transport` (exact type + frozen host allowlist) |
| 12 | Scheduler registration import | AST import scan for taskiq / prefect / cron / celery / apscheduler / `app.tasks` / `app.flows` |

Every zero-I/O claim is asserted as `httpx_mock.get_requests() == []` — an
observed transport call count, not an inference.

### Direct call vs. dispatch chain

A guard that only fires when a test calls it by name protects nothing. For each
of items 1, 2, 3, 5, 6, and 8 there are **two** kinds of coverage and they are
labelled separately:

- a **direct** assertion at the guard function, so a neutered guard dies at its
  own call site rather than at some later, unrelated check; and
- a **chain** assertion that drives the real `validate_pre_dispatch`, asserting
  on its own line that the refusal came from *this* guard and naming what it got
  instead if it did not (`test_mutant_01c_*`, `test_mutant_02c_*`,
  `test_mutant_03c_*`).

The writer-domain invariants (items 1–3) run **before** `assert_lineage_registry_
binding`, pinned by `test_the_domain_invariants_are_reached_before_the_j2a_
binding_check`. Placed after it they would be dead code for this lane, because
the signed registry always dies on the binding check first — which is exactly the
gap the r1 round left open: the three guards were defined, exported, and
documented, but nothing on the dispatch path called them.

Cancel-path coverage (attribution, dry-run silence, post-send uncertainty) is in
§6a and is likewise driven through the real method, not through a helper.

Zero-HTTP claims are scoped honestly. `submit_limit_order(..., confirm=True)`
reaching no transport is the result of a **structural** refusal at
`assert_lineage_registry_binding`, not evidence that the code ran up to the send
and stopped there; the dispatch closure is unreachable under the signed registry,
which is why its ACK/unknown branches are proven at the `classify_submit_outcome`
seam instead.

---

## 9. Running the offline suite

```bash
uv run pytest -q \
  tests/services/brokers/binance/spot_demo/test_mock_auto_limit.py \
  tests/services/brokers/binance/ \
  tests/test_mock_lane_registry.py
```

No database, broker credential, network, or scheduler is required or touched.

---

## 10. C5 — TaskGroup / `asyncio.timeout` cancellation count

J3A deliberately left C5 `UNKNOWN`; J3B, J3C, and J4-V all landed keeping that
status. J6B introduces no `asyncio.TaskGroup` and no `asyncio.timeout`, and
consumes J3A's retained-task cancellation shielding through
`coordinate_mock_order_mutation` rather than re-implementing it. This module
cannot independently certify J3A's cancellation count, so **C5 remains
UNKNOWN** on that evidence. It is not "not applicable".
