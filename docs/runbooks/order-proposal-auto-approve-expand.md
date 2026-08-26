# Auto-approve eligibility expansion (§40차, §141차, §156차)

Extends ROB-871 resting-class auto-approval with the §40차 classification:
**auto-submit, then veto**, for buys and for *proven* profit-take sells.
§141차 additionally admits `replace` and `cancel` proposals to the same lane
(§7) without relaxing any gate.

§156차 removes `table_disagreement` from the approval-blocking tag set only.
The tag remains safely auditable, including existing rejection evidence. Its
final scope addendum also permits one narrow marketable exception: an
`expanded`-mode **limit sell** whose fresh broker preview is classified
`classify_sell_profit(...)=take_profit`. This is an objective, fee-netted
predicate — not a self-described tier. Every marketable buy, loss sell,
break-even-band sell, and unclassifiable sell remains manual.

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

1. `action` outside `{place, replace, cancel}` → `action_not_supported`
   (§141차; before it, every non-`place` action was rejected as
   `action_not_place` — retired, see §7)
2. `replace`/`cancel` with absent or self-inconsistent target evidence →
   `target_evidence_missing` (§141차)
3. `order_type != limit` → `order_type_not_limit` (**not applied to `cancel`**,
   which places no order)
4. `exit_intent == "loss_cut"` → **`loss_cut_intent`**
5. any other `exit_intent` → `exit_intent_present`
6. account/market not in `_VETO_CAPABLE_ACCOUNT_MARKETS` → `account_not_veto_capable`
7. `policy_deviation` tag → **`approval_required_tag`**
8. *(`cancel` is decided here — see §7. Everything below prices an order, and a
   cancel places none.)*
9. preview did not succeed → `preview_guard_failed`
10. missing/non-positive price or quantity → `price_or_quantity_missing`
11. per-order cap → `per_order_cap_exceeded`
12. daily cap → `daily_cap_exceeded`

Then, per mode:

| | `off` | `expanded` |
| --- | --- | --- |
| buy | limit ≤ market × (1 − `min_distance_pct`) | limit **<** market |
| sell | limit ≥ market × (1 + `min_distance_pct`) | proven profit-take; it may be marketable only under §156's narrow exception |

In `expanded`, a buy priced exactly *on* the market is marketable and rejected.
A sell exactly on or below the market is also marketable, but may proceed only
when the proven-profit test below passes. `off` keeps ROB-871's non-strict
boundary (a rung exactly `min_distance_pct` away stays eligible), so it never
inherits the exception.

For the one §156 marketable `take_profit` sell, both cap checks meter
`max(limit_price, current_price) × quantity`: a limit below the fresh current
price cannot make an immediately executable sell look smaller than it is. The
cap observation persisted with the auto approval carries that same number, so
later dispatches use it in their daily total. Resting sells, buys, and every
`off`-mode rung retain the established `limit_price × quantity` basis.

"Proven profit-take" (`classify_sell_profit`), evaluated in this order:

1. **no cost basis, no verdict.** A missing, zero or unparseable
   `avg_buy_price` → `sell_classification_unavailable`, ahead of both tests
   below — an unpriceable sell never reports a band or a P&L verdict. The
   preview's own avg×1.01 guard fails *open* on unknown cost basis and is
   bypassable (`defensive_trim` / `loss_cut` / mock), so a passing preview is
   not evidence of profit and `expanded` proves it independently.
