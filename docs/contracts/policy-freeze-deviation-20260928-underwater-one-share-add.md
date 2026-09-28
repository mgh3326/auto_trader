# Freeze deviation record — #877 underwater add one-share exception

This record precedes the policy implementation PR. The operator-approved scope is
section B of hk:doc strategy-lab/2026-09-28/policy-corrections-kr-order-lifetime
(recorded 2026-09-28). The 2026-09-24 new-entry one-share exception record is
docs/contracts/policy-freeze-deviation-20260924-kr-one-share-exception.md.
Section A of the same hk document corrects the KR order-lifetime description;
section C is a draft and is outside this change.

```yaml
freeze_epoch_id: UNKNOWN
change_id: s877-underwater-one-share-add-20260928
pr_number: 2132
merge_sha: TBD_ON_MERGE
operator_decision_ref: >-
  hk:doc strategy-lab/2026-09-28/policy-corrections-kr-order-lifetime
  section B, operator approval recorded 2026-09-28.
market: [kr, us]
account_scope: >-
  Held, losing KR and US account lots evaluated under
  buy.underwater_support_net; the add stays in the same account as its lot.
change_class:
  - sizing_relaxation
  - new_exception
affected_policy_keys:
  - decision_rules.buy.underwater_support_net.tiers[underwater_support_net].conditions.one_share_exception_for_adds
  - decision_rules.buy.underwater_support_net.semantics
  - thresholds.order.day_expiry_kst
  - version
  - source
consumer_paths:
  - app/services/underwater_support_net.py
  - app/mcp_server/tooling/buy_candidate_fanout.py
  - docs/playbooks/trading-decision-playbook.md
effective_at: on merge of the implementation PR, after this record is merged
live_only_claim: false
mock_projection_hash_before: dda6a0541849
mock_projection_hash_after: 8bc4db3fe010
mock_experiment_effect: UNKNOWN
rollback: >-
  Revert the implementation PR. This record remains as an audit of the
  approved deviation. No migration or scheduler is authorized.
evidence: >-
  The implementation PR must carry exact-head tests and assertion-RED
  mutants for the broker split and one-share sizing and caps.
```

## Scope and counter-evidence

The present 50% computation floors a one-share lot's add to zero. The approved
exception permits exactly one share when that computed add is below one share.
This is a sizing increase: a one-share add can exceed 50% of the existing
position notional. It does not increase the ordinary automatic approval caps
(KR 2,000,000 per order; US 1,500 per order), daily caps, or available account
cash. An over-cap or cash-short one-share add cannot become an automatic order.
The policy retains the existing loss, support, thesis, and rebound-improvement
conditions; the implementation must test the one-share boundary in both markets.

The tier remains a held-lot add and does not enter the new-entry buy-gate A/B
population. The raw policy content hash and version necessarily change and
must be recorded as a new cohort stamp. No claim is made that every mock pilot
is unaffected: a mock account evaluating this tier can produce a larger add.
The before hash above is the first 12 characters of SHA-256 over the raw policy
file at origin/main cab5da08d; the after hash is the implementation's pinned
policy_content_hash in PR #2132.
Whether that field denotes a different mock projection remains unconfirmed,
as in the 2026-09-24 precedent.
The D+20 outcome tag is rounded_up_to_one_share under underwater-d20-v1;
the existing scoring horizon and thresholds remain unchanged.

The same implementation PR also corrects order.day_expiry_kst by broker under
#876. That correction must not be treated as evidence for a KIS value: KIS
expiry is to confirm unless existing code or documentation proves a value.
