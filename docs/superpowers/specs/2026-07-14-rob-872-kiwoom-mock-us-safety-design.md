# ROB-872 Kiwoom Mock US Safety Hardening Design

## Goal

Make the Kiwoom Mock US manual smoke lifecycle truthful after PR #1528 without
performing broker calls, widening the public account envelope, or claiming any
mutation capability.  Fake transports must prove every failure and recovery
path.

## Boundaries

- Keep broker network, credentials, user `.env` files, migrations, ledgers,
  reservations, sizing, schedulers, and `PaperBrokerPort` out of scope.
- Keep stable account envelopes and TradingCodex/account-read exposure in
  ROB-874.
- Keep `ust20000`-`ust20003`, `trde_tp=00/03`, advanced order types, and
  `BRK.B` pending/unverified until ROB-873 records dated market-hours evidence.
- Preserve leading zeroes in broker order IDs. Accept only 1-18 ASCII digits.

## Architecture

The smoke script owns a deliberately narrow evidence model. A bounded page
walker calls the existing history/positions MCP tools, validates strict broker
success, consumes only the documented `result_list`, `continuation`,
`cont_yn`, and `next_key` shapes, and fails closed on malformed, repeated, or
over-cap continuation. Order matching examines documented order-ID fields only;
it never recursively compares arbitrary scalar values.

The same cleanup proof serves full and probe flows. It captures paginated
positions before submit, requires the target in open or today history after
acceptance, classifies the target from documented quantity/status fields, sends
cancel when trackable, and polls with injected monotonic clock and sleep until a
terminal target is proven. Cleanup succeeds only when the target is terminal
and the final paginated position quantities equal the baseline. Unknown state,
timeouts, fill evidence, or position deltas return exit 2 with redacted manual
cleanup/unwind guidance.

The shared Kiwoom response layer remains backward compatible for reads and
cancel. Confirmed place and modify use the tracked-mutation finalizer: strict
broker success plus exactly one non-conflicting canonical ID across documented
fields yields `submitted`; strict success with missing, invalid, or conflicting
ID evidence yields `accepted_untracked`, `reconcile_required=true`, and
`success=false`; a missing/malformed response, transport exception, or
post-send provenance conflict yields `acceptance_uncertain`; explicit broker
failure remains `rejected`. Raw broker evidence is retained through existing
redaction and uncertain acceptance is never automatically retried. An uncertain
modify also leaves replacement lineage unknown, so proving the original order
terminal cannot produce a successful final reconciliation by itself.
Client construction and the transport's OAuth/rate-limit/request-build phases
are distinguished from HTTP send: a typed pre-dispatch failure is
`not_submitted` with no reconciliation requirement, while only failure after
send begins is acceptance-uncertain. Full-smoke modify keeps lineage complete
for `not_submitted` and cleans up only the original order. Fixed local
validation messages remain actionable; provider-controlled exception text is
withheld.

The seven registered tools lazily share one mock-host-pinned client per MCP
registration. Its locked OAuth cache prevents bounded page walks and cleanup
polling from issuing a token request for every tool invocation. A transport
dispatch hook serializes each US mock `api-id` at one-second intervals within
that client, covering continuation pages, cleanup polls, repeated probe types,
and concurrent MCP calls without unnecessarily serializing different TRs.
Probe preflight and probe execution explicitly reuse one client. Cross-process
account coordination remains an operator boundary; concurrent smoke/MCP
processes for the same mock US account are prohibited.

MCP startup reads `MCP_TYPE` before import-time auth validation. Network
transports (`streamable-http` and `sse`) require a non-empty token when the
Kiwoom profile is selected or when the default profile exposes the enabled US
mutation gate. Explicit local `stdio` remains available without a token.
Unrelated profiles and a disabled US gate retain their current behavior.

## Evidence Classification

- `open`: target is in open history with zero executed quantity.
- `partial`: target has both executed and remaining quantity.
- `filled`: target has executed quantity and zero remaining quantity.
- `cancel_pending`: documented status denotes a cancel request but remaining
  quantity is still positive.
- `cancelled`: documented terminal cancel status or a cancel row links to the
  target and remaining quantity is zero.
- `rejected`: documented rejection status.
- `unknown`: missing, contradictory, or malformed documented fields.

Only `cancelled` or `rejected` are clean no-position terminal outcomes for this
smoke. `filled`, `partial`, any position delta, target absence, and `unknown`
require operator reconciliation.

## Test Strategy

Tests first reproduce empty post-place history, unrelated numeric false
matches, page-two target discovery, repeated tokens, page caps, partial/fill
position deltas, cancel-pending timeout, and a probe that remains open after a
successful cancel response. Injected clock/sleep makes polling deterministic.
Shared response tests cover `False`, `0.0`, missing, whitespace, and malformed
return codes. MCP place tests cover missing, invalid, 19-digit, and non-digit
IDs. Startup module-load tests isolate environment and fake settings for HTTP,
SSE, stdio, default profile gate-on, and gate-off cases.

## Self-review

The design contains no placeholders, does not create a ROB-874 stable envelope,
does not infer undocumented broker fields, and maps every ROB-872 acceptance
criterion to a deterministic non-live test.
