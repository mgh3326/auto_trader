# Freeze deviation record — #880 US parking cumulative cap to USD 20,000

Written before the code PR is merged. The authority is hk doc
strategy-lab/2026-09-28/cash-sweep-parking-lane, section 5, decision 2
(2026-09-28 evening): the operator adopted the US parking cumulative cap
change from USD 10,000 to USD 20,000 through
PARKING_CUMULATIVE_CAP_USD. This record does not itself change an approval
decision or enable an order path.

```yaml
freeze_epoch_id: UNKNOWN
  # No freeze-epoch value was supplied with decision 2. The buy-gate shadow
  # epoch is a separate axis and is not substituted here.
change_id: task880-parking-cap-usd-20000-20260928
pr_number: 2122
merge_sha: TBD_ON_MERGE
operator_decision_ref: >-
  hk doc strategy-lab/2026-09-28/cash-sweep-parking-lane, section 5,
  decision 2 (2026-09-28 evening): US parking cumulative cap USD 10,000 to
  USD 20,000 through PARKING_CUMULATIVE_CAP_USD.
market: [us]
account_scope: >-
  Live kis_live and toss_live equity_us parking tuples only. Each explicitly
  bound broker account is measured separately. Mock and simulated account
  modes are excluded.
change_class:
  - auto_approval_notional_cap_relaxation
affected_policy_keys: []
  # No trading_policy.yaml key, version, or projection changes.
consumer_paths:
  - app/services/order_proposals/parking_allowlist.py
  - app/services/order_proposals/auto_approve.py
  - app/services/order_proposals/parking_exposure.py
  - app/services/order_proposals/service.py
effective_at: >-
  Only after the code PR merges and its code is deployed. The deployment
  freeze through approximately 2026-09-29 18:00 KST remains in force.
live_only_claim: true
mock_projection_hash_before: NOT_APPLICABLE
mock_projection_hash_after: NOT_APPLICABLE
mock_experiment_effect: NONE
rollback: >-
  Revert the code PR that changes PARKING_CUMULATIVE_CAP_USD and redeploy.
  Already broker-accepted orders remain real; reconcile them from broker
  evidence and retain their audit records.
evidence: >-
  The code PR must prove the USD 20,000 cumulative boundary, unchanged USD
  10,000 per-order boundary, unchanged KRW boundaries, and account-scoped
  SGOV/BIL aggregation. The hk decision is the authority for the relaxation;
  this document is only its deviation record.
```

## Changed rule and exact scope

The existing expanded auto-approval path uses a closed US parking allowlist:
SGOV and BIL on kis_live or toss_live equity_us. A parking BUY is subject to
two independent USD guards. Each order remains at or below USD 10,000. The
projected parking exposure, broker holdings plus the same account's durable
same-day auto-approved parking buys plus the proposed buy, changes from at
most USD 10,000 to at most USD 20,000. Equality is allowed; any amount above
the cap is refused. Missing or invalid exposure remains a refusal.

SGOV and BIL share this USD 20,000 cumulative cap **within each bound broker
account**. The meter sums both symbols; buying BIL does not create a second
USD 20,000 allowance beside SGOV. KIS and Toss use separate broker holdings
and separate account-scoped durable sums, so the cap is per account face, not
one portfolio-wide USD pool. If both faces are available, their two accepted
exposure ceilings total USD 40,000. This is a consequence of the existing
account scope, not an added cross-account allocation rule.

The same PARKING_CUMULATIVE_CAP_USD value also bounds the existing
cash_funding path's same-day cumulative US parking SELL notional. Changing the
constant therefore raises that sell-path cumulative threshold to USD 20,000
as well; the USD 10,000 per-order check still applies. Ordinary parking sells
do not consume this cumulative buy cap. The separate KR parking constants
stay at KRW 10,000,000 per order and KRW 15,000,000 cumulative. No symbol,
account mode, broker gate, scheduler, migration, or policy YAML threshold is
added by this deviation.

## Risk and counter-evidence

The larger cap permits a second USD 10,000 parking buy on an account already
near the former limit. It doubles the maximum permitted parking exposure per
account face and raises the cash_funding sell-path daily cumulative threshold.
The per-order guard bounds one mistaken authorization, but repeated valid
orders can still reach the new cumulative ceiling. Existing measurement gaps
remain: a broker balance can lag an accepted order; the durable meter is
same-day and account-bound, with known cross-day and reservation limitations.
An unavailable balance or durable read refuses auto approval, but the guard
is not a broker-side reservation and cannot prove that simultaneous activity
outside this proposal path is absent.

The required update to the section 163 explanatory comments in
config/trading_policy.yaml changes the runtime policy content hash because
the loader hashes the raw file bytes, including comments. Parsed policy keys
and values and the declared version do not change. The code PR must update
the pinned hash assertion and verify that this metadata change does not move
the separately sealed buy-gate shadow policy projection.

The reported SGOV position of 98 shares, approximately USD 9,861, explains
why the former ceiling blocks additional parking. That estimate is context,
not a fresh broker balance or proof of unused capacity. A live decision still
requires the broker-origin account balance and durable proposal evidence at
evaluation time. No production account query is asserted by this record.

## Rollback and effective time

Revert the code PR and redeploy to restore the USD 10,000 cumulative constant.
The document and the code PR may merge during the deployment freeze, but no
new limit takes effect until deployment. A rollback does not cancel orders
already accepted by a broker; their state must continue through the normal
evidence-based reconciliation path.
