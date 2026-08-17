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

* **Default plugin, and one entry point.** `NullStrategy` returns `None` for
  every input, and `scripts/binance_demo_strategy_loop.py` passes exactly
  `strategy=NullStrategy()` to `run_tick`. "Only entry point" is asserted
  against the whole repository, not against that one file: the regression
  enumerates every `*.py` under `app/`, `scripts/` and `research/` that either
  imports the strategy-loop package or calls `run_tick`, and asserts the set is
  exactly that one CLI. The round-2 revision parsed only the named file, and a
  second CLI calling `run_tick(strategy=object(), confirm=True)` passed the
  whole suite; a repository-wide enumeration is the only shape in which an
  "only X" claim is checkable at all.
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
  `_close_with_reduce_only`, whose single `submit_order` call site carries
  `reduce_only=True`, `confirm=True`, whose `order_test` pre-check carries
  `reduce_only=True`, and whose broker-echo check requires
  `expected_reduce_only=True` — all asserted as **argument values at those call
  sites**. The round-2 revision asserted the substring `reduce_only=True`
  appeared somewhere in the module, which a mutant flipping the real close
  submit to `False` walked straight through: the string survived in the planned
  row's metadata, the invariant did not. The open leg's `reduce_only=False` is
  pinned alongside it so the close-leg pin cannot be satisfied vacuously.
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
has to be split in half, because **neither half survives as written** — they are
wrong for different reasons and at different strengths:

| clause | verdict | why | what does hold |
| --- | --- | --- | --- |
| "No scheduler / TaskIQ / … wiring touches the Futures Demo execution client" | **inaccurate as written (narrowly)** | `app/tasks/binance_demo_root_reservation_reconcile_tasks.py` is a TaskIQ module and it does reach the client, so "no TaskIQ wiring touches" is false. | Only the narrower operational fact: that single reach carries no `schedule` keyword and calls no order-producing method. |
| "The smoke CLI is the **only** path that produces real Demo futures orders" | **false** | A second operator CLI produces them. | Nothing narrower — the claim is simply false. |

Round 2 recorded the first row's verdict correctly and then wrote "the first
clause survives" two paragraphs down and "scheduler clause is accurate" in §8.
Independent verification flagged the three statements as mutually
contradictory, and it was right. The single sentence this contract now states in
all three places (here, in the prose below, and in §8) is:

> The literal scheduler clause is inaccurate as written; what holds is the
> narrower fact that the one TaskIQ reach is scheduleless and calls no
> order-producing method.

The second producer is `scripts/binance_demo_strategy_loop.py:288-303`, which
passes `confirm=args.confirm` and `signal_override=` straight into
`run_tick`; `run_tick` short-circuits to a `dry_run` outcome only while
`confirm` is false (`demo_strategy_loop/orchestrator.py:326-332`). ROB-993's
`--paper-signal` mode is the end-to-end Demo round trip. So
`scripts/binance_futures_demo_smoke.py` is not the only producer.

This second producer is **not** a scheduler reach — both producers are
operator-invoked foreground processes — which is why the two clauses fail for
different reasons and are pinned by different regressions. It is not why the
first clause "survives": it does not survive. Bounding the second producer: the
strategy loop is default-disabled by `BINANCE_DEMO_STRATEGY_LOOP_ENABLED`, and
its shipped plugin is `NullStrategy`, so `--once` / `--loop` emit no signal at
all; only an explicitly injected `--paper-signal` reaches the submit path.

Two earlier revisions of this contract overstated this section and each is
recorded here rather than quietly dropped. Round 1 translated the narrow TaskIQ
accuracy into "that runbook's operative promise still holds", which was false and
unenforced. Round 2 corrected the table but left "the first clause survives" and
an §8 row reading "accurate", contradicting its own verdict. Both halves are now
pinned separately, and the scheduler regression asserts the contradiction *and*
the narrower fact together, so neither half can be dropped silently
(`test_the_futures_runbook_scheduler_clause_is_inaccurate_as_written`,
`test_futures_runbook_only_path_claim_is_contradicted_by_a_second_cli`). The
second of those two pins `confirm=args.confirm` **by value** at the `run_tick`
call site; round 2 collected only the keyword's name, and a mutant rewriting it
to `confirm=False` — which would make this whole row false — passed.

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

