# ROB-868 Upbit WebSocket Fill Projection Design

## Source of truth

Linear ROB-868 requires the Upbit `myOrder` websocket path to project committed
fill evidence into proposal rungs without waiting for reconcile. The projection
must be best-effort, idempotent, account-scoped to `upbit`, observable through
runtime health counters, and exempt matched proposal fills from the ordinary
small-fill notification threshold.

## Architecture

Keep orchestration in `websocket_monitor.py`, immediately after the execution
ledger commit. A focused helper opens a new `AsyncSessionLocal`, calls
`OrderProposalsService.record_fill_evidence`, commits that independent session,
and returns whether a non-terminal Upbit rung matched. It passes the Upbit UUID
as `broker_order_id`, the websocket `identifier` as `idempotency_key`, and
`account_mode="upbit"` so evidence cannot cross account boundaries. The proposal
repository and service gain this optional evidence key because Upbit submission
stores the client identifier on `OrderProposalRung.idempotency_key`, not on
`correlation_id`.

`trade` means `partially_filled` and projects cumulative `executed_volume`, while
its execution-ledger row and notification use the event's per-trade `volume`.
`done` means `filled` and uses the same cumulative quantity. A `done` payload is
order-level cumulative evidence rather than a new trade delta, so it closes the
rung only after verifying that a durable ledger fill already exists for the
same Upbit UUID, without inserting another execution-ledger row; the preceding
`trade` event owns that row. Other states are ignored; no terminal state is
inferred without broker evidence. Projection exceptions are logged and swallowed
because the execution ledger remains authoritative and a later reconcile can
converge the rung.

The boolean match result is passed to `_send_fill_notification` as a
`proposal_rung_fill` flag. Only matched proposal fills bypass the currency
threshold; unmatched fills retain existing notification behavior. Duplicate
ledger events skip duplicate notifications after a successful projection, while
a redelivery may send the alert if it is the first attempt that successfully
projects the rung after an earlier best-effort projection failure and the
ordinary notification was suppressed by the small-fill threshold. Large fills
are never notified twice during projection recovery.

## Runtime health

`UnifiedWebSocketMonitor._on_upbit_order` is the `myOrder` consumer boundary, so
the monitor increments Upbit message/event counters and timestamps there before
state filtering. Health logging combines those values with the existing KIS
snapshot according to enabled mode and continues to report successful
notification forwarding through `fills_forwarded`.

## Tests

All tests use fake sessions, repositories, services, websocket iterators, and
notifiers. Coverage includes trade/done projection semantics, UUID/identifier
matching, account scoping, independent commit, no-match behavior, exception
swallowing, duplicate idempotency, small matched-rung notification delivery, and
Upbit health counter/timestamp updates. No broker or real websocket is contacted.

## Deployment

`com.robinco.auto-trader.upbit-websocket` is a single-active launchd service,
not part of the API/MCP blue/green pair. Deployment must restart that service;
the repository's native deploy workflow does so in
`restart_single_active_services`, while out-of-band deployment requires an
explicit launchd restart.
