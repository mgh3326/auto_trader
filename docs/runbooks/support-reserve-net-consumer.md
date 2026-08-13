# Support reserve-net consumer — seam-gated proposal creation

## Current state

`app.services.support_reserve_net_consumer.SupportReserveNetConsumer` is a
deterministic consumer of `decision_rules.buy.support_reserve_net`. It still has
no scheduler registration, MCP tool, broker client, account lookup, DB-session
factory, or deployment activation. It receives all evidence and an already-open
`OrderProposalsService` transaction from its caller; it creates proposal rows
only through the public watcher-scope seam.

This consumer does not send a broker order. Existing downstream approval,
classification, veto, and dispatch boundaries remain unchanged.

## Seam readiness and call order

Readiness is not an environment switch or a constant set to true. At each
`consume` call, the consumer checks that the supplied object actually exposes
both public operations:

- `inspect_watch_to_order_scope`;
- `create_proposal_in_watch_to_order_scope`.

If either operation is unavailable, `consume` returns
`atomic_self_open_order_read_seam_unavailable` and creates zero proposals.
It does not fall back to `list_recent`, an internal repository import, a cached
open-order answer, or ordinary `create_proposal`.

For every selected proposal, the required sequence is:

```text
validate canonical broker_account_id
→ inspect legacy broker_account_id=None scope (lock + empty required)
→ inspect concrete four-axis scope (lock + empty required)
→ create_proposal_in_watch_to_order_scope on that concrete inspection
→ caller-owned commit or rollback
```

All selected scopes are inspected before the first companion create. The
consumer does not commit or roll back, so the service instance and transaction
that acquired each advisory lock remain in force through its create.

## Broker-account representation contract

For automatic reserve-net creation, `broker_account_id` is an opaque canonical
identifier from the account-evidence source. It must be a non-empty string in
its exact supplied representation, with no leading or trailing whitespace.
The consumer does not invent an alias, substitute a default account, or rewrite
case or punctuation. Candidate and cash evidence must use that same canonical
value; otherwise the candidate is rejected before a seam inspection and the
result is zero proposals.

`None` is never a canonical automatic-creation identity. It is a distinct
legacy ledger scope, not a wildcard. The consumer locks and checks that legacy
scope first. An active legacy proposal blocks the concrete-id create with
`legacy_unscoped_active_proposal_exists`; this closes the NULL-ledger probe.

## Residual race with the manual create path

`app/mcp_server/tooling/order_proposal_tools.py::order_proposal_create` remains
outside the watcher-scope lock. Therefore a manual human/agent create can still
commit in the interval after this consumer has inspected a scope and before its
companion create. The seam serializes its own callers, not that manual path.

This is an explicit operating constraint, accepted only while the manual path
remains separately reviewed and the existing proposal approval/dispatch gates
remain in place: do not manually create a matching account×market×symbol scope
while this consumer is running. The consumer never interprets its seam snapshot
as proof that an out-of-seam manual create cannot intervene. Migrating every
automatic caller to the seam does not remove this manual-path window.

## Candidate controls

- Inputs must carry fresh account/currency cash evidence and explicitly account
  for same-account/currency local pending cash. Missing, stale, ambiguous, or
  incomplete self-open-order/sector evidence yields no candidate.
- `max_owned_or_open_symbols_per_market` counts only reserve-net-attributable
  `armed`, `open`, and `filled` symbols. It does not count another strategy's
  holdings. Same-beneficial-owner self unfilled buys still block a matching
  candidate across accounts.
- The pool order is `[eligible_new, eligible_add]`: all eligible new candidates
  are considered before at most one add fallback per market. Add exact ties end
  with `normalized_symbol ASC`.
- Add ranking is exactly support strength, independent source count, honest
  upside, post-fill sector increase, and required cash. Deep-loss depth is never
  a positive ranking input.
- An add needs R-931 `PASS` (≤7 days), a policy table ≤36 hours, recomputed
  `A_limit(10%)` at the proposed limit, full lot-rounded funding, no partial
  fill, and at most one add symbol per market. US adds are disabled pending a
  thesis-review contract.
- The caller must prove a non-negative post-fill sector increment and a
  post-fill sector concentration no higher than the policy's 10% cap. A missing
  sector or incomplete sector exposure is not treated as zero exposure.
- A KR new candidate requires net available cash of at least `400,000 KRW`; a
  lower amount yields zero KR new arms. The all-buy 90% and reserve-net armed
  50% cash caps remain fail-closed.
- Anchor math is `tick_floor(support × (1 - discount))`; the 5–10% support
  discount and final -15% to -5% current-price band are both checked. An
  out-of-band anchor is excluded, never clamped.

## Verification and operating rule

Run the consumer and seam contract suites before changing this consumer:

```bash
uv run pytest tests/services/test_support_reserve_net_consumer.py \
  tests/services/order_proposals/test_watch_to_order_scope.py -q
```

Passing these suites does not authorize a broker probe, environment arm,
deployment, or direct broker order. This consumer only persists a proposal via
the existing service-layer seam; all later execution gates remain independent.
