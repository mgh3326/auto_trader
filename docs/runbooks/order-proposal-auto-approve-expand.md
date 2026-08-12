# Auto-approve eligibility expansion (§40차)

Extends ROB-871 resting-class auto-approval with the §40차 classification:
**auto-submit, then veto**, for buys and for *proven* profit-take sells.

Ships inert. `ORDER_PROPOSALS_AUTO_APPROVE_MODE` defaults to `off`, which is
ROB-871 behaviour unchanged. Arming it is an operator decision and is not part
of the change that introduced this document.

---

## 1. Where the decision is made

`app/services/order_proposals/auto_approve.py :: evaluate_auto_approve_eligibility`
— pure, no DB, no broker. The dispatch boundary
(`order_proposals/dispatch.py :: dispatch_proposal`) supplies the day's
cumulative auto-approved notional and hands each rung's fresh submit-time
preview to the classifier through `revalidation._apply_eligibility_gate`.

Two gates sit above it and neither moved:

| gate | effect |
| --- | --- |
| `settings.ORDER_PROPOSALS_AUTO_APPROVE` (ROB-871) | master. Off ⇒ every proposal goes to Telegram. |
| `settings.ORDER_PROPOSALS_AUTO_APPROVE_MODE` (§40차) | selects the classification. `off` ⇒ ROB-871 rules. |

## 2. The classification

Structural gates, applied in both modes, in this order — the first that fires
wins and the proposal goes to a human:

1. `action != place` → `action_not_place`
2. `order_type != limit` → `order_type_not_limit`
3. `exit_intent == "loss_cut"` → **`loss_cut_intent`**
4. any other `exit_intent` → `exit_intent_present`
5. account/market not in `_VETO_CAPABLE_ACCOUNT_MARKETS` → `account_not_veto_capable`
6. `policy_deviation` / `table_disagreement` tag → **`approval_required_tag`**
7. preview did not succeed → `preview_guard_failed`
8. missing/non-positive price or quantity → `price_or_quantity_missing`
9. per-order cap → `per_order_cap_exceeded`
10. daily cap → `daily_cap_exceeded`

Then, per mode:

| | `off` | `expanded` |
| --- | --- | --- |
| buy | limit ≤ market × (1 − `min_distance_pct`) | limit ≤ market |
| sell | limit ≥ market × (1 + `min_distance_pct`) | limit ≥ market **and** proven profit-take |

"Proven profit-take" (`classify_sell_profit`), evaluated in this order:

1. **break-even band first.** `|limit − avg_buy_price| ≤ avg_buy_price × breakeven_band_pct`
   → `breakeven_band`. Checked before the sign test on purpose: §40차 sends the
   band to a human *whatever the sign*, so a sell 0.5% above cost is still a
   human's call. The band is inclusive — exactly ±1% is inside it.
2. **net P&L strictly positive.**
   `net = (limit − avg) × qty − max(limit, avg) × qty × round_trip_cost_bps / 10000`.
   `net > 0` → `take_profit`; `net == 0` → `expected_pnl_not_positive`. Exactly
   zero is not "> 0".
3. **no cost basis, no verdict.** A missing, zero or unparseable
   `avg_buy_price` → `sell_classification_unavailable`. The preview's own
   avg×1.01 guard fails *open* on unknown cost basis and is bypassable
   (`defensive_trim` / `loss_cut` / mock), so a passing preview is not evidence
   of profit and `expanded` proves it independently.

## 3. Deliberately narrower than the §40차 literal

`expanded` drops `min_distance_pct` but still requires the rung to **rest** —
a buy at or above the market, or a sell at or below it, can fill before the
operator ever sees the card, which would make the veto button that §40차 safety
invariant ① depends on a lie. §40차 forbids being *broader* than its literal;
this is narrower, which is the permitted direction. Relaxing it is an operator
decision, not a code cleanup.

The tag gate (step 6) also applies in `off` mode. It can only reject, so it is
a tightening of ROB-871, not a widening.

## 4. Fee rate provenance

`round_trip_cost_bps` is **not a measured rate**, and there is currently no way
to make it one from inside this repo: the two veto-capable ledgers
(`review.kis_live_order_ledger`, `review.live_order_ledger`) have no
`commission`/`tax` columns, so no realized fee evidence exists for `kis_live`
or `upbit`. Only `review.toss_live_order_ledger` records commission and tax,
and `toss_live` is not veto-capable.

Until that changes, the values are the **conservative maximum of the rates this
repo already declares**, so the profit-take test is narrower than reality:

| market | bps | derivation |
| --- | --- | --- |
| kr | 47.4 | 14.7bp × 2 legs (`account_routing` `kis_domestic.commission_bps`) + 18bp sell tax (`paper_trading.FEE_RATES["equity_kr"]["tax_sell"]`). The `paper_trading` commission alone would give 21bp. |
| us | 90.0 | (25bp commission + 20bp FX spread) × 2 legs (`account_routing` `kis_overseas`). `paper_trading` alone would give 14bp. |
| crypto | 10.0 | 5bp × 2 legs (`paper_trading.FEE_RATES["crypto"]`). |

The rate lives in `config/trading_policy.yaml` (operator-owned, PR-only), and
`_ROUND_TRIP_COST_BPS_FLOOR` in `auto_approve.py` enforces the same numbers as
a floor. **Lowering a number in the YAML has no effect** — only raising one
does. Replacing these with a measured rate means adding fee columns to the
kis_live / live ledgers first; that is a separate change.

Both legs are charged at the full round-trip rate against the larger leg, which
overstates the cost. That is the direction that narrows the test.

## 5. Atomic dispatch recheck (#1836 / ROB-1238)

Auto-approved rungs are **not** on a shortcut path. Eligibility runs inside
`_revalidate_place_rung`, and every rung it clears then continues through the
same sequence as a Telegram-approved rung:

```
_apply_eligibility_gate        revalidation.py:1149   <- auto-approve verdict
_pre_mutation_window_gate      revalidation.py:1160
buying-power claim             revalidation.py:1171
_pre_mutation_window_gate      revalidation.py:1225
_pre_submit_lifecycle_gate     revalidation.py:1241   <- ROB-1238 atomic recheck
place_order(dry_run=False)     revalidation.py:1267
```

`_pre_submit_lifecycle_gate` re-reads the proposal row `FOR UPDATE` and holds
that lock across the submit, rechecking terminal state, `no_resubmit`,
`valid_until`, the approval token and the loss-guard verdict. There is no code
path on which an auto-approved rung reaches the broker without it.

## 6. Arming it (operator)

Not part of the introducing change. When the time comes:

1. Confirm `ORDER_PROPOSALS_AUTO_APPROVE=true` is already the intended state —
   the mode is subordinate to it.
2. Set `ORDER_PROPOSALS_AUTO_APPROVE_MODE=expanded` for one account lane and
   watch `source_asof.auto_approved.eligibility[]` on the resulting proposals:
   every rung records its reason and, for sells, `gross_pnl`,
   `round_trip_cost` and `net_pnl`.
3. Roll back by setting the mode back to `off`. Orders already submitted are
   unaffected — cancel those through the veto button or
   `order_proposal_cancel`.
