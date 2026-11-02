# ROB-1458: evidence for adjusting the live buy and sell rules

This committed version holds ratios, counts and masked account labels only. Symbols, amounts and account names are left out because the repository is public and the export is private trading data. The full version is outside the repository.

It states measured facts and an options table for the operator's policy decision. It recommends no security and no amount. Every number is in-sample: it is computed on the same six months any rule would be judged on, so it is a candidate to test, not a forecast.

## Reproduce

```bash
uv run python -m research.strategy_evidence.run report --export ~/work/herdr-artifacts/dots-history-2026-11-01 --prices ~/work/herdr-artifacts/rob-1439-trade-retro/prices-2026-11-01 --dividends ~/work/herdr-artifacts/rob-1458-strategy-evidence/dividends-2026-11-02 --public-out docs/research/rob-1458-strategy-evidence-2026-11-02.md --private-dir ~/work/herdr-artifacts/rob-1458-strategy-evidence
```

Add `--check` to compare both reports with a fresh computation byte for byte, without writing. It exits 3, writing nothing, if any input differs from its manifest. The dividends snapshot was made once with the command below, which uses the network and refuses a directory that already holds one.

```bash
uv run python -m research.strategy_evidence.run fetch-dividends --out <new directory>
```

- Export: `dots-history-export/v1`, window 2026-05-01 to 2026-11-01, `manifest.json` sha256 `9de029eb656814dcf4fa9efe6279609c3586f58a283cc30fc6e94259d1f44424`, every file checked against it, not committed.
- Prices: the ROB-1439 snapshot, `prices_manifest.json` sha256 `0cf9b5b9ea978c8ec3c46989590a9608fabbbf4f4f2ec0fe44ca75dfd8b26dd1`, not committed.
- Dividends: `dividends_manifest.json` sha256 `398485935868b0d02d61a43d829939bf54c6d2352212d0ec8d88839affcee472` (Yahoo Finance distributions of the parking instruments), not committed.
- Code: `research/strategy_evidence/`, on top of `research/trade_retro/`, whose canonical fills, orders, positions and account cash this pack reuses unchanged. Where ROB-1439 already measured something, this pack cites it and does not measure it again.

**The regime.** Index return over the export window, first to last bar: KR -17.4% (2026-05-04 to 2026-10-30); US +4.8% (2026-05-01 to 2026-10-30); Crypto -9.9% (2026-05-01 to 2026-10-31). KR is one falling market, so any long rule loses there and any rule that holds cash or cuts early looks good. Read every KR number with that in mind.

## Options table

Each row is an option, not a recommendation. The rows use thresholds picked by role: -8% (the tier's floor), -20% (a middle value) and -40% (today's `sell.loss_cut` depth), and from-scratch variants A2 (tight cut, wide target), A4 (loose cut, wide target) and B3 (capped averaging down with a stop). The picks were made after the full grids had been computed once, so they are not blind; every other threshold and variant is in the full tables below. `Q1` is the lower quartile. P&L effects are on the real book, as a share of average NAV, cash shares are the equal-weight mean of the market's accounts, and drawdowns are of account NAV (the worst account of the market).

Whether a row reads as a change or as keeping today's rule follows a test fixed in the code, not a row-by-row reading. The tests were written alongside these results, so they are not blind either: averaging down becomes a stop when both of its cohorts fail the tier's own rule, and a mechanical add when at least one passes and neither fails; a loss-cut changes only when, at -20%, cutting beat the actual outcome by the last bar in more than half of at least five crossings and on the notional-weighted mean; the buy style changes only when the unfilled deep limits missed a gain at D+20 (n >= 5); parking changes only when an account's median hold lost to idle cash after commission; the crypto ladder changes only when sells at +8% or more filled less often and more slowly than those at +1% to +8%.

