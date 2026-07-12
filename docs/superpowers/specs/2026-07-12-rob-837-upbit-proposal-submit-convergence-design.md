# ROB-837 Upbit Proposal Submit Convergence Design

## Problem and evidence

An approved crypto proposal can create an Upbit order and still finish locally as
`rejected`. The measured incident was proposal `b81ffd0e`, whose BTC limit order
`35bee07f` remained live at Upbit while the proposal rung was terminalized as
rejected.

The submit path is:

`revalidate_and_submit` → `_revalidate_place_rung` → `_default_place_order_fn` →
`_place_order_impl` → `_execute_and_record` → `_execute_crypto_order` → Upbit
`orders.py` → `client._request_with_auth`.

The proposal loop calls the live placement function once per eligible rung.
Telegram callback replay is separately guarded by a row-locked nonce and commit
lease. The remaining retry is inside Upbit transport: ROB-645 passes
`retry_request_errors=False` for `POST /v1/orders`, but `_retry_with_backoff`
still retries HTTP 429 responses. If Upbit has accepted the first request before
returning 429, the retry reuses the ROB-653 deterministic identifier and Upbit
returns 400 because identifiers cannot be reused. `_place_order_impl` converts
that final error to `success=False`, and `_classify_submit` currently records
`rejected` without querying the broker.

This is the same identifier on both sends, not a newly generated identifier.
The identifier prevents a second live order but does not make the response
idempotent: Upbit rejects reuse instead of returning the original order.

## Scope boundaries

ROB-837 changes only proposal-driven Upbit placement and the shared Upbit POST
retry policy needed to make that path single-send. It does not implement ROB-825
terminal-attempt generations or ROB-835 replace cancellation polling. It does not
change KIS submission semantics, add database migrations, or place any live order
during tests or repair.

## Design

### Single-send Upbit creation

`POST /v1/orders` will be passed to the retry helper with both request-error and
response retry disabled. The helper retains 429 and RequestError retries for
reads and other existing paths. The order-create test will model a first 429 and
prove the send coroutine is invoked exactly once.

### Proposal-scoped identifier

Every proposal rung receives a stable identifier derived from the full proposal
UUID and rung index. Preview and live submit use the same value. The proposal
binding passes it into `_place_order_impl` as an explicit client-order-id
override; `_place_order_impl` continues deriving the existing ROB-653 canonical
key when no override is supplied. Only Upbit sends the value to a broker.

This separates proposal identity from ROB-825's content-level attempt semantics:
two different proposals do not accidentally share an Upbit identifier, while a
re-entry of the same proposal/rung cannot create a second order.

### Evidence-based classification gate

The revalidation service receives an injectable submit-evidence lookup. The
production lookup applies to `account_mode=upbit`, queries `GET /v1/order` by the
proposal identifier, and returns one of:

- found: normalized status plus broker UUID;
- absent: a definitive Upbit not-found response;
- unknown: lookup timeout, permission failure, malformed response, or other
  inability to prove presence or absence.

When submit returns `success=False`, classification consults that evidence before
terminalizing the rung:

- found `wait`/`watch`: record `resting` and bind `broker_order_id`;
- found accepted market-order evidence: record `acked` where appropriate;
- absent after a definitive broker rejection: record `rejected`;
- unknown: record `unverified` with correlation and identifier evidence.

A normal explicit rejection such as insufficient balance remains rejected when
the identifier lookup definitively reports no order. `rejected` therefore means
the broker has confirmed that no order exists; lookup ambiguity never becomes a
terminal rejection.

### One-time incident repair

A dedicated CLI is dry-run by default and contains no order placement, replace,
cancel, or resubmit call. It requires the operator to supply the full proposal
UUID (accepted only when its first eight hexadecimal characters are `b81ffd0e`)
and defaults the broker order UUID to `35bee07f` only if the full value is passed
through the deployment secret/runbook channel.

The CLI reads the Upbit order by UUID, verifies symbol `KRW-BTC`, state `wait`,
and a non-empty identifier, then locks the proposal and its rung. It aborts unless
there is exactly one matching proposal, the rung is `rejected`, the broker order
ID is not already bound elsewhere, and the stored order fields match the broker
evidence. Dry-run prints the guarded diff. `--commit` directly repairs the
historically invalid terminal state to `resting`, binds the broker UUID and
identifier, clears the rejection reason, recomputes the group lifecycle to
`submitted`, and commits once. The script never invokes a submit function.

Because `rejected` is intentionally terminal in the normal state machine, this
incident-only correction uses a narrowly guarded repository/SQL update rather
than widening legal runtime transitions.

## Tests and verification

All broker and HTTP behavior is mocked.

- Upbit transport: a create-order 429 is returned once and the send count remains
  one; read retries remain unchanged.
- Proposal binding: preview and submit receive the same proposal-scoped ID, and
  the Upbit order body uses it.
- Convergence: submit failure followed by identifier lookup returning order
  `35bee07f` in `wait` records `resting` and binds the broker ID.
- True rejection: insufficient balance plus definitive identifier not-found
  remains `rejected`.
- Ambiguity: lookup failure records `unverified`, never `rejected`.
- Repair CLI: dry-run performs no DB write, all guard mismatches abort, commit
  updates only the intended rung/group, and no order mutation symbol is imported
  or called.

Verification gates are targeted pytest suites, `make lint`, and the repository's
type check. No live credentials or real broker request is used.

## Deployment procedure

Deploy the code first. The operator then runs the repair CLI in dry-run mode,
compares its broker evidence with Upbit order `35bee07f`, and reruns with
`--commit` only when every guard passes. The live order remains untouched and is
never resubmitted. The final output must show broker `wait`, proposal rung
`resting`, and the bound broker order UUID.