### 5.0 The single criterion used for C3-4, stated before it is applied

The authority asks for "the lane-native evidence written" for seven kinds. Round
2 answered that question with two different criteria in one table — it counted
`unknown` as **PRESENT** on nothing more than `record_anomaly` plus a reason
string, while calling `reject` and `expiry` **ABSENT** for having exactly that
same shape. Independent verification measured the source and found both native
statuses are in fact persisted and distinguishable. One criterion now applies to
all seven kinds in both lanes:

| verdict | means |
| --- | --- |
| **PRESENT** | A typed lifecycle state is stamped by a dedicated writer, and that kind is the only kind routed to it. A reader can tell the kind apart from a column. |
| **DEGRADED** | Evidence *is* persisted and *is* distinguishable, but only inside free-form text (`anomaly_reason`) or free-form JSON (`extra_metadata`). Telling the kind apart requires string-matching, and the typed state is shared with other kinds. |
| **ABSENT** | Nothing persisted distinguishes the kind at all. |

The writer→state map this turns on is asserted directly, read off each writer's
own transition call rather than off its name
(`test_c3_4_futures_evidence_writers_each_own_one_typed_state_and_ack_carries_broker_id`).

### 5.1 `crypto.upbit.shadow`

| item | status | evidence | regression |
| --- | --- | --- | --- |
| C3-1 recovery owner | **ABSENT** | No owner is designated anywhere. `scheduler_owner=None` and `MissingBinding.OWNER` is present on the row. Per the acceptance rule this is an unmet item, reported as such rather than filled in. | `test_upbit_shadow_row_is_frozen_on_all_four_axes` |
| C3-2 restart trigger | **ABSENT** | The lane has no durable claim to rediscover: `allowed_hosts=()`, `credential_namespace=None`, no ledger table, no journal root. | `test_upbit_shadow_is_synthetic_and_never_labelled_broker_paper_or_native` (pins `allowed_hosts` / `credential_namespace`; "no ledger table, no journal root" is **record only**) |
| C3-3 readback operation | **ABSENT** | No authoritative broker readback is possible; broker I/O is structurally refused with `shadow_broker_io_forbidden`. | `test_upbit_shadow_rejects_every_endpoint_it_is_offered`, `test_upbit_shadow_chain_reaches_and_dies_at_the_shadow_guard` |
| C3-4 lane-native evidence (7 kinds) | **ABSENT — 0 of 7** | Under §5.0's criterion all seven are ABSENT: nothing is persisted, so nothing distinguishes any kind. The lane is synthetic, owns no host and no credential namespace, and writes no broker lifecycle evidence at all. | **record only** — implied by the C3-2/C3-3 pins, but no test asserts the absence of a lane-native writer by name |
| C3-5 exact `release_if_matches` condition | **ABSENT** | `mock_integration.coordination.release_if_matches` exists but is not called from this lane's surface. | `test_neither_lane_calls_the_j3a_release_if_matches_contract` |
| C3-6 operator-visible blocked state | **PRESENT** | `lane_status=SHADOW_ONLY`, `activation_status=DISABLED`, `identity_status="UNKNOWN"`, and the full `missing_bindings` tuple are all operator-visible on the registry row. | `test_upbit_shadow_row_is_frozen_on_all_four_axes`, `test_upbit_shadow_is_synthetic_and_never_labelled_broker_paper_or_native` |

### 5.2 `crypto.binance.futures_demo`

