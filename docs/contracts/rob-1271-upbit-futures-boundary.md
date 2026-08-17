# ROB-1271 (J6C) — Upbit shadow / Binance Futures Demo boundary contract

## Scope and authority

Contract-only artifact. It consumes the signed registry without changing it and
authorizes no broker, network, database, scheduler, deployment, or account
operation. This job changes **no production `app/**` file**; its only outputs
are this document and `tests/services/test_upbit_futures_boundary.py`.

The controlling registry is
`app/services/mock_lane_registry.py::CANONICAL_LANE_REGISTRY`. The two rows
below are a literal consumption of that registry. `role` is a purpose-only
registry value; it is not execution authority. The upstream J6A crypto
ownership contract (`docs/contracts/rob-1269-crypto-owner.md`) is consumed
unchanged.

## Signed lane rows (unmodified consumption)

| lane_id | role (purpose only) | lane_status | activation_status | scheduler_owner | writer | auto_order_enabled | quote_currency |
| --- | --- | --- | --- | --- | ---: | ---: | --- |
| crypto.upbit.shadow | SHADOW_ONLY | SHADOW_ONLY | DISABLED | None (owner absent; mutation-ineligible; downstream bind authority 없음) | false | false | KRW |
| crypto.binance.futures_demo | None | DISABLED_NO_STRATEGY | DISABLED | DISABLED | false | false | USDT |

`DISABLED` is the explicit `SchedulerOwner.DISABLED` enum member. The Upbit
`None` means exactly **owner absent; mutation-ineligible; downstream bind
authority 없음** — it is not an alternative spelling of `DISABLED`, and it
grants no authority to invent an owner. Both spellings are pinned separately
and asserted to be distinct
(`test_absent_and_disabled_scheduler_owners_stay_distinct_values`), so a future
`scheduler_owner is None or is DISABLED` branch cannot silently collapse two
different meanings.

Registry anchors: `app/services/mock_lane_registry.py:487-509` (both rows),
`:319-368` (`writer=False` / `auto_order_enabled=False` for all twelve),
`:266-267` (the signed status allowlists).

## 1. Upbit — `SHADOW_ONLY` is structural, not conventional

`crypto.upbit.shadow` is synthetic. It carries `account_mode=SHADOW`,
`endpoint_class=SHADOW`, `allowed_hosts=()`, and `credential_namespace=None`
— it owns no broker host and no credential namespace at all, so there is
nothing for it to be labelled as broker paper or native against.

Broker I/O is refused by lane class rather than by host string:
`assert_mock_only_endpoint` raises `shadow_broker_io_forbidden` for any
`AccountMode.SHADOW` entry before it parses the URL
(`app/services/mock_lane_registry.py:1139-1140`). The regression exercises
Upbit, Binance Demo, Alpaca paper, and Kiwoom mock hosts and gets the same
rejection for all of them.

**The guard is on the live chain, not merely exported.** A direct call to
`assert_mock_only_endpoint` proves the function rejects; it does not prove
`guarded_broker_io` ever consults it. Two chain tests separate the two facts
honestly:

| fixture | first guard to fire | broker callback invocations |
| --- | --- | --- |
| `CANONICAL_LANE_REGISTRY` (unmodified) | `lane_binding_incomplete` | 0 |
| shadow row granted a policy binding | `shadow_broker_io_forbidden` | 0 |

Under the signed registry the chain stops at `lane_binding_incomplete` and
never reaches the shadow gate, because the shadow row has no policy binding and
still lists `MissingBinding.POLICY`. The second fixture grants the strongest
upstream weakening that the registry's own startup validation still accepts, so
the chain actually reaches the shadow gate and the kill can only come from
there. Both rows are recorded; neither is presented as the other.

Upbit's private credential surface (`app/services/upbit_websocket.py` — JWT
signed with `settings.upbit_access_key` / `settings.upbit_secret_key` against
`wss://api.upbit.com/websocket/v1/private`) is **not reachable** from the lane
surface. The transitive first-party import closure of
`app/services/mock_lane_registry.py` is four modules and contains no module
whose name includes `upbit`.

## 2. Futures Demo — `DISABLED_NO_STRATEGY` with no activation path

* **Default plugin.** `NullStrategy` returns `None` for every input, and
  `scripts/binance_demo_strategy_loop.py` — the only entry point — passes
  exactly `strategy=NullStrategy()` to `run_tick`. The AST check asserts that
  the set of `strategy=` arguments anywhere in the CLI is exactly
  `["NullStrategy()"]`, so adding a second, non-null wiring fails.