| Market | Rule change | Evidence (number, n, window) | Expected effect (in-sample) | Risk / counter-evidence | Post-adoption check (pre-registered) |
| --- | --- | --- | --- | --- | --- |
| KR, US | Keep `buy.underwater_support_net` as is and score it under `underwater-d20-v1` at review | Tagged buys accepted: KR 95, US 7. Filled: KR 2, US 0. Mature D+20 inside the contract: 0 → INSUFFICIENT_SAMPLE. Window 2026-09-08 to the last bar | None measurable yet | At 2.0% fill, 5 mature fills need roughly 255 tagged orders; the review may never be reached | The tier's own rule, unchanged: n >= 5 mature fills, keep if D+20 median >= 0 and lower quartile > -8%. Report the tagged fill rate at each review |
| KR | Stop averaging down into lots <= -8% (suspend the tier in this market) | Real adds into lots <= -8% (any tag): n=21 mature, D+20 median -7.9%, Q1 -15.6% → FAIL (retire). Systematic 50% add at the first -8% close: n=46, median -2.8%, Q1 -12.1% → FAIL (retire). Whole window | Systematic adds: P&L -8.4% of average NAV, extra cash 22.7% of average NAV, max drawdown -29.1% → -26.5% | KR index -17.4% over the window, so the adds moved with one regime. The tier's own cohort is unscored, and these adds skip its support and thesis clauses | Shadow-log every first -8% close of a held lot and score the would-be 50% add under underwater-d20-v1; lift the suspension when n >= 20 mature and the rule passes |
| US | Average down mechanically: add 50% at the first close <= -8%, without the support clause | Real adds into lots <= -8% (any tag): n=4 mature, D+20 median +6.7%, Q1 +4.1% → INSUFFICIENT_SAMPLE. Systematic 50% add at the first -8% close: n=12, median +7.0%, Q1 +2.4% → PASS (keep). Whole window | Systematic adds: P&L +1.6% of average NAV, extra cash 1.5% of average NAV, max drawdown -8.2% → -9.4% | US index +4.8% over the window, so the adds moved with one regime. The tier's own cohort is unscored, and these adds skip its support and thesis clauses | Tag the adds; underwater-d20-v1 on them at n >= 5 and again at n >= 20; revert on FAIL |
| Crypto | Keep `buy.held_majors_support_net` as it stands, and tag its orders in plain ASCII so it can be scored | The tag never appears in the export. Proxy cohort (held, in profit, limit 3-12% below, filled from 2026-08-22): strict n=0, loose n=0 → INSUFFICIENT_SAMPLE. For contrast, adds into coins <= -8%: other buys n=8, D+20 median +3.5%, Q1 -3.6% → PASS (keep); recurring micro-buys n=802, median -2.1%, Q1 -8.8% → FAIL (retire) | Makes the tier measurable; no P&L effect | The export hashes non-ASCII tags, so a Korean label hides the tier | Count tagged fills per month; score the tier's own rule (D+20 median >= 0, Q1 > -8%) at n >= 5 |
| KR | Cut mechanically at the first close <= -X% (today: classification-only) | Episodes crossing -X% in the window. -8%: n=44, cut beat actual by the last bar in 63.6% (D+20: 53.7%), cut minus actual +4.0% notional-weighted; -20%: n=35, cut beat actual by the last bar in 71.4% (D+20: 40.0%), cut minus actual +14.0% notional-weighted; -40%: n=12, cut beat actual by the last bar in 75.0% (D+20: 33.3%), cut minus actual +6.0% notional-weighted | -8%: P&L +5.5% of avg NAV, drawdown -29.1% → -25.0%, cash share 25.8% → 62.4%; -20%: P&L +6.9% of avg NAV, drawdown -29.1% → -27.0%, cash share 25.8% → 54.6%; -40%: P&L +1.0% of avg NAV, drawdown -29.1% → -29.1%, cash share 25.8% → 27.3% | KR index -17.4% over the window; a cut wins when prices keep falling. At D+20 a -20% cut beat actual in 40.0%. Cut cash sits idle unless redeployed | Shadow-log each first close <= -X% of a held lot; at n >= 20 with 60 sessions matured, keep the cut only if cut beats actual in more than half and the notional-weighted difference is > 0 |
| US | Keep `sell.loss_cut` classification-only (no mechanical cut) | Episodes crossing -X% in the window. -8%: n=11, cut beat actual by the last bar in 36.4% (D+20: 9.1%), cut minus actual -28.3% notional-weighted; -20%: n=4 (INSUFFICIENT_SAMPLE), cut beat actual by the last bar in 50.0% (D+20: 33.3%), cut minus actual -35.7% notional-weighted; -40%: n=0 | -8%: P&L -3.3% of avg NAV, drawdown -8.2% → -6.1%, cash share 14.2% → 22.1%; -20%: P&L -1.0% of avg NAV, drawdown -8.2% → -6.3%, cash share 14.2% → 16.6% | US index +4.8% over the window; a cut wins when prices keep falling. At D+20 a -20% cut beat actual in 33.3%. Cut cash sits idle unless redeployed | Shadow-log each first close <= -X% of a held lot; at n >= 20 with 60 sessions matured, keep the cut only if cut beats actual in more than half and the notional-weighted difference is > 0 |
| Crypto | Keep crypto without a mechanical cut | Episodes crossing -X% in the window. -8%: n=12, cut beat actual by the last bar in 66.7% (D+20: 83.3%), cut minus actual -1.5% notional-weighted; -20%: n=9, cut beat actual by the last bar in 55.6% (D+20: 77.8%), cut minus actual -14.0% notional-weighted; -40%: n=4 (INSUFFICIENT_SAMPLE), cut beat actual by the last bar in 75.0% (D+20: 75.0%), cut minus actual +6.3% notional-weighted | -8%: P&L +1.0% of avg NAV, drawdown -19.3% → -10.4%, cash share 34.6% → 63.3%; -20%: P&L -5.0% of avg NAV, drawdown -19.3% → -15.8%, cash share 34.6% → 58.0%; -40%: P&L +0.2% of avg NAV, drawdown -19.3% → -19.3%, cash share 34.6% → 34.9% | Crypto index -9.9% over the window; a cut wins when prices keep falling. At D+20 a -20% cut beat actual in 77.8%. Cut cash sits idle unless redeployed | Shadow-log each first close <= -X% of a held lot; at n >= 20 with 60 sessions matured, keep the cut only if cut beats actual in more than half and the notional-weighted difference is > 0 |
| KR | Keep deep limits for buys | Deep-limit buy intents: 138, fill 21.7%; near-market 89, fill 41.6%. Unfilled deep intents bought at the next open, D+20: n=68, mean -3.9%, median -3.9%. Filled deep buys were +1.3% cheaper than the next open | Filling every unfilled intent at the next open: P&L -18.3% of avg NAV, extra cash 71.8% of avg NAV, cash share 25.8% → 23.6% | KR index -17.4%: in a falling market unfilled limits avoid losses, in a rising one they miss gains. Fill-at-open has no slippage; one buy leg costs up to 0.24% | Paired per intent: record the next-open price for every buy intent; at n >= 50 matured intents, compare intent-level D+20 (unfilled = 0%) between styles; switch only if near-market leads |
| US | Buy near the market instead of with deep limits | Deep-limit buy intents: 64, fill 4.7%; near-market 37, fill 35.1%. Unfilled deep intents bought at the next open, D+20: n=47, mean +5.9%, median +4.5%. Filled deep buys were +2.7% cheaper than the next open | Filling every unfilled intent at the next open: P&L +0.2% of avg NAV, extra cash 63.5% of avg NAV, cash share 14.2% → 25.4% | US index +4.8%: in a falling market unfilled limits avoid losses, in a rising one they miss gains. Fill-at-open has no slippage; one buy leg costs up to 0.45% | Paired per intent: record the next-open price for every buy intent; at n >= 50 matured intents, compare intent-level D+20 (unfilled = 0%) between styles; switch only if near-market leads |
| Crypto | No change to the buy order style | Deep-limit buy intents: 4, fill 25.0%; near-market 9, fill 66.7%. Unfilled deep intents bought at the next open, D+20: n=3, mean -0.9%, median -0.3%. Filled deep buys were +4.7% cheaper than the next open → INSUFFICIENT_SAMPLE | Filling every unfilled intent at the next open: P&L +0.2% of avg NAV, extra cash 3.5% of avg NAV, cash share 34.6% → 33.7% | Crypto index -9.9%: in a falling market unfilled limits avoid losses, in a rising one they miss gains. Fill-at-open has no slippage; one buy leg costs up to 0.05% | Paired per intent: record the next-open price for every buy intent; at n >= 50 matured intents, compare intent-level D+20 (unfilled = 0%) between styles; switch only if near-market leads |
| KR | Park only cash expected to stay idle longer than the break-even hold | Instrument total return +3.0% a year (+1.5% of it over the window from distributions). Break-even hold by account (ETF commission to policy maximum): Account A: 36-58 days; Account C: 55-88 days. Observed parking lots: n=3, median hold 31 days, 0 closed | Sweeping cash above 10% of NAV, ETF costs, one round trip: Account A +0.24% of avg NAV; Account C -0.11% of avg NAV | Short holds lose to costs; yields are price plus distributions, pre-tax; the policy cost table is a maximum, not a measured fee; KIS cash is taken to earn 0% | For each parking lot record days held and net return after fees vs the account's cash rate; review at 10 closed lots, keep if the median is > 0 |
| US | Park only cash expected to stay idle longer than the break-even hold | Instrument total return +3.7% a year (+1.5% of it over the window from distributions). Break-even hold by account (ETF commission to policy maximum): Account A: 50-90 days; Account C: 60-108 days. Observed parking lots: n=9, median hold 32 days, 0 closed | Sweeping cash above 10% of NAV, ETF costs, one round trip: Account A -0.02% of avg NAV; Account C -0.14% of avg NAV | Short holds lose to costs; yields are price plus distributions, pre-tax; the policy cost table is a maximum, not a measured fee; KIS cash is taken to earn 0% | For each parking lot record days held and net return after fees vs the account's cash rate; review at 10 closed lots, keep if the median is > 0 |
| Crypto | Cap resting take-profit sells below +8% above the close, or give them an expiry | Sells at +1% to +8%: n=54, fill 70.4%, median days to fill 1, rest vs selling at the next open +0.4%. At +8% or more: n=9, fill 44.4%, median days to fill 18, rest vs sell now +5.7% | Selling every resting take-profit at the next open instead: P&L -0.9% of avg NAV, cash share 34.6% → 40.0% | Far sells are few; BTC -9.9% over the window, which favours any sell | Record placement distance, fill time and the price at expiry for each resting sell; at 30 sells per band compare rest-vs-sell-now by band |
| KR | From scratch A2: near-market entry, cut at -10%, take profit at +30%, time stop 60 sessions | 231 signals (91 symbols), mean trade -4.9%, median -11.0%, wins 19.9%, worst trade -20.0%. Return per unit -4.9% vs index -9.2%. Whole window | Return on fully funded capital -18.0%, max drawdown -21.2%, capital used on an average day 45.6% | Signals are this window's own picks; one regime; fully funded unit sizing; fills at the open without slippage | Register the variant before it runs; mock or paper forward on new signals for 60 sessions; keep if return per unit beats the index per unit and the max drawdown stays above the limit the operator sets in advance |
| KR | From scratch A4: near-market entry, cut at -20%, take profit at +30%, time stop 60 sessions | 231 signals (91 symbols), mean trade -7.8%, median -12.4%, wins 22.9%, worst trade -29.9%. Return per unit -7.8% vs index -13.2%. Whole window | Return on fully funded capital -20.3%, max drawdown -22.0%, capital used on an average day 48.2% | Signals are this window's own picks; one regime; fully funded unit sizing; fills at the open without slippage | Register the variant before it runs; mock or paper forward on new signals for 60 sessions; keep if return per unit beats the index per unit and the max drawdown stays above the limit the operator sets in advance |
| KR | From scratch B3: near-market entry, add 1 unit at -10% and -20% (cap 2 adds), exit all at +10% over average cost, stop at -35% from average cost | 231 signals (91 symbols), mean trade -2.1%, median -0.3%, wins 49.4%, worst trade -43.5%. Return per unit -6.6% vs index -12.5%. Whole window | Return on fully funded capital -11.9%, max drawdown -13.9%, capital used on an average day 37.0% | Signals are this window's own picks; one regime; fully funded unit sizing; fills at the open without slippage; averaging down's worst trade is the tail risk | Register the variant before it runs; mock or paper forward on new signals for 60 sessions; keep if return per unit beats the index per unit and the max drawdown stays above the limit the operator sets in advance |
| US | From scratch A2: near-market entry, cut at -10%, take profit at +30%, time stop 60 sessions | 104 signals (49 symbols), mean trade +3.1%, median -4.4%, wins 40.4%, worst trade -19.1%. Return per unit +3.1% vs index -0.2%. Whole window | Return on fully funded capital +6.8%, max drawdown -7.3%, capital used on an average day 43.2% | Signals are this window's own picks; one regime; fully funded unit sizing; fills at the open without slippage | Register the variant before it runs; mock or paper forward on new signals for 60 sessions; keep if return per unit beats the index per unit and the max drawdown stays above the limit the operator sets in advance |
| US | From scratch A4: near-market entry, cut at -20%, take profit at +30%, time stop 60 sessions | 104 signals (49 symbols), mean trade +4.3%, median -2.0%, wins 46.2%, worst trade -27.6%. Return per unit +4.3% vs index -0.1%. Whole window | Return on fully funded capital +8.2%, max drawdown -7.2%, capital used on an average day 45.0% | Signals are this window's own picks; one regime; fully funded unit sizing; fills at the open without slippage | Register the variant before it runs; mock or paper forward on new signals for 60 sessions; keep if return per unit beats the index per unit and the max drawdown stays above the limit the operator sets in advance |
| US | From scratch B3: near-market entry, add 1 unit at -10% and -20% (cap 2 adds), exit all at +10% over average cost, stop at -35% from average cost | 104 signals (49 symbols), mean trade +3.7%, median +9.4%, wins 60.6%, worst trade -22.8%. Return per unit +2.2% vs index -1.0%. Whole window | Return on fully funded capital +4.3%, max drawdown -5.3%, capital used on an average day 28.3% | Signals are this window's own picks; one regime; fully funded unit sizing; fills at the open without slippage; averaging down's worst trade is the tail risk | Register the variant before it runs; mock or paper forward on new signals for 60 sessions; keep if return per unit beats the index per unit and the max drawdown stays above the limit the operator sets in advance |
| Crypto | From scratch A2: near-market entry, cut at -10%, take profit at +30%, time stop 60 sessions | 73 signals (9 symbols), mean trade +1.5%, median -10.3%, wins 35.6%, worst trade -17.6%. Return per unit +1.5% vs index +0.1%. Whole window | Return on fully funded capital +3.5%, max drawdown -18.4%, capital used on an average day 35.1% | Signals are this window's own picks; one regime; fully funded unit sizing; fills at the open without slippage | Register the variant before it runs; mock or paper forward on new signals for 60 sessions; keep if return per unit beats the index per unit and the max drawdown stays above the limit the operator sets in advance |
| Crypto | From scratch A4: near-market entry, cut at -20%, take profit at +30%, time stop 60 sessions | 73 signals (9 symbols), mean trade +1.0%, median -4.4%, wins 43.8%, worst trade -25.9%. Return per unit +1.0% vs index -0.9%. Whole window | Return on fully funded capital +1.9%, max drawdown -20.7%, capital used on an average day 37.8% | Signals are this window's own picks; one regime; fully funded unit sizing; fills at the open without slippage | Register the variant before it runs; mock or paper forward on new signals for 60 sessions; keep if return per unit beats the index per unit and the max drawdown stays above the limit the operator sets in advance |
| Crypto | From scratch B3: near-market entry, add 1 unit at -10% and -20% (cap 2 adds), exit all at +10% over average cost, stop at -35% from average cost | 73 signals (9 symbols), mean trade +11.7%, median +12.0%, wins 94.5%, worst trade -47.2%. Return per unit +11.2% vs index +3.4%. Whole window | Return on fully funded capital +15.1%, max drawdown -10.3%, capital used on an average day 34.4% | Signals are this window's own picks; one regime; fully funded unit sizing; fills at the open without slippage; averaging down's worst trade is the tail risk | Register the variant before it runs; mock or paper forward on new signals for 60 sessions; keep if return per unit beats the index per unit and the max drawdown stays above the limit the operator sets in advance |


