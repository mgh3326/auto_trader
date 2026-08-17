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
* **Kill switch.** `LOCKED_LIMITS.max_concurrent_positions == 1` and
  `LOCKED_LIMITS.max_consecutive_stop_losses_per_utc_day == 2` are asserted as
  literals written in the test, and the guard is exercised against
  literal-built pairs — never against `LOCKED_LIMITS` alone.
  `assert_kill_switch_limits_locked` compares its argument to `LOCKED_LIMITS`,
  so a guard-only assertion would have zero discriminating power over the cap
  values: widening the constant would move the expectation with it. The
  round-1 revision of this contract made exactly that claim about a
  self-referential test, and it was wrong; the field set of
  `StrategyLoopKillSwitchLimits` is now pinned as a literal too, so a third
  field with a permissive default cannot slip past the two value pins.

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

Bounding this honestly. Static import reach is not the same as a runtime order,
and three separate gates sit between the schedule and a submit:

* The schedule list is empty unless `settings.PAPER_COHORT_ENABLED` is set
  (`app/core/config.py:356`, default `False`), and the adapter's symbol
  allowlist is `{BTCUSDT, ETHUSDT}`.
* A `SHADOW`-mode invocation returns from `PaperCohortRunner` **before**
  `application.submit` is reached (`app/services/paper_cohort/runner.py:828-846`),
  so a shadow soak submits nothing even with the schedule on.
* Reaching the submit path at all additionally requires
  `settings.PAPER_EXECUTION_ENABLED` (`runner.py:142`, default `False`), and an
  unmapped caller id fails closed with `actor_identity_unavailable`
  (`app/jobs/paper_cohort.py:40-45`, `PAPER_VALIDATION_ACTOR_ROLES` default
  empty).
* This is **pre-existing** (ROB-845/849) and was not introduced by ROB-1271.

### 4.1 The same "only path" claim is false for Futures too — for a different reason

The identical sentence in `docs/runbooks/binance-futures-demo-smoke.md:50-53`
has to be split in half, because only one half survives:

| clause | verdict | why |
| --- | --- | --- |
| "No scheduler / TaskIQ / … wiring touches the Futures Demo execution client" | **narrowly inaccurate** | One TaskIQ module reaches the client, but it is scheduleless and calls no order-producing method. |
| "The smoke CLI is the **only** path that produces real Demo futures orders" | **false** | A second operator CLI produces them. |

The second producer is `scripts/binance_demo_strategy_loop.py:288-303`, which
passes `confirm=args.confirm` and `signal_override=` straight into
`run_tick`; `run_tick` short-circuits to a `dry_run` outcome only while
`confirm` is false (`demo_strategy_loop/orchestrator.py:326-332`). ROB-993's
`--paper-signal` mode is the end-to-end Demo round trip. So
`scripts/binance_futures_demo_smoke.py` is not the only producer.

This is **not** a scheduler reach — both producers are operator-invoked
foreground processes — which is exactly why the first clause survives and the
second does not. Bounding it: the strategy loop is default-disabled by
`BINANCE_DEMO_STRATEGY_LOOP_ENABLED`, and its shipped plugin is `NullStrategy`,
so `--once` / `--loop` emit no signal at all; only an explicitly injected
`--paper-signal` reaches the submit path.

An earlier revision of this contract translated the narrow TaskIQ accuracy into
"that runbook's operative promise still holds". That sentence was false and no
test enforced it. Both halves are now pinned separately
(`test_the_futures_runbook_scheduler_clause_matches_repo_fact`,
`test_futures_runbook_only_path_claim_is_contradicted_by_a_second_cli`).

J6C owns neither `docs/runbooks/**` nor `app/**`, so this divergence is
recorded and pinned rather than repaired. Each regression asserts the runbook
sentence and the contradicting repo fact together, so resolving the divergence
in either direction must update the test in the same change.
`FINDING_F1 = OPEN (Spot and Futures) — orch disposition required.`

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

Every row carries its enforcement status. A row marked **record only** is an
observation at `1e1c75f8` with **no regression behind it** — it can go stale
without any test noticing, and it must not be read as a guaranteed invariant.
Full sentence-level mapping is in §8.

### 5.1 `crypto.upbit.shadow`