* **No recurring registration.** No module under `app/tasks/**` or
  `app/flows/**` reaches
  `app.services.brokers.binance.demo_strategy_loop` at all. Exactly one
  scheduler entry point reaches the futures execution client —
  `app/tasks/binance_demo_root_reservation_reconcile_tasks.py` — and its
  `@broker.task(...)` decorator carries only `task_name`, with no `schedule`
  keyword.
* **That one reach is read-only.** Across the task and its job module, the
  called attribute names include `get_order_status` and `get_order` and exclude
  `submit_order`, `cancel_order`, `order_test`, and `set_leverage`. The positive
  half of that assertion is deliberate: it stops the negative half from passing
  vacuously if the chain ever stops touching the clients entirely.
* **Leverage 1x.** `set_leverage` rejects any `leverage != 1` at the adapter
  boundary before signing (`futures_demo/execution_client.py:739-744`); the
  regression drives 0, 2, 3, 5, 10, and 125 and dispatches zero HTTP.
* **One-way only.** There is no `position_side` parameter on `submit_order` to
  set, and `demo_strategy_loop/execution.py` raises
  `BinanceFuturesDemoHedgeModeBlocked` on `mode_result.is_hedge_mode` before any
  submit.
* **reduceOnly.** `submit_order` defaults `reduce_only=False` and
  `confirm=False`; an unconfirmed submit returns a `FuturesDemoDryRunResult`
  with `reduce_only=False` and dispatches zero HTTP. The close leg goes through
  `_close_with_reduce_only` with `reduce_only=True`.
* **Leg cap.** `LEG_NOTIONAL_CAP_MIN_USDT == Decimal("6")` and
  `LEG_NOTIONAL_CAP_MAX_USDT == Decimal("10")` are asserted as literals written
  in the test rather than read back out of the module, so shrinking the
  constants cannot shrink the assertion with them.
* **Kill switch.** `assert_kill_switch_limits_locked` accepts `LOCKED_LIMITS`
  and rejects a widened `max_concurrent_positions`.

Neither lane can be activated: `transition_activation(..., ENABLED)` raises
`lane_signed_restriction_violation` (guard B2) for both.

## 3. Spot / Futures `cancel_order` confirm asymmetry — recorded, not resolved

| adapter | `submit_order` | `cancel_order` |
| --- | --- | --- |
| `BinanceSpotDemoExecutionClient` | `confirm: bool = False` | `confirm: bool = False` |
| `BinanceFuturesDemoExecutionClient` | `confirm: bool = False` | **no `confirm` parameter** |

The futures cancel signature is exactly `(self, symbol, client_order_id)`. The
asymmetry is declared intent, not an oversight — the futures docstring states
that there is no dry-run gate on cancel because "by the time a cancel is being
called, the operator has already committed to running against the broker".

ROB-1271 pins this asymmetry in both directions. Adding `confirm` to the
futures cancel, or dropping it from the spot cancel, fails the regression.
Unifying either side is out of scope for this job, and the asymmetry is not
activation evidence for anything.

## 4. Finding F-1 — the Spot Demo runbook's no-scheduler claim is inaccurate

`docs/runbooks/binance-spot-demo-smoke.md:45-48` states:

> No scheduler / TaskIQ / Prefect / cron / Hermes wiring touches the Spot Demo
> execution client. The smoke CLI is the **only** path that produces real Demo
> orders, and only when the operator passes `--confirm`.

Both clauses are inaccurate as of the ROB-845/849 paper-cohort lane. **Two**
scheduler entry points reach `spot_demo.execution_client`:

| entry point | schedule | reaches an order-producing path |
| --- | --- | --- |
| `app/tasks/binance_demo_root_reservation_reconcile_tasks.py` | none | no — read-only lookups |
| `app/tasks/paper_cohort_tasks.py` | `schedule=_scheduled_paper_cohort_labels()` | yes |

The paper-cohort path is
`app.tasks.paper_cohort_tasks → app.jobs.paper_cohort →
app.services.paper_cohort.runner → app.services.brokers.paper.composition →
app.services.brokers.binance.paper_adapter →
app.services.brokers.binance.spot_demo.execution_client`, and
`BinanceSpotDemoPaperAdapter.submit` calls the executor with `confirm=True`
(`app/services/brokers/binance/paper_adapter.py:186-192`).