## 1. Averaging down

**`buy.underwater_support_net`, scored under `underwater-d20-v1`.** D0 is the order's last fill date, D+20 the close twenty sessions later on the symbol's own bars, against the order's execution price. The contract covers fills from 2026-09-08. The tag survives the export in plain text, so this cohort is exact.

| Market | Tagged buys accepted | Filled | Fill rate | Filled from 2026-09-08 | Mature D+20 | D+20 median | D+20 Q1 | Verdict | Lot <= -8% at order time |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| KR | 95 | 2 | 2.1% | 2 | 0 | n/a | n/a | INSUFFICIENT_SAMPLE | 95.7% of 92 |
| US | 7 | 0 | 0.0% | 0 | 0 | n/a | n/a | INSUFFICIENT_SAMPLE | 100.0% of 7 |


The filled tier orders have D0 from 2026-10-12 to 2026-10-21, and the last KR bar is 2026-10-30, so a fill reaches D+20 only if it is twenty sessions old. The other tier orders expired, were cancelled, or are `open`, which means no ending was recorded, not that the order is live. The last column checks clause (2) of the tier at order time, from the rebuilt average cost and the last complete close.

**Every real buy, by the position's state just before it.** The same D+20 arithmetic, any tag, the whole window. The position's unrealized P&L is the fill price against its average cost (`research/strategy_evidence/lots.py`). The verdict column applies the tier's rule to each bucket for comparison only: these are not tier orders.

| Market | Scope | Position before the buy | Buys | Mature D+20 | D+5 median | D+20 median | D+20 Q1 | D+20 Q3 | D+20 mean | Share > 0 | D+20 mean net of index | Tier rule, applied |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| KR | other buys | add, lot <= -8% | 23 | 21 | -0.7% | -7.9% | -15.6% | +7.9% | -5.1% | 28.6% | +9.3% | FAIL (retire) |
| KR | other buys | add, lot -8% to 0% | 18 | 18 | +0.5% | -2.6% | -15.4% | +3.5% | -5.7% | 38.9% | +10.7% | FAIL (retire) |
| KR | other buys | add, lot >= 0% | 3 | 3 | +8.9% | -12.3% | -17.0% | -5.9% | -11.2% | 33.3% | -4.6% | INSUFFICIENT_SAMPLE |
| KR | other buys | new entry (flat before) | 48 | 48 | -5.1% | -3.3% | -20.1% | +5.1% | -5.7% | 41.7% | +8.9% | FAIL (retire) |
| KR | other buys | add, cost unknown | 3 | 3 | -6.9% | -15.7% | -18.1% | -12.7% | -15.3% | 0.0% | -1.4% | INSUFFICIENT_SAMPLE |
| US | other buys | add, lot <= -8% | 4 | 4 | +2.4% | +6.7% | +4.1% | +9.8% | +7.2% | 100.0% | +6.4% | INSUFFICIENT_SAMPLE |
| US | other buys | add, lot -8% to 0% | 2 | 2 | -5.4% | +15.0% | +11.1% | +18.9% | +15.0% | 100.0% | +13.1% | INSUFFICIENT_SAMPLE |
| US | other buys | add, lot >= 0% | 1 | 1 | -10.1% | -18.6% | -18.6% | -18.6% | -18.6% | 0.0% | -17.2% | INSUFFICIENT_SAMPLE |
| US | other buys | new entry (flat before) | 17 | 17 | -3.2% | +3.9% | +0.7% | +6.9% | +4.4% | 82.4% | +4.2% | PASS (keep) |
| US | other buys | add, cost unknown | 3 | 3 | +7.2% | +2.1% | +1.0% | +5.4% | +3.6% | 100.0% | +3.5% | INSUFFICIENT_SAMPLE |
| Crypto | other buys | add, lot <= -8% | 8 | 8 | +3.1% | +3.5% | -3.6% | +9.1% | +3.7% | 62.5% | +5.0% | PASS (keep) |
| Crypto | other buys | add, lot -8% to 0% | 5 | 5 | +2.9% | -2.1% | -2.1% | -0.9% | +3.6% | 20.0% | +5.6% | FAIL (retire) |
| Crypto | other buys | new entry (flat before) | 4 | 4 | -6.2% | +7.5% | -0.9% | +13.3% | +4.9% | 75.0% | +16.1% | INSUFFICIENT_SAMPLE |
| Crypto | recurring micro-buys | add, lot <= -8% | 802 | 802 | -1.0% | -2.1% | -8.8% | +7.6% | +0.6% | 41.3% | +2.2% | FAIL (retire) |
| Crypto | recurring micro-buys | add, lot -8% to 0% | 439 | 439 | -1.5% | -3.5% | -6.7% | +0.2% | -2.0% | 25.1% | -0.2% | FAIL (retire) |
| Crypto | recurring micro-buys | add, lot >= 0% | 225 | 225 | -2.8% | -13.4% | -19.5% | -8.4% | -13.3% | 3.1% | -1.8% | FAIL (retire) |
| Crypto | recurring micro-buys | new entry (flat before) | 6 | 6 | +1.0% | -1.0% | -4.9% | +2.7% | +0.4% | 33.3% | +0.9% | FAIL (retire) |
| Crypto | recurring micro-buys | add, cost unknown | 89 | 89 | +0.6% | -6.7% | -11.4% | -1.5% | -5.8% | 16.9% | -1.6% | FAIL (retire) |


The same `add, lot <= -8%` bucket restricted to fills from 2026-09-08, the contract's start:

| Market | Buys | Mature D+20 | D+20 median | D+20 Q1 | Tier rule, applied |
| --- | --- | --- | --- | --- | --- |
| KR | 2 | 0 | n/a | n/a | INSUFFICIENT_SAMPLE |
| US | 0 | 0 | n/a | n/a | INSUFFICIENT_SAMPLE |


**A systematic add.** At each episode's first close at or below -X% against average cost, add 50% of the quantity held (the tier's size cap) at that close, once per episode, without the support and thesis clauses. Scored like the tier; `End` is the return to the last bar.