| item | status | evidence | regression |
| --- | --- | --- | --- |
| C3-1 recovery owner | **ABSENT** | No owner is designated anywhere. `scheduler_owner=None` and `MissingBinding.OWNER` is present on the row. Per the acceptance rule this is an unmet item, reported as such rather than filled in. | `test_upbit_shadow_row_is_frozen_on_all_four_axes` |
| C3-2 restart trigger | **ABSENT** | The lane has no durable claim to rediscover: `allowed_hosts=()`, `credential_namespace=None`, no ledger table, no journal root. | `test_upbit_shadow_is_synthetic_and_never_labelled_broker_paper_or_native` (pins `allowed_hosts` / `credential_namespace`; "no ledger table, no journal root" is **record only**) |
| C3-3 readback operation | **ABSENT** | No authoritative broker readback is possible; broker I/O is structurally refused with `shadow_broker_io_forbidden`. | `test_upbit_shadow_rejects_every_endpoint_it_is_offered`, `test_upbit_shadow_chain_reaches_and_dies_at_the_shadow_guard` |
| C3-4 lane-native evidence (7 kinds) | **ABSENT — 0 of 7** | None of ACK, unknown, reject, expiry, partial fill, cancel, or terminal reconciliation has a lane-native write point. The lane is synthetic and writes no broker lifecycle evidence at all. | **record only** — implied by the C3-2/C3-3 pins, but no test asserts the absence of a lane-native writer by name |
| C3-5 exact `release_if_matches` condition | **ABSENT** | `mock_integration.coordination.release_if_matches` exists but is not called from this lane's surface. | `test_neither_lane_calls_the_j3a_release_if_matches_contract` |
| C3-6 operator-visible blocked state | **PRESENT** | `lane_status=SHADOW_ONLY`, `activation_status=DISABLED`, `identity_status="UNKNOWN"`, and the full `missing_bindings` tuple are all operator-visible on the registry row. | `test_upbit_shadow_row_is_frozen_on_all_four_axes`, `test_upbit_shadow_is_synthetic_and_never_labelled_broker_paper_or_native` |

### 5.2 `crypto.binance.futures_demo`