| item | status | evidence | regression |
| --- | --- | --- | --- |
| C3-1 recovery owner | **NOT SATISFIED — no single designation** | **Three** distinct modules own recovery phases and no contract names one: `app/jobs/binance_demo_root_reservation_reconciliation.py` (pre-acknowledgement roots), `demo_strategy_loop/execution.py` (in-tick `_close_with_reduce_only` / `_reconcile`), and `scripts/binance_futures_demo_smoke.py` (the same close/reconcile pair at `:861` / `:1001`, called on the real confirm round trip at `:845` / `:990`). Round 2 listed only the first two; counting the strategy loop's in-tick phase while omitting the CLI's isomorphic one had no principle behind it. None of the three delegates to another (asserted pairwise). "Exactly one" is not met, so this is reported as a failure rather than resolved by picking one. `DemoScalpingExecutor` also carries futures close/reconcile code but its only production caller is spot-configured, so it is not counted here — recorded so the omission is a stated judgement rather than a gap. | `test_c3_1_futures_recovery_ownership_is_split_across_three_surfaces` |
| C3-2 restart trigger | **PRESENT** | `_candidate_where_clauses` rediscovers surviving durable roots after restart — `planned`, or `previewed`/`validated` with `broker_order_id IS NULL`, older than `stale_before` (`app/jobs/binance_demo_root_reservation_reconciliation.py:125-154`). Pinned as a **closed equality** against the ledger's nine-state universe, not as a list of forbidden names: round 2 forbade `submitted`/`filled`/`anomaly` and a mutant adding the terminal state `cancelled` to the candidate tuple passed. Adding a tenth lifecycle state to the ledger now fails this row until someone decides on the record whether the sweep should claim it. | `test_c3_2_futures_restart_trigger_rediscovers_only_pre_ack_roots` |
| C3-3 readback operation | **PRESENT** | `BinanceFuturesDemoExecutionClient.get_order` (`GET /fapi/v1/order`), dispatched through `_lookup_order` (`:119-122`). | `test_c3_3_futures_authoritative_readback_is_get_order` |
| C3-4 lane-native evidence (7 kinds) | **PARTIAL — 3 present, 4 degraded, 0 absent** (§5.0 criterion) | **Present (3):** ACK → typed `submitted`, and `record_submitted` carries `broker_order_id` through to `row.broker_order_id`; cancel → typed `cancelled`; terminal reconciliation → typed `reconciled`. **Degraded (4):** `unknown`, `reject` and `expiry` all route to the one typed `anomaly` state and are told apart only by the free-form `anomaly_reason` text — both execution surfaces persist the native status via `record_submitted(extra_metadata_merge={"submit_status": …})` and `record_anomaly(reason="open_did_not_take_effect: … status=<native>")`, with `_TERMINAL_NONFILL_STATUSES = {CANCELED, REJECTED, EXPIRED}`; partial fill routes to the same typed `filled` state as a full fill, and `record_filled` takes no quantity argument while the ledger has no `executed_qty` / `remaining_qty` column. **Absent (0).** Round 2 called reject/expiry `absent` after looking for `record_rejected` / `record_expired` method *names*; the authority asks for evidence written, and it is written. There is still no typed column for any of the four, which is why they are degraded and not present. | `test_c3_4_futures_evidence_writers_each_own_one_typed_state_and_ack_carries_broker_id`, `test_c3_4_reject_and_expiry_leave_free_form_evidence_rather_than_none` |
| C3-5 exact `release_if_matches` condition | **ABSENT** | The J3A contract is not called from this lane. The lane's own release condition is narrower and differently named: release only on explicit `BinanceDemoOrderNotFound` within an 89-day bound, or a terminal status in `{CANCELED, REJECTED, EXPIRED}` with `executedQty == 0`, and only after venue-host, credential-fingerprint, and broker-identity equality all match. That is a release rule, but it is not the exact `release_if_matches` contract the authority names. | `test_neither_lane_calls_the_j3a_release_if_matches_contract` (call-site absence). The narrower lane-own release rule quoted here is **record only**. |
| C3-6 operator-visible blocked state | **PRESENT** | `anomaly` lifecycle state (writer `record_anomaly`), plus per-candidate `kept` outcomes carrying **ten** stable reasons: `client_unavailable`, `venue_host_mismatch`, `credential_fingerprint_missing`, `client_credential_fingerprint_unavailable`, `credential_fingerprint_mismatch`, `broker_lookup_failed`, `malformed_broker_truth`, `broker_identity_mismatch`, `broker_exposure_not_disproven`, `broker_lookup_retention_exceeded`. The round-1 revision of this row enumerated eight and silently dropped the two fingerprint-availability reasons; the closed-set regression added in round 2 is what measured the real count. Round 2's regression harvested every literal `reason` in the job **without pairing it to its `action`**, so a mutant re-pointing `client_unavailable` from `kept` to `would_release` — turning a blocked-state row into a release — left the harvested set byte-identical. The claim is about pairs, so the regression now collects only reasons paired with a literal `action="kept"`, and additionally asserts that the set of literal actions across all enumerable outcomes is exactly `{kept}`. | `test_c3_6_futures_blocked_state_reasons_are_pinned_as_a_closed_set` |

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
- Lifecycle evidence writers: `app/services/brokers/binance/demo/ledger/service.py:238-500`; the state universe `_ALLOWED_TRANSITIONS` at `:56-66`; broker-id persistence `app/services/brokers/binance/demo/ledger/repository.py:485`; ledger columns `app/models/binance_demo_order_ledger.py:132-208`.
- Native terminal non-fill statuses (C3-4 degraded evidence): `app/services/brokers/binance/demo_strategy_loop/execution.py:80`, `:446-451` (ACK metadata), `:483` (anomaly reason); `scripts/binance_futures_demo_smoke.py:102`, `:761-766`, `:801-810`.
- Third recovery surface (C3-1): `scripts/binance_futures_demo_smoke.py:845`, `:861`, `:990`, `:1001`.
- Close-leg reduceOnly call sites: `app/services/brokers/binance/demo_strategy_loop/execution.py:598-603` (`order_test`), `:617-625` (`submit_order`), `:642-653` (echo check); open-leg counterpart `:406-414`.
- Blocked-state reasons: `app/jobs/binance_demo_root_reservation_reconciliation.py:231-379`.
- Regressions: `tests/services/test_upbit_futures_boundary.py`.

