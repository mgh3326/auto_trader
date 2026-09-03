# Auto-approve eligibility expansion (§40차, §141차, §156차, §163차, §170차)

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

§163차 adds a US cash-parking allowlist (`SGOV`, `BIL`) on `kis_live` /
`equity_us`; §170차 adds `459580` and `357870` on `kis_live` / `equity_kr`.
These are the only marketable **buy** releases, and the only places where the
per-order cap takes a different *value*.

🔴 **The per-order cap is RAISED, not removed.** §106차's "maximum loss of one
automation error" boundary keeps its shape: the check still runs on every
parking rung, against **USD 10,000** instead of the ordinary US USD 1,500. A
parking order above that is rejected exactly as any other over-cap order is.

A parking rung is therefore bounded **twice**, and neither boundary is the
only one:

| | boundary | scope |
| --- | --- | --- |
| 1st | per-order **USD 10,000** (raised) | every parking rung, both sides |
| 2nd | cumulative parking exposure **USD 10,000** | parking **buys**, across orders |

The second is measured from the broker balance **plus the same-day durable
record of already-auto-approved parking buys**, and fails closed. Both halves
of that measurement are load-bearing — the balance alone cannot see an
accepted-but-unfilled order, and a balance-only cap re-meters the next
proposal from zero. The cumulative cap has known residual gaps (§8.7); the
per-order cap above is what keeps each of them bounded **per order**. Full
scope, what it does not touch, and its real limits are in §8.

Ships inert. `ORDER_PROPOSALS_AUTO_APPROVE_MODE` defaults to `off`, which is
ROB-871 behaviour unchanged. Arming it is an operator decision and is not part
of the change that introduced this document. §163차 adds no environment
variable and no default change: the allowlist is live exactly when `expanded`
mode is.

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
unchanged. Per-order and daily caps remain hard gates for every non-parking
rung; §S170 excludes only its exact enabled parking subset. The sole
marketable profit-sell path evaluates its applicable cap notional at
`max(limit_price, current_price) × quantity` as described above.

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

## 8. The cash-parking ticker allowlist (§163차)

Operator decision, 2026-08-28. Cash parking instruments are not what the
anti-chase rules were written to protect against: an ultra-short Treasury ETF
has essentially no intraday price risk, so "rest below the market" buys nothing
and only costs a Telegram tap on money that is being parked, not invested.

### 8.1 Scope — exactly this, and nothing adjacent

| dimension | value |
| --- | --- |
| symbols | `SGOV`, `BIL` — closed `frozenset`, hardcoded in `app/services/order_proposals/parking_allowlist.py` |
| account mode × market | `kis_live` × `equity_us` **only** |
| mode | `expanded` only. `off` never reaches the branch. |
| marketability | released on the **buy** side only |
| per-order cap | 🔴 **raised** USD 1,500 → **USD 10,000**, and still enforced — this is a value change, not a removal |
| 2nd boundary | cumulative parking exposure ≤ **USD 10,000**, buy side, where exposure = broker balance **+** same-day auto-approved parking buys (see §8.3) |
| shared daily cap (§S170) | 🔴 **excluded on both buy and sell** for this explicitly enabled scope in `expanded` mode: it neither blocks the parking rung nor consumes budget for a later ordinary order |

Neither constant is a settings key, an environment variable, or a
`config/trading_policy.yaml` entry — §163차 adds **no** policy key, which
`tests/schemas/test_trading_policy_schema.py` re-proves against the frozen
auto-approve keyset. There is no setter and no loader. Widening the allowlist
or raising either cap is an operator PR editing that one module. The raised
per-order cap and the cumulative cap are **separate named constants**
(`PARKING_PER_ORDER_CAP_USD`, `PARKING_CUMULATIVE_CAP_USD`) so that changing
one can never silently move the other.