| item | status | evidence | regression |
| --- | --- | --- | --- |
| C3-1 recovery owner | **NOT SATISFIED — no single designation** | Two distinct modules own different recovery phases and no contract names one: `app/jobs/binance_demo_root_reservation_reconciliation.py` (pre-acknowledgement roots) and `demo_strategy_loop/execution.py` (in-tick close/reconcile). "Exactly one" is not met, so this is reported as a failure rather than resolved by picking one. | `test_c3_1_futures_recovery_ownership_is_split_across_two_modules` |
| C3-2 restart trigger | **PRESENT** | `_candidate_where_clauses` rediscovers surviving durable roots after restart — `planned`, or `previewed`/`validated` with `broker_order_id IS NULL`, older than `stale_before` (`app/jobs/binance_demo_root_reservation_reconciliation.py:125-154`). | `test_c3_2_futures_restart_trigger_rediscovers_only_pre_ack_roots` |
| C3-3 readback operation | **PRESENT** | `BinanceFuturesDemoExecutionClient.get_order` (`GET /fapi/v1/order`), dispatched through `_lookup_order` (`:119-122`). | `test_c3_3_futures_authoritative_readback_is_get_order` |
| C3-4 lane-native evidence (7 kinds) | **PARTIAL — 4 present, 1 degraded, 2 absent** | Present: `record_submitted` (ACK, carries `broker_order_id`), `record_anomaly` (unknown, carries `anomaly_reason`), `record_cancelled` (cancel), `record_reconciled` (terminal reconciliation). Degraded: partial fill — `record_filled` takes no quantity argument and `binance_demo_order_ledger` has no `executed_qty` / `remaining_qty` column, so a partial fill cannot be distinguished from a full one except through free-form `extra_metadata`. Absent: **reject** and **expiry** have no distinct write point; both collapse into the `cancelled` / `anomaly` branches, which also absorb unrelated causes. | `test_c3_4_futures_lane_evidence_has_no_reject_expiry_or_partial_fill_writer` |
| C3-5 exact `release_if_matches` condition | **ABSENT** | The J3A contract is not called from this lane. The lane's own release condition is narrower and differently named: release only on explicit `BinanceDemoOrderNotFound` within an 89-day bound, or a terminal status in `{CANCELED, REJECTED, EXPIRED}` with `executedQty == 0`, and only after venue-host, credential-fingerprint, and broker-identity equality all match. That is a release rule, but it is not the exact `release_if_matches` contract the authority names. | `test_neither_lane_calls_the_j3a_release_if_matches_contract` (call-site absence). The narrower lane-own release rule quoted here is **record only**. |
| C3-6 operator-visible blocked state | **PRESENT** | `anomaly` lifecycle state (writer `record_anomaly`), plus per-candidate `kept` outcomes carrying **ten** stable reasons: `client_unavailable`, `venue_host_mismatch`, `credential_fingerprint_missing`, `client_credential_fingerprint_unavailable`, `credential_fingerprint_mismatch`, `broker_lookup_failed`, `malformed_broker_truth`, `broker_identity_mismatch`, `broker_exposure_not_disproven`, `broker_lookup_retention_exceeded`. The round-1 revision of this row enumerated eight and silently dropped the two fingerprint-availability reasons; the closed-set regression added in round 2 is what measured the real count. | `test_c3_6_futures_blocked_state_reasons_are_pinned_as_a_closed_set` |

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
- Paper-cohort reach (finding F-1, Spot): `app/tasks/paper_cohort_tasks.py:17-22`, `app/services/brokers/binance/paper_adapter.py:186-192`; gates at `app/core/config.py:353`, `:356`, `app/services/paper_cohort/runner.py:142`, `:828-846`, `app/jobs/paper_cohort.py:40-45`.
- Second futures producer (finding F-1, Futures): `scripts/binance_demo_strategy_loop.py:288-303`, `app/services/brokers/binance/demo_strategy_loop/orchestrator.py:326-332`.
- Kill-switch caps: `app/services/brokers/binance/demo_strategy_loop/kill_switch.py:37-49`, `:56-69`.
- Lifecycle evidence writers: `app/services/brokers/binance/demo/ledger/service.py:238-500`; ledger columns `app/models/binance_demo_order_ledger.py:132-208`.
- Blocked-state reasons: `app/jobs/binance_demo_root_reservation_reconciliation.py:231-379`.
- Regressions: `tests/services/test_upbit_futures_boundary.py`.

## 8. Assertion ↔ regression map

Failure mode this section exists to prevent: a contract sentence that reads like
a guaranteed invariant while nothing enforces it, so the runbook or ledger moves
and only the contract rots. Every safety assertion in this document appears
below with the test that enforces it, or is labelled **record only**.

**Record only** means: observed at `1e1c75f8`, no regression behind it, may go
stale silently. It is not a weaker guarantee — it is *not a guarantee*.