Bounding this honestly:

* The schedule list is empty unless `settings.PAPER_COHORT_ENABLED` is set, and
  the adapter's symbol allowlist is `{BTCUSDT, ETHUSDT}`. Static import reach is
  not the same as a runtime order.
* This is **pre-existing** (ROB-845/849) and was not introduced by ROB-1271.
* It concerns **Spot**, not Futures. The identical sentence in
  `docs/runbooks/binance-futures-demo-smoke.md:50-53` is only narrowly
  inaccurate: a TaskIQ module does reach the futures client, but it is
  scheduleless and calls no order-producing method, so that runbook's operative
  promise still holds.

J6C owns neither `docs/runbooks/**` nor `app/**`, so this divergence is
recorded and pinned rather than repaired. The regression asserts the runbook
sentence and the contradicting repo fact together, so resolving the divergence
in either direction must update the test in the same change.
`FINDING_F1 = OPEN — orch disposition required.`

## 5. Lane-native lifecycle recovery ownership (§83 correction 3)

The controlling authority is reproduced verbatim:

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

No broker-specific retry, readback, or manual-resolution queue is added to the
common coordination layer by this job. The six items are assessed lane-native
below. **Neither lane is being made `AUTO_ENABLED`; this section records the
prerequisites, and activation remains outside this job's scope.**

### 5.1 `crypto.upbit.shadow`

| item | status | evidence |
| --- | --- | --- |
| C3-1 recovery owner | **ABSENT** | No owner is designated anywhere. `scheduler_owner=None` and `MissingBinding.OWNER` is present on the row. Per the acceptance rule this is an unmet item, reported as such rather than filled in. |
| C3-2 restart trigger | **ABSENT** | The lane has no durable claim to rediscover: `allowed_hosts=()`, `credential_namespace=None`, no ledger table, no journal root. |
| C3-3 readback operation | **ABSENT** | No authoritative broker readback is possible; broker I/O is structurally refused with `shadow_broker_io_forbidden`. |
| C3-4 lane-native evidence (7 kinds) | **ABSENT — 0 of 7** | None of ACK, unknown, reject, expiry, partial fill, cancel, or terminal reconciliation has a lane-native write point. The lane is synthetic and writes no broker lifecycle evidence at all. |
| C3-5 exact `release_if_matches` condition | **ABSENT** | `mock_integration.coordination.release_if_matches` exists but is not called from this lane's surface. |
| C3-6 operator-visible blocked state | **PRESENT** | `lane_status=SHADOW_ONLY`, `activation_status=DISABLED`, `identity_status="UNKNOWN"`, and the full `missing_bindings` tuple are all operator-visible on the registry row. |

### 5.2 `crypto.binance.futures_demo`

| item | status | evidence |
| --- | --- | --- |
| C3-1 recovery owner | **NOT SATISFIED — no single designation** | Two distinct modules own different recovery phases and no contract names one: `app/jobs/binance_demo_root_reservation_reconciliation.py` (pre-acknowledgement roots) and `demo_strategy_loop/execution.py` (in-tick close/reconcile). "Exactly one" is not met, so this is reported as a failure rather than resolved by picking one. |
| C3-2 restart trigger | **PRESENT** | `_candidate_where_clauses` rediscovers surviving durable roots after restart — `planned`, or `previewed`/`validated` with `broker_order_id IS NULL`, older than `stale_before` (`app/jobs/binance_demo_root_reservation_reconciliation.py:125-154`). |
| C3-3 readback operation | **PRESENT** | `BinanceFuturesDemoExecutionClient.get_order` (`GET /fapi/v1/order`), dispatched through `_lookup_order` (`:119-122`). |
| C3-4 lane-native evidence (7 kinds) | **PARTIAL — 4 present, 1 degraded, 2 absent** | Present: `record_submitted` (ACK, carries `broker_order_id`), `record_anomaly` (unknown, carries `anomaly_reason`), `record_cancelled` (cancel), `record_reconciled` (terminal reconciliation). Degraded: partial fill — `record_filled` takes no quantity argument and `binance_demo_order_ledger` has no `executed_qty` / `remaining_qty` column, so a partial fill cannot be distinguished from a full one except through free-form `extra_metadata`. Absent: **reject** and **expiry** have no distinct write point; both collapse into the `cancelled` / `anomaly` branches, which also absorb unrelated causes. |
| C3-5 exact `release_if_matches` condition | **ABSENT** | The J3A contract is not called from this lane. The lane's own release condition is narrower and differently named: release only on explicit `BinanceDemoOrderNotFound` within an 89-day bound, or a terminal status in `{CANCELED, REJECTED, EXPIRED}` with `executedQty == 0`, and only after venue-host, credential-fingerprint, and broker-identity equality all match. That is a release rule, but it is not the exact `release_if_matches` contract the authority names. |
| C3-6 operator-visible blocked state | **PRESENT** | `anomaly` lifecycle state, plus per-candidate `kept` outcomes carrying stable reasons (`client_unavailable`, `venue_host_mismatch`, `credential_fingerprint_mismatch`, `broker_lookup_failed`, `malformed_broker_truth`, `broker_identity_mismatch`, `broker_exposure_not_disproven`, `broker_lookup_retention_exceeded`). |