🔴 **Guard strength, stated honestly: accidental prevention + static detection,
not structural impossibility** (the BL-4 / NHPLUG framing).
`test_allowlist_module_reads_no_settings_env_db_or_policy` asserts the module's
import surface (stdlib + `app.core.symbol`) and rejects direct references to
settings, `os`/`getenv`/`environ`, `open`, the policy loader and the DB session
factory, and it catches the plain `importlib` / `getattr` spellings. It does
**not** defeat a determined obfuscation — a string-assembled `__import__`
passes it — and it is deliberately not hardened further, because enumerating
spellings is a losing game. What it buys is that re-introducing configurability
*the obvious way* turns red in CI. The real boundary is that this file is
operator-PR-only, like the policy document.

### 8.2 Why only exact `kis_live` market tuples

The cumulative cap is only meaningful if parking exposure can be read back at
all. `kis_live` has the existing read surface for each authorized market:
`equity_us` uses `KISClient.fetch_my_us_stocks` → `ovrs_stck_evlu_amt` (USD),
while `equity_kr` uses `KISClient.fetch_my_stocks` → `evlu_amt` (KRW).
`toss_live` is veto-capable in principle behind
`ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED` for both `equity_us` and `equity_kr`,
but its exposure lives on a different broker surface. Metering a KIS balance to
authorize a Toss order would be a wrong-account cap, which is worse than no
parking treatment at all. No `toss_live` tuple is authorized, so Toss parking
orders keep the ordinary gates. A proposal outside an exact KIS
symbol×account×market tuple gets nothing.

🔴 **This is an account *label*, not a proven account identity — do not read
more into it than the code does.** Both halves of the measurement are scoped
by the `account_mode` / `market` / `broker_account_id` labels carried on the
proposal and by whichever credentials `KISClient()` happens to resolve; the
repo does not prove that the balance it read and the account the order will
reach are the same physical account. Two `kis_live` proposals carrying
*different* `broker_account_id` labels are metered as separate books by the
durable half while the broker balance is whatever the ambient credentials
return. That is **BL-37**, tracked as backlog and deliberately not fixed here.
What contains it is the scope-native per-order cap in §8.1: whatever the
labelling does, one automation error remains bounded per order.

### 8.3 The measurement — two halves, both load-bearing

`app/services/order_proposals/parking_exposure.py` produces the exposure
figure. It is **not** the broker balance alone.

**Half 1 — broker balance.** Sums `ovrs_stck_evlu_amt` over the allowlisted
rows of one fresh KIS overseas balance (`currency_code="USD"`, so no FX
conversion enters the comparison — the US per-order and daily caps are USD
too). Same client and balance surface `order_validation._get_holdings_for_order`
already uses for the avg-cost loss guard: no new credential, host or account.
A row that *declares* a currency other than USD fails closed.

**🔴 Half 2 — same-day auto-approved parking buys.**
`KISAccount._filter_nonzero_holdings` keeps only rows with
`ovrs_cblc_qty > 0`, so an order that was auto-approved and **sent but has not
filled** has no balance row at all. Measured from the balance alone, two
separate USD 10,000 SGOV proposals both see zero exposure and both clear —
USD 20,000 against a USD 10,000 cap. `OrderProposalsService
.auto_approved_parking_notional` closes that window, reusing the KST-day
window, advisory lock and row filter of the already-vetted
`auto_approved_daily_notional`. It counts parking **buys** only (a sell reduces
exposure) and uses the lenient exposure normalizer, so a parking buy
auto-approved under the *ordinary* gates — a small resting SGOV rung that never
touched the parking treatment — still counts, because it creates the same real
exposure.

**Double counting is deliberate.** Once a parking buy fills it is in the
balance *and* still inside the same-day durable window, so it counts twice
until the KST day rolls over. De-duplicating would mean matching fills back to
the orders that produced them, and an error in *that* matching restores the
under-count this exists to prevent. Over-counting only tightens the cap (it can
refuse an auto-approval that would have been allowed — a Telegram tap).
Under-counting submits money that was never authorized.

**Why a session cannot forge it.** A proposal contributes exactly **one** input
to the whole §163차 path — the symbol — and that same symbol is what the order
is submitted against, so claiming `SGOV` to obtain the parking treatment submits a real
`SGOV` order. There is no proposer-supplied notional, position, valuation or
exposure field anywhere in the computation; the durable half reads the same
vetted per-rung cap measure the daily circuit breaker reads.