| § | assertion | enforcing regression |
| --- | --- | --- |
| rows | both lane rows frozen on 4 axes + `writer`/`auto_order_enabled` false + quote currency | `test_upbit_shadow_row_is_frozen_on_all_four_axes`, `test_futures_demo_row_is_frozen_on_all_four_axes` |
| rows | `None` is not a spelling of `DISABLED` | `test_absent_and_disabled_scheduler_owners_stay_distinct_values` |
| 1 | shadow lane is synthetic: no host, no credential namespace, never labelled broker paper/native | `test_upbit_shadow_is_synthetic_and_never_labelled_broker_paper_or_native` |
| 1 | broker I/O refused by lane class, not host string | `test_upbit_shadow_rejects_every_endpoint_it_is_offered` (5 hosts) |
| 1 | the guard is on the live chain, and the canonical registry dies earlier | `test_upbit_shadow_chain_reaches_and_dies_at_the_shadow_guard`, `test_upbit_shadow_chain_under_the_canonical_registry_dies_even_earlier` |
| 1 | Upbit private credential module unreachable from the lane surface | `test_upbit_shadow_surface_never_reaches_upbit_private_credential_modules`, `test_registry_module_imports_no_broker_transport_or_credential_module` |
| 2 | default plugin is `NullStrategy`, and the CLI passes only that | `test_default_strategy_plugin_is_null_strategy_and_never_emits_a_signal`, `test_the_strategy_loop_cli_wires_run_tick_to_null_strategy` |
| 2 | no scheduler entry point reaches the strategy loop | `test_no_scheduler_entrypoint_reaches_the_demo_strategy_loop` |
| 2 | exactly one scheduleless scheduler reach into the futures client | `test_only_the_scheduleless_reconcile_task_reaches_the_futures_demo_client` |
| 2 | that reach calls no order-producing method | `test_the_reconcile_chain_calls_no_order_producing_client_method` |
| 2 | leverage 1x refused pre-HTTP | `test_futures_leverage_is_pinned_to_1x_before_any_http` (6 values) |
| 2 | one-way only: no `position_side` parameter; hedge blocked | `test_futures_one_way_mode_is_structural_and_hedge_mode_is_blocked` (source pin for the hedge raise; the runtime hedge path is **record only**) |
| 2 | `reduce_only` / `confirm` default false, unconfirmed submit is a dry run | `test_futures_reduce_only_defaults_off_and_submit_defaults_to_dry_run` |
| 2 | close leg always `reduce_only=True` | `test_the_strategy_loop_close_leg_always_sets_reduce_only_true` (source pin) |
| 2 | leg cap `[6, 10]` USDT literals | `test_leg_notional_cap_constants_are_locked_literals` |
| 2 | kill-switch caps are 1 and 2, field set closed, guard rejects each widening | `test_kill_switch_locked_limits_are_pinned_to_literal_1_and_2`, `test_kill_switch_guard_accepts_exactly_the_literal_locked_pair`, `test_kill_switch_guard_rejects_each_widened_cap_individually` (4 rows) |
| 2 | futures host allowlist is a single demo host | `test_futures_demo_host_allowlist_stays_a_single_demo_host` |
| 2 | neither lane can be activated | `test_upbit_shadow_lane_cannot_be_activated`, `test_futures_demo_lane_cannot_be_activated` |
| 3 | cancel-confirm asymmetry in both directions, declared intent, submit gated on both | `test_spot_cancel_has_a_per_call_confirm_gate`, `test_futures_cancel_has_no_confirm_gate`, `test_the_cancel_confirm_asymmetry_is_declared_intent_not_an_oversight`, `test_both_submit_paths_keep_the_confirm_gate_they_do_share` |
| 4 | Spot: two scheduler reaches, one schedule-bearing | `test_the_spot_demo_client_is_reached_by_exactly_two_scheduler_entrypoints`, `test_the_paper_cohort_task_carries_a_schedule_unlike_the_reconcile_task` |
| 4 | Spot runbook sentence + contradicting repo fact together | `test_spot_demo_runbook_no_scheduler_claim_is_contradicted_by_repo_fact` |
| 4 | Spot bounding gates (`PAPER_COHORT_ENABLED`, SHADOW early return, `PAPER_EXECUTION_ENABLED`, actor id, `{BTCUSDT, ETHUSDT}` allowlist) | **record only** — cited with file:line, no regression. These are `app/**` facts J6C does not own; a J6C test pinning them would assert someone else's invariant. |
| 4.1 | futures runbook scheduler clause is accurate | `test_the_futures_runbook_scheduler_clause_matches_repo_fact` |
| 4.1 | futures runbook "only path" clause is false — second CLI produces orders | `test_futures_runbook_only_path_claim_is_contradicted_by_a_second_cli` |
| 5.1 | Upbit C3-1 … C3-6 | per-row column in §5.1 (C3-4 and part of C3-2 are **record only**) |
| 5.2 | Futures C3-1 … C3-6 | per-row column in §5.2 (the lane-own release rule text in C3-5 is **record only**) |
| 5.3 | `AUTO_READY_BLOCKED_BY_LIFECYCLE` is unreachable for both lanes | `test_auto_ready_blocked_by_lifecycle_is_unreachable_for_both_lanes` (2 lanes) |
| 5.3 | the three reasons for not relabelling | **record only** — reason 2 is enforced by the test above; reasons 1 and 3 are readings of the signed registry and the status ladder |
| 6 | negative guarantees | not test-shaped — enforced by the file fence and `git diff` (two added files, zero `app/**` change) |
| 8 | this map itself: every cited test exists, and every test is cited | `test_the_contract_assertion_map_matches_this_modules_tests_exactly` (asserts set **equality** between the citations above and the module's test functions, so the map cannot drift in either direction) |
