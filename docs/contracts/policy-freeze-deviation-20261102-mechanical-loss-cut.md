# Freeze deviation record — ROB-1460 mechanical loss-cut

Written **before the ROB-1460 PR merges** because it adds a new automatic sell
class. Until now every loss-cut sell needed the operator's approval card:
`sell.loss_cut` is classification-only, and `auto_approve.py` rejects
`exit_intent=loss_cut` outright. The PR adds a path around that rejection, behind
switches that default off. The merge turns on nothing.

```yaml
freeze_epoch_id: UNKNOWN
  # No freeze-epoch value was supplied with this decision. The buy-gate shadow
  # epoch is a separate axis and is not substituted here.
change_id: rob1460-mechanical-loss-cut-20261102
pr_number: 2266
merge_sha: TBD_ON_MERGE
operator_decision_ref: >-
  Operator chat 2026-11-02, relayed by operator-desk: the brief for hk task 2605
  (ROB-1460), Addendum 1 (the trigger must be price action, not a lot sitting
  below average cost), Addendum 2 (the stop is decided and recorded per lot at
  buy time), and the ESC answer approving one additive migration.
market: [kr, us, crypto]
account_scope: >-
  Live kis_live, toss_live and upbit, only when named in
  MECHANICAL_LOSS_CUT_LIVE_ACCOUNT_MODES. Mock and simulated accounts cannot
  reach this path.
change_class:
  - new_automatic_sell_class
  - auto_approval_guard_relaxation_behind_default_off_switch
affected_policy_keys:
  - mechanical_loss_cut
  # New top-level block. No existing key, threshold or cap value changes.
consumer_paths:
  - app/services/mechanical_loss_cut/
  - app/services/order_proposals/auto_approve.py
  - app/services/order_proposals/dispatch.py
  - app/services/order_proposals/revalidation.py
  - app/mcp_server/tooling/order_proposal_tools.py
  - app/mcp_server/tooling/order_execution.py
  - app/mcp_server/tooling/orders_toss_variants.py
effective_at: >-
  Shadow: after the migration runs and the operator sets
  MECHANICAL_LOSS_CUT_SHADOW_ENABLED. Live: only after the operator also accepts
  thresholds from ROB-1458 evidence, sets thresholds_confirmed in a policy PR,
  and sets the live switch, market allowlist and account allowlist. This record
  alone enables nothing.
live_only_claim: true
mock_projection_hash_before: NOT_APPLICABLE
mock_projection_hash_after: NOT_APPLICABLE
  # Adding the block changes the document's content hash and version. The sealed
  # buy-gate projection covers named buy-gate keys and is not recomputed here.
mock_experiment_effect: NONE
  # The classifier release needs a live account allowlist entry and a decision
  # row. It cannot enter a mock account or a buy-gate shadow candidate.
rollback: >-
  Set MECHANICAL_LOSS_CUT_LIVE_ENABLED=false for the next configuration load.
  Every later mechanical proposal then goes to the human card. Revert the PR for
  code rollback. Neither action reverses a broker-accepted order; reconcile those
  from broker evidence. See docs/runbooks/mechanical-loss-cut.md.
evidence: >-
  tests/services/mechanical_loss_cut/ covers each invariant, including default
  off, a trapped broker client in shadow, and caps. The thresholds in the policy
  block are provisional shadow inputs, not evidence-backed values. ROB-1458 is
  expected to propose the real ones.
```

## Changed rule and reason

Before: every loss-cut waits for a person, so a falling lot can keep falling while
the card waits. After, with every switch on: a lot whose completed daily close is
below the stop recorded for it may be sold by limit order with no card, and a veto
card follows.

The stop comes from a per-lot exit plan recorded at buy time, as the operator
asked. A lot below average cost is not enough. It must also be below its stop, at
or below the unrealized floor, and not rising, so averaging down cannot cause a
sale on its own while the price recovers.

## Exact scope and retained bounds

| Boundary | Recorded rule |
|---|---|
| Who can release | Only a row in `review.mechanical_loss_cut_decisions` bound to this exact proposal. The job is the only writer. A strategy label or `source_asof` value grants nothing. |
| Switches | `MECHANICAL_LOSS_CUT_LIVE_ENABLED` exactly true, market in `..._LIVE_MARKETS`, account in `..._LIVE_ACCOUNT_MODES`, `thresholds_confirmed` true for the market, and `ORDER_PROPOSALS_AUTO_APPROVE_MODE=expanded`. |
| Caps | The existing per-order and daily caps. Neither is raised. Both meter `max(limit, current) x quantity`, because this sell may be marketable. Over a cap goes to the human card. |
| Order | Limit only, with a fresh successful preview. The limit is at most `order.max_slippage_pct` below the fresh price, and policy refuses a value wider than `sell.loss_cut_max_slip`. |
| Fresh re-check | At submit time the current price must still be below the stop and below average cost. Otherwise the proposal goes to the human card. |
| Session | Equities need the exchange's regular session. A loss-cut is otherwise exempt from the approval window so a person can act at any hour. An automatic sell does not inherit that. |
| Excluded | Parking instruments on any account, backfilled plans unless policy admits them, and protected quantity. |
| Everything else | `policy_deviation` tag scan, veto-capable account/market allowlist, veto thesis, broker env gates, approval hash, nonce and idempotency controls all stay. |
| Non-mechanical sells | Unchanged. A loss-cut without a decision row keeps reason `loss_cut_intent`, and the average-cost guard still blocks every other loss sell. |

## Risk and counter-evidence

- A marketable sell can fill before the veto card is seen. Exposure is bounded per
  order and per day by the existing caps, which are per-account per-market.
- The trigger reads completed daily closes. A fast intraday fall is not cut until
  the session closes and the job next runs, and that job has no schedule yet.
- The shadow position comes from `review.execution_ledger`, which cannot see a
  trade made in a broker's own app. Shadow results can be wrong for such lots. The
  live path's fresh preview reads quantity and cost from the broker, and a mismatch
  fails or goes to the card.
- Open-sell coverage has the same blind spot for orders placed outside
  auto_trader. On the live path, broker sellable quantity is the remaining check.
- The session check runs at classification, not again inside the send. An order
  classified in the last seconds of a session can reach the broker after the close.
- `exit_plan` lets a buyer state a lower stop with an add, which widens the loss a
  plan allows. Every version is recorded, but nothing refuses this yet.
- Fresh thresholds are unproven. Live stays refused until `thresholds_confirmed`.