### 5.3 C3-7 — lifecycle status disposition, and a flagged conflict

Both lanes fail multiple items above, so the authority's consequent would place
each at `AUTO_READY_BLOCKED_BY_LIFECYCLE`. **This contract does not record that
status, for three reasons, and the conflict is escalated rather than resolved
here:**

1. The signed registry (§B of the dispatch brief, extracted from merged J2A
   code) freezes these rows at `SHADOW_ONLY` and `DISABLED_NO_STRATEGY` and
   forbids redefinition by downstream consumers.
2. The status is **not reachable by any computation path**. The signed
   allowlist admits exactly `{SHADOW_ONLY}` for the Upbit row and exactly
   `{DISABLED_NO_STRATEGY}` for the futures row, so assigning
   `AUTO_READY_BLOCKED_BY_LIFECYCLE` to either raises
   `lane_signed_restriction_violation` at registry startup. This is asserted,
   not assumed (`test_auto_ready_blocked_by_lifecycle_is_unreachable_for_both_lanes`).
   Claiming a status that no code path can produce is precisely the failure mode
   the J6B round-1 review rejected.
3. Both frozen statuses are **strictly more restrictive** than
   `AUTO_READY_BLOCKED_BY_LIFECYCLE`: that status sits on the `AUTO_READY_*`
   ladder, whereas `SHADOW_ONLY` and `DISABLED_NO_STRATEGY` are not
   auto-ready at all. Applying the authority literally would *relax* the
   recorded posture of both lanes.

`LIFECYCLE_PREREQUISITES = UNMET (both lanes)`.
`LIFECYCLE_STATUS_RELABEL = NOT APPLIED — conflict escalated to orch.`

## 6. Negative guarantees

This artifact performs and authorizes all of the following at zero:

- broker or network mutation;
- database migration, schema operation, or DML probe;
- TaskIQ, Prefect, cron, launchd, or systemd registration;
- deployment, service restart, canary, or account cleanup;
- any live path, default, environment, host allowlist, cap, or confirm-gate
  change; and
- any change to existing recurring or manual ownership semantics.

The job changes no production `app/**` file, no runbook, no model, no
migration, no broker adapter, and no scheduler file. No execution, profile,
writer, cadence, cap, canary, or FX value is selected by this document.

## 7. Verification anchors

- Registry rows and signed allowlists: `app/services/mock_lane_registry.py:266-267`, `:319-368`, `:487-509`.
- Shadow broker-I/O refusal: `app/services/mock_lane_registry.py:1139-1140`.
- Futures leverage pin: `app/services/brokers/binance/futures_demo/execution_client.py:739-744`.
- Futures cancel (no confirm gate): `app/services/brokers/binance/futures_demo/execution_client.py:423-457`.
- Spot cancel (confirm gate): `app/services/brokers/binance/spot_demo/execution_client.py:372-393`.
- Default plugin: `app/services/brokers/binance/demo_strategy_loop/strategy.py:67-84`; CLI wiring at `scripts/binance_demo_strategy_loop.py:289`.
- Reservation reconcile chain: `app/tasks/binance_demo_root_reservation_reconcile_tasks.py:27`, `app/jobs/binance_demo_root_reservation_reconciliation.py:119-154`.
- Paper-cohort reach (finding F-1): `app/tasks/paper_cohort_tasks.py:17-22`, `app/services/brokers/binance/paper_adapter.py:186-192`.
- Regressions: `tests/services/test_upbit_futures_boundary.py`.
