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
`AUTO_READY_BLOCKED_BY_LIFECYCLE`.

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

Machine constant: `SPOT_DEMO_RESTART_TRIGGER`. Each rediscovered claim is
resolved by `BinanceSpotDemoLimitComposition.resolve_restart_claim`.

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
| cancel | `cancel_limit_order` after the cancel call; `resolve_restart_claim` for `CANCELED` |
| terminal reconciliation | `resolve_restart_claim` for `FILLED` / `NOT_CREATED`, and again after `release_with_terminal_evidence` succeeds |

The evidence port is required **at construction**: a composition that cannot
write its own evidence cannot be built, let alone dispatch.

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

### C3-7 Lifecycle status

This lane remains at `AUTO_READY_BLOCKED_BY_LIFECYCLE` for activation purposes.
This job activates nothing.

**Never reposted.** `RestartDisposition.repost` is a structural `False` with
`init=False`: no code path can construct a disposition that authorizes
re-sending an order whose outcome is unknown. This is proven exhaustively over
every readback outcome.

---

## 7. Transport gate — re-asserted before every send

1. `type(client) is BinanceSpotDemoExecutionClient` — exact type, because a
   subclass can override the transport;
2. the client's base-URL host passes `assert_spot_demo_host` (frozen
   `SPOT_DEMO_HOSTS = {demo-api.binance.com}`);
3. the registry's declared endpoint passes `assert_mock_only_endpoint`;
4. `CoordinationScope.assert_owned()` immediately before the send — the
   coordinator's single pre-callback attestation is not sufficient on its own;
5. `assert_spot_demo_transport` runs **again** inside the callback.

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

Fifteen injections were run against these twelve claims (items 4, 5, and 6 each
got two, to separate byte identity from behaviour, the notional-only branch from
the time-in-force branch, and the rounding direction from the delta guard). All
fifteen went red; none survived. Guard tests assert **at the guard function
first** and then through the pre-dispatch chain, so a neutered guard fails at
its own call site rather than falling through to a later registry check and
producing a red test whose traceback points somewhere else.

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