Metering uses `max(limit_price, current_price) × quantity`, never the possibly
discounted limit: a parking buy is *allowed* to be marketable, so the limit
price is no longer an upper bound on what it can spend.

### 8.4 Fail-closed

Every one of these rejects the auto-approval and produces a human card:

| situation | recorded reason |
| --- | --- |
| exposure not supplied to the classifier at all | `parking_exposure_unavailable` / `not_supplied` |
| broker read raised or timed out | `… / fetch_failed` |
| payload is not a list | `… / payload_not_a_list` |
| a row is not a mapping | `… / row_not_a_mapping` |
| a row's symbol is absent or unreadable | `… / symbol_unreadable` |
| an allowlisted row has no evaluation amount | `… / evaluation_amount_missing` |
| the amount is unparseable or non-finite | `… / evaluation_amount_invalid` |
| the amount is negative | `… / evaluation_amount_negative` |
| a row declares a currency that is not USD | `… / currency_not_usd` |
| no durable reader was supplied (balance-only is the broken measure) | `… / durable_reader_missing` |
| the durable read raised or timed out | `… / durable_read_failed` |
| the durable read returned a missing/unparseable/negative value | `… / durable_notional_invalid` |
| projected exposure > USD 10,000 | `parking_cap_exceeded` |

`parking_exposure` defaults to `None` on the classifier, and `None` is the
fail-closed value — a caller that forgets to supply it cannot clear the
cumulative boundary.

### 8.5 Symbol matching is deliberately asymmetric

Two different rules, and the asymmetry is the safety property.

**Eligibility (grants the parking treatment) — strict.** The proposal symbol must
*already be* the exact canonical ticker: exact built-in `str` (not a subclass,
whose `strip`/`upper` could lie), pure ASCII, uppercase, no surrounding
whitespace, no separator that `to_db_symbol` would rewrite. `sgov`, `" SGOV "`,
`SGOV.U`, `SGOVX`, `BILS`, `BILL`, `BIL.TO`, `SG`, fullwidth `ＳＧＯＶ`, and
Cyrillic/long-s look-alikes are all rejected. Membership is exact-element
against a `frozenset` — never a prefix, suffix or substring test.

The ASCII gate is load-bearing, not decoration: `str.upper()` folds U+017F
LATIN SMALL LETTER LONG S onto `"S"`, so `"ſGOV".upper() == "SGOV"` and a
naive uppercase-then-compare matcher would admit it. Rejecting non-ASCII before
any case operation closes that whole class.

Over-strictness here is safe in the only direction that matters: a rejected
spelling does not make some *other* instrument allowlisted, it makes this
proposal fall back to the ordinary gates and cost a Telegram tap. 🔴 The
operational consequence is real, though: **a parking proposal must carry the
ticker as exactly `SGOV` or `BIL`.** The proposal service stores the symbol
verbatim, so a lowercase proposal will silently just be carded.

**Exposure (counts against the cap) — lenient.** Broker balance rows are
normalized for case and whitespace, because over-inclusion can only *raise*
measured exposure and reject a buy, while under-inclusion would understate it
and let one through. Non-ASCII rows are still rejected so an unrelated holding
cannot inflate parking exposure.

### 8.6 What §163차/§S170 did **not** authorize

Proven unchanged by test, for parking rungs as for every other:

* `loss_cut` and every other exit intent,
* the `policy_deviation` tag scan,
* the veto-capable account/market allowlist and the Toss freeze,
* `order_type == "limit"`, the fresh-preview requirement,
* the **per-order cap itself** — its value is raised for parking, the check is
  not removed (`test_single_parking_order_over_the_raised_cap_is_rejected`),
  and for every non-allowlisted US ticker the value is still USD 1,500
  (`test_non_allowlisted_us_ticker_keeps_the_ordinary_1500_cap`),
* the sell-side **break-even band and round-trip-cost profit proof** — a
  break-even `SGOV` sell still goes to a human. §163차 raises the per-order cap
  and releases marketability on the buy side, **not** the profit proof,
* the mandatory veto thesis,
* every `off`-mode verdict, and every verdict for every non-allowlisted symbol.

