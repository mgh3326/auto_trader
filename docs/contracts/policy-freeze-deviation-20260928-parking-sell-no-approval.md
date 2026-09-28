# Freeze deviation record — #817 parking ETF sell without approval

Written **before PR #2117 merges** because it relaxes a live auto-approval and
loss-sell boundary. The operator's 2026-09-28 decision for hk #817 was conveyed
through the fable-strategy bundle: "from now on parking-ETF sells without
approval." Here parking means cash held in an ETF without a deposit or FX
transfer. Task #765 supplied the explicit proposal account prerequisite before
PR #2117.

```yaml
freeze_epoch_id: UNKNOWN
  # No freeze-epoch value was supplied with the #817 decision. The buy-gate
  # shadow epoch is a separate axis and is not substituted here.
change_id: task817-parking-sell-no-approval-20260928
pr_number: 2117
merge_sha: TBD_ON_MERGE
operator_decision_ref: >-
  Operator decision 2026-09-28 for hk #817, conveyed through the
  fable-strategy bundle: parking ETF sells proceed without an approval tap.
  Parking is cash held without deposit or FX. Task #765 is the explicit-account
  prerequisite, merged before PR #2117.
market: [us, kr]
account_scope: >-
  Live kis_live and toss_live only, with an explicit broker_account_id on the
  persisted proposal matching the broker-selected account. For Toss, the
  account-scoped meter requires the selected sequence to appear exactly once
  in the broker account list, as provided by task #765. Mock and simulated
  account modes are excluded.
change_class:
  - auto_approval_guard_relaxation
  - proposal_bound_loss_sell_exception
affected_policy_keys: []
  # No trading_policy.yaml key, version or projection changes in PR #2117.
  # The exception is implemented in the proposal classifier and broker guards.
consumer_paths:
  - app/services/order_proposals/auto_approve.py
  - app/services/order_proposals/parking_sell_exemption.py
  - app/services/order_proposals/dispatch.py
  - app/services/order_proposals/revalidation.py
  - app/mcp_server/tooling/order_validation.py
  - app/mcp_server/tooling/order_execution.py
  - app/mcp_server/tooling/orders_toss_variants.py
effective_at: >-
  After PR #2117 merges and its code is deployed. This deviation record must
  be filed before that merge; the document alone enables no order path.
live_only_claim: true
mock_projection_hash_before: NOT_APPLICABLE
mock_projection_hash_after: NOT_APPLICABLE
  # PR #2117 does not edit trading_policy.yaml or the sealed buy-gate shadow
  # projection. No new policy hash is asserted for this code-only change.
mock_experiment_effect: NONE
  # The exact exception predicate requires kis_live or toss_live, SELL and
  # a persisted proposal. It cannot enter a mock account or a buy-gate
  # shadow candidate.
rollback: >-
  Set ORDER_PROPOSALS_AUTO_APPROVE_MODE=off to disable the expanded-mode
  exception on the next configuration load; ORDER_PROPOSALS_AUTO_APPROVE=false
  disables all proposal auto approval. Revert PR #2117 and redeploy to restore
  the prior funding-target, profit and average-cost requirements. Neither
  action reverses broker-accepted orders; reconcile those from broker evidence
  and retain their audit records.
evidence: >-
  PR #2117 implements the closed scope, retained guards and negative tests.
  Task #765 supplies explicit account identity. The operator's hk #817
  decision is the authority for the relaxation, not this record by itself.
```

## Changed rule and reason

Before #817, an ordinary parking loss or break-even sell cannot use the
expanded auto-approval profit proof, and an ordinary live sell still faces the
broker average-cost floor even after manual approval. The separate
cash-funding exception needs a planned-buy funding target and a measured
shortfall; a sell that seeks auto
approval outside that path needs the existing fee-net profit proof beyond the
inclusive break-even band. PR #2117 permits an **ordinary proposal-bound
parking ETF limit SELL** in expanded auto-approval mode without a funding
target or approval tap. It exempts that sell from the average-cost floor and
the break-even and fee-net profit proof. The de-minimis trim watch is advisory
and has no runtime consumer; it does not veto this conversion. The targeted
cash-funding path keeps its evidence contract.

The operator treats SGOV, BIL, 459580 and 357870 as parked cash to convert to
buying power for a cash shortage or operator instruction. A distribution
ex-date price reset can make an ETF appear below average cost without changing
the cash-conversion intent. Waiting for an approval tap can delay that
conversion. These are the operator reasons recorded for hk #817, not a claim
that every such sell is economically riskless.

## Exact scope and retained bounds

| Boundary | Recorded rule |
|---|---|
| Symbols and markets | SGOV and BIL in equity_us; 459580 and 357870 in equity_kr. The exact live kis_live and toss_live tuples only. |
| Action and authority | Persisted place proposal, SELL side, limit order, no exit intent, expanded auto-approval mode. Preview and submit must bind the same proposal, rung, symbol, market, account, side, quantity and price. Direct order calls receive no exception. |
| Account | The proposal supplies an exact broker_account_id matching the selected account; it is never inferred from settings. The account-scoped meter must be available. Toss also needs task #765's same-client broker-listed sequence proof. |
| Price and amount | Fresh successful preview and a marketable limit from 98% through 100% of the fresh current price, checked again at submit. Per-order parking cap: USD 10,000 for US or KRW 10,000,000 for KR. |
| Session | KR sells only inside the XKRX regular session, checked at classification and before send. PR #2117 conservatively excludes 09:00–09:59:59 KST on the published 2026-11-19 exam date because the calendar does not model that possible late opening. The actual 2026 exchange notice must be checked before that date. |
| Other vetoes | Existing policy_deviation tag scan, renderable veto-card thesis, account/market allowlist, broker env and per-call confirmation gates, nonce and idempotency controls remain. Missing evidence or an uncertain session demotes to a card. |

The cumulative parking-exposure cap remains a **buy** guard. This decision
creates **no cumulative parking-sell cap**. Multiple eligible sells can exceed
the per-order cap in aggregate, so the per-order cap and the operator's weekly
parking-balance review are the stated amount controls. No new scheduler,
migration or direct broker authority is introduced.

A demoted loss-sell proposal may produce a manual card whose tap still fails
the ordinary broker average-cost guard. The operator must correct the missing
account or meter evidence and create a new proposal; the card is not a second
route around that guard.

## Risk and counter-evidence

An eligible marketable limit sell may fill before the veto card becomes visible.
If classification is wrong, the immediate exposure is bounded **per order** by
the US or KR cap above, but repeated sells have no new cumulative sell limit.
More frequent sales can reduce the parking balance and change actual available
cash independently of the manual_cash deployment-cap denominator. Inspect the
held parking balance weekly and compare it with the recorded cash figure.
This absence of a cumulative sell cap and the timing of a possible fill are
the counter-evidence to treating the relaxation as costless.

## Mock experiment effect and rollback limit

The exception's exact predicate excludes buys and mock or simulated accounts.
PR #2117 does not change trading_policy.yaml or the sealed buy-gate shadow
projection, so no mock cohort or projection hash is moved by this record.
This is a scope claim about the changed code, not a production-data audit.

For immediate containment, disable expanded auto approval through
ORDER_PROPOSALS_AUTO_APPROVE_MODE=off, or disable all proposal auto approval
through ORDER_PROPOSALS_AUTO_APPROVE=false, then apply the setting through the
normal runtime configuration load. Revert PR #2117 and redeploy for code
rollback. Accepted orders remain real and require broker-evidence reconcile;
rollback does not cancel or erase them.
