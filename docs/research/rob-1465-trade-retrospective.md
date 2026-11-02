# ROB-1465: real-data dots export, trade retrospective, and live canary design

2026-11-02. Operator decision: the strategies should reach live trading soon, some loss is
acceptable, and dots gets the real data. This document carries the numbers, the canary
design the operator asked for, and the request to dots.

Every number below is computed from the files named in [1](#1-the-real-data-export) and
[3](#3-retrospective). Symbols are masked here because this repository is public; the full
version, with symbols and amounts, is in `~/work/herdr-artifacts/rob-1465-trade-retro/`
and in the Drive folder. Nothing here is a recommendation to buy a security; the rules
and their evidence are described, not securities.

## 1. The real-data export

The ROB-1438 tool gained a real-value mode (`--real-values`, schema
`dots-history-export/real-v1`). The operator's 2026-11-02 decision allows real symbols,
order ids, labels and account suffixes; credentials and full account numbers stay out.
Implementation and contract: `app/services/dots_history_export/`,
`scripts/export_dots_history.py`, `docs/runbooks/dots-history-export.md` §Real-value mode.

- Drive: **https://drive.google.com/drive/folders/1xYcTDCcNgx4oS_N0PN_OKEE_UoWA8bM3**
  (`auto_trader/dots/2026-11-02-real`).
- Window: `[2026-05-01T00:00:00Z, 2026-11-03T00:00:00Z)`, `end: exclusive`.
- Code commit `5a3c0535569b1f008eaf26db810061e5861ee93c`, `code_dirty: false`.
- Snapshot read at 2026-11-02 05:44Z (production moves between runs; a later export is a
  later snapshot, not a corrected one).
- Read: one `REPEATABLE READ READ ONLY` transaction against production
  (production database, read-only). No broker call, no write.
- `manifest.json` sha256 `ca85915241e68ae1478783e30e99aa5e251276a8c72ddacb6558089d36cde286`.
- Leak check: passed on the rows and again on the written files. Rules kept:
  `account_number`, `bearer`, `credential_assignment`, `jwt`, `token_prefix`, plus the
  operator's deny-values list (6 account identifiers read from `.env.prod`, never printed).

| Table | Rows | CSV bytes | CSV sha256 (first 16) | Parquet bytes | Parquet sha256 (first 16) |
| --- | ---: | ---: | --- | ---: | --- |
| `account_balance_daily` | 7 | 938 | `afc7505f14c5a8cf` | 4178 | `04f0420aa54fd6b6` |
| `alpaca_paper_order_ledger` | 45 | 16866 | `b3d3bbc488bcc7a7` | 18005 | `f657eed2c7cbc558` |
| `execution_ledger` | 3682 | 1424029 | `bb36ad39086e451a` | 340742 | `665f96b1ad660377` |
| `kis_live_order_ledger` | 251 | 117445 | `9bf402cf45a9bca6` | 56399 | `bd00975f9f246b09` |
| `kis_mock_order_ledger` | 76 | 26839 | `f9700616783c8b80` | 22573 | `8c7bb344e31ed86c` |
| `live_order_ledger` | 437 | 241126 | `b8b72e06ac142b11` | 104123 | `a028ec568e0267d0` |
| `order_proposal_rungs` | 1458 | 638769 | `27681bcbb66aecce` | 287624 | `bde1b36c3cf8cd56` |
| `order_proposals` | 1440 | 651118 | `88b5f6a1a0443dea` | 267047 | `2afac79a85abea82` |
| `paper_pending_orders` | 58 | 22994 | `ed7224d66c94a61e` | 18805 | `caab43269832c3b8` |
| `paper_trades` | 35 | 14356 | `f792b25c9c15cffa` | 16105 | `030b40043e8c3933` |
| `toss_live_order_ledger` | 1129 | 623255 | `b84622d1abf1415f` | 277820 | `21defd57314620db` |
| **total** | **8618** | | | | |

The full sha256 of every file is in the appendix. `account_balance_daily` is 7 rows
because only the DB paper simulator has a daily cash table; the seven broker accounts
carry `available = false` markers.

### New columns that answer the ROB-1459 `[요청]`

| Column | Table | Meaning |
| --- | --- | --- |
| `lot_ref`, `lot_link_kind`, `lot_opened_at_utc`, `lot_cost_basis`, `lot_basis_kind`, `linked_fill_ref` | `execution_ledger` | FIFO lot walk. `opens_lot` / `adds_to_lot` / `partial_sell` / `full_sell` / `unlinked`. `lot_basis_kind = period_start_seed` restarts the lot at an opening import, which is the period-start cost basis. The import is 45 rows on 2026-05-10 (KR 14, US 20, crypto 11) and 6 KR rows on 2026-09-29; it is not a single cutover and its date is when the position was imported, not when it was acquired. |
| `first_fill_at_utc`, `first_fill_at_local` | every order table | First in-effect fill, next to the existing last-fill time. |
| `reference_price`, `reference_price_asof_utc`, `reference_price_source`, `distance_above_reference_pct` | `order_proposal_rungs` | For crypto sells: the last completed UTC daily close before the order was created (`public.crypto_candles_1d`) and the limit's distance above it. |

Reference-price coverage: every crypto sell rung in the window has a
`reference_price_source` (§3.3).

### Drive visibility

This host has no Drive MCP server configured (only `auto_trader`, `brew`, `sentry`,
`linear`), so the "Drive MCP search" step could not be run as written. Visibility was
confirmed through the Drive sync client instead: the folder is indexed as
`1xYcTDCcNgx4oS_N0PN_OKEE_UoWA8bM3` under the `dots` folder
`18S25Tg8exd8Zog0KYZUQLmvF9Wm2Et8F`, with all 23 files registered as children. That is
the same Drive tree the link points at.

## 2. Method and data limits

**Sources.** The export above (orders, fills, proposals, rungs, lots) and the ROB-1439
price snapshot `~/work/herdr-artifacts/rob-1439-trade-retro/prices-2026-11-01/`
(`trade-retro-prices/v1`, 2026-03-22 to 2026-11-01, public daily bars plus
`benchmark/{kr,us,crypto}.csv`). The analysis script is
`~/work/herdr-artifacts/rob-1465-trade-retro/analyze.py`; its output is `numbers.json` in
the same directory.

**Fills.** `execution_ledger.csv`, `superseded_duplicate = false` only, live accounts only
(`kis_live`, `toss_live`, `upbit_live`), quantity from `incremental_qty`, price from
`fill_price`. Seeds (`is_seed = true`) set a lot, they are not executions.

**How the two duplicate sources are reconciled.** One execution can be written twice: the
websocket monitor sees the exchange notice and the reconciler sees the broker's order row.
Three rules keep the quantity right, and the export's lot walk follows exactly the rows
they leave:

1. A `websocket` row is provisional. If any authoritative row (`reconciler` or
   `manual_import`) shares its order identity — broker, account mode, instrument, symbol,
   side, currency, normalized order number and, for KIS, the trading day — the websocket
   row is `superseded_duplicate = true` and is not counted (ROB-1379).
2. A cumulative writer (KIS reconciler rows, Upbit aggregate rows with ledger `fill_seq` 0)
   contributes `incremental_qty`: the rise over the same source's earlier high-water mark
   for that order, so a re-reported running total is never counted twice. A repeated total
   adds 0; a dip adds nothing.
3. A non-seed `manual_import` row has no defined quantity basis, so it carries no increment
   and does not enter the walk. A `SEED-` row is a whole position and resets the lot.

The export's lot columns are therefore computed **on the non-superseded rows only**, and a
superseded row carries `lot_link_kind = unlinked` with empty lot columns. A sell that is
larger than the lots the window can see is also `unlinked`, so the overhang is countable
rather than folded into a lot it does not fit.

**Price screen.** Following ROB-1439, a fill priced more than 30% away from that day's
close is not treated as a trade (a legacy or mis-mapped row). 3 of 2164 live fills were
dropped this way; the largest was a KR buy at 25% of the day's close. Without the screen
that single lot showed a +130% unrealised gain that is not real.

**Positions.** FIFO per (account, market, symbol), rebuilt from the fills; a seed resets
the lot. A round trip is a lot from open to fully closed; a lot still open at
2026-10-30 (KR/US) or 2026-10-31 (crypto) is marked at that day's close.

**What is not measured.**
- Sells whose lot is not in the export: **238 sells**. That is 13.7% of KR sells, 48.4%
  of US sells and 43.1% of crypto sells by count — positions opened before 2026-05-01 or
  covered by a missing fill. Their realised P&L is not in the totals.
- Broker cash per day: only DB paper has a balance table, so cash-weighted returns and
  NAV-based drawdown use the ROB-1439 lower-bound method, not measured cash.
- Fees and taxes: not in the export. KR/US slippage is measured against the limit, not
  against a fee schedule.
- FX: KRW and USD are never summed. The committed tables use returns on cost, so a KRW
  loss and a USD gain cannot be added by accident; the private report shows both
  currencies separately.

**Regime.** Index return over the same window: KR **-17.39%** (2026-05-04 to 2026-10-30),
US **+4.84%** (2026-05-01 to 2026-10-30), crypto **-9.89%** (2026-05-01 to 2026-10-31),
from `benchmark/*.csv` first and last bar in the window.

## 3. Retrospective

### 3.1 Per market

`execution_ledger.lot_*` and `order_*_ledger.terminal_outcome`, live accounts only.

| Market | Closed lots | Open lots | Realised P&L, % of closed cost | Unrealised P&L, % of open cost | Mean lot return | Mean excess vs index (same holding period) | Mean hold, days |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| KR | 30 | 40 | +11.54% | -21.78% | -8.44% | +13.94pp | 95.8 |
| US | 9 | 3 | +17.54% | +0.07% | +11.19% | +10.25pp | 67.1 |
| Crypto | 13 | 1 | +17.80% | -4.35% | -1.34% | +4.73pp | 55.6 |

The absolute amounts are in the private full report (§7). The percentages answer the same
question without publishing the book's size.

Three things follow from the columns, not from an opinion:

- **KR is the whole problem.** The closed book earns +11.5% on its cost, but the open
  book is -21.8% on its cost, and it is the larger book (40 lots against 30). In money
  terms the open loss more than offsets every closed gain. That is the signature of taking
  gains and holding losses.
- **The excess column is not a victory.** KR's +13.9pp is measured against a falling
  index; the absolute mean lot return is still negative. A market-neutral read is in
  §3.2, where the rule families that bought strength versus weakness separate.
- **US is positive on both books** with only 12 lots, so it is the one market where the
  current style has evidence, and it is thin evidence.

### 3.2 Per rule family

Strategy tags are now plain text. Long free-text thesis tags are grouped as *session
thesis*; the named rules are the ASCII tags the tooling writes.

Means here are **lot-weighted** (a family's lots, not the average of its tags' averages), so
they add up to the market table in §3.1. An unweighted mean of tag means is a different
statistic and is not used.

| Market | Rule family | Closed | Open | Lot-weighted mean return | Lot-weighted mean excess |
| --- | --- | ---: | ---: | ---: | ---: |
| KR | session thesis (free text) | 29 | 37 | -8.55% | +14.32pp |
| KR | `underwater_support_net` (averaging-down tier) | 0 | 1 | -41.43% | -17.86pp |
| KR | cash parking | 0 | 2 | +0.02% | +16.40pp |
| KR | `pullback_starter_ladder` | 1 | 0 | +15.27% | +16.07pp |
| US | session thesis (free text) | 8 | 1 | +12.82% | +11.42pp |
| US | `oversold_quality_kis_deploy` | 1 | 0 | +18.56% | +16.40pp |
| US | cash parking | 0 | 2 | +0.16% | +1.91pp |
| Crypto | session thesis (free text) | 13 | 1 | -1.34% | +4.73pp |

KR's one `underwater_support_net` lot is -41.4% and 17.9pp behind the index. That is the
only named averaging-down position with a measurable cost basis, and it is the rule the
operator decided to suspend on 2026-11-02.

### 3.3 Fill rate

`terminal_outcome` over submitted live orders (not `not_submitted`). A `partial` counts as
filled.

| Market | Submitted | Filled | Fill rate | Excluding `operation_kind = cancel` |
| --- | ---: | ---: | ---: | ---: |
| KR | 938 | 188 | 20.04% | 188 / 917 = 20.50% |
| US | 723 | 129 | 17.84% | 129 / 720 = 17.92% |
| Crypto | 156 | 45 | 28.85% | 45 / 156 = 28.85% |

A cancel is not an intent to buy, so the right-hand column is the closer denominator. It is
still not one row per order: a Toss replace chain adds a row per operation, and that is not
normalized here.

The rule that dominates the submitted count is the TRIM/resting sell family
(`sell.trim_preplace*`), which is a resting limit by design, so the blended rate is not a
statement about buy style. Named buy cohorts with n ≥ 10:

| Market | Tag | Submitted | Fill rate |
| --- | --- | ---: | ---: |
| KR | `underwater_support_net` | 101 (filled 1, open 53, expired 47) | 0.99% |
| KR | `discovery_new_entry_mean_reversion` | 15 | 0.00% |
| US | (untagged buys) | 72 | 9.72% |

`underwater_support_net` fills 1 of 101 in KR. That is the tier's own problem: the deep
support clause asks for a price the market rarely prints.

**Crypto sell ladder, by distance above the reference close.** All 131 crypto sell rungs in
the window have `reference_price_source = crypto_candles_1d_close`; the median distance is
**+0.90%** above the last completed close. Rung `state` is the authoritative ending
(`order_proposal_rungs.csv`, `state` and `filled_qty`).

| Band | Rungs | Filled | Fill rate | Resting | Cancelled | Unverified | Superseded |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| below +1% | 66 | 52 | 78.8% | 11 | 0 | 0 | 3 |
| +1% to +3% | 27 | 18 | 66.7% | 7 | 1 | 1 | 0 |
| +3% to +8% | 26 | 19 | 73.1% | 3 | 1 | 2 | 1 |
| +8% to +16% | 5 | 2 | 40.0% | 0 | 1 | 2 | 0 |
| +16% or more | 7 | 2 | 28.6% | 0 | 1 | 4 | 0 |

The columns add to the rung count in every band (66 = 52+11+3, 26 = 19+3+1+2+1). A
`superseded` rung is one the proposal replaced; it is not a fill and not an open order.

The +8% and above bands fill at 28.6–40.0% against 66.7–78.8% below +8%, which is the same
direction ROB-1458 measured (44.4% against 70.4%) and supports giving those rungs an
expiry. `unverified` means the ledger never recorded an ending; it is not proof of a live
order.

### 3.4 Slippage against the intended price

`(fill_price / limit_price - 1) × 100`, live orders that had both.

| Market | Buy n | Mean buy slippage | Sell n | Mean sell slippage |
| --- | ---: | ---: | ---: | ---: |
| KR | 84 | -0.126% | 103 | +0.052% |
| US | 25 | -0.081% | 104 | +0.024% |
| Crypto | 11 | 0.000% | 34 | 0.000% |

Buys fill slightly below their limit and sells slightly above, which is what a maker-style
limit should do. Crypto shows 0.000% because the crypto sells are limit orders that filled
at their limit. This is not a fee measurement.

### 3.5 Five best and five worst closed trades

Round trips (a lot from open to closed), ranked by return. Symbols are masked.

| Rank | Market | Account | Lot | Return | Hold | What the row shows |
| ---: | --- | --- | --- | ---: | ---: | --- |
| 1 | US | kis_live | US-1 | +48.81% | 99d | A resting buy filled, then a trim sell closed it. Entry was 20% below the last close of its first week. |
| 2 | Crypto | upbit_live | CR-1 | +28.70% | 131d | A period-start seed lot, closed by a ladder sell. The seed carries the cutover cost basis. |
| 3 | KR | kis_live | KR-1 | +28.70% | 89d | A near-market buy, closed by a trim at a resistance rung. |
| 4 | US | toss_live | US-2 | +26.88% | 64d | A rotation limit that filled at 37 and was sold into strength. |
| 5 | Crypto | upbit_live | CR-2 | +25.16% | 105d | A period-start seed lot closed by a ladder rung. |
| 1 worst | Crypto | upbit_live | CR-3 | -75.40% | 57d | A 794-unit altcoin lot bought in one fill and sold 57 days later after the token fell three quarters. No stop existed. |
| 2 worst | Crypto | upbit_live | CR-4 | -13.32% | 58d | A seed lot closed by a trim during a drawdown. |
| 3 worst | US | kis_live | US-3 | -10.06% | 6d | Bought and sold within a week; the exit was a trim, not a stop. |
| 4 worst | Crypto | upbit_live | CR-5 | -8.27% | 40d | A 40-unit lot bought at a fixed ladder price and exited lower. |
| 5 worst | KR | toss_live | KR-2 | -6.83% | 90d | A one-share add at a fib support, sold 90 days later below entry. |

The asymmetry is the finding: the worst closed trade is -75.4% while the best is +48.8%,
and the worst losses are not closed at all — the five largest open losses are -42.2%,
-36.1%, -58.5%, -52.4% and -43.5% (KR, `lot_cost_basis` against the 2026-10-30 close).

### 3.6 Hold time

Mean hold by market: KR 95.8 days, US 67.1 days, Crypto 55.6 days (§3.1). Open KR lots
average 129–137 days (the five largest open losses in §3.5). Nothing in the data closes a
KR loser on a rule; the closes are profits taken at a resistance rung.

## 4. Live canary design

The operator adopted the ROB-1458 recommendations on 2026-11-02 (decision doc item 1) and
set the stop values under item 3. Each rule runs as a mock/shadow arm and, in parallel, a
small live canary. Every money amount is a placeholder the operator sets; no amount is
chosen here.

Common to all arms:

- The shadow/mock arm records the decision and the counterfactual and places no order.
- The live canary is a separate, labelled reason code on the account so its fills can be
  separated from the main book (`strategy_tag` carries the canary name).
- The pre-registered metric, horizon, scale-up rule and stop rule are fixed before the
  first canary order; changing any of them starts a new epoch with a new label.
- A canary that hits its stop rule is switched off by the operator, not by the model.

### 4.1 US: near-market entry instead of deep limits

| | |
| --- | --- |
| Shadow/mock | Every US buy intent gets a shadow twin filled at the next open at the same notional. |
| Live canary | A pre-declared 1-in-N subset of US buy intents is placed at the touch instead of deep support, same notional cap. |
| Pre-registered metric | Intent-level D+20 return net of the US index over the same dates; comparison is paired by intent. Fill rate as the secondary metric. |
| Verdict horizon | 50 matured intents or 60 US sessions, whichever is later. |
| Scale-up rule | Near-market leads on the paired D+20 median **and** its fill rate is at least 2× the deep-limit arm at that horizon. Scale in one step to the full US book. |
| Stop rule | Cumulative canary P&L worse than `<loss budget per strategy>`, **or** paired D+20 median worse than the deep-limit arm by more than 5pp at n ≥ 20. |

### 4.2 US: mechanical 50% add at the first close ≤ -8%

| | |
| --- | --- |
| Shadow/mock | Log every first close ≤ -8% against a held US lot, with the would-be add price. No order. |
| Live canary | One add per lot per episode, 50% of the held quantity, capped at `<add size cap>`; never more than `<max adds per day>`. |
| Pre-registered metric | D+20 return of the added tranche against the same quantity not added, index-neutral. |
| Verdict horizon | 20 matured adds or 60 US sessions. |
| Scale-up rule | D+20 median ≥ 0 and lower quartile > -8% (the tier's own rule) at n ≥ 20. |
| Stop rule | Canary tranche P&L worse than `<loss budget per strategy>`, **or** lower quartile ≤ -15% at n ≥ 10. |

### 4.3 KR: averaging-down tier suspension, with a -20% cut shadow

The operator kept the tier suspended and made the -20% cut shadow-only (items 1 and 3), so
the live arm starts at zero and only opens after the shadow gate passes.

| | |
| --- | --- |
| Shadow/mock | Log every first close ≤ -20% against a held KR lot. Compute cut-vs-actual at D+20 and to the last bar, notional-weighted. No order. |
| Live canary | Off until the shadow gate passes. Then one mechanical cut per episode, at the next open, on a pre-declared subset. |
| Pre-registered metric | Cut-minus-actual, notional-weighted, at D+20 and to the end of the window; share of episodes where the cut beat the actual. |
| Verdict horizon | 20 touched episodes with 60 KR sessions matured. |
| Gate to start the live arm | The cut beats the actual outcome in more than half of episodes **and** the notional-weighted difference is > 0. |
| Scale-up rule | Same test at 40 episodes; if it holds, the cut applies to the whole KR book. |
| Stop rule | Cut-minus-actual < 0 at n ≥ 20, **or** realised canary loss worse than `<loss budget per strategy>`. |

### 4.4 KR and US: parking only cash that will stay idle

| | |
| --- | --- |
| Shadow/mock | Record days held and the net return after fees for every parking lot, against the account's cash rate. |
| Live canary | Sweep only cash above 10% of NAV that is expected to stay idle longer than the measured break-even hold (`<break-even days by account>`). |
| Pre-registered metric | Median lot return net of fees minus the account's cash rate. |
| Verdict horizon | 10 closed parking lots. |
| Scale-up rule | Median > 0 → allow the full sweep rule and raise the sweep share to `<parking sweep share>`. |
| Stop rule | Median < 0 at 10 lots, **or** any single lot loses more than `<parking loss budget>`. |

### 4.5 Crypto: an expiry on resting sells at +8% or more

| | |
| --- | --- |
| Shadow/mock | For every resting crypto sell, record the reference close at creation (`reference_price`), the band, the days to fill, and what selling at the next open would have produced. |
| Live canary | Resting sells in the +8% and above band get an expiry (`<ladder expiry days>`); below +8% is unchanged. |
| Pre-registered metric | Per band: fill rate, median days to fill, and rest-vs-sell-now. |
| Verdict horizon | 30 sells per band, or 60 days. |
| Scale-up rule | If the +8% band's rest-vs-sell-now is negative, the expiry becomes the permanent rule for that band. |
| Stop rule | Expiry causes a relative loss worse than `<loss budget per strategy>` against the no-expiry counterfactual. |

### 4.6 Crypto: B3 rule set (mock only first)

| | |
| --- | --- |
| Shadow/mock | B3 on the accounts' own signals: near-market entry, add 1 unit at -10% and -20% (cap 2 adds), exit all at +10% over average cost, stop at -35% from average cost. No live order. |
| Live canary | Opens only after the mock verdict, then at `<canary notional per signal>`. |
| Pre-registered metric | Return per unit against the crypto index per unit over the same dates; max drawdown of the mock book. |
| Verdict horizon | 60 crypto sessions or 30 completed round trips. |
| Scale-up rule | Return per unit beats the index per unit **and** max drawdown is better than `<max drawdown cap>`. |
| Stop rule | Max drawdown worse than `<max drawdown cap>`, **or** any single trade worse than -50%. |

### 4.7 Budget placeholders

The operator sets these; no amount is proposed here.

| Parameter | Meaning |
| --- | --- |
| `<total canary budget>` | Capital across every live canary arm, total. |
| `<loss budget per strategy>` | The realised loss that switches one arm off. |
| `<add size cap>` | Largest single averaging-down add. |
| `<max adds per day>` | Add orders per day across the canary. |
| `<break-even days by account>` | Minimum expected idle days before parking. |
| `<parking sweep share>` | Share of excess cash the sweep may use. |
| `<ladder expiry days>` | Expiry for resting sells at +8% or more. |
| `<canary notional per signal>` | Size of one B3 live signal. |
| `<max drawdown cap>` | B3 drawdown limit. |

## 5. What worked, what lost money, and the ROB-1458 rules

**Worked.**
- US rotation and near-market entries: 9 closed lots, +17.5% realised on cost, the open
  book flat (+0.07%), +11.19% mean lot return against a +4.84% index. Supports the ROB-1458 recommendation to
  buy near the market in US.
- US averaging-down cohort: ROB-1458 measured 12 systematic adds at D+20 median +7.0%,
  Q1 +2.4% (PASS). This window's realised US adds are small but positive and do not
  contradict it.
- Crypto ladder sells: the reference close is now real for all 131 rungs, and the fill rate
  falls from 78.8% below +1% to 28.6% at +16% or more (§3.3).
- Parking: every parking lot is non-negative and two KR lots are +0.02% against a -17.4%
  index. Small, consistent with ROB-1458's "long-idle cash only".

**Lost money.**
- KR open book: -21.8% on its cost, five lots between -36% and -58%, 119–137 days old.
  Nothing closed them. This supports ROB-1458's KR -20% cut finding and the operator's
  decision to keep the tier suspended.
- KR `underwater_support_net`: 1 filled lot at -41.4%, 17.9pp behind the index, and 0.99%
  fill rate on 101 orders. Supports retiring the tier in KR.
- Crypto single-shot altcoin lots: the -75.4% trade and the -8.3% AVAX lot are both
  one-decision lots with no stop. Contradicts "hold and wait" for crypto and supports B3's
  stop-at-35% idea (as a mock first).

**Each finding against the ROB-1458 rule it supports or contradicts.**

| Finding (§) | ROB-1458 row | Relation |
| --- | --- | --- |
| KR open book -21.8% on cost, losers held 119–137 days (§3.1, §3.5) | KR cut at -20% | Supports: a mechanical cut would have removed part of this book. |
| KR `underwater_support_net` 1 fill / 101 orders, -41.4% (§3.2, §3.3) | KR averaging-down tier: suspend | Supports the suspension. |
| US 9 closed lots +17.5% realised, +10.25pp excess (§3.1) | US averaging down: keep | Consistent; sample is small (n=9 lots, one add cohort). |
| KR deep-limit buy fill rate 20.1% blended, 0.99% for the tier (§3.3) | KR keep deep limits | Does not contradict; the tier's own clause is the binding constraint. |
| Crypto -75.4% single-shot lot, no stop (§3.5) | Crypto B3 stop at -35% (mock) | Supports running B3 as a mock before live. |
| Parking lots 0.02–0.19% positive (§3.2) | Parking: long-idle cash only | Consistent; the closed-lot test is still unmet (0 closed). |
| Crypto fill rate 28.6% at +16% vs 78.8% below +1% (§3.3); sells fill at the limit (§3.4) | Crypto ladder +8% expiry | Supports the expiry: the far bands rarely fill and price is not the lever. |

**Not supported by this data.** Any claim that the strategies are ready for full live
sizing: 238 sells have no cost basis, the KR sample is one falling regime, and the US book
has 12 lots.

## 6. Request to dots

Linear issue **ROB-1466** — https://linear.app/mgh3326/issue/ROB-1466/at-rob-1465-real-data-dots-export-trade-retrospective-and-live-canary

It asks dots to (a) redo the ROB-1459 checks with the real data, (b) red-team §3, and
(c) red-team §4, and tells dots to analyse actively, mark unverifiable values `미확인`, and
ask with `[요청]` rather than stopping. The issue links the Drive folder and this document.

## 7. Reproduce

The private full report — symbols, quantities, amounts, one row per lot — is at
`~/work/herdr-artifacts/rob-1465-trade-retro/report-full-rob1465.md` and in Drive at
`auto_trader/dots/2026-11-02-real-report/`. It is deliberately outside the export folder,
because `manifest.json` vouches for exactly the export's files.

```bash
# export (read-only production, no salt in real mode)
uv run python -m scripts.export_dots_history \
  --database-url-env DOTS_EXPORT_DATABASE_URL --real-values \
  --since 2026-05-01 --until 2026-11-03 \
  --deny-values-file ~/.config/auto_trader/dots_deny_values \
  --out ~/dots-exports/2026-11-02-real

# retrospective numbers
python3 ~/work/herdr-artifacts/rob-1465-trade-retro/analyze.py \
  ~/dots-exports/2026-11-02-real \
  ~/work/herdr-artifacts/rob-1439-trade-retro/prices-2026-11-01 \
  ~/work/herdr-artifacts/rob-1465-trade-retro/numbers.json
```

Tests for the new mode: `uv run pytest tests/services/dots_history_export -m unit`.

## 8. Review response (ROB-1466)

dots found a real defect in the first export: the lot walk ran over every in-window fill
and only then marked `superseded_duplicate`, so a reader who excluded the duplicates got
cost bases that had already absorbed the duplicate's price. 1,441 of 1,887 comparable rows
disagreed with an independent non-superseded replay, and 9 sells exceeded the quantity
left after de-duplication.

**Fixed in the export** (`app/services/dots_history_export/projection.py`): the walk now
receives the non-superseded rows only; a superseded row carries `lot_link_kind = unlinked`
and empty lot columns; a sell larger than the visible lots is also `unlinked`. A test
(`test_a_superseded_fill_never_moves_the_lot_cost_basis`) plants a websocket duplicate at
a different price and asserts the lot keeps the authoritative price — with the old walk it
would be the average of the two. Two more tests cover the over-sized sell and the rule that
no active row links to a superseded one.

**Re-export and effect.** The export was re-run read-only at commit `5a3c0535`; §1 and the
appendix are its numbers. Comparing the two exports row by row (same account, symbol, fill
time, side, quantity and order ref): **1,470 of 2,164 active rows changed cost basis, 694
did not**, no active row links to a superseded row (was 22), and 146 active rows are
`unlinked` and countable.

**The retrospective numbers did not change.** `analyze.py` reads `execution_ledger.csv`,
filters `superseded_duplicate = false` and rebuilds FIFO itself; it never consumed the
export's lot columns. That answers dots' open question: the first report's P&L was not
computed from the defective columns. The §3 figures are byte-identical after the re-export.

**Other corrections taken from the review.**

- Seeds are not one 2026-09-29 cutover: 45 rows are dated 2026-05-10 (KR 14, US 20, crypto
  11) and 6 KR rows 2026-09-29. The date is the import date, not the acquisition date.
- The ladder table now lists `superseded` rungs, so each band's columns add to its rung
  count.
- The fill-rate table adds the denominator with `operation_kind = cancel` removed, because
  a cancel is not a buy intent.
- §3.2 means are now explicitly lot-weighted, so they add up to §3.1. The earlier table was
  an unweighted mean of tag means, which is why dots could not reproduce it from the full
  report.

**Still open** (from the same review, not fixed here): fees, taxes, distributions, FX and
real cash balances are absent, so a net-of-cost round trip cannot be computed; 238 sells
have no lot in the window; D+20 is not mature for the two `underwater_support_net` fills;
a rung's `reference_price` is the previous UTC daily close, not a contemporaneous quote;
and the ledger's own `filled_qty` disagrees with the canonical first fill for 64 of the 93
filled rungs, so rung state and ledger quantity must not be mixed. The canary design in
§4 was not changed in this pass; the review's objections to its stop rule, shadow/live
interference and small-sample scale-up remain open for the operator.

## Appendix: full sha256 of every exported file

`manifest.json` itself: `ca85915241e68ae1478783e30e99aa5e251276a8c72ddacb6558089d36cde286`

| File | Bytes | sha256 |
| --- | ---: | --- |
| `account_balance_daily.csv` | 938 | `afc7505f14c5a8cf547f1f81ea501bb1383160ab1e8ae82a6a6b3b1d8b277961` |
| `account_balance_daily.parquet` | 4178 | `04f0420aa54fd6b68382f77372bafc0f9e3a12b7b5710be1da84ca8ff208196c` |
| `alpaca_paper_order_ledger.csv` | 16866 | `b3d3bbc488bcc7a7e6b1aadcdb0cc834aeef18d193c1541631dd329ead265840` |
| `alpaca_paper_order_ledger.parquet` | 18005 | `f657eed2c7cbc558bb7052f51efe86570d6ab591a9df14b0e31198b65904b6fb` |
| `execution_ledger.csv` | 1424029 | `bb36ad39086e451a448860f3601cec5b89c29b35da272aefb58170d460fe5a5e` |
| `execution_ledger.parquet` | 340742 | `665f96b1ad6603773353dc08b275c76487d62797c39fde69f147f1b0c8a3bfc1` |
| `kis_live_order_ledger.csv` | 117445 | `9bf402cf45a9bca6d6c2f7a16e96e3e80caea2a65f590bd0c1c96008a4f98b1f` |
| `kis_live_order_ledger.parquet` | 56399 | `bd00975f9f246b09f3f9ebb93d0dbb5783489370a58c59a10fcd6aa57c913182` |
| `kis_mock_order_ledger.csv` | 26839 | `f9700616783c8b801924a33035c57b1f470649e4a5f75608880a011c703a521d` |
| `kis_mock_order_ledger.parquet` | 22573 | `8c7bb344e31ed86c5faed27ba4f6b5ede113d4a967b78b874291ff30d46fb23a` |
| `live_order_ledger.csv` | 241126 | `b8b72e06ac142b111aad9738fd954f085f780c8d0b94e8b61ff2c0ad5889b8b1` |
| `live_order_ledger.parquet` | 104123 | `a028ec568e0267d024a9afa22c1a28ef64126d0eab03ce05227626473c2081b7` |
| `order_proposal_rungs.csv` | 638769 | `27681bcbb66aecce188733bde4e917956ddd468d8d42981574035f172237d1c4` |
| `order_proposal_rungs.parquet` | 287624 | `bde1b36c3cf8cd564ddb2b1b54e4d8726d635d94a13b969a35c2564a33f015c6` |
| `order_proposals.csv` | 651118 | `88b5f6a1a0443deac36c46e59269badfb1bd6fd7ff8811d7a24f85892d0330f0` |
| `order_proposals.parquet` | 267047 | `2afac79a85abea82a8fcfede93e4584514bfc91320c4f162177ea2833adddcdf` |
| `paper_pending_orders.csv` | 22994 | `ed7224d66c94a61e0fdf4a62df8bde397c0d54848faeff7c897c57227e7306a4` |
| `paper_pending_orders.parquet` | 18805 | `caab43269832c3b8105633ff0e94b4dd2a8dbf2975b5fcb9bd13e73725d7dc9a` |
| `paper_trades.csv` | 14356 | `f792b25c9c15cffa6ee2b2f35a496decb06cc1c2bec84583ceef43859bcc4448` |
| `paper_trades.parquet` | 16105 | `030b40043e8c3933670bd6b556dce96a986b9bca1a5219744e8c47b482298490` |
| `toss_live_order_ledger.csv` | 623255 | `b84622d1abf1415f3023da4b60ee536aebb6a7c262e6ba7b2e1da0c4d19e4361` |
| `toss_live_order_ledger.parquet` | 277820 | `21defd57314620db29c1abfa7100bdfb0db00d8b01202a94b5ed11807c9b89c7` |