🔴 **§S170's sole change beyond the existing parking treatment is precise:** an
enabled parking scope in `expanded` mode is excluded from the shared daily-cap
**decision and durable contribution sum**, on **both buy and sell**. It uses
the identical strict `parking_scope` predicate that grants the parking
treatment; `SGOVX`, another market, another account mode, `off` mode, a
legacy row with no writer marker, or malformed durable evidence all remain
charged. This is not a policy/YAML/env setting: each `ParkingScope` carries a
closed `daily_cap_exempt` switch sourced from the closed
`PARKING_DAILY_CAP_EXEMPT_US` / `PARKING_DAILY_CAP_EXEMPT_KR` literals. Both
ship as `true`; narrowing one market later is a one-line operator code change.

### 8.7 Known limits — read these before trusting the cap

🔴 **Read this list against §8.1 first.** Every item below is a limit on the
*cumulative* (2nd) boundary. None of them is unbounded, because the per-order
cap in §8.1 still runs: whatever these gaps allow, a single automation error
on a parking ticker is capped at **USD 10,000 per order**. That is precisely
why the per-order cap was raised rather than removed.

Three of them are tracked as backlog and are deliberately **not** fixed here:

| id | limit |
| --- | --- |
| **BL-37** | account *labelling*, not proven account identity — see §8.2 |
| **BL-38** | no pre-submit reservation: the measurement is not atomic with the send |
| **BL-39** | the durable half is a current-KST-day window |

BL-38 is not new to parking — the daily circuit breaker has the same shape.