| Market | First touch | Adds | Mature D+20 | D+20 median | D+20 Q1 | Tier rule, applied | End mean | End median |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| KR | -8% | 49 | 46 | -2.8% | -12.1% | FAIL (retire) | -18.2% | -16.4% |
| KR | -15% | 42 | 38 | -0.1% | -10.3% | FAIL (retire) | -14.6% | -16.1% |
| US | -8% | 12 | 12 | +7.0% | +2.4% | PASS (keep) | +11.0% | +6.3% |
| US | -15% | 4 | 4 | +4.5% | -4.4% | INSUFFICIENT_SAMPLE | +3.3% | -3.4% |
| Crypto | -8% | 14 | 14 | -13.2% | -18.7% | FAIL (retire) | -8.4% | -7.9% |
| Crypto | -15% | 12 | 12 | -9.9% | -19.1% | FAIL (retire) | -6.6% | -7.2% |


**`buy.held_majors_support_net` (crypto).** Its tag does not appear anywhere in the export: the export copies a tag only when it is short plain ASCII, so these orders carry hashed labels. A proxy stands in for it. It is not proof that a session meant the tier.

| Proxy | Definition | Filled buys | Mature D+20 | D+20 median | D+20 Q1 | Retire rule |
| --- | --- | --- | --- | --- | --- | --- |
| strict | order row, filled from 2026-08-22, coin held and in profit at the order (last complete close vs average cost), limit 3-12% below that close, not a recurring micro-buy | 0 | 0 | n/a | n/a | INSUFFICIENT_SAMPLE |
| loose | the same without the distance clause | 0 | 0 | n/a | n/a | INSUFFICIENT_SAMPLE |


## 2. Loss-cut at the first touch of -X%

`sell.loss_cut` is classification-only today: it creates no order. For each position episode (flat back to flat) and threshold, the touch is the first close at or below average cost x (1 - X). `Hold` is the close at the horizon against the touch close. `Actual` is what became of the shares held at the touch: later real sells take them first at their real prices, and the rest is marked at the horizon close. A cut earns 0% less one sell leg (KR 0.24%, US 0.45%, Crypto 0.05%), so `Cut better` is the share where the actual outcome was worse than that, and `Cut minus actual` is the average gain of cutting. Daily bars only: an intraday touch that recovered by the close is not seen. `Crossed` means the episode was above -X% earlier in the window; `already below` means its first observed day (an opening lot) was already past -X%.

