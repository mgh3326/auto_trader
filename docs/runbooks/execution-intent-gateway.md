# Execution intent gateway route: enabling it and the operator-present mock run (ROB-1461)

This runbook covers the HTTP route between the fillwire E2 executor and the E1 intent gateway, for the KIS mock account only. It has the steps to enable the route, run one mock order with an operator present, stop, and roll back.

The contract is in `docs/design/1440-intent-gateway-contract.md`. fillwire's half of the run is in fillwire `docs/e3a-mock-runbook.md`.

The route is off by default. Merging the code enables nothing, and no scheduler or service unit starts it.

## Before you schedule a run

Every item must hold. Today, the first two do not.

| # | Precondition | Owner | State |
|---|---|---|---|
| 1 | A KR mock coordination route is installed. Without it, every submit ends `uncertain` and no order is sent | Operator. A separate, approval-gated decision: `docs/contracts/rob-1263-kis-coordination-adapter.md` §6 and §10 | **Not done** |
| 2 | fillwire has a live book feed and ledger fills, its `execmock` send-mode refusal is removed, and its `Query` calls the [query endpoint](#what-the-route-serves) | fillwire PR | **Not done** |
| 3 | The ROB-1440 migration `20261101_rob1440_intent_gw` is applied | Operator | Check with `uv run alembic current` |
| 4 | One intent is stored through `SqlAlchemyGatewayStore.store_intent`: `account = "kis_mock"`, 1 share, `max_notional_krw` below 100,000, expiry within 30 minutes | Operator | Per run |

Until item 1 holds, the route can run, but it cannot place an order. That is the expected result, and you can use it to check the wiring.

## What the route serves

| Request | Purpose |
|---|---|
| `POST /execution-intents/children` | One child request: submit, amend or cancel |
| `GET /execution-intents/children/<idempotency_key>` | One recorded child |
| `GET /execution-intents/intents/<intent_id>` | The intent's children and the reconcile view |

## Settings

| Name | Default | Effect |
|---|---|---|
| `EXECUTION_INTENT_GATEWAY_ROUTE_ENABLED` | `false` | Off: every path answers 404 before any DB access |
| `EXECUTION_INTENT_GATEWAY_TOKEN` | empty | Empty: every request answers 403. Wrong or missing token: 401 |
| `EXECUTION_INTENT_GATEWAY_TOKEN_HEADER` | `X-Execution-Intent-Gateway-Token` | Must match fillwire's `token_header` |
| `EXECUTION_INTENT_GATEWAY_ENABLED` | unset | The E1 gate. Anything except exactly `true` means E1 answers `gateway_disabled`. It is read from the process environment, not from an env file |

Put these on the one container or process that serves the route. Do not add them to a shared env file such as `.env.api`.

## Enable the route

1. Generate a token and put it in the operator secret store. Do not commit it, and do not print it in a shared terminal.

   ```sh
   openssl rand -hex 32
   ```

2. Check that the kill switch reads `ACTIVE`:

   ```sql
   SELECT mode, reason, updated_by, updated_at FROM review.kis_mock_runner_control WHERE id = 1;
   ```

3. Start the route in the foreground on the host where fillwire runs. Set `image` to the digest of the deployed version and `env_file` to the shared env file, as in `docs/runbooks/binance-h5-ncp-manual-playbook.md`. Export the token in your shell first, so that its value is not on the command line.

   ```sh
   docker run --rm --network host --env-file "$env_file" \
     -e EXECUTION_INTENT_GATEWAY_ROUTE_ENABLED=true \
     -e EXECUTION_INTENT_GATEWAY_ENABLED=true \
     -e EXECUTION_INTENT_GATEWAY_TOKEN \
     "$image" \
     /app/.venv/bin/python -m scripts.run_execution_intent_gateway --port 8782
   ```

   Do not add `-d` or `--restart`. The process stays up only while an operator is watching it. The server exits 2 for any host that is not loopback.

4. Check the gates from the same host. The first answer must be 401. The second must be 200 with `"status":"refused"` and `"reason":"confirm_required"`. Neither one creates a journal row.

   ```sh
   URL=http://127.0.0.1:8782/execution-intents/children
   BODY='{"action":"submit","intent_id":"00000000-0000-4000-8000-000000000000","idempotency_key":"gate-check-0001","quantity":1,"limit_price_krw":1}'
   curl -s -w ' [%{http_code}]\n' -X POST "$URL" -H 'Content-Type: application/json' -d "$BODY"
   curl -s -w ' [%{http_code}]\n' -X POST "$URL" -H 'Content-Type: application/json' \
     -H "X-Execution-Intent-Gateway-Token: $EXECUTION_INTENT_GATEWAY_TOKEN" -d "$BODY"
   ```

## Run one mock order

Do this only when every precondition holds. The operator stays present for the whole run.

1. Store the intent (precondition 4), and note its `intent_id`.
2. Read the intent. Expect `"reconciled":true`, `"view":{"filled_qty":0}` and no children.

   ```sh
   curl -s -H "X-Execution-Intent-Gateway-Token: $EXECUTION_INTENT_GATEWAY_TOKEN" \
     http://127.0.0.1:8782/execution-intents/intents/<intent_id>
   ```

3. Follow fillwire `docs/e3a-mock-runbook.md`, "Run steps": dry run first, then send with `url = "http://127.0.0.1:8782/execution-intents/children"`.
4. Watch these until the intent finishes or a stop condition fires:
   - **Executor journal:** every `send` is followed by an `outcome`, and no key repeats.
   - **E1 rows:** `SELECT idempotency_key, action, state, refusal_reason, broker_order_id FROM review.execution_child_requests WHERE intent_id = '<intent_id>' ORDER BY id;`. Each state must match the executor journal.
   - **Ledger:** every fill the executor counts has a ledger row whose order id is in the journal.
   - **KIS mock HTS:** at most one working order for the intent, and its price is within the intent's bound.

## Stop conditions

Stop on any of these. Set the kill switch, then stop the process.

| Condition | Why |
|---|---|
| An outcome is `conflict` | The executor reused a key with a different body. That is a bug |
| An outcome is `uncertain` or `sending`, and the query keeps answering `view: null` | The order may exist, and there is no evidence to say. Clear it at the broker |
| A `result` carries a `followup_*` `reason_code` | The capability was refused, so the binding between journal and ledger is broken. Zero broker calls were made for that child |
| A `refused` outcome other than `submissions_killed` or `authorization_expired` | E2 sent something E1 forbids, so the mirror or the binding is wrong |
| A second working order for the intent appears at the broker | E2 tracks one order |
| A fill has no ledger row, or a ledger row names an unknown order id | Fill attribution is broken |
| Any answer other than 200 repeats | The route or the DB is unhealthy, and E2 reads each one as an unknown outcome |

To set the kill switch, use the existing control service (`PostgresKillSwitchStore.set_mode`). Do not edit the row by hand. `GLOBAL_FREEZE` refuses submit and amend. A cancel is still allowed.

## After a stop

1. Cancel the working order through E1 if the journal allows it. A cancel needs no `ACTIVE` kill state. It is refused with `target_superseded` if a later child has an unknown outcome.
2. If E1 refuses the cancel, clear the order at the broker (KIS mock HTS), and record that you did.
3. Do not edit journal rows. DB triggers refuse it, and an unknown row stays on the record.
4. Attach the executor journal, the E1 rows and the query answer to the ticket.

## Roll back

1. Stop the process. The route is gone, because nothing else serves it. Stored intents expire on their own.
2. If you only want to disable it, restart without `EXECUTION_INTENT_GATEWAY_ROUTE_ENABLED=true`, and every path answers 404.
3. Remove the token from the secret store.
4. Keep the journal. The downgrade of the ROB-1440 migration refuses while rows exist, because a forgotten key could send again. This PR adds no migration, so there is nothing else to undo in the database.

A reverted build keeps one data effect. An amend that already ran re-pointed its `kis_mock_order_ledger.order_no` at the replacement number, and the previous number stays in `last_reconcile_detail.modified_from_order_no`. Leave those rows as they are.

## Limits

- **`view` is not broker truth.** It comes from the journal and the ledger, and the route calls no broker. An order placed outside the gateway does not appear.
- **`filled_qty` can overstate,** because KIS order numbers repeat across days. It only holds back further sends, and it is never fill evidence.
- **A cancel after a partial fill sends the ledger's order quantity.** The capability checks the remainder, but the existing cancel path sends the full quantity to KIS. The first mock run should check how KIS answers this.
- **A refused or contended follow-up strands the order in the journal.** If the mint is refused, or another writer holds the KIS mock lease, the child ends `uncertain` with zero broker calls. E1 cannot tell that apart from a send that may have happened, so later cancels of that order answer `target_superseded`. Clear the order at the broker. `result.reason_code` or `result.error_class` on the child says which case it was.
- **The kill state is read once, at reservation.** A send can still cross a newly engaged kill in the time between the journal commit and the wire (design doc, "Expiry and kill at the wire").
