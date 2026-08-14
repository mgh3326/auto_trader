# route_request lifecycle cleanup surface (ROUTE-GATE-STAGE1)

This runbook records the first-stage `route_request` allowance for clearing
stale watches and proposals. It changes only the advisory MCP surface; it does
not submit, cancel, or reconcile a broker order.

## Allowed surface

`route_request_lanes.PROPOSAL_LED_LANES` is the explicit resource-owner set:
`buy` and `sell`. Their ordered sequences both terminate at
`order_proposal_create`; `discovery` and `bootstrap` are not members. Stage one
therefore exposes these unsequenced cleanup helpers only to `buy` and `sell`:

| Tool | Candidate selection / existing gate | Exposed lanes |
|---|---|---|
| `order_proposal_expire_sweep` | Server selects only non-terminal proposals with `valid_until <= now`, then re-checks under a row lock and skips any group with a non-voidable rung. The caller cannot name a proposal. | `buy`, `sell` |
| `investment_watch_expire` | Caller supplies one `alert_uuid`; service requires a non-blank reason and an `active` row. | `buy`, `sell` |

`discovery` and `bootstrap` retain both tools in `blocked_actions`. The helpers
are allowances, not ordered workflow steps.

## Authorization finding — BLOCKER for operator review

`investment_watch_expire` has **no caller, creator/owner, or time-based
authorization gate**. `investment_watch_expire_impl` parses the UUID and calls
`WatchLifecycleService.expire`; `WatchLifecycleService._transition` checks only
that the reason is non-blank and the row is `active`. It does not compare
`valid_until`, inspect caller identity, or check watch provenance.

Consequently, once a session reaches this surface it can explicitly expire an
arbitrary active watch by UUID. `void` and `expire` use the same transition
path; their material difference is the terminal status label (`canceled` versus
`expired`). This first-stage surface change intentionally does not add or alter
authorization logic. Operator review is required before treating the exposure
as a constrained expiry capability.

## Stage boundary

The individual watch-cancel helper and proposal re-dispatch helper remain
blocked in every lane in this stage.

No scheduler registration, broker/account mutation, policy change, migration,
or authorization-logic change is part of this surface update.