| Market | X | Group | Horizon | Episodes | Symbols | Hold median | Actual median | Cut better | Cut minus actual, mean | Cut minus actual, notional-weighted | Worst close after touch, median | Share of shares really sold by then |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| KR | -8% | crossed | D+20 | 41 | 36 | -2.4% | -2.4% | 53.7% | -0.3% | -11.4% | -10.3% | 17.7% |
| KR | -8% | crossed | D+60 | 37 | 33 | -10.4% | -3.0% | 56.8% | +1.0% | -5.5% | -14.5% | 30.2% |
| KR | -8% | crossed | end | 44 | 37 | -15.6% | -9.9% | 63.6% | +9.4% | +4.0% | -22.6% | 28.1% |
| KR | -12% | crossed | D+20 | 41 | 35 | -1.5% | -1.5% | 53.7% | -1.7% | -10.0% | -8.9% | 11.2% |
| KR | -12% | crossed | D+60 | 37 | 32 | -8.4% | -3.4% | 54.1% | +1.5% | -3.5% | -15.7% | 20.9% |
| KR | -12% | crossed | end | 46 | 38 | -14.9% | -9.2% | 65.2% | +9.1% | +7.0% | -20.4% | 19.5% |
| KR | -15% | crossed | D+20 | 37 | 30 | +0.8% | +0.8% | 48.6% | -3.5% | -14.5% | -9.5% | 5.7% |
| KR | -15% | crossed | D+60 | 33 | 28 | -3.8% | -3.8% | 57.6% | +1.9% | -6.3% | -15.1% | 13.0% |
| KR | -15% | crossed | end | 41 | 34 | -15.5% | -14.1% | 70.7% | +9.7% | +5.0% | -20.9% | 13.4% |
| KR | -20% | crossed | D+20 | 30 | 27 | +3.9% | +4.2% | 40.0% | -5.5% | -8.8% | -6.8% | 3.3% |
| KR | -20% | crossed | D+60 | 26 | 23 | -5.2% | -5.2% | 65.4% | +3.8% | +6.2% | -14.4% | 6.0% |
| KR | -20% | crossed | end | 35 | 31 | -15.0% | -12.8% | 71.4% | +10.7% | +14.0% | -18.0% | 7.3% |
| KR | -30% | crossed | D+20 | 14 | 13 | -0.5% | -0.5% | 50.0% | -1.7% | -2.2% | -8.4% | 0.0% |
| KR | -30% | crossed | D+60 | 11 | 10 | -2.2% | -2.2% | 54.5% | +3.7% | +10.3% | -15.8% | 0.0% |
| KR | -30% | crossed | end | 24 | 22 | -10.7% | -10.7% | 75.0% | +6.8% | +11.0% | -12.7% | 0.0% |
| KR | -40% | crossed | D+20 | 3 (INSUFFICIENT_SAMPLE) | 3 | +6.7% | +6.7% | 33.3% | +3.0% | +19.5% | -22.5% | 0.0% |
| KR | -40% | crossed | D+60 | 2 (INSUFFICIENT_SAMPLE) | 2 | +29.3% | +29.3% | 0.0% | -29.6% | -30.4% | -16.1% | 0.0% |
| KR | -40% | crossed | end | 12 | 11 | -3.2% | -3.2% | 75.0% | +3.7% | +6.0% | -6.5% | 0.0% |
| KR | -8% | already below | D+20 | 5 | 5 | -3.1% | -2.1% | 80.0% | +2.0% | +6.9% | -10.5% | 40.0% |
| KR | -8% | already below | D+60 | 4 (INSUFFICIENT_SAMPLE) | 4 | -8.3% | +2.4% | 50.0% | -2.2% | +6.1% | -15.0% | 25.0% |
| KR | -8% | already below | end | 5 | 5 | -20.9% | -9.2% | 80.0% | +9.8% | +17.6% | -30.4% | 40.0% |
| KR | -12% | already below | D+20 | 2 (INSUFFICIENT_SAMPLE) | 2 | -6.8% | -2.0% | 100.0% | +1.8% | +1.3% | -10.7% | 50.0% |
| KR | -12% | already below | D+60 | 1 (INSUFFICIENT_SAMPLE) | 1 | +17.4% | +17.4% | 0.0% | -17.6% | -17.6% | -17.5% | 0.0% |
| KR | -12% | already below | end | 2 (INSUFFICIENT_SAMPLE) | 2 | -18.2% | -8.2% | 100.0% | +7.9% | +4.6% | -24.7% | 50.0% |
| KR | -15% | already below | D+20 | 1 (INSUFFICIENT_SAMPLE) | 1 | -10.5% | -0.9% | 100.0% | +0.7% | +0.7% | -10.5% | 100.0% |
| KR | -15% | already below | end | 1 (INSUFFICIENT_SAMPLE) | 1 | -20.9% | -0.9% | 100.0% | +0.7% | +0.7% | -31.8% | 100.0% |
| US | -8% | crossed | D+20 | 11 | 11 | +8.0% | +8.0% | 9.1% | -10.4% | -10.3% | -1.5% | 1.1% |
| US | -8% | crossed | D+60 | 11 | 11 | +6.1% | +9.4% | 9.1% | -12.6% | -25.6% | -3.4% | 31.8% |
| US | -8% | crossed | end | 11 | 11 | -1.0% | +7.9% | 36.4% | -10.8% | -28.3% | -4.0% | 31.8% |
| US | -12% | crossed | D+20 | 6 | 6 | +11.4% | +9.6% | 33.3% | -10.3% | -9.1% | -2.7% | 16.7% |
| US | -12% | crossed | D+60 | 5 | 5 | +19.5% | +13.6% | 0.0% | -19.2% | -37.4% | -0.8% | 25.0% |
| US | -12% | crossed | end | 7 | 7 | -0.5% | -0.5% | 57.1% | -9.8% | -38.3% | -1.4% | 17.9% |
| US | -15% | crossed | D+20 | 3 (INSUFFICIENT_SAMPLE) | 3 | +12.5% | +12.5% | 33.3% | -11.6% | -14.4% | -7.1% | 0.0% |
| US | -15% | crossed | D+60 | 2 (INSUFFICIENT_SAMPLE) | 2 | +8.9% | +8.9% | 0.0% | -9.4% | -7.9% | -4.8% | 0.0% |
| US | -15% | crossed | end | 3 (INSUFFICIENT_SAMPLE) | 3 | -8.2% | -8.2% | 66.7% | +6.6% | +8.0% | -11.1% | 0.0% |
| US | -20% | crossed | D+20 | 3 (INSUFFICIENT_SAMPLE) | 3 | +0.8% | +0.8% | 33.3% | -12.5% | -2.6% | -2.6% | 0.0% |
| US | -20% | crossed | D+60 | 2 (INSUFFICIENT_SAMPLE) | 2 | +14.2% | +14.2% | 50.0% | -14.7% | -33.5% | -11.9% | 0.0% |
| US | -20% | crossed | end | 4 (INSUFFICIENT_SAMPLE) | 4 | +2.6% | +2.6% | 50.0% | -9.8% | -35.7% | -6.9% | 6.2% |
| US | -30% | crossed | D+20 | 1 (INSUFFICIENT_SAMPLE) | 1 | +14.6% | +14.6% | 0.0% | -15.0% | -15.0% | +0.0% | 0.0% |
| US | -30% | crossed | D+60 | 1 (INSUFFICIENT_SAMPLE) | 1 | +70.7% | +69.5% | 0.0% | -70.0% | -70.0% | +0.0% | 13.3% |
| US | -30% | crossed | end | 1 (INSUFFICIENT_SAMPLE) | 1 | +71.6% | +70.9% | 0.0% | -71.4% | -71.4% | +0.0% | 20.0% |
| US | -8% | already below | D+20 | 1 (INSUFFICIENT_SAMPLE) | 1 | -3.5% | -3.5% | 100.0% | +3.1% | +3.1% | -5.0% | 0.0% |
| US | -8% | already below | D+60 | 1 (INSUFFICIENT_SAMPLE) | 1 | +13.9% | +13.9% | 0.0% | -14.4% | -14.4% | -21.6% | 0.0% |
| US | -8% | already below | end | 1 (INSUFFICIENT_SAMPLE) | 1 | +34.5% | +33.8% | 0.0% | -34.3% | -34.3% | -21.6% | 25.0% |
| US | -12% | already below | D+20 | 1 (INSUFFICIENT_SAMPLE) | 1 | -3.5% | -3.5% | 100.0% | +3.1% | +3.1% | -5.0% | 0.0% |
| US | -12% | already below | D+60 | 1 (INSUFFICIENT_SAMPLE) | 1 | +13.9% | +13.9% | 0.0% | -14.4% | -14.4% | -21.6% | 0.0% |
| US | -12% | already below | end | 1 (INSUFFICIENT_SAMPLE) | 1 | +34.5% | +33.8% | 0.0% | -34.3% | -34.3% | -21.6% | 25.0% |
| US | -15% | already below | D+20 | 1 (INSUFFICIENT_SAMPLE) | 1 | -3.5% | -3.5% | 100.0% | +3.1% | +3.1% | -5.0% | 0.0% |
| US | -15% | already below | D+60 | 1 (INSUFFICIENT_SAMPLE) | 1 | +13.9% | +13.9% | 0.0% | -14.4% | -14.4% | -21.6% | 0.0% |
| US | -15% | already below | end | 1 (INSUFFICIENT_SAMPLE) | 1 | +34.5% | +33.8% | 0.0% | -34.3% | -34.3% | -21.6% | 25.0% |
| Crypto | -8% | crossed | D+20 | 12 | 12 | -13.2% | -13.2% | 83.3% | +10.4% | +12.9% | -13.7% | 8.3% |
| Crypto | -8% | crossed | D+60 | 12 | 12 | -17.2% | -17.2% | 91.7% | +20.4% | +15.7% | -23.7% | 33.3% |
| Crypto | -8% | crossed | end | 12 | 12 | -0.1% | -5.4% | 66.7% | +9.8% | -1.5% | -26.7% | 60.9% |
| Crypto | -12% | crossed | D+20 | 11 | 11 | -9.0% | -9.0% | 100.0% | +10.5% | +11.1% | -13.6% | 9.1% |
| Crypto | -12% | crossed | D+60 | 11 | 11 | -13.5% | -13.5% | 100.0% | +17.9% | +11.8% | -19.8% | 27.3% |
| Crypto | -12% | crossed | end | 11 | 11 | +1.4% | -1.2% | 54.5% | +6.8% | -5.6% | -23.3% | 57.3% |
| Crypto | -15% | crossed | D+20 | 11 | 11 | -9.4% | -9.4% | 72.7% | +8.5% | +11.9% | -12.7% | 18.2% |
| Crypto | -15% | crossed | D+60 | 11 | 11 | -14.3% | -14.3% | 81.8% | +18.1% | +12.6% | -19.4% | 36.4% |
| Crypto | -15% | crossed | end | 11 | 11 | -3.4% | -0.5% | 54.5% | +7.4% | -4.9% | -24.2% | 57.3% |
| Crypto | -20% | crossed | D+20 | 9 | 9 | -4.7% | -4.7% | 77.8% | +5.8% | +5.0% | -9.4% | 0.0% |
| Crypto | -20% | crossed | D+60 | 9 | 9 | -9.1% | -9.1% | 88.9% | +17.1% | +8.5% | -14.9% | 22.2% |
| Crypto | -20% | crossed | end | 9 | 9 | -5.9% | -5.9% | 55.6% | +2.5% | -14.0% | -21.4% | 47.8% |
| Crypto | -30% | crossed | D+20 | 8 | 8 | -5.0% | -5.0% | 75.0% | +2.5% | -16.3% | -7.6% | 2.2% |
| Crypto | -30% | crossed | D+60 | 8 | 8 | +12.4% | +12.4% | 37.5% | -11.8% | -26.5% | -7.6% | 25.0% |
| Crypto | -30% | crossed | end | 8 | 8 | +13.0% | +13.0% | 37.5% | -7.8% | -27.1% | -7.6% | 41.3% |
| Crypto | -40% | crossed | D+20 | 4 (INSUFFICIENT_SAMPLE) | 4 | -12.4% | -10.1% | 75.0% | +15.3% | +6.2% | -12.7% | 25.0% |
| Crypto | -40% | crossed | D+60 | 4 (INSUFFICIENT_SAMPLE) | 4 | +14.0% | +15.4% | 50.0% | -5.0% | +6.3% | -20.0% | 50.0% |
| Crypto | -40% | crossed | end | 4 (INSUFFICIENT_SAMPLE) | 4 | -4.3% | -3.3% | 75.0% | +9.9% | +6.3% | -20.0% | 50.0% |
| Crypto | -8% | already below | D+20 | 2 (INSUFFICIENT_SAMPLE) | 2 | -15.9% | -15.9% | 100.0% | +15.9% | +12.8% | -16.1% | 0.0% |
| Crypto | -8% | already below | D+60 | 2 (INSUFFICIENT_SAMPLE) | 2 | -36.5% | -35.2% | 100.0% | +35.1% | +36.2% | -39.4% | 13.3% |
| Crypto | -8% | already below | end | 2 (INSUFFICIENT_SAMPLE) | 2 | -25.1% | -23.4% | 100.0% | +23.3% | +33.6% | -48.0% | 50.0% |
| Crypto | -12% | already below | D+20 | 2 (INSUFFICIENT_SAMPLE) | 2 | -15.9% | -15.9% | 100.0% | +15.9% | +12.8% | -16.1% | 0.0% |
| Crypto | -12% | already below | D+60 | 2 (INSUFFICIENT_SAMPLE) | 2 | -36.5% | -35.2% | 100.0% | +35.1% | +36.2% | -39.4% | 13.3% |
| Crypto | -12% | already below | end | 2 (INSUFFICIENT_SAMPLE) | 2 | -25.1% | -23.4% | 100.0% | +23.3% | +33.6% | -48.0% | 50.0% |
| Crypto | -15% | already below | D+20 | 1 (INSUFFICIENT_SAMPLE) | 1 | -19.0% | -19.0% | 100.0% | +18.9% | +18.9% | -19.5% | 0.0% |
| Crypto | -15% | already below | D+60 | 1 (INSUFFICIENT_SAMPLE) | 1 | -34.1% | -34.1% | 100.0% | +34.0% | +34.0% | -39.5% | 0.0% |
| Crypto | -15% | already below | end | 1 (INSUFFICIENT_SAMPLE) | 1 | -13.2% | -13.2% | 100.0% | +13.1% | +13.1% | -48.1% | 0.0% |
| Crypto | -20% | already below | D+20 | 1 (INSUFFICIENT_SAMPLE) | 1 | -19.0% | -19.0% | 100.0% | +18.9% | +18.9% | -19.5% | 0.0% |
| Crypto | -20% | already below | D+60 | 1 (INSUFFICIENT_SAMPLE) | 1 | -34.1% | -34.1% | 100.0% | +34.0% | +34.0% | -39.5% | 0.0% |
| Crypto | -20% | already below | end | 1 (INSUFFICIENT_SAMPLE) | 1 | -13.2% | -13.2% | 100.0% | +13.1% | +13.1% | -48.1% | 0.0% |
| Crypto | -30% | already below | D+20 | 1 (INSUFFICIENT_SAMPLE) | 1 | -19.0% | -19.0% | 100.0% | +18.9% | +18.9% | -19.5% | 0.0% |
| Crypto | -30% | already below | D+60 | 1 (INSUFFICIENT_SAMPLE) | 1 | -34.1% | -34.1% | 100.0% | +34.0% | +34.0% | -39.5% | 0.0% |
| Crypto | -30% | already below | end | 1 (INSUFFICIENT_SAMPLE) | 1 | -13.2% | -13.2% | 100.0% | +13.1% | +13.1% | -48.1% | 0.0% |
| Crypto | -40% | already below | D+20 | 1 (INSUFFICIENT_SAMPLE) | 1 | -19.0% | -19.0% | 100.0% | +18.9% | +18.9% | -19.5% | 0.0% |
| Crypto | -40% | already below | D+60 | 1 (INSUFFICIENT_SAMPLE) | 1 | -34.1% | -34.1% | 100.0% | +34.0% | +34.0% | -39.5% | 0.0% |
| Crypto | -40% | already below | end | 1 (INSUFFICIENT_SAMPLE) | 1 | -13.2% | -13.2% | 100.0% | +13.1% | +13.1% | -48.1% | 0.0% |