2. **break-even band before the sign test.**
   `|limit − avg_buy_price| ≤ avg_buy_price × breakeven_band_pct`
   → `breakeven_band`. Ahead of the sign test on purpose: §40차 sends the band
   to a human *whatever the sign*, so a sell 0.5% above cost is still a human's
   call. The band is inclusive — exactly ±1% is inside it.

   > 🔴 **§142차 (2026-08-23) — the inclusive edge is a producer problem, not a
   > classifier problem.** Two sell tiers anchor at `average_cost × 1.01`
   > (`sell.loss_guard_min_multiple`), which *is* this edge when
   > `breakeven_band_pct = 1`. Whenever `average_cost × 1.01` already sits on the
   > market tick grid the ceil snap is a no-op and the rung lands exactly on the
   > boundary, so it is classified `breakeven_band` and can never be auto-approved
   > (measured: avg 4,000,000 KRW → 4,040,000 → `eligible=False
   > reason='breakeven_band'`). The repair moved the **rungs**:
   > `decision_rules.sell.trim_preplace` now declares an effective anchor of
   > `max(tick_ceil(raw anchor), first valid tick strictly above the band edge)`
   > for `breakeven_extension_ladder` rung 1 and for the
   > `sell.breakeven_reserve_trim` post-max anchor. **This comparison stayed
   > inclusive on purpose** — changing `<=` to `<` here would silently re-classify
   > every other consumer of the band and was explicitly out of scope. If you are
   > adding a new sell producer, clear the edge on the producer side.
3. **net P&L strictly positive.**
   `net = (limit − avg) × qty − max(limit, avg) × qty × round_trip_cost_bps / 10000`.
   `net > 0` → `take_profit`; `net == 0` → `expected_pnl_not_positive`. Exactly
   zero is not "> 0".

`table_disagreement` is retained by `auto_approve_audit.py` as an audit
vocabulary, not an eligibility gate. This preserves historical rows and
externally supplied stored audit rows without claiming the classifier creates
new `table_disagreement` matches or silently dropping existing evidence from
the read model.

## 3. §156's deliberate marketable profit-sell exception

A marketable order can fill before the operator sees the veto card. The default
therefore remains conservative: all marketable buys are `marketable_not_resting`.
The operator explicitly accepted the post-hoc-veto trade-off for one case only:
a `limit` sell in `expanded` mode whose fresh preview proves
`classify_sell_profit(...)=take_profit`. That classifier requires all of:

1. an available, positive `avg_buy_price`;
2. a limit outside the inclusive ±`breakeven_band_pct` band; and
3. strictly positive P&L after the conservative full round-trip cost charge
   (using a more pessimistic preview `realized_pnl` when present).

It therefore implies `limit_price > avg_buy_price`; the superficially looser
`limit_price >= avg_buy_price` comparison is not independently wired. At
`limit == avg`, the sell is in the inclusive band and remains manual. A sell
just outside the band whose fee-netted P&L is zero or negative is also manual.
`loss_cut`, every other exit intent, `policy_deviation`, account/market
allowlists, thesis/card rendering, and the existing loss-sell guard remain
unchanged. Per-order and daily caps remain hard gates; the sole marketable
profit-sell path evaluates their notional at `max(limit_price, current_price) ×
quantity` as described above.

The cost basis is not a caller/session field: `order_proposal_create` accepts
no preview. On the approval click, `_revalidate_place_rung` calls the internal
`_default_place_order_fn(dry_run=True)` and passes that fresh response straight
to the classifier. KIS maps its just-read broker holdings `pchs_avg_pric`,
Upbit maps its just-read account `avg_buy_price`, and Toss maps the just-read
`holdings().average_purchase_price`. Missing or malformed cost basis yields
`sell_classification_unavailable`; it never clears the exception.

This deliberately makes the veto post-hoc for an objective, proven-profit
marketable sell only. It is an operator decision, not a general relaxation of
the veto invariant; do not extend it to buys, market orders, tier-shaped JSON,
or any non-`take_profit` verdict.

The tag gate (step 7) also applies in `off` mode. It can only reject, so it is
a tightening of ROB-871, not a widening.

## 4. Fee rate provenance

`round_trip_cost_bps` is **not a measured rate**, and there is currently no way
to make it one from inside this repo: the two default-active veto-capable
ledgers (`review.kis_live_order_ledger`, `review.live_order_ledger`) have no
`commission`/`tax` columns, so no realized fee evidence exists for `kis_live`
or `upbit`. `review.toss_live_order_ledger` records commission and tax;
`toss_live` remains non-veto-capable by default and is eligible only after the
separate, default-off TOSS-AUTO-FULL gate and its acceptance procedure.

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

These generic gates do not arm Toss. The separately default-off
`ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED` gate, terminal-evidence acceptance,
and rollback sequence are operator-only and documented in
`docs/runbooks/toss-auto-acceptance.md`.

