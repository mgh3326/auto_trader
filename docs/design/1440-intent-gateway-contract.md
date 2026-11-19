# Intent gateway: the Go and Python contract (ROB-1440, plan unit E1)

This document is the contract the E2 Go executor implements. Python owns what may trade. Go owns when, and at what price, inside that permission. Go never reaches a broker or the database directly in this design: it sends child requests to a Python gateway, which calls the existing broker services.

**Accounts:** `kis_mock` (schema v1, everything below unless it says otherwise) and, since hk 5441, `upbit_live` (schema v2, see [upbit_live](#upbit_live-schema-v2-hk-5441)). A v1 intent, its children, its answers and the recorded fixtures are unchanged.

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

All reads need the token, take no lock, and run in a `READ ONLY` transaction. None calls a broker.

`GET /execution-intents/children/<idempotency_key>` returns one journal row: `intent_id`, `child_id`, `idempotency_key`, `action`, `target_idempotency_key`, `quantity`, `limit_price_krw`, `state`, `refusal_reason`, `broker_order_id` and `result`.

`GET /execution-intents/intents/<intent_id>` returns:

```json
{"intent_id": "<uuid>", "reconciled": true,
 "view": {"filled_qty": 4, "order": {"key": "<key>", "price": 70100, "qty": 10}},
 "unresolved": [],
 "children": [{"child_id": 1, "idempotency_key": "<key>", "state": "accepted"}]}
```

`children` holds every journal row, with the fields of the single-row answer except `intent_id` and `result`.

`view` has exactly the fields of fillwire's `exec.View`:

- `order` is absent when no order is working. `qty` is the quantity last sent, and fills do not reduce it. `view` does not carry the fills on that order alone.
- `filled_qty` is the sum of `execution_ledger` fills on the lineages' orders. It is cumulative, as fillwire's `State.Remaining` expects. It holds back further sends, and it is only as complete as that feed: a fill that has not reached `execution_ledger` is not in it. See [How the query joins its evidence](#how-the-query-joins-its-evidence).

**`view` is `null` when `reconciled` is false, and E2 must keep holding.** No fill total is reported then, because a total without every lineage is partial. `unresolved` names each child and why:

| `reason` | Condition |
|---|---|
| `no_ledger_evidence` | A child is `sending` or `uncertain`, and no ledger row resolves it |
| `ledger_state_unresolved` | The ledger row is in a state that proves nothing, such as `anomaly`, `stale`, or a soft cancel |
| `ledger_row_ambiguous` | More than one ledger row carries this order's correlation |
| `multiple_working_orders` | More than one order is working, and E2 tracks one |
| `order_identity_ambiguous` | The journal and the ledger do not name one order for each sender, so fills cannot be assigned |
| `fill_evidence_conflict` | An order has more fills than it held, or the ledger row's estimate is unreadable or above what the execution ledger shows |

What resolves an unknown outcome:

| Evidence | Result |
|---|---|
| An `accepted` ledger row with an order number, for an unknown submit | The order is working under the submit's key |
| A `rejected` ledger row, unless the journal or the row holds a broker number | No order |
| A ledger row in `expired`, or a cancel the broker confirmed | No order |
| `execution_ledger` fills on the current order that reach the quantity it was sent for | No order |
| An accepted cancel in the journal | No order |

A missing ledger row proves nothing, because the row is written after the send. An unknown amend or cancel on an open order stays unresolved. An unknown amend closes only on evidence about the order the ledger names for it: fills, expiry or a cancel on the order it may have replaced say nothing about a replacement. The journal rows themselves never change: a query reports them and does not move them.

### Committed fills (ROB-1461)

`GET /execution-intents/intents/<intent_id>/fills` uses the same route gate
(404 when disabled) and machine token (unset: 403; missing/wrong: 401).
Unknown intents return 404; read failures return a fixed 503, never an empty
success. It performs no writes, locks, broker calls or stream reads.

```json
{"intent_id":"<uuid>","orders":[{"broker_order_id":"000123","cumulative_qty":2,
 "fills":[{"fill_id":"42","side":"buy","qty":2,"price":"70000.00000000",
 "filled_at":"2026-11-02T01:00:00+00:00",
 "committed_at":"2026-11-02T01:00:01+00:00"}]}]}
```

Only submit/amend broker order IDs already recorded in
`review.execution_child_requests` for this intent establish orders. Cancel rows
repeat a target and do not establish another trading day. Missing journal
numbers are never inferred from the mock order ledger, symbol or time proximity.
Numbers compare without leading zeros and fills must match the sender's KST
trading day, broker `kis`, account mode `mock`, venue, KR instrument, symbol,
side and KRW currency. The response preserves the journal's order-number spelling.
Each `orders` entry represents one normalized number and KST day, ordered by
its first journaled sender. A reused number on another day produces a separate
entry, even when its spelling is identical or it has no fills. Consumers must
preserve these entries rather than merge them by `broker_order_id` alone; a
returned fill's `filled_at` identifies its KST day. The fixed response fields
remain unchanged.

The reader uses `execution_ledger_in_effect()` and ROB-1379
`supersede_provisional` to exclude quarantine and websocket rows covered by an
authoritative row of the same order identity. `cumulative_qty` is the sum of the
returned `qty` values, whole shares. `fill_id` is the stable ledger row ID as a
decimal string; `price` is an exact decimal string. Rows are ordered by
`filled_at`, then row ID. Known orders with no effective fills have zero quantity
and an empty fill list; intents with no journaled order IDs have `orders: []`.

`committed_at` exposes the ledger's stored `created_at` ingestion timestamp,
visible only after the writer commits. PostgreSQL stores transaction-start time
there; it is **not an exact COMMIT clock**. A later upsert can update a row under
the same `fill_id`, so consumers must refresh the full snapshot rather than add
repeated snapshots. Supersession may replace previously returned websocket IDs.
The reader selects only identity/projection columns, excludes raw payloads and
account numbers, and never exposes broker responses. This endpoint does not
change the existing reconcile-view calculation or resolve unknown sends.

### How the query joins its evidence

`lineage_orders` is the only statement of which orders a lineage placed. The evidence reader loads fills for those orders, and closure reads fills from the same list.

An order is its number and its KST trading day, the ROB-1379 identity. KIS reuses numbers across days, and writers differ on leading zeros, so numbers compare with leading zeros removed. A submit or an amend places an order on the day it was sent. A cancel journals its target's number and places nothing.

E1 accepts a sender only with a number and amends only an accepted sender, so only the last sender in a lineage can lack a number. The ledger row names that order if it followed the send, and the order's quantity is still what the journal sent. A ledger number from an earlier order on another day cannot be told apart from a reused number, so it names nothing.

`kis_mock_order_ledger.last_reconcile_detail.attributed_fill_qty` is not evidence about one order. The holdings reconciler shares out the whole symbol's holdings change, so another order's fill can appear on a resting order. That estimate can never close an order or enter `filled_qty`. It can only conflict with the execution ledger.

Each row has a test named `test_j<n>_*` in `tests/services/execution_intent_gateway/test_intent_query.py`:

| Row | Invariant | Fails toward |
|---|---|---|
| J1 | A fill counts only on its order's number and KST day. A reused number's old fills never count | Working order stays working |
| J2 | Numbers join with leading zeros removed, by the same rule as ROB-1379's `normalize_order_id` | An understated fill would be possible otherwise |
| J3 | An order closes only on its own `execution_ledger` fills, compared with the quantity the journal sent. Fills on the order it replaced do not count | Working order stays working |
| J4 | The ledger row's holdings estimate never closes an order or enters `filled_qty`. If it exceeds what the execution ledger shows, the answer is `fill_evidence_conflict` | `view: null` |
| J5 | Closure reads only orders the reader loads. Yesterday's number under today's unknown amend is `order_identity_ambiguous`. Evidence about the old order never closes an unknown amend the ledger has not followed | `view: null` |
| J6 | More than one ledger row is `ledger_row_ambiguous`, and no partial total exists anywhere in the answer | `view: null` |
| J7 | A missing or unreadable estimate on a `fill` row is unknown, not zero and not a full fill. A blank ledger number names nothing. An unreadable share quantity or a journal row the DB forbids answers 503 | `view: null` or 503 |
| J8 | More fills than an order held are not believed | `view: null` |
| J9 | One order has one owner: an amend that keeps its number, or two lineages holding one number on one day, is `order_identity_ambiguous` | `view: null` |
| J10 | A ledger number the journal cannot explain is `order_identity_ambiguous`. This also covers a journal read before a later amend, because the two reads are separate transactions | `view: null` |
| J11 | The answer does not depend on the order of the journal rows | |
| J12 | A `rejected` ledger row does not override a broker number | `view: null` |

## Intent (`ExecutionIntentV1`)

The schema is in `app/schemas/execution_intent.py`. Intents are immutable once stored, and a DB trigger refuses any update or delete. Table CHECKs repeat the caps below, and the same trigger refuses a new intent whose expiry is in the past or more than 24 hours away. Storing the same contents twice is a no-op, and storing different contents under the same id raises `IntentConflict`. An intent must expire within 24 hours of being stored.

| Field | Rule |
|---|---|
| `schema_version` | Exactly `1` |
| `intent_id` | UUID |
| `strategy` | At most 128 characters, before and after normalization. Placeholders are refused, using the same check as the kis_mock attribution gate |
| `account` | Exactly `kis_mock` for a v1 intent. A DB CHECK binds each schema version to its account and venue |
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
- **Other accounts:** adding `kiwoom_mock` or `nh_mock` means adding a forwarder and extending the schema `Literal`, `MOCK_ACCOUNTS`, and the DB CHECK in a new migration. `upbit_live` was added differently, as its own path with its own schema version (below), because a real account needs gates a mock forwarder does not have.

## upbit_live (schema v2, hk 5441)

Design: `docs/design/5438-go-execution-path.md` §2-§4 and §8.2. Operator decision: `hk:doc decision/2026-11-20/at-5438-d1-d19`. This is a real account. Everything above still holds for it (one route, env gate, confirm header, journal before forward, one send per key, no replay of an unknown outcome). This section lists what differs.

### What must be true before anything is sent

| Gate | Off means |
|---|---|
| `EXECUTION_INTENT_GATEWAY_ENABLED` exactly `true`, and the confirm header | Refused before any DB access (unchanged) |
| `EXECUTION_INTENT_GATEWAY_UPBIT_LIVE_ENABLED` exactly `true` (default off) | A child of a live intent is refused `upbit_live_disabled` with no row, a cancel included. A shadow intent does not need it |
| Intent `execution_mode` | `shadow` can only reach the shadow forwarder, which sends nothing |
| Mandate (`review.execution_mandates`) | No row, revoked, expired, or the intent is outside it: `mandate_missing`, `mandate_revoked`, `mandate_expired`, `mandate_scope_mismatch` |
| Sleeve state | A live intent needs the sleeve `live`, a shadow intent needs it `shadow` |
| Account kill row (`review.execution_account_control`) | Only `ACTIVE` allows a submit. A missing row or a failed read is a freeze. A cancel does not read it |

The live forwarder checks the live switch, the kill row, the intent's expiry, the mandate, the sleeve state, an open exit handoff and the account fingerprint again in the transport's pre-send hook, immediately before the request. This closes "Expiry and kill at the wire" above for this account.

### Intent (`ExecutionIntentV2`)

Stored only through `UpbitGatewayStore.store_intent`, under the sleeve lock and the mandate row lock. The same contents again is a no-op.

| Field | Rule |
|---|---|
| `schema_version` | Exactly `2` |
| `account`, `venue` | Exactly `upbit_live`, `upbit` |
| `execution_mode` | `shadow` or `live`. Required, no default, never changes. Equal to the mandate's `mode` |
| `mandate_id`, `sleeve_id` | Required. The sleeve is the mandate's |
| `symbol` | `KRW-<ticker>`, and in the mandate's list |
| `max_quantity_decimal` | Decimal string, at most 8 places, above 0, at most 1,000,000. `max_quantity` is not a v2 field |
| `max_notional_krw` | Integer KRW, at least 5,000. Binds a buy, and must be within the mandate's per-intent cap. A sell has a quantity cap only |
| `limit_price_krw` | Integer KRW on the Upbit KRW price unit (`app/core/upbit_tick.py`) |
| `max_slippage_bps` | At most the mandate's `price_band_bps` |
| `strategy_code_sha` | 40 lowercase hex characters, equal to the mandate's |
| `expires_at` | Within the mandate's `max_intent_ttl` of storing, and before the mandate expires |
| `inventory` | The three positions as decimal strings |
| `authorization_ref` | `mandate:<mandate_id>`, or `operator-exit:<id>` for an operator exit |
| `decision_ref` | Required. The decision this intent belongs to |
| `origin`, `operator_exit_id` | `strategy` by default. `operator_exit` is a sell that names its `review.execution_operator_exits` row |

`store_intent` refuses with `IntentRefused` when a rule below refuses. A DB trigger repeats the mandate checks at INSERT.

### Child request

```json
{"action": "submit", "intent_id": "<uuid>", "idempotency_key": "<key>",
 "quantity_decimal": "0.00320000", "limit_price_krw": 150000000}

{"action": "cancel", "intent_id": "<uuid>", "idempotency_key": "<key>",
 "target_idempotency_key": "<key of the working order>"}
```

- The account of the stored intent decides the field set. A submit that carries `quantity_decimal` is read as the Upbit shape; every other body is validated exactly as before.
- A child whose shape does not match its intent's account is refused `child_shape_invalid_for_account` and leaves no row. So does a key longer than 40 characters: the key is the Upbit `identifier` and cannot be shortened. This holds for a kis_mock-shaped submit (integer `quantity`, or no `quantity_decimal`) that fails kis_mock validation — for example a price above the kis_mock ceiling: the route reads the common fields, looks the intent up, and answers the refusal when the intent is an `upbit_live` one. With the gateway off or the confirm header missing nothing is read and the answer is 422, as it is for any other intent. If the key already names a child the answer is `conflict` (`idempotency_key_reused`), as for any changed body under a used key.
- There is no amend. `POST /children` answers 422 `amend_unsupported_for_account`; a replacement is a cancel and a new submit under a new key.
- `quantity_decimal × limit_price_krw` is computed exactly. Nothing is rounded.

### Outcome

| `status` | Meaning | What Go does |
|---|---|---|
| `shadowed` | A shadow intent's child passed every check. Nothing was sent, and `broker_order_id` is always empty | Treat as a virtual acceptance. Never as `accepted` |
| `refused` with a `child_id` and a state `not_sent` in the journal | The row was reserved, and the request provably never reached the broker | Same as any refusal: a new decision with a new key |

The other statuses are unchanged. A `not_sent` row replays as `refused` with its reason. A shadow child left `sending` (its outcome write failed) is finished as `shadowed` when the same key is sent again: a shadow child never reached a broker, so nothing about it is unknown. Until then `GET /intents/<id>` reports it unresolved (`no_ledger_evidence`, `view: null`): the query says what the journal recorded. A live `sending` row is never forwarded again.

**What proves a non-send.** The order layer calls the pre-send hook immediately before the HTTP request and nowhere else. If the hook did not return normally, no request was made: a guard below refused first (`lower_guard_refused`), the approved price or volume differed from what was about to be posted (`price_normalization_mismatch`), or the hook's own recheck refused (its reason). The hook judges expiry against the clock as it is after its own reads, immediately before the request. Once the hook has returned, any failure or exception is `uncertain`, as for kis_mock. A reason outside the closed list is recorded as `lower_guard_refused`, so a proven non-send can never be left `sending`. A cancel checks only whose account it is going to (`account_mismatch`) and is `not_sent` when that does not match the mandate. A DB trigger accepts `sending → not_sent` only for a v2 intent, with no broker order id and a reason from the closed list in `upbit_decisions.NOT_SENT_REASONS`.

### Refusal reasons added

| Reason | Condition |
|---|---|
| `upbit_live_disabled` | The live switch is off (no row) |
| `child_shape_invalid_for_account`, `amend_unsupported_for_account` | The body does not fit the account (no row) |
| `price_off_tick`, `price_tick_unsupported` | Not a multiple of the price unit, or a range whose unit is below 1 KRW |
| `mandate_missing`, `mandate_revoked`, `mandate_expired`, `mandate_scope_mismatch` | See the gates above |
| `max_open_intents_exceeded` | More unexpired buy intents than the mandate allows (at store) |
| `sleeve_not_live`, `sleeve_not_shadow` | The sleeve state does not match the execution mode |
| `sleeve_budget_exceeded`, `sleeve_loss_limit_reached`, `sleeve_pnl_unmeasured`, `sleeve_used_unmeasured`, `sleeve_unreadable` | The sleeve gate for a buy. An unmeasured value refuses; it is never zero |
| `sleeve_holdings_unmeasured`, `inventory_constraint_exceeded` | A sell above the sleeve's proven position, or a position that cannot be proven |
| `open_sell_intent_exists` | Another unfinished sell intent on the same sleeve and symbol |
| `unresolved_prior_order` | An order on the same sleeve and symbol whose end the broker has not proven |
| `exit_handoff_active` | The symbol is handed off to an approval card |
| `loss_sell_requires_card`, `avg_price_unproven` | The loss-sell guard would refuse the sale, or the account's average buy price cannot be read. A live `loss_sell_requires_card` opens the exit handoff in the same transaction |
| `cooldown_active` | The symbol is in its stop-loss cooldown, or the cooldown could not be read |
| `operator_exit_invalid` | An operator exit intent without a valid, unexpired exit row |
| `account_mismatch` | The configured Upbit credentials are not the ones the mandate was approved for (pre-send, `not_sent`) |

A cancel reads none of these except the live switch: it needs a target submit of the same intent that has a broker order id and that no later child may have moved.

### Query

`GET /execution-intents/intents?account=upbit_live&open=true` lists the unexpired intents of the account, oldest first. It is read only and carries no mandate terms. Any other `account`, or `open` other than `true`, is 422.

`GET /intents/<id>` and `/fills` answer for a v2 intent with the same shape as v1, with quantities as exact decimal strings: `view.filled_qty`, `view.order.qty`, `children[].quantity_decimal`, and in `/fills` `qty` and `cumulative_qty`. `price` in `view.order` stays an integer.

An Upbit order is one uuid for life, so there are no day identities and no lineage. Each row has a test in `tests/services/execution_intent_gateway/test_upbit_query.py`:

| Case | Answer |
|---|---|
| A submit that was refused or is `not_sent` | Left out of the view, kept in `children`. A retry after a lost answer never sees a `not_sent` submit as a working order |
| A `shadowed` submit | Not working. Counted as filled in full |
| An accepted submit with no end | The working order |
| `review.execution_child_order_evidence` shows a final broker state | Not working. `filled_qty` still comes from the ledger only |
| A `sending` or `uncertain` submit, no evidence | `no_ledger_evidence`, `view: null` |
| A `sending` or `uncertain` cancel on an open order | `no_ledger_evidence` until the order's own evidence is final |
| Ledger fills above the order's quantity | `fill_evidence_conflict` |
| Two working orders | `multiple_working_orders` |

`filled_qty` counts only in-effect `execution_ledger` rows of the live Upbit account that carry a uuid this intent's own children are bound to. A child whose outcome was never recorded is bound by the evidence row found under its own identifier.

### Evidence and what stays unresolved

Nothing moves a journal row. `scripts/execution_intent_upbit_resolve.py` (preview by default) reads each unresolved order from Upbit, by uuid or by identifier, and records what the broker returned in `review.execution_child_order_evidence`. "Not found" and a failed read record nothing and prove nothing. An order is resolved only by a final broker state in that table. Until then the gateway refuses a new intent on that sleeve and symbol.