Horizons past the last bar are censored, so D+60 counts fewer episodes than D+20. `end` uses every touch, at different lengths. One episode can count under several thresholds.

## 3. Buy order style

ROB-1439 measured fill rates by entry type (KR `deep_limit/proposal` 0.6%, US 2.4%, against 39-89% near the market) and the next-open counterfactual R1. This table adds the intent-level view: one row per account, symbol and ISO week, styled by its first accepted order. `Actual` earns from the fill price when filled and 0% when not. `Next open` buys every intent at the first open after its first order. `Unfilled` is the opportunity cost of the intents that never filled. Means are equal-weight over intents; no fees on either side.

| Market | Style | Intents | Filled | Fill rate | Fill vs next open | n D+5 | Actual D+5 | Next open D+5 | n D+20 | Actual D+20 | Next open D+20 | Next open D+20 net of index | Unfilled n D+5 | Unfilled cost D+5 | Unfilled n D+20 | Unfilled cost D+20, mean | Unfilled cost D+20, median |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Crypto | deep_limit | 4 | 1 | 25.0% | +4.68% | 4 | +0.57% | +0.10% | 4 | +0.14% | -1.56% | +1.60% | 3 | +1.54% | 3 (INSUFFICIENT_SAMPLE) | -0.90% | -0.34% |
| Crypto | near_market | 9 | 6 | 66.7% | +2.40% | 9 | +0.81% | +0.45% | 9 | +4.61% | +3.00% | +4.65% | 3 | -0.29% | 3 (INSUFFICIENT_SAMPLE) | +1.37% | +2.58% |
| KR | deep_limit | 138 | 30 | 21.7% | +1.26% | 129 | -0.37% | -1.65% | 97 | -2.67% | -5.61% | +6.89% | 99 | -1.49% | 68 | -3.86% | -3.88% |
| KR | near_market | 89 | 37 | 41.6% | +1.57% | 83 | -0.53% | -2.63% | 64 | -1.56% | -5.43% | +12.83% | 46 | -2.81% | 28 | -5.95% | -8.11% |
| US | deep_limit | 64 | 3 | 4.7% | +2.71% | 61 | -0.11% | +1.45% | 50 | +0.75% | +5.88% | +4.54% | 58 | +1.69% | 47 | +5.88% | +4.47% |
| US | near_market | 37 | 13 | 35.1% | -0.25% | 33 | -1.18% | -0.90% | 30 | +1.48% | +2.42% | +1.89% | 20 | +0.22% | 17 | +2.69% | +0.66% |


## 4. Parking

Instrument total return over the window: price plus distributions (ex-date inside the span, not reinvested), annualized on calendar days. 357870 and BIL were never held in the window.

| Market | Instrument | Span | Days | Price return | Distributions | Total | Annualized |
| --- | --- | --- | --- | --- | --- | --- | --- |
| KR | 459580 | 2026-05-04 to 2026-10-30 | 179 | -0.04% | +1.49% | +1.45% | +2.97% |
| US | SGOV | 2026-05-01 to 2026-10-30 | 182 | +0.32% | +1.50% | +1.82% | +3.66% |


Round trip: `policy` is `round_trip_cost_bps` as written (KR 0.47%, US 0.90%); `etf` is the same table's commission alone (KR 0.29%, US 0.50%), since a listed ETF pays no KR sell tax and USD cash needs no FX to buy a USD ETF. Idle cash earns the account's `cash_yields` rate; KIS has none listed, so 0%. Lots still open are valued at the last bar as if sold there.

| Market | Account | Lots | Closed | Median hold, days | Longest | Idle cash rate | Lot return, mean | Net vs idle (etf) | Break-even days (etf) | Positive at median hold (etf) | Net vs idle (policy) | Break-even days (policy) | Positive at median hold (policy) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| KR | Account A | 1 | 0 | 31 | 31 | 0.00% | +0.29% | -0.00% | 36 | no | -0.18% | 58 | no |
| KR | Account C | 2 | 0 | 45 | 59 | 1.00% | +0.41% | -0.01% | 55 | no | -0.19% | 88 | no |
| US | Account A | 2 | 0 | 44 | 56 | 0.00% | +0.46% | -0.04% | 50 | no | -0.44% | 90 | no |
| US | Account C | 7 | 0 | 32 | 63 | 0.60% | +0.30% | -0.25% | 60 | no | -0.65% | 108 | no |


## 5. Crypto sell ladders

Every accepted crypto sell, by its distance above the last complete UTC daily close when placed. `Rest vs sell now` compares what the order's coins became by the last bar (filled part at the fill price then cash, the rest marked at the last close) with selling everything at the next open. `Rest vs hold` compares it with never selling. Days to fill are UTC days from placement to the first fill row. `Open at end` means no ending was recorded.

| Distance above close | Sells | Filled | Fill rate | Days to fill, median | Days to fill, max | Open at end | Age of open ones, median days | Rest vs sell now, mean | Rest vs sell now, weighted | Rest vs hold, mean | Hold vs sell now, mean | BTC, same span |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| a. below +1% | 66 | 55 | 83.3% | 1.0 | 10 | 11 | 23 | -1.53% | -1.06% | +7.36% | -7.11% | -2.99% |
| b. +1 to +3% | 26 | 17 | 65.4% | 1.0 | 21 | 8 | 15 | -0.26% | +0.31% | +8.11% | -7.45% | -4.19% |
| c. +3 to +8% | 28 | 21 | 75.0% | 1.0 | 31 | 3 | 29 | +1.00% | +1.05% | +5.17% | -3.60% | +0.92% |
| d. +8 to +16% | 6 | 2 | 33.3% | 18.5 | 31 | 0 | n/a | +2.63% | +2.79% | +2.61% | +0.14% | +7.66% |
| e. +16% or more | 3 (INSUFFICIENT_SAMPLE) | 2 | 66.7% | 19.5 | 38 | 0 | n/a | +11.82% | +7.57% | +14.00% | -1.66% | -2.77% |


Still open at the last bar: 22 sells, 11 of them placed at +1% or more (placement distance 1.0% to 5.7%, age 0 to 58 days). Against selling at the next open they stand at -8.04% on average.

## 6. Idle cash, and how each rule moves it

The base is ROB-1439's account cash: a lower bound from fills, no deposits or withdrawals, opening cash the least that keeps cash non-negative. Each rule is replayed as extra trades on top of the real ones; cut proceeds stay in cash (no redeployment). A buy rule that runs cash below zero needs extra cash, shown as a share of average NAV and added from the first day. Drawdown is NAV's largest fall from its running peak, measured with that extra cash in NAV, so a rule that needs extra cash can show a smaller drawdown than the base.

