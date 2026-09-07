# Freeze deviation record — §177차 underwater averaging-down tier

Checkpoint #4 appendix-A schema. Written **before merge**, as the Q4 rule
requires for a new tier. Precedent: PR #1990.

```yaml
freeze_epoch_id: q4-tier-freeze-2026-08-30
change_id: s177-underwater-support-net-20260907
pr_number: TBD_ON_OPEN
merge_sha: TBD_ON_MERGE
operator_decision_ref: >-
  operator decision 2026-09-07 11:05 KST ("물타기 티어 등록하자"), relayed in
  herdr-inbox/jobs/captain-25-underwater-tier-20260907/captain-brief.md;
  measured trigger in week1-exec-report.md §4-1 (three KIS averaging-down
  proposals with no matching registered tier) and cash-active-week1-plan
  §0/§b/§c.
market: [kr, us]
account_scope: >-
  Held equity lots on KR and US live accounts. No crypto surface is touched:
  §139차 buy.held_majors_support_net keeps markets [crypto] and its own
  profitable-lot floor.
change_class:
  - new_tier          # buy.underwater_support_net
  - new_tier          # sell.loss_cut  (classification criteria only)
  - new_tier          # buy.deployment_cap (advisory ceiling)
affected_policy_keys:
  - decision_rules.buy.underwater_support_net      # NEW
  - decision_rules.sell.loss_cut                   # NEW
  - decision_rules.buy.deployment_cap              # NEW
  - version                                        # 2026-09-07.2 -> 2026-09-07.3
  - source                                         # append-only provenance
  # No pre-existing key's VALUE changes. Proven by
  # tests/schemas/test_trading_policy_schema.py::
  #   test_rob_1289_preserves_all_preexisting_policy_keys_and_values
  # which strips the three new rules and then asserts closed equivalence with
  # the ROB-1289 baseline over every remaining key.
consumer_paths:
  - app/schemas/trading_policy.py                       # three new validators
  - app/services/underwater_support_net.py              # NEW pure evaluator
  - app/services/deployment_cap.py                      # NEW pure evaluator
  - app/mcp_server/tooling/buy_candidate_fanout.py      # reads + echoes, fail-closed
  - app/mcp_server/tooling/portfolio_cash.py            # advisory emit, fail-open
  - app/services/order_proposals/buying_power.py        # sequential_approval_shortfall
  - app/mcp_server/tooling/order_proposal_tools.py      # advisory now also built for kis_live buys
  - app/mcp_server/tooling/order_validation.py          # comment only, no behaviour change
  - app/services/order_proposals/auto_approve.py        # docstring only, no behaviour change
effective_at: on merge (policy version 2026-09-07.3)
live_only_claim: false
mock_projection_hash_before: "e364868237fb"   # policy content_hash 2026-09-07.2 (post-#2058)
mock_projection_hash_after: "265ff4942ded"    # policy content_hash 2026-09-07.3
mock_experiment_effect: NONE
rollback: >-
  Revert the PR. The three decision rules are additive and no consumer stores
  derived state: the two evaluators are pure, the fan-out only echoes the
  literals, the capital advisory is fail-open, and the sequential-approval
  field is computed per call. No migration, no scheduler registration, no
  broker or order-path behaviour to unwind.
evidence: >-
  See "mock_experiment_effect justification" and "What is NOT changed" below.
```

## `mock_experiment_effect: NONE` — the semantic consumer proof

Q4 requires this verdict to be earned by a consumer trace, not asserted.
`NONE` is claimed on the following chain, and would be `UNKNOWN` without it:

1. **Which experiment is at risk.** The live mock/shadow scoring experiment
   under the freeze is the ROB-1301 / ROB-1351 buy-gate A/B shadow, whose
   `only_difference` is `support_strength_min` on the **new-entry discovery**
   population, plus the shared gates `screen.rsi_max` (45) and
   `screen.upside_min_pct` (40).
2. **Which keys this change touches.** Three new `decision_rules` keys and
   nothing else — machine-proven by the ROB-1289 closed-equivalence test named
   above, which strips exactly the three additions and then compares every
   remaining key against the pre-change baseline.