## 7. `replace` and `cancel` (§141차)

Until §141차 the classifier rejected every non-`place` action outright
(`action_not_place`), so a cancel or a replace always cost a Telegram tap no
matter how ordinary it was. Measured friction with no matching safety
contribution: the 08-20 ETH batch-cancel card and the 08-23 SOL dead-target
replace card. **That one exclusion is gone. Nothing it stood in front of moved.**

### `replace` — the whole `place` stack, including §156's narrow exception

A replacement rung *is* a new order, so it runs every gate in §2 plus the mode
table: caps, `min_distance_pct` in `off` mode, break-even band, and
round-trip-cost profit proof for sells. Its only marketable release is the same
§156 `take_profit` exception; nothing is skipped or loosened because the rung
happens to replace something.

One ordering change makes that possible: `revalidation._revalidate_replace_rung`
used to call the eligibility gate *before* the dry-run preview with an empty
`preview={}`, which was safe only because the classifier rejected
`action_not_place` before it ever read `preview`. The gate now runs **after** the
preview (still a `dry_run`, still strictly before the cancel leg) so it sees the
`current_price` and `avg_buy_price` a `place` gets. Handing it `{}` would demote
every replace to `preview_guard_failed` — the categorical exclusion by another
name.

### `cancel` — ownership + tag scan, no amount gates

A cancel places no order, so the amount and marketability gates have no subject.
It clears on gates 1–7 of §2 (notably: still blocked by `loss_cut`, by any
`exit_intent`, by a non-veto-capable account/market, and by the tag scan) plus:

* **Ownership.** The authoritative proof is `revalidation._validate_target_action`,
  which reads the order id back out of *this account's* broker order history and
  diffs it against the approved snapshot — it runs before the gate, and an order
  this account cannot read never reaches `BROKER_CANCEL`. The classifier adds a
  pure second check (`target_evidence_missing`) that the evidence exists and its
  snapshot names the same order id.
* **Zero budget.** A cancel reduces exposure and buys nothing, so it consumes no
  daily cap: the decision reports `notional=0` and an unchanged
  `daily_notional_after`, and `repository.auto_approved_notional_between` excludes
  `action='cancel'` groups from the KST-day sum. Without that exclusion the cap
  would be charged twice for the same order — once when it was placed, again when
  it was pulled — because a cancel proposal's rung mirrors the *target* order's
  price and quantity.

### The cancel notification is a receipt, not a card

An auto-approved cancel is published **without a veto button**, as
`✅ 자동 취소됨`. A veto cancels the order the proposal just put on the book; a
cancel proposal put none there, and the order it retired cannot be un-retired.
A button that reports `🛑 취소됨` while doing nothing reads as a successful undo.

This is also structural, not stylistic: cancelling the last rung drives the
group to `lifecycle_state="terminal"`, and the approval-card machinery
fail-closes on exactly that — `set_approval_nonce` raises
`proposal_terminal:terminal`, and `finish_approval_dispatch` would flag a
published card with no nonce as `approval_dispatch_snapshot_missing`. Those
guards are right. The receipt therefore skips them entirely: no nonce, no
binding, no `order_proposal_approval_dispatch_attempts` row, delivery outcome
reported straight from the publication. The auto-approval decision itself is
still durable — `record_auto_approval` stamps `source_asof.auto_approved`
before any Telegram I/O, and a failed send still runs the compensation branch
and records `record_auto_notification_failure`.

`replace` keeps its veto button (`✅ 자동 정정 접수됨`): its replacement rung is
live and is exactly what a veto is for.

### What did not change

* `loss_cut` two-stage approval, `approval_required_tag`, the nonce/idempotency
  system, and the §40차 classification inputs.
* The manual Telegram-click path (`eligibility_gate=None`) — unaffected in every
  branch.
* `action_not_place` remains in `auto_approve_audit._KNOWN_REASON_CODES` as a
  **read-only legacy code**. It is never emitted again, but rows carrying it are
  already durable in `source_asof`; dropping it would silently rewrite that
  history to `invalid_reason_code`.