| Market | Account | Rule | Avg cash share | Change | Avg cash + parking share | Extra cash needed, % avg NAV | P&L change, % avg NAV | Max drawdown |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| KR | Account A | as traded | 33.3% | +0.0pp | 33.9% | 0.0% | +0.00% | -19.5% |
| KR | Account C | as traded | 18.2% | +0.0pp | 27.5% | 0.0% | +0.00% | -29.1% |
| KR | Account A | loss-cut at first close <= -8% | 74.6% | +41.3pp | 75.2% | 0.0% | +4.67% | -9.7% |
| KR | Account C | loss-cut at first close <= -8% | 50.3% | +32.1pp | 59.3% | 0.0% | +6.41% | -25.0% |
| KR | Account A | loss-cut at first close <= -20% | 64.8% | +31.6pp | 65.4% | 0.0% | +7.94% | -12.3% |
| KR | Account C | loss-cut at first close <= -20% | 44.3% | +26.1pp | 53.5% | 0.0% | +5.83% | -27.0% |
| KR | Account A | loss-cut at first close <= -40% | 35.0% | +1.7pp | 35.6% | 0.0% | +0.76% | -18.9% |
| KR | Account C | loss-cut at first close <= -40% | 19.6% | +1.4pp | 28.9% | 0.0% | +1.17% | -29.1% |
| KR | Account A | add 50% at first close <= -8% | 18.6% | -14.7pp | 19.1% | 23.7% | -9.28% | -24.1% |
| KR | Account C | add 50% at first close <= -8% | 12.5% | -5.7pp | 20.3% | 21.7% | -7.55% | -26.5% |
| KR | Account A | add 50% at first close <= -15% | 15.7% | -17.6pp | 16.3% | 13.2% | -6.74% | -24.9% |
| KR | Account C | add 50% at first close <= -15% | 11.7% | -6.6pp | 20.0% | 13.2% | -5.42% | -27.6% |
| KR | Account A | unfilled buy intents filled at the next open | 19.1% | -14.2pp | 19.7% | 10.1% | -8.17% | -25.7% |
| KR | Account C | unfilled buy intents filled at the next open | 28.2% | +10.0pp | 32.3% | 133.6% | -28.41% | -22.6% |
| US | Account A | as traded | 16.5% | +0.0pp | 24.5% | 0.0% | +0.00% | -8.2% |
| US | Account C | as traded | 12.0% | +0.0pp | 39.3% | 0.0% | +0.00% | -5.8% |
| US | Account A | loss-cut at first close <= -8% | 29.9% | +13.4pp | 38.4% | 0.0% | -5.81% | -6.0% |
| US | Account C | loss-cut at first close <= -8% | 14.4% | +2.4pp | 41.9% | 0.0% | -0.80% | -6.1% |
| US | Account A | loss-cut at first close <= -20% | 21.2% | +4.7pp | 29.4% | 0.0% | -2.05% | -6.3% |
| US | Account C | loss-cut at first close <= -20% | 12.0% | +0.0pp | 39.4% | 0.0% | +0.02% | -5.8% |
| US | Account A | add 50% at first close <= -8% | 9.3% | -7.2pp | 17.0% | 0.4% | +3.06% | -9.4% |
| US | Account C | add 50% at first close <= -8% | 12.0% | +0.0pp | 38.6% | 2.6% | +0.06% | -5.5% |
| US | Account A | add 50% at first close <= -15% | 13.7% | -2.8pp | 21.6% | 0.0% | +0.90% | -9.2% |
| US | Account C | add 50% at first close <= -15% | 12.0% | +0.0pp | 39.3% | 0.4% | -0.03% | -5.7% |
| US | Account A | unfilled buy intents filled at the next open | 13.3% | -3.2pp | 21.2% | 0.0% | +0.62% | -8.3% |
| US | Account C | unfilled buy intents filled at the next open | 37.5% | +25.5pp | 49.5% | 127.0% | -0.19% | -3.6% |
| Crypto | Account B | as traded | 34.6% | +0.0pp | 34.6% | 0.0% | +0.00% | -19.3% |
| Crypto | Account B | loss-cut at first close <= -8% | 63.3% | +28.7pp | 63.3% | 0.0% | +1.00% | -10.4% |
| Crypto | Account B | loss-cut at first close <= -20% | 58.0% | +23.4pp | 58.0% | 0.0% | -5.05% | -15.8% |
| Crypto | Account B | loss-cut at first close <= -40% | 34.9% | +0.4pp | 34.9% | 0.0% | +0.17% | -19.3% |
| Crypto | Account B | add 50% at first close <= -8% | 30.5% | -4.1pp | 30.5% | 22.6% | -1.14% | -19.7% |
| Crypto | Account B | add 50% at first close <= -15% | 31.4% | -3.2pp | 31.4% | 19.6% | +0.03% | -19.0% |
| Crypto | Account B | unfilled buy intents filled at the next open | 33.7% | -0.9pp | 33.7% | 3.5% | +0.22% | -18.7% |
| Crypto | Account B | resting take-profits sold at the next open | 40.0% | +5.4pp | 40.0% | 0.0% | -0.86% | -19.3% |


**Parking sweep.** Cash above 10% of NAV held in the market's parking instrument at its measured yield, less the account's idle cash rate. `Once` pays one round trip on the largest excess, `monthly` one every 30 days on the average excess.

| Market | Account | Costs | Excess cash, % avg NAV | Cash share after | Gross, % avg NAV | Net once, % avg NAV | Net monthly, % avg NAV |
| --- | --- | --- | --- | --- | --- | --- | --- |
| KR | Account A | etf | 23.9% | 9.1% | +0.35% | +0.24% | -0.08% |
| KR | Account A | policy | 23.9% | 9.1% | +0.35% | +0.18% | -0.33% |
| US | Account A | etf | 7.3% | 9.3% | +0.13% | -0.02% | -0.09% |
| US | Account A | policy | 7.3% | 9.3% | +0.13% | -0.13% | -0.26% |
| KR | Account C | etf | 9.7% | 8.8% | +0.07% | -0.11% | -0.06% |
| KR | Account C | policy | 9.7% | 8.8% | +0.07% | -0.23% | -0.14% |
| US | Account C | etf | 4.8% | 7.2% | +0.05% | -0.14% | -0.06% |
| US | Account C | policy | 4.8% | 7.2% | +0.05% | -0.29% | -0.15% |


## 7. From-scratch rule sets

Signals are what the accounts chose to buy: each market, symbol and ISO week with an accepted buy order or a real buy, parking excluded, once across accounts. Each signal buys one unit of notional at the next open, so the variants differ only in execution and exit. Every unit pays the market's full round trip. Portfolio figures treat each unit as fully funded: capital is the peak number of units held on one day, and drawdown is of capital plus cumulative P&L. The index column puts the same units in the benchmark over the same dates. The variant list is fixed in `rulesets.py` and every variant is shown.

Signals: KR 231, US 104, Crypto 73.