* 🔴 **BL-39 — the durable half is scoped to the current KST day, and that
  boundary cuts through the US session.** A parking buy auto-approved before
  the KST-day boundary that is still unfilled and still absent from the balance
  is not counted.

  An earlier version of this runbook justified the window with ROB-671 ("US day
  orders do not survive the session"). 🔴 **That justification is withdrawn —
  it does not describe what this window does.** KST midnight is 15:00 UTC,
  while XNYS regular hours are 13:30–20:00 UTC (EDT) / 14:30–21:00 UTC (EST),
  so KST midnight falls *in the middle of* the US session. Measured: a durable
  USD 10,000 row recorded at 23:00 KST reads back as exactly 0 from
  `auto_approved_parking_notional()` at 00:01 KST, while the same XNYS session
  is still open (`allowed_now == True` at both instants) and the order is still
  live.

  The window is a deliberate reuse of the daily circuit breaker's already-vetted
  KST-day scoping, **not** something the market calendar guarantees. This is
  the sharpest remaining edge of the cumulative cap, and it is bounded per
  order by §8.1.
* 🔴 **An allowlisted position the broker does not return, and that is not in
  today's durable record, is counted as zero.** "Not held" and "not in this
  response" are indistinguishable. The durable half removes the large, routine
  case of this (an accepted-but-unfilled order placed today); what remains is
  the cross-day case above and any genuine broker omission.
* 🔴 **BL-38 — the measurement and the submit are not atomic.** Both halves are read
  once per dispatch, before revalidation; a fill or a concurrent dispatch on
  another process landing between the read and the submit is not reflected.
  The durable read takes the same per-account/day advisory lock as the daily
  circuit breaker, which serializes concurrent dispatches for the same account
  inside a transaction, but it is the same TOCTOU shape as the daily-notional
  accumulator that ships today — not a distributed guarantee.
* **Double counting until the KST day rolls over** is deliberate and tightens
  the cap; see §8.3 for why that direction was chosen.
* 🔴 **A parking buy can fill before the veto card is visible.** That is what
  "marketable" means and it is the accepted cost of the release. §40차 safety
  invariant ① (the veto button is honest) holds only post-hoc for this path,
  exactly as it already does for §156's proven-profit marketable sell.
* A limit buy priced *strictly above* the market never reaches the classifier
  at all: `order_validation._preview_buy` rejects it (`Buy price … exceeds
  current price …`) and the rung fails `preview_guard_failed`. In practice the
  §163차 buy release therefore admits the at-market case (`limit == current`),
  not an above-market chase.
* 🔴 **The USD row-currency guard is a guard, not a verified fact.** The
  request pins `TR_CRCY_CD=USD`; if a row *declares* a different currency the
  sum fails closed. Whether KIS actually emits a per-row currency key on an
  overseas balance is **unverified here** — this repo holds no captured sample
  proving it either way — so the guard is deliberately inert when no row
  declares one, rather than failing closed on normal traffic.
* Parking **sells** get the same raised per-order cap (USD 10,000) and the
  same §S170 daily-cap exclusion. The cumulative cap remains buy-side per the
  operator decision, because a sell reduces exposure; the sell-side profit,
  break-even, exit-intent, tag, veto-account and thesis gates are unchanged.
  🔴 **A parking sell has no remaining daily aggregate boundary.** Its only
  amount/eligibility boundaries are the immutable per-order cap, available held
  quantity, and the existing `take_profit` proof; it does not receive a
  cumulative-parking cap because that cap is deliberately buy-side only.

### 8.8 KR extension (§170차) and daily-cap exclusion finalization (§S170)

The two §170차 additions are these closed authorization tuples:

| symbol | account mode × market | currency | per-order cap | cumulative buy cap |
| --- | --- | --- | --- | --- |
| `459580` KODEX CD금리액티브(합성) | `kis_live` × `equity_kr` | KRW | 10,000,000 | 15,000,000 |
| `357870` TIGER CD금리투자KIS(합성) | `kis_live` × `equity_kr` | KRW | 10,000,000 | 15,000,000 |

🔴 **The shared daily cap does not apply to either KR tuple's enabled
`expanded`-mode parking orders, on either side.** A KR parking order therefore
does not consume the ordinary KRW 5,000,000 daily budget and cannot prevent a
later market-exposure-changing auto approval. It remains bounded by its
KRW 10,000,000 per-order cap, and parking buys remain bounded by the shared
KRW 15,000,000 cumulative parking cap.

The code represents every parking authorization as one immutable tuple of
symbol, account mode, market, currency, cap pair, and native KIS balance
fields. It does **not** maintain independent symbol and market sets: `SGOV` /
`BIL` on `equity_kr`, and `459580` or `357870` on `equity_us`, are not parking
rungs and retain every ordinary gate. Tests assert all three rejections;
removing the tuple binding makes the assertions red.

KR cumulative exposure is the existing KIS domestic balance's `pdno` +
`evlu_amt` for **both** KR parking symbols, plus the same KST-day durable sum
of auto-approved KR parking buys. The two symbols share **one** KRW 15,000,000
cumulative buy cap; they do not receive one cap each, so the effective shared
boundary is not KRW 30,000,000. A balance of `459580` KRW 9,000,000 plus
`357870` KRW 5,000,000 measures as KRW 14,000,000 from either KR scope;
likewise, same-day durable buys of KRW 6,000,000 plus KRW 5,000,000 measure as
KRW 11,000,000. Thus a KRW 2,000,000 `357870` buy against a held KRW
14,000,000 `459580` is rejected as `parking_cap_exceeded` against the single
KRW 15,000,000 cap. The durable reader is required exactly as it is for US, so
an accepted-but-unfilled KR buy cannot reset the next proposal's measurement to
zero. A missing balance or durable read is fail-closed. The comparison is
native KRW only; neither this reader nor the cap selector performs FX
conversion, and a declared non-KRW balance-row currency is rejected.

The observed liquidity is asymmetric. From the same 매일경제TV 증권 source and
the same displayed session (2026-08-28 15:30 KST), `459580` showed KRW 454.50bn
turnover and a KRW 5 spread (0.047bp midpoint), whereas `357870` showed KRW
8.85bn turnover and the same KRW 5 spread (0.862bp midpoint): the latter has
about 51.4× less turnover and about 18.5× the relative spread. They are not
equivalent by turnover or relative spread. Separately, the immutable KRW 10m
per-order cap is about 0.113% of the observed `357870` turnover; this is not a
claim that the two products have equivalent liquidity. Source URLs:
<https://mbnmoney.mbn.co.kr/stock/item?code=459580> and
<https://mbnmoney.mbn.co.kr/stock/item?code=357870>.

Both `459580` and `357870` are marked `nxt_eligible=false` in the referenced
universe data. Neither can use NXT; for an ordinary regular-session sell there
is no NXT extension and the exit window closes at 15:30 KST. This is an
execution-window fact for operators, not a new eligibility rule or a reason
added to the gate.

### 8.9 Toss KR extension (§S174) — a separate account meter

The same two KR symbols are additionally authorized only for the following
separate Toss surface:

| symbol | account mode × market | balance provider | currency | per-order cap | cumulative buy cap |
| --- | --- | --- | --- | --- | --- |
| `459580` | `toss_live` × `equity_kr` | `TossReadClient.holdings()` | KRW | 10,000,000 | 15,000,000 |
| `357870` | `toss_live` × `equity_kr` | `TossReadClient.holdings()` | KRW | 10,000,000 | 15,000,000 |

This is not a tuple-only expansion. Each immutable `ParkingScope` binds its
`balance_provider` together with symbol, account mode, market, native fields,
and cap pair. `toss_kr_holdings` reads only typed `TossReadClient.holdings()`
data, projects `symbol` and `market_value.amount`, and requires each counted
row to declare `currency == KRW` and `market_country == KR`. There is no FX
conversion. A non-KRW/null/unreadable value, non-KR market, malformed payload,
or broker failure returns an unavailable exposure and produces the ordinary
human card. The reader neither calls nor imports Toss place/modify/cancel
methods.

🔴 **Account identity is a hard precondition.** `TossReadClient.from_settings()`
selects `settings.toss_api_account_seq`; it does not accept the proposal's
`broker_account_id`. Before any Toss HTTP read, the parking meter therefore
requires `broker_account_id` to be the exact canonical decimal string of that
positive configured sequence. Null, whitespace/leading-zero variants, opaque
IDs, a missing setting, and mismatches all fail closed as
`account_identity_unavailable`. The implementation does not guess an
account-number-to-sequence mapping. This makes the balance half and the
durable `WHERE account_mode AND market AND broker_account_id` half refer to
the same Toss scope; Toss and KIS rows remain disjoint even if their textual
account labels happen to match.

The shared daily cap is excluded for these exact enabled `expanded`-mode Toss
KR tuples on both buy and sell, just as for the existing KIS parking tuples.
The immutable KRW 10,000,000 per-order boundary and the shared Toss-only KRW
15,000,000 cumulative-buy boundary remain in force. The KIS KR and Toss KR
faces are deliberately independent, so their accepted aggregate is **KRW
30,000,000** (KIS KRW 15,000,000 + Toss KRW 15,000,000), never one blended
ledger. Operator decision, 2026-09-01: this aggregate is accepted and recorded
because the parking instruments are cash-equivalents with zero market exposure;
it remains appropriate to the cap's purpose of bounding one automation error.
`toss_live × equity_us` is deliberately absent: its USD valuation contract is
not authorized here.

#### Critical execution warning: no-card does not mean filled

S174 can remove the Telegram card for an otherwise eligible Toss KR parking
order; it does **not** prove that the order will fill. `app/mcp_server/tick_size.py`
currently has no KRX ETF branch and applies the KRX stock tick table. For
`459580` at an ask of KRW 1,073,480, it produces a KRW 1,000 tick and floors a
buy limit to KRW 1,073,000 — KRW 480 below the ask. The order can therefore
remain an unfilled limit order. This is intentionally not changed by S174:
correcting ETF ticks affects all KRX ETF orders and requires a separate review.

### 8.10 Toss US extension (§173) — a separate USD account meter

The two US parking symbols are additionally authorized on a separate Toss
surface. This is a new exception with the same immutable USD controls already
used by the KIS US face; no cap value or policy key is changed:

| symbol | account mode × market | currency | per-order cap | cumulative buy cap | daily cap |
| --- | --- | --- | --- | --- | --- |
| `SGOV` | `toss_live` × `equity_us` | USD | 10,000 | 10,000 | exempt |
| `BIL` | `toss_live` × `equity_us` | USD | 10,000 | 10,000 | exempt |

Each closed scope binds `balance_provider=toss_us_holdings`,
`balance_symbol_field=symbol`, `balance_evaluation_field=market_value.amount`,
`balance_currency_field=currency`, `balance_market_field=market_country`, and
`balance_market_value=US`. `_select_balance_fetcher` maps this provider to the
same read-only `TossReadClient.holdings()` method used by `toss_kr_holdings`,
but keeps a separate provider key because the provider binding is also an
exact `(account_mode, market, currency)` contract. The response may contain KR
and US rows together; only native `currency == USD` and
`market_country == US` rows are projected into the US meter. `market_value.amount`
is consumed directly as USD and is not FX-converted.

🔴 **Account identity is a hard precondition.** Before the Toss holdings read,
the parking exposure path requires `broker_account_id` to be the exact
canonical decimal string of `settings.toss_api_account_seq`. Missing,
non-canonical, or mismatched values fail closed as
`account_identity_unavailable`; no KIS balance is substituted. The US Toss
face therefore cannot charge a KIS balance or a different Toss account's cap.

The Toss US face and the KIS US face are deliberately independent meters. Each
can consume its own USD 10,000 cumulative-buy cap, so the accepted US aggregate
upper bound is **USD 20,000** (KIS USD 10,000 + Toss USD 10,000), not one shared
USD 10,000 pool. This is the same per-face structure accepted for the two KR
faces in §S174. The existing daily-cap exclusion applies through the exact
expanded parking marker; no scheduler or automatic trigger is added.

The native-USD interpretation is supported by today's live observation of
three Toss holdings rows (SGOV, IVV, and NVDA) where quantity × last price
matched `market_value.amount` with zero arithmetic error. That is an observed
wire-value fact, not a Toss API documentation guarantee; the field contract
must be revalidated if Toss changes its representation. BL-37 account
labelling, BL-38 missing pre-submit reservation, and BL-39 KST-day-window
residuals remain unchanged and are exposed on this additional face. A parking
buy can also fill before a veto card under the existing `marketable`
definition; that accepted risk is unchanged.

### 8.11 §S170 aggregate-limit change; BL-38/BL-39 mechanism unchanged

The one thing §S170 removes is the **shared daily aggregate ceiling for
successfully durable-recorded parking rungs**. That is intentional: parking is
cash-equivalent and its own immutable per-order and cumulative boundaries are
the applicable controls; successful parking must not crowd out later
market-exposure-changing auto approvals.

For a parking **sell**, this removes the only aggregate limit: no daily total
or cumulative-parking cap remains. The immutable per-order cap, available held
quantity, and existing `take_profit` proof remain required.

The mechanism of the known cumulative-cap limits is unchanged, but their
effective binding amount is not symmetric by market:

* **BL-38:** an order accepted by the broker but missing from
  `source_asof.auto_approved` is absent from both the parking durable half and
  the ordinary daily-cap reader today. §S170 does not make that pre-existing
  missing-record case newly unbounded.
* **BL-39:** both readers use the same current-KST-day window. Their reset at
  KST midnight therefore occurs together; the daily cap was never a backstop
  for the US intra-session reset.

For **US**, the prior USD 20,000 daily cap was already looser than the
USD 10,000 cumulative parking-buy cap. For the pre-existing **KIS KR face**,
the prior KRW 5,000,000 daily cap was the binding gate below its KRW 15,000,000
cumulative parking-buy cap. §S170 moves that one face from **KRW 5,000,000 to
KRW 15,000,000**; the **3×** BL-37/BL-39 statement applies only to that
per-face comparison. S174 adds a second, independently metered Toss KR face at
KRW 15,000,000, so the accepted KR aggregate is KRW 30,000,000 across the two
faces, not a single KRW 15,000,000 cap and not a claim that KRW 30,000,000 is
3× the old KIS limit. This is an intentional aggregate-limit change, not a
claim that the BL-38/BL-39 mechanisms are eliminated or mechanically worsened.

The daily-cap reader excludes only a post-§S170 writer marker for the exact
`expanded`-mode parking scope. Legacy rows, off-mode rows, and malformed
markers continue to count, which is conservative during rollout. No
per-order-cap value, cumulative-cap value, currency keying, roster, broker
surface, scheduler, or other auto-approval gate changes here.