3. **Whether any A/B consumer reads them.** It cannot: the A/B package
   deliberately does not live-read `trading_policy.yaml` at all
   (`app/services/buy_gate_ab_shadow/spec.py`: "Do not live-read
   ``trading_policy.yaml``; a later policy edit must not retcon..."), so its
   pre-registered spec is unreachable from any policy edit, this one included.
   The new fan-out read is additive
   (`_UnderwaterAddGates`), is echoed under `policy.frozen_gates.underwater_add`,
   and is not consulted by any selection branch —
   `tests/mcp_server/tooling/test_buy_candidate_fanout.py::
   test_underwater_add_gates_do_not_change_the_discovery_gates` pins the six
   discovery literals against drift.
4. **Whether the tier can enter the A/B population.** It cannot: the tier
   admits only `holding_required: true` lots, and the A/B population is new
   entries. The tier declares this as `buy_gate_ab_shadow_population_unchanged:
   true` and `new_symbol_discovery_gate_unchanged: true`, both machine-pinned.
5. **Projection hash.** The policy `content_hash` necessarily moves
   (`e364868237fb` -> `265ff4942ded`) because the document changed. That is a
   document hash, not an A/B projection: no A/B spec, epoch marker, or scored
   field takes an input from any of the three new keys.

Honest limit on this verdict: the argument is a static consumer trace plus the
closed-equivalence test, not a re-run of the A/B projection over a captured
cohort. If a reviewer wants the stronger form, the downgrade is `UNKNOWN`, not
a silent `NONE`.

## What is NOT changed

- **No gate relaxed.** `screen.rsi_max`, `screen.support_within_pct`,
  `screen.upside_min_pct`, `buy.deep_limit_pct_range`, the per-symbol notional
  bands, `portfolio.sector_cluster_cap_pct` and
  `portfolio.max_symbols_per_theme` all keep their values (pinned by the
  existing §147차 invariant tests, still green).
- **No approval cap raised.** `order_proposals.auto_approve.per_order_cap` and
  `daily_cap` are byte-identical. An over-cap averaging-down rung is rejected
  as `per_order_cap_exceeded` and goes to a human card —
  `tests/services/test_underwater_support_net.py::
  test_an_over_cap_underwater_rung_still_falls_back_to_a_human_card`.
- **No new approval surface.** `auto_approve.py` is not taught the tier's name.
  A tier-passing rung is auto-approvable purely because the tier's support band
  ends at -3%, which is exactly `min_distance_pct`. Asserted by
  `test_underwater_rung_is_auto_approvable_under_the_existing_off_mode_rules`.
- **No code-enforced guard touched.** The average-cost loss-sell guard in
  `order_validation.py`, `sell.loss_cut_max_slip`, and the ladder guards are
  untouched; the `order_validation.py` diff is a comment.
- **No migration, no scheduler registration, no broker mutation.**

## Deliberate deviations from the brief

1. **`unrealized_pnl_pct_max_inclusive`, not the draft's `_max_exclusive`.**
   The §0 formula says `손실 <= -8%` (inclusive) while the §c draft key name
   said exclusive. The key now carries the comparison it performs, and a lot at
   exactly -8.00% qualifies (`test_loss_floor_is_inclusive_at_exactly_minus_eight_percent`).
2. **No absolute KRW ceilings on the tier.** The §c draft proposed
   `max_notional_krw_per_symbol: 1,600,000` / `per_tier: 4,000,000`, reverse-
   engineered from one week's KR demand. The brief specifies the cap as 50% of
   the existing position, and a KRW ceiling is incoherent on the US half of a
   `[kr, us]` tier. The proportional cap plus the existing per-order
   auto-approve cap are the two ceilings.
3. **The deployment-cap advisory is emitted from `get_available_capital_impl`,
   not from `order_validation` or proposal-create.** The brief offered those
   two call sites. Neither knows the fleet-wide denominator the formula needs
   (broker orderable across accounts + parking), and obtaining it there would
   mean adding a multi-broker balance fan-out to an order path. The capital
   read already holds both terms separately, so the advisory costs zero extra
   I/O and touches no order path. Flagged for the reviewer as a judgement call.
4. **`sequential_approval_shortfall` is emitted for `kis_live` without a
   buying-power reader.** Wiring a KIS balance read into proposal-create was
   rejected for the same reason as (3). KIS therefore reports
   `status: unavailable` for buying power while still emitting the per-approval
   ladder, which needs no balance and is the half that names the 2026-09-07
   failure. This is a partial close of that incident, stated as such.