| Market | Variant | Rule | Signals | Symbols | Units | Mean trade | Median trade | Wins | Worst trade | Mean trade 95% CI (symbols) | Return per unit | Index per unit | Return on capital | Max drawdown | Capital used, avg day | Exits |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| KR | A1 | near-market entry, cut at -10%, take profit at +15%, time stop 60 sessions | 231 | 91 | 231 | -3.9% | -10.7% | 27.7% | -20.0% | -5.6% to -1.9% | -3.9% | -7.5% | -18.4% | -21.5% | 45.4% | loss-cut 128, open at end 47, target 48, time 8 |
| KR | A2 | near-market entry, cut at -10%, take profit at +30%, time stop 60 sessions | 231 | 91 | 231 | -4.9% | -11.0% | 19.9% | -20.0% | -6.9% to -2.6% | -4.9% | -9.2% | -18.0% | -21.2% | 45.6% | loss-cut 142, open at end 48, target 19, time 22 |
| KR | A3 | near-market entry, cut at -20%, take profit at +15%, time stop 60 sessions | 231 | 91 | 231 | -6.3% | -8.2% | 31.6% | -29.9% | -8.6% to -3.6% | -6.3% | -11.1% | -19.9% | -21.7% | 47.1% | loss-cut 80, open at end 69, target 55, time 27 |
| KR | A4 | near-market entry, cut at -20%, take profit at +30%, time stop 60 sessions | 231 | 91 | 231 | -7.8% | -12.4% | 22.9% | -29.9% | -10.6% to -4.8% | -7.8% | -13.2% | -20.3% | -22.0% | 48.2% | loss-cut 89, open at end 73, target 22, time 47 |
| KR | B1 | near-market entry, add 1 unit at -10% and -20% (cap 2 adds), exit all at +10% over average cost, time stop 60 sessions | 231 | 91 | 416 | -0.1% | +0.3% | 50.6% | -36.3% | -2.5% to +2.3% | -3.4% | -9.2% | -8.6% | -14.6% | 44.6% | open at end 86, target 99, time 46 |
| KR | B2 | near-market entry, add 1 unit at -10% and -20% (cap 2 adds), exit all at +10% over average cost | 231 | 91 | 426 | -2.2% | -0.2% | 49.8% | -59.4% | -5.2% to +0.8% | -6.8% | -12.6% | -10.8% | -13.3% | 35.3% | open at end 127, target 104 |
| KR | B3 | near-market entry, add 1 unit at -10% and -20% (cap 2 adds), exit all at +10% over average cost, stop at -35% from average cost | 231 | 91 | 426 | -2.1% | -0.3% | 49.4% | -43.5% | -4.9% to +0.8% | -6.6% | -12.5% | -11.9% | -13.9% | 37.0% | loss-cut 12, open at end 116, target 103 |
| KR | C1 | near-market entry, hold to the last bar | 231 | 91 | 231 | -16.8% | -15.7% | 15.2% | -64.3% | -20.2% to -13.3% | -16.8% | -21.5% | -16.8% | -17.6% | 39.2% | open at end 231 |
| KR | C2 | near-market entry, hold exactly 20 sessions | 231 | 91 | 231 | -6.6% | -5.2% | 29.9% | -40.3% | -8.9% to -4.1% | -6.6% | -11.9% | -14.8% | -16.5% | 27.2% | open at end 66, time 165 |
| US | A1 | near-market entry, cut at -10%, take profit at +15%, time stop 60 sessions | 104 | 49 | 104 | +0.3% | -4.4% | 40.4% | -19.1% | -2.9% to +3.8% | +0.3% | -0.9% | +0.8% | -8.9% | 37.7% | loss-cut 32, open at end 28, target 35, time 9 |
| US | A2 | near-market entry, cut at -10%, take profit at +30%, time stop 60 sessions | 104 | 49 | 104 | +3.1% | -4.4% | 40.4% | -19.1% | -1.2% to +8.0% | +3.1% | -0.2% | +6.8% | -7.3% | 43.2% | loss-cut 32, open at end 30, target 22, time 20 |
| US | A3 | near-market entry, cut at -20%, take profit at +15%, time stop 60 sessions | 104 | 49 | 104 | +1.5% | -2.0% | 46.2% | -27.6% | -2.5% to +5.4% | +1.5% | -0.8% | +3.3% | -8.4% | 40.5% | loss-cut 6, open at end 41, target 39, time 18 |
| US | A4 | near-market entry, cut at -20%, take profit at +30%, time stop 60 sessions | 104 | 49 | 104 | +4.3% | -2.0% | 46.2% | -27.6% | -0.8% to +9.8% | +4.3% | -0.1% | +8.2% | -7.2% | 45.0% | loss-cut 6, open at end 43, target 24, time 31 |
| US | B1 | near-market entry, add 1 unit at -10% and -20% (cap 2 adds), exit all at +10% over average cost, time stop 60 sessions | 104 | 49 | 140 | +3.6% | +9.4% | 58.7% | -22.8% | +0.5% to +6.9% | +2.3% | -0.9% | +5.4% | -5.8% | 30.9% | open at end 40, target 57, time 7 |
| US | B2 | near-market entry, add 1 unit at -10% and -20% (cap 2 adds), exit all at +10% over average cost | 104 | 49 | 141 | +3.7% | +9.4% | 60.6% | -22.8% | +0.3% to +7.1% | +2.2% | -1.0% | +4.3% | -5.3% | 28.3% | open at end 46, target 58 |
| US | B3 | near-market entry, add 1 unit at -10% and -20% (cap 2 adds), exit all at +10% over average cost, stop at -35% from average cost | 104 | 49 | 141 | +3.7% | +9.4% | 60.6% | -22.8% | +0.3% to +7.1% | +2.2% | -1.0% | +4.3% | -5.3% | 28.3% | open at end 46, target 58 |
| US | C1 | near-market entry, hold to the last bar | 104 | 49 | 104 | +5.7% | -2.5% | 42.3% | -32.3% | -1.4% to +14.2% | +5.7% | -0.9% | +5.7% | -5.4% | 37.3% | open at end 104 |
| US | C2 | near-market entry, hold exactly 20 sessions | 104 | 49 | 104 | +1.4% | +0.9% | 53.8% | -19.1% | -1.7% to +4.5% | +1.4% | -0.6% | +3.3% | -5.1% | 28.9% | open at end 21, time 83 |
| Crypto | A1 | near-market entry, cut at -10%, take profit at +15%, time stop 60 sessions | 73 | 9 | 73 | -0.2% | -10.2% | 39.7% | -17.6% | -5.0% to +2.7% | -0.2% | -1.2% | -0.5% | -17.9% | 31.9% | loss-cut 38, open at end 2, target 26, time 7 |
| Crypto | A2 | near-market entry, cut at -10%, take profit at +30%, time stop 60 sessions | 73 | 9 | 73 | +1.5% | -10.3% | 35.6% | -17.6% | -6.6% to +5.4% | +1.5% | +0.1% | +3.5% | -18.4% | 35.1% | loss-cut 40, open at end 3, target 13, time 17 |
| Crypto | A3 | near-market entry, cut at -20%, take profit at +15%, time stop 60 sessions | 73 | 9 | 73 | -0.5% | -1.4% | 47.9% | -25.9% | -3.3% to +1.2% | -0.5% | -2.1% | -1.0% | -20.1% | 34.7% | loss-cut 22, open at end 3, target 32, time 16 |
| Crypto | A4 | near-market entry, cut at -20%, take profit at +30%, time stop 60 sessions | 73 | 9 | 73 | +1.0% | -4.4% | 43.8% | -25.9% | -3.8% to +3.6% | +1.0% | -0.9% | +1.9% | -20.7% | 37.8% | loss-cut 23, open at end 4, target 15, time 31 |
| Crypto | B1 | near-market entry, add 1 unit at -10% and -20% (cap 2 adds), exit all at +10% over average cost, time stop 60 sessions | 73 | 9 | 130 | +4.4% | +10.7% | 67.1% | -74.5% | -1.4% to +8.1% | +0.1% | -3.2% | +0.2% | -12.4% | 25.9% | open at end 1, target 46, time 26 |
| Crypto | B2 | near-market entry, add 1 unit at -10% and -20% (cap 2 adds), exit all at +10% over average cost | 73 | 9 | 132 | +11.2% | +12.0% | 94.5% | -82.4% | +3.9% to +13.8% | +10.4% | +3.7% | +13.6% | -10.7% | 35.3% | open at end 4, target 69 |
| Crypto | B3 | near-market entry, add 1 unit at -10% and -20% (cap 2 adds), exit all at +10% over average cost, stop at -35% from average cost | 73 | 9 | 132 | +11.7% | +12.0% | 94.5% | -47.2% | +6.8% to +13.9% | +11.2% | +3.4% | +15.1% | -10.3% | 34.4% | loss-cut 1, open at end 3, target 69 |
| Crypto | C1 | near-market entry, hold to the last bar | 73 | 9 | 73 | +1.3% | +3.7% | 57.5% | -85.1% | -7.3% to +6.2% | +1.3% | +2.2% | +1.3% | -17.4% | 58.0% | open at end 73 |
| Crypto | C2 | near-market entry, hold exactly 20 sessions | 73 | 9 | 73 | -2.1% | -3.6% | 31.5% | -27.3% | -4.2% to -0.1% | -2.1% | -3.5% | -8.4% | -21.6% | 38.0% | time 73 |


For scale, the real lots bought in the window returned, from ROB-1439's headline: KR -14.47%; US +15.51%; Crypto +16.62%. Those are cost-weighted and before fees, so they are context, not a like-for-like row.

## Method and limits

- **In-sample, one window.** 2026-05-01 to 2026-11-01. KR fell over the whole window, US rose, crypto fell. Each counterfactual is computed on the data it would be judged on and overlaps the others, so they do not add up.
- **Not an experiment look.** Like ROB-1439, this is a descriptive replay of live trading. It reads no sealed data and is not registered in `research/experiments`. A rule chosen from it needs its own pre-registered forward test (the last column of the options table).
- **Average cost** is rebuilt from fills (`lots.py`): a buy moves it to the weighted mean, a sell leaves it, a seed row resets quantity and cost to the broker's figures, and days with older holdings of unknown cost are left out. It is not read from the broker.
- **Daily bars.** Touches, cuts and exits use closes; entries use the next open. No intraday low, no slippage, no price impact.
- **Costs** come from the policy's `round_trip_cost_bps`, a conservative maximum, not measured fees. Parking also shows a commission-only case. Taxes beyond those rates, FX on KRW/USD and dividends on stocks are left out.
- **Cash** is the ROB-1439 lower bound: no balances or external flows are in the export, so cash shares are estimates and the extra cash a buy rule needs is relative to that bound.
- **Tags.** Only short plain-ASCII strategy tags survive the export. `underwater_support_net` does, `held_majors_support_net` does not, so the crypto tier is a proxy.
- **Rule-set signals** are the accounts' own picks in this window, so they carry its selection. Units are equal notional and fully funded; the real accounts sized differently.
- **INSUFFICIENT_SAMPLE** marks any cohort below 5 observations, the tiers' own minimum.
- **Export v1.** No question here needed the v2 fields (`fill_seq`, seed flag, daily balances). Daily balances would replace the cash lower bound in question 6.