## 8. Assertion ↔ regression map

Failure mode this section exists to prevent: a contract sentence that reads like
a guaranteed invariant while nothing enforces it, so the runbook or ledger moves
and only the contract rots. Every safety assertion in this document appears
below with the test that enforces it, or is labelled **record only**.

**Record only** means: observed at `1e1c75f8`, no regression behind it, may go
stale silently. It is not a weaker guarantee — it is *not a guarantee*.

🔴 **What the map's own regression does and does not prove.**
`test_the_contract_assertion_map_matches_this_modules_tests_exactly` compares the
set of backtick-quoted test names in this document to the set of `test_*`
functions in the module. That proves every cited test **exists** and every test
**is cited** — nothing more. It does **not** prove that a cited test enforces the
row it is cited on. Round 2 presented name equality as if it discharged the
enforcement requirement; independent verification then made seven of these rows
false with mutants that the whole suite passed. Enforcement strength is a
property of each individual regression, and §9 lists the rows where it is still
weaker than the sentence beside it.

| § | assertion | enforcing regression |
| --- | --- | --- |
| rows | both lane rows frozen on 4 axes + `writer`/`auto_order_enabled` false + quote currency | `test_upbit_shadow_row_is_frozen_on_all_four_axes`, `test_futures_demo_row_is_frozen_on_all_four_axes` |
| rows | `None` is not a spelling of `DISABLED` | `test_absent_and_disabled_scheduler_owners_stay_distinct_values` |
| 1 | shadow lane is synthetic: no host, no credential namespace, never labelled broker paper/native | `test_upbit_shadow_is_synthetic_and_never_labelled_broker_paper_or_native` |
| 1 | broker I/O refused by lane class, not host string | `test_upbit_shadow_rejects_every_endpoint_it_is_offered` (5 hosts) |
| 1 | the guard is on the live chain, and the canonical registry dies earlier | `test_upbit_shadow_chain_reaches_and_dies_at_the_shadow_guard`, `test_upbit_shadow_chain_under_the_canonical_registry_dies_even_earlier` |
| 1 | Upbit private credential module unreachable from the lane surface | `test_upbit_shadow_surface_never_reaches_upbit_private_credential_modules`, `test_registry_module_imports_no_broker_transport_or_credential_module` |
| 2 | default plugin is `NullStrategy`; that CLI is the **only** `run_tick` entry point in `app/` + `scripts/` + `research/`, and it passes only `NullStrategy()` | `test_default_strategy_plugin_is_null_strategy_and_never_emits_a_signal`, `test_the_strategy_loop_cli_is_the_only_run_tick_entry_point_and_wires_null_strategy` |
| 2 | no scheduler entry point reaches the strategy loop | `test_no_scheduler_entrypoint_reaches_the_demo_strategy_loop` |
| 2 | exactly one scheduleless scheduler reach into the futures client | `test_only_the_scheduleless_reconcile_task_reaches_the_futures_demo_client` |
| 2 | that reach calls no order-producing method | `test_the_reconcile_chain_calls_no_order_producing_client_method` |
| 2 | leverage 1x refused pre-HTTP | `test_futures_leverage_is_pinned_to_1x_before_any_http` (6 values) |
| 2 | one-way only: no `position_side` parameter; hedge blocked | `test_futures_one_way_mode_is_structural_and_hedge_mode_is_blocked` (source pin for the hedge raise; the runtime hedge path is **record only**) |
| 2 | `reduce_only` / `confirm` default false, unconfirmed submit is a dry run | `test_futures_reduce_only_defaults_off_and_submit_defaults_to_dry_run` |
| 2 | close leg always `reduce_only=True` — `order_test`, `submit_order` and echo check pinned **by argument value at each call site**, with the open leg's `False` as the non-vacuity counterpart | `test_the_strategy_loop_close_leg_always_sets_reduce_only_true` |
| 2 | leg cap `[6, 10]` USDT literals | `test_leg_notional_cap_constants_are_locked_literals` |
| 2 | kill-switch caps are 1 and 2, field set closed, guard rejects each widening | `test_kill_switch_locked_limits_are_pinned_to_literal_1_and_2`, `test_kill_switch_guard_accepts_exactly_the_literal_locked_pair`, `test_kill_switch_guard_rejects_each_widened_cap_individually` (4 rows) |
| 2 | futures host allowlist is a single demo host | `test_futures_demo_host_allowlist_stays_a_single_demo_host` |
| 2 | neither lane can be activated | `test_upbit_shadow_lane_cannot_be_activated`, `test_futures_demo_lane_cannot_be_activated` |
| 3 | cancel-confirm asymmetry in both directions, declared intent, submit gated on both | `test_spot_cancel_has_a_per_call_confirm_gate`, `test_futures_cancel_has_no_confirm_gate`, `test_the_cancel_confirm_asymmetry_is_declared_intent_not_an_oversight`, `test_both_submit_paths_keep_the_confirm_gate_they_do_share` |
| 4 | Spot: two scheduler reaches, one schedule-bearing | `test_the_spot_demo_client_is_reached_by_exactly_two_scheduler_entrypoints`, `test_the_paper_cohort_task_carries_a_schedule_unlike_the_reconcile_task` |
| 4 | Spot runbook sentence + contradicting repo fact together | `test_spot_demo_runbook_no_scheduler_claim_is_contradicted_by_repo_fact` |
| 4 | Spot bounding gates (`PAPER_COHORT_ENABLED`, SHADOW early return, `PAPER_EXECUTION_ENABLED`, actor id, `{BTCUSDT, ETHUSDT}` allowlist) | **record only** — cited with file:line, no regression. These are `app/**` facts J6C does not own; a J6C test pinning them would assert someone else's invariant. |
| 4.1 | futures runbook scheduler clause is **inaccurate as written**; only the narrower "scheduleless, no order-producing method" fact holds. Both the contradiction and the narrower fact are asserted together | `test_the_futures_runbook_scheduler_clause_is_inaccurate_as_written` |
| 4.1 | futures runbook "only path" clause is false — second CLI produces orders, with `confirm=args.confirm` pinned **by value** at the `run_tick` call site | `test_futures_runbook_only_path_claim_is_contradicted_by_a_second_cli` |
| 5.0 | the single PRESENT / DEGRADED / ABSENT criterion, and the writer→typed-state map it turns on | `test_c3_4_futures_evidence_writers_each_own_one_typed_state_and_ack_carries_broker_id` |
| 5.1 | Upbit C3-1 … C3-6 | per-row column in §5.1 (C3-4 and part of C3-2 are **record only**) |
| 5.2 | Futures C3-1 … C3-6 | per-row column in §5.2 (the lane-own release rule text in C3-5 is **record only**) |
| 5.2 | C3-1: three recovery surfaces, each still *reaching* its broker operation, none delegating to another | `test_c3_1_futures_recovery_ownership_is_split_across_three_surfaces` |
| 5.2 | C3-4: reject/expiry/unknown are DEGRADED — persisted and distinguishable, but only in free-form text/JSON, with no typed column | `test_c3_4_reject_and_expiry_leave_free_form_evidence_rather_than_none` |
| 5.3 | `AUTO_READY_BLOCKED_BY_LIFECYCLE` is unreachable for both lanes | `test_auto_ready_blocked_by_lifecycle_is_unreachable_for_both_lanes` (2 lanes) |
| 5.3 | the three reasons for not relabelling | **record only** — reason 2 is enforced by the test above; reasons 1 and 3 are readings of the signed registry and the status ladder |
| 6 | negative guarantees | not test-shaped — enforced by the file fence and `git diff` (two added files, zero `app/**` change) |
| 8 | this map itself: every cited test exists, and every test is cited — **name equality only**, which is not evidence of enforcement (see the note above §8's table and §9) | `test_the_contract_assertion_map_matches_this_modules_tests_exactly` |

## 9. Claims this job could not enforce, stated as such

Round 3 was the last round available. Rather than leave a sentence reading like a
guarantee where none exists, every remaining gap is listed here. **Nothing below
is a guaranteed invariant.**

| claim | why it is not enforced here | disposition |
| --- | --- | --- |
| §4 Spot bounding gates (`PAPER_COHORT_ENABLED`, the `SHADOW` early return, `PAPER_EXECUTION_ENABLED`, the actor-id fail-closed, the `{BTCUSDT, ETHUSDT}` allowlist) | These are `app/**` invariants owned by ROB-845/849. A J6C regression pinning them would assert someone else's contract and would go red on a legitimate change there. | **record only**, cited with file:line. Unchanged from round 2. |
| §5.1 C3-4 "0 of 7" for the Upbit lane | Absence of a writer *by name* is not assertable without enumerating every possible writer name. The C3-2/C3-3 pins (`allowed_hosts=()`, `credential_namespace=None`, `shadow_broker_io_forbidden` on the live chain) imply it, but no test states it. | **record only**. |
| §5.2 C3-5 the lane's own narrower release rule (89-day bound, terminal status + `executedQty == 0`, triple identity match) | Only the *absence* of the J3A `release_if_matches` call site is pinned. The positive description of the lane's own rule is prose. | **record only**. |
| §5.3 reasons 1 and 3 for not relabelling to `AUTO_READY_BLOCKED_BY_LIFECYCLE` | Reason 2 (unreachability) is asserted. Reasons 1 and 3 are readings of the signed registry and of the status ladder's ordering, not computable facts. | **record only**; the relabel conflict stays escalated to orch. |
| §5.2 C3-1 recovery-phase bodies are only checked for *reachability* | `_reaches_call` kills a phase function that has been emptied by a top-level `raise`/`return`. A raise nested inside an always-true `if`, or a body that reaches its broker call but then discards the result, would still escape it. Actually executing these functions would require faking a ledger service, an `AsyncSession`, and the futures client — coupling this suite to `app/**` internals it does not own. | **bounded**, and the bound is written into the helper's docstring. |
| §2 "one-way only" runtime hedge path | The `BinanceFuturesDemoHedgeModeBlocked` raise is a source pin; the runtime behaviour of the hedge branch is not driven. | **record only**, unchanged from round 2. |
| C5 | No authority in either deliverable closes it. | `C5 = UNKNOWN`, unchanged across all three rounds. |
| `FINDING_F1` | J6C owns neither `docs/runbooks/**` nor `app/**`. Both halves are pinned against the contradicting repo fact so the divergence cannot be resolved in silence, but the divergence itself remains. | `OPEN (Spot and Futures)` — **orch disposition required**. |
