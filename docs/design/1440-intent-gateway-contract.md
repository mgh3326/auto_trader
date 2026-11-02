# Intent gateway: the Go and Python contract (ROB-1440, plan unit E1)

This document is the contract the E2 Go executor implements. Python owns what may trade. Go owns when, and at what price, inside that permission. Go never reaches a broker or the database directly in this design: it sends child requests to a Python gateway, which calls the existing broker services.

**Status:** default-disabled. Since ROB-1461, one HTTP route reaches the gateway. It is served only by a separate loopback process that an operator starts by hand. `app.main`, MCP tools and schedulers do not reach it. No KR mock order can be sent until a coordination route is installed (see [Open items](#open-items-for-e2-and-e3)).

## Roles

| | Python | Go executor (E2) |
|---|---|---|
| Decides | Strategy, symbol, side, caps, price bound, expiry, authorization | Timing and price inside the bound |
| Writes | `review.execution_intents`, `review.execution_child_requests` | Nothing durable in Postgres |
| Sends to brokers | Yes, through existing services only | Never |
| Records fills | Existing ledgers, from broker evidence only | Never |

## Transport

The transport is HTTP on loopback (ROB-1461), and fillwire `docs/e3a-mock-runbook.md` defines the binding. `app/routers/execution_intent_gateway.py` implements it, and `tests/fixtures/fillwire_e1/` holds fillwire's recorded requests and answers.

```http
POST /execution-intents/children
Content-Type: application/json
X-Execution-Intent-Gateway-Token: <token>
X-Execution-Intent-Confirm: <idempotency_key of this body>

<one child request>
```

| Answer | Meaning |
|---|---|
| 200 with the [outcome](#outcome) | E1 decided. This includes every refusal, such as `confirm_required` and `gateway_disabled` |
| 404 | The route setting is off |
| 403 or 401 | The token is unset on the server, or the supplied token is missing or wrong |
| 415, 413 or 422 | The body is not JSON, is too large, or is not a valid child request. Nothing was decided |
| 503 | `handle` raised. If the row was already journaled, a retry with the same key replays it and never sends again |

fillwire treats every answer except 200 as an unknown outcome.

- **Confirm:** the route passes `confirm=True` only when the confirm header equals the body's `idempotency_key`.
- **Mount:** `app/execution_intent_gateway_server.py` serves the route, and `scripts/run_execution_intent_gateway.py` refuses any host that is not loopback. `app.main` does not include it, because `app.main` sits behind the public proxy.
- **No way to create an intent:** intents enter only through `SqlAlchemyGatewayStore.store_intent`, which Python calls.

If `handle` raises, nothing was forwarded for that call, unless the row was already journaled. The caller retries with the same key, and the retry replays the row. It never sends again.

## Query

Both reads need the token, take no lock, and run in a `READ ONLY` transaction. Neither calls a broker.

`GET /execution-intents/children/<idempotency_key>` returns one journal row: `intent_id`, `child_id`, `idempotency_key`, `action`, `target_idempotency_key`, `quantity`, `limit_price_krw`, `state`, `refusal_reason`, `broker_order_id` and `result`.

`GET /execution-intents/intents/<intent_id>` returns:

```json
{"intent_id": "<uuid>", "reconciled": true,
 "view": {"filled_qty": 4, "order": {"key": "<key>", "price": 70100, "qty": 10}},
 "filled_qty": 4,
 "unresolved": [],
 "children": [{"child_id": 1, "idempotency_key": "<key>", "state": "accepted"}]}
```

`children` holds every journal row, with the fields of the single-row answer except `intent_id` and `result`.

`view` has exactly the fields of fillwire's `exec.View`:

- `order` is absent when no order is working. `qty` is the quantity last sent, and fills do not reduce it. The open quantity is `qty` minus the fills on that order.
- `filled_qty` only holds back further sends. It is not fill evidence, and it can overstate because KIS order numbers repeat across days.

**`view` is `null` when `reconciled` is false, and E2 must keep holding.** `unresolved` then names each child and why:

| `reason` | Condition |
|---|---|
| `no_ledger_evidence` | A child is `sending` or `uncertain`, and no ledger row resolves it |
| `ledger_state_unresolved` | The ledger row is in a state that proves nothing, such as `anomaly`, `stale`, or a soft cancel |
| `ledger_row_ambiguous` | More than one ledger row carries this order's correlation |
| `multiple_working_orders` | More than one order is working, and E2 tracks one |

What resolves an unknown outcome:

| Evidence | Result |
|---|---|
| An `accepted` ledger row with an order number, for an unknown submit | The order is working under the submit's key |
| A `rejected` ledger row | No order |
| A ledger row in `expired`, a cancel the broker confirmed, or a quantity filled in full | No order |
| An accepted cancel in the journal | No order |

A missing ledger row proves nothing, because the row is written after the send. An unknown amend or cancel on an open order stays unresolved. The journal rows themselves never change: a query reports them and does not move them.

## Intent (`ExecutionIntentV1`)

The schema is in `app/schemas/execution_intent.py`. Intents are immutable once stored, and a DB trigger refuses any update or delete. Table CHECKs repeat the caps below, and the same trigger refuses a new intent whose expiry is in the past or more than 24 hours away. Storing the same contents twice is a no-op, and storing different contents under the same id raises `IntentConflict`. An intent must expire within 24 hours of being stored.

| Field | Rule |
|---|---|
| `schema_version` | Exactly `1` |
| `intent_id` | UUID |
| `strategy` | At most 128 characters, before and after normalization. Placeholders are refused, using the same check as the kis_mock attribution gate |
| `account` | Exactly `kis_mock`. A DB CHECK enforces the same list |
| `venue` | Exactly `krx` |
| `symbol` | KRX code, six characters |
| `side` | `buy` or `sell` |
| `max_quantity` | Integer, 1 to 1,000,000 shares. Bounds the sum of all children |
| `max_notional_krw` | Integer, 1 to 5,000,000 KRW, the locked single-order cap. Bounds the sum of all children |
| `limit_price_krw` | Integer, 1 to 10,000,000 KRW |
| `max_slippage_bps` | Integer, 0 to 500 |
| `expires_at` | Timezone-aware. The end of the authorization |
| `inventory` | `position_at_authorization`, `min_position_after`, `max_position_after`. All are zero or more, so an intent cannot authorize a short |
| `policy_version` | Required reference, at most 128 characters |
| `model_version` | Optional reference, at most 128 characters |
| `authorization_ref` | Required reference, at most 128 characters |

## Child request

Unknown fields are refused. Quantity and price are integers, so nothing can be truncated below the gateway.

```json
{"action": "submit", "intent_id": "<uuid>", "idempotency_key": "<key>",
 "quantity": 3, "limit_price_krw": 70100}

{"action": "amend", "intent_id": "<uuid>", "idempotency_key": "<key>",
 "target_idempotency_key": "<key of the working order>",
 "quantity": 2, "limit_price_krw": 70200}

{"action": "cancel", "intent_id": "<uuid>", "idempotency_key": "<key>",
 "target_idempotency_key": "<key of the working order>"}
```

- **`idempotency_key`:** 8 to 128 characters from `[A-Za-z0-9._:-]`. One key means one decision, and keys are unique across all intents.
- **Target:** Go names a target by its idempotency key, never by a broker order id. The gateway reads the order id from its own journal, so Go can only act on orders sent under the same intent.
- **Amend chain:** a successful KIS amend issues a new order id. Later amends and cancels must target the amend's key, not the original submit's key.

## Outcome

| `status` | Meaning | What Go does |
|---|---|---|
| `accepted` | The broker service reported success | Track the order. This is not a fill |
| `uncertain` | The service failed, raised, or returned no order id, or a cancel was not confirmed by the broker. The order may exist | Stop acting on this order and wait for reconciliation |
| `sending` | The row is journaled and no outcome is recorded | Same as `uncertain` |
| `refused` | The gateway sent nothing. `reason` says why | Make a new decision with a new key |
| `conflict` | The key was used with a different payload | Treat as a bug in the executor. Nothing was sent |

Other fields are `child_id`, `broker_order_id`, `result`, `replayed` and `outcome_not_recorded`.

`result` is the journaled broker service output, limited to 16 KB. Retry hints in it, such as `retry_allowed`, carry no authority. Only a new decision with a new key can send again.

## Processing order

1. **Env gate:** `EXECUTION_INTENT_GATEWAY_ENABLED` must be exactly `true`. Otherwise the call is refused before any DB access, for every action.
2. **Confirm:** `confirm` must be exactly `True`. Otherwise the call is refused before any DB access.
3. **Kill state (submit and amend only):** the gateway reads the existing `review.kis_mock_runner_control`. Only `ACTIVE` allows them, and a failed read counts as `GLOBAL_FREEZE`.
4. **Reserve:** under the intent's row lock, a known key replays its first answer. A new key is decided, and the row commits as `sending` or `refused`. This commit is the write-ahead record, and it happens before any broker call.
5. **Forward:** the matching forwarder runs once. `KisMockForwarder` calls `_place_order_impl`, `modify_order_impl` or `cancel_order_impl` with `is_mock=True`. For an amend or cancel it first mints the follow-up capability (see [Follow-up capability](#follow-up-capability)).
6. **Record:** the row moves from `sending` to `accepted` or `uncertain`, once only. A DB trigger refuses any other state change, any change to the request fields, a new row that already carries an outcome, and any delete. The app role can update only the outcome columns.

A refusal from step 4 is stored, so the same key gets the same answer later. A delayed retry cannot send a stale decision after a kill is lifted. Refusals from steps 1 and 2 leave no row, because nothing was decided.

## Refusal reasons

| Reason | Applies to | Condition |
|---|---|---|
| `gateway_disabled` | All | The env gate is not exactly `true` |
| `confirm_required` | All | `confirm` is not exactly `True` |
| `intent_not_found` | All | No stored intent has this id |
| `account_not_mock` | All | The stored account is not in `MOCK_ACCOUNTS` |
| `forwarder_unavailable` | All | No forwarder is configured for the account |
| `authorization_expired` | Submit, amend | Now is at or after `expires_at` |
| `submissions_killed` | Submit, amend | The kill state is not `ACTIVE` |
| `price_outside_bound` | Submit, amend | A buy above `limit × (1 + bps)` or a sell below `limit × (1 − bps)` |
| `locked_envelope_cap_exceeded` | Submit, amend | One child's notional is above `LOCKED_ENVELOPE.max_single_notional_krw` |
| `quantity_cap_exceeded` | Submit, amend | The total would exceed `max_quantity` |
| `notional_cap_exceeded` | Submit, amend | The total would exceed `max_notional_krw` |
| `inventory_constraint_exceeded` | Submit, amend | The projected position leaves the inventory range |
| `target_not_found` | Amend, cancel | The target is not a submit or amend in this intent's journal |
| `target_not_working` | Amend, cancel | The target has no broker order id. An amend also needs the target to be `accepted` |
| `target_superseded` | Amend, cancel | A later child that was not refused targets the order, including one still `sending` or `uncertain`. It may have replaced or cancelled the order, so the target's broker order id can no longer be trusted |

## How caps count

An intent's caps bound everything it ever sent, not what is open now.

- Each order counts at its high-water mark across its amends, because an amend with an unknown outcome may or may not have applied.
- Every row that was not refused counts, including cancelled and failed orders.
- A cancel frees nothing, because the gateway has no fill evidence and cannot prove the quantity is free.

A strategy that needs more capacity gets a new intent. If E2 needs cancel-and-replace inside one intent, releasing capacity must come from ledger evidence. That is a design decision for E2.

## Cancel rules

A cancel does not check expiry, kill state or caps. It needs the env gate, confirm, a mock account, and a target order id in the journal that no later child may have moved.

If a later amend or cancel ends `sending` or `uncertain`, the gateway refuses further cancels of that order with `target_superseded`, because it cannot tell which broker order id is live. Those orders wait for reconciliation, and E1 only reports the recorded state (see [Query](#query)).

This includes `GLOBAL_FREEZE`. The runner's permission table says `GLOBAL_FREEZE` blocks every automatic mutation, while this contract keeps cancels allowed whenever submissions are blocked. The gateway follows this contract for its own path and does not change the runner's table. The guards below the gateway still apply to the cancel.

## Follow-up capability

KIS mock amend and cancel need an operation-scoped capability. `KisMockForwarder` mints one for each call, with the gateway journal row as the durable claim. The scope and the bindings are in `docs/contracts/rob-1263-kis-coordination-adapter.md` §7, "Amendment: kis_mock gateway-origin orders".

If a binding fails, the forwarder makes zero broker calls and the row ends `uncertain` with a `followup_*` `reason_code` in `result`. An order from any other origin cannot mint.

A KIS amend issues a new order number, so a successful amend re-points the `kis_mock_order_ledger` row at it. Later follow-ups then find the order under the amend's key.

## Rollback

The migration's downgrade refuses while either table holds rows. Dropping the journal forgets which keys were used, so after a reapply a used key would look new and could send again. To roll back, export the rows and clear the tables on purpose first, and do not re-enable the gateway until the journal is restored or every old key is retired.

## Open items for E2 and E3

- **Coordination route:** `run_kis_mock_send` refuses every KR mock send until a coordinated route is installed. Until then a submit ends as `uncertain`, and no order is sent. Installing a route is a separate, approval-gated decision (`docs/contracts/rob-1263-kis-coordination-adapter.md` §6 and §10).
- **Other envelope caps:** gross exposure, position count, per-session entry and exit counts, and the NLV-based order cap all need an account snapshot. The gateway enforces only the locked single-order notional. The E3 pilot must decide how the others apply.
- **Inventory drift:** the inventory check uses the position stated at authorization, and the gateway reads no broker position. A position that moves outside the intent is not detected here.
- **Resolving `uncertain` and `sending`:** nothing in E1 moves these rows, and there is no automatic replay. The [query](#query) lets E2 reconcile when the ledger has evidence, and it answers `view: null` when it does not. An order with an unknown later child still cannot be cancelled through the gateway (`target_superseded`), so it must be cleared outside it.
- **Expiry and kill at the wire:** both are judged at reservation, not at the broker call. Expiry is read under the intent row lock. The kill state is read just before that lock, so a lock wait can make it stale. A send can still cross `expires_at` or a newly engaged kill in the time between the journal commit and the wire. Closing that needs a journal state for "reserved, proven not sent" and a pre-send check on every forward path. `_place_order_impl` and `cancel_order_impl` take a `pre_send_hook`, but `modify_order_impl` does not. Decide this in E2, before the E3 pilot.
- **What the DB cannot check:** a role with INSERT on intents can still write a row inside every bound, because the database cannot verify `authorization_ref`. Placeholder strategies are not checked in the DB either; the kis_mock attribution gate refuses them before a send. Intents must enter only through `store_intent`.
- **Other accounts:** adding `kiwoom_mock` or `nh_mock` means adding a forwarder and extending the schema `Literal`, `MOCK_ACCOUNTS`, and the DB CHECK in a new migration.
