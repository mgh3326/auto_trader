# Support reserve-net consumer — candidate-only safety boundary

## Current state

`app.services.support_reserve_net_consumer.SupportReserveNetConsumer` is a
deterministic, **candidate-only** consumer of
`decision_rules.buy.support_reserve_net`. It has no scheduler registration,
MCP tool, broker client, account lookup, DB session, or deployment activation.
It receives evidence as explicit input and produces proposal-shaped candidates
only.

**ATOMICITY_STANCE = (a): 지금은 원자적이지 않다.** The public
`OrderProposalsService` API has no transactionally locked read that covers the
beneficial owner, account, market, symbol, and non-terminal rung state. A
best-effort list followed by `create_proposal` could race a resting buy and
produce duplicate live exposure. The consumer therefore stops immediately
before proposal creation with
`atomic_self_open_order_read_seam_unavailable`.

Do not substitute any of the following for the missing seam:

- `list_recent` (it has no account/rung-state/lock contract);
- a direct import of the internal proposal repository;
- `supersedes_proposal_id` (a mixed-state group can retain a resting rung);
- a cached or manually asserted open-order result.

The source-visible future call location is
`SupportReserveNetConsumer._create_after_atomic_self_open_order_check`, but its
hard non-configurable gate is false in this revision. It does not run.

## Required future seam

After #1844 is merged, a separately owned seam job must add a public
order-proposal service operation that, in one transaction, acquires a key scoped
to the beneficial owner/account/market/symbol, reads both proposal and broker
non-terminal state with rung-state detail, and permits creation only when it
observes no same-symbol self buy. That job, not this consumer, owns the lock and
must be reviewed before the hard candidate-only boundary is removed.

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

Run the dedicated contract suite before changing this consumer:

```bash
uv run pytest tests/services/test_support_reserve_net_consumer.py -q
```

Passing this suite does **not** authorize a proposal, order, broker probe, or
environment arm. Until the future atomic seam acceptance is complete, the only
permitted output is the candidate plan and its explicit atomicity block.
