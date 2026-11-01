# ROB-1439: retrospective of real buys and sells

This committed version holds ratios and counts only. Symbols, amounts and account names are masked because the repository is public and the export is private trading data. The full version is outside the repository.

This report states measured facts for the operator's later policy decision. It recommends nothing.

## Reproduce

```bash
uv run python -m research.trade_retro.run report --export ~/work/herdr-artifacts/dots-history-2026-11-01 --prices ~/work/herdr-artifacts/rob-1439-trade-retro/prices-2026-11-01 --public-out docs/research/rob-1439-trade-retrospective-2026-11-01.md --private-dir ~/work/herdr-artifacts/rob-1439-trade-retro
```

Add `--check` to compare against the files on disk without writing. Every number comes from `research/trade_retro/`: section 2 from `positions.py`, section 3 from `entry.py` and `markouts.py`, section 4 from `idle.py`, section 5 from `rules.py`, the checks from `audit.py` and the sensitivity table from `sensitivity.py`, all rendered by `report.py`.

- Export: `dots-history-export/v1`, window 2026-05-01 to 2026-11-01, `manifest.json` sha256 `9de029eb656814dcf4fa9efe6279609c3586f58a283cc30fc6e94259d1f44424`, 8585 rows, not committed.
- Price snapshot: `prices_manifest.json` sha256 `0cf9b5b9ea978c8ec3c46989590a9608fabbbf4f4f2ec0fe44ca75dfd8b26dd1`, fetched 2026-11-01, not committed. Re-fetching can pick up revised prices, so it goes to a new directory and produces a new report.
- Prices: Yahoo Finance daily bars for KR (KRX regular session) and US, Upbit public daily candles for crypto. Benchmarks: KOSPI (^KS11, Yahoo), S&P 500 (^GSPC, Yahoo), BTC/KRW (KRW-BTC, Upbit). The crypto benchmark is one coin, not a market index, because no public crypto index with free daily history was used.

## Headline

| Market | Lots bought in window | Return (realized + marked) | Index, same dates | vs index | Buy fill rate | Buy markout 20s | Buy markout 20s, without recurring micro-buys | Sell markout 20s | Avg cash share by account | Index return by account span |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| KR | 94 | -14.55% | -19.90% | +5.34% | 18.8% | -6.08% | -6.08% | +7.69% | 27.2% / 18.2% | -17.39% / -32.94% |
| US | 27 | +15.51% | +1.10% | +14.40% | 13.6% | +4.67% | +4.67% | -0.80% | 16.8% / 12.0% | +2.92% / +0.34% |
| Crypto | 1442 | +16.62% | +9.05% | +7.58% | 40.7% | -2.39% | +3.95% | +0.45% | 34.6% | -9.89% |

Returns are price return before fees. Each line's definitions are in the sections below. An idle share is a lower-bound estimate, so read the data limits first.

## Method

- **Scope.** Real-money order tables and `execution_ledger` only, with `superseded_duplicate = false`. A fill is the union of the ledger and the order row, taking the larger quantity for each order.
- **Position.** One account's holding in one symbol from flat to flat, matched first in, first out. An open quantity is marked at the last daily close. Returns are weighted by cost. When a sell proves older holdings existed, those go first in the queue, so selling old shares does not use up a lot bought in the window.
- **Lots bought in the window** return from the fill price. **Opening lots** (seed rows) return from the close on the seed date, because their broker cost predates the window. Each is reported separately, and only the first is a judgement of this window's buying.
- **Against cash.** Cash is assumed to earn 0%, so the return is also the excess over cash. **Against the index.** The same money in the benchmark from each lot's entry date to its exit or mark date.
- **Entry type.** `deep_limit` is a limit at least 3% from the last complete daily close, below it for a buy and above it for a sell, matching the near edge of `buy.deep_limit_pct_range`. `near_market` is anything closer, including market orders. The suffix `/proposal` means a proposal rung links to the order, and `/session` means an order row with no rung. `external` means a fill with no order row at all: an app trade or one from before the order ledger began. `external_micro` is the crypto subset at or below the small fixed size of a recurring buy programme.
- **Fill rate.** Accepted orders are rows that reached the broker, less rejections and cancel-operation rows. An order is filled if any quantity filled. `Open` counts as unfilled in one column and is removed in the other. An **intent** is one account, symbol, side and local ISO week, so a resubmitted order counts once.
- **Markout.** One row per executed order at its volume-weighted price, to the close 1, 5 and 20 sessions after the fill's session, signed so a positive value favours the trade.
- **Idle cash.** Opening cash is the smallest amount keeping cash at or above zero, assuming no external flows. Opening lots count as held from the account's first day, because they have no purchase date and no cash outflow. Cash share is cash over cash plus holdings at market, averaged by day from the account's first fill. Forgone is each day's cash times the next day's index return, not compounded, so a negative value is a loss that cash avoided. Parking instruments are SGOV, BIL, 459580 and 357870.

## Data checks

Each check below comes from a question raised by the independent dots analysis of the same export. No check found a wrong value written by the export. Two columns it does not have are listed as gaps for the desk.

| Check | Count | Evidence | Treatment | Source |
| --- | --- | --- | --- | --- |
| Rows equal on every economic field, flag already applied | Extra rows: 8. Groups: 2. Orders: 1 | Orders summing to no more than their quantity: 1. Over it: 0. With no order row: 0 | Kept. They are separate trades of one order, with the time cut to the minute | **Export gap**: no `fill_seq` column, so trades cannot be told from repeats |
| Websocket row repeating the next day's reconciler row | Rows: 5 | Same order, side, quantity and price, recorded exactly 24h after its `filled_at` | Removed. Seed rows that absorbed one, reduced by the same quantity: 1 | Ledger data. The export copies `filled_at` as stored. The flag misses them because the stored day differs. Cause of the stamp: unverified |
| Order whose fills exceed its quantity | Orders before cleaning: 5. After: 0 | The repeated rows above, plus an order with a row equal to its whole quantity | Rows removed as partials inside a running total: 2. Unresolved orders: 0 | Ledger data |
| Partial fill of an order with no order row, proven by inventory | Rows: 1 | The symbol's sells exceed all its holdings by exactly this row's quantity, and a later row of the order is larger | Removed | Ledger data |
| Order row behind the ledger | Orders: 76 | The row's filled quantity is below the ledger's, mostly `open` rows with no ending | The ledger's quantity counts, and the outcome is corrected | Ledger data |
| Ledger behind the order row | Fills: 26 | The row says filled and the ledger has less | The residual is added as a fill at the row's price, on the order's date | Ledger data. The export's only row filters are the time window and quarantine, and neither can remove these |
| `manual_import` rows, stamped on a weekend | On a weekend: 45 of 51 rows | `opening_lots.py` stamps the cutover instant and sets quantity to holdings minus net fills, at the broker's average cost. They are opening lots, not transfers | No markout or cash flow. Return runs from the last close before the stamp | Ledger data, documented. **Export gap**: nothing separates a seed from another manual import |
| Other equity rows stamped on a weekend | On a weekend: 3 of 357 rows, before removals | One broker's rows can be stamped a day away from the session | Mapped to the next session. Twins removed as above | Ledger data |
| Fill far from its session's close | Rows: 2 | More than 30% from the close, at prices like an old average cost | Quantity kept, treated as opening lots, with no markout or cash flow | Ledger data |

**Join between the ledger and the order tables.** The two tables hold different populations, so neither is a denominator for the other. The hashed key itself works: executions with no order row whose reference is on any order row of that account: 0.

| Market | Account | Executed orders | With order row | Share of orders | Share of notional | Before the first order row | Recurring micro-buys | Other, after the first order row | Filled order rows that have ledger fills |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| crypto | Account B | 1689 | 108 | 6.4% | 76.4% | 618 | 1561 | 10 | 44 of 45 |
| kr | Account A | 87 | 62 | 71.3% | 48.5% | 24 | 0 | 1 | 60 of 60 |
| kr | Account C | 128 | 128 | 100.0% | 100.0% | 0 | 0 | 0 | 119 of 128 |
| us | Account A | 78 | 58 | 74.4% | 76.1% | 17 | 0 | 3 | 47 of 47 |
| us | Account C | 82 | 82 | 100.0% | 100.0% | 0 | 0 | 0 | 66 of 82 |

## Denominators and exclusions

Fills, from every live ledger row to the fills every section uses. Each row leaves at one step, and the steps add up to the last column:

| Market | Ledger rows | Flagged superseded | Opening seeds | Repeats | Inside a running total | Proven by inventory | Cannot be trades | Added from order rows | Fills used |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| KR | 295 | 63 | 20 | 5 | 0 | 0 | 1 | 9 | 215 |
| US | 165 | 0 | 20 | 0 | 0 | 0 | 1 | 16 | 160 |
| Crypto | 3221 | 1455 | 11 | 0 | 2 | 1 | 0 | 1 | 1753 |

Orders, from every live order row to the fill-rate denominator:

| Market | Order rows | Cancel rows | Rejected | Never accepted | Parking instruments | Accepted orders used |
| --- | --- | --- | --- | --- | --- | --- |
| Crypto | 156 | 0 | 0 | 0 | 0 | 156 |
| KR | 931 | 21 | 91 | 0 | 3 | 816 |
| US | 723 | 3 | 0 | 0 | 10 | 710 |

## Sensitivity

Return on lots bought in the window, under each treatment that could be argued. Only the named treatment changes on each line. Cash shares run KR, US, then crypto, in account-label order.

| Treatment | KR | US | Crypto | Avg cash share by account |
| --- | --- | --- | --- | --- |
| Base: every rule in this report | -14.55% (+5.34% vs index, 94 lots) | +15.51% (+14.40% vs index, 27 lots) | +16.62% (+7.58% vs index, 1442 lots) | 27.2% / 18.2% / 16.8% / 12.0% / 34.6% |
| No duplicate removal: trust `superseded_duplicate` alone | -14.47% (+5.35% vs index, 94 lots) | +15.51% (+14.40% vs index, 27 lots) | +17.43% (+8.12% vs index, 1444 lots) | 37.3% / 18.2% / 16.8% / 12.0% / 35.0% |
| Keep the fill proven a partial only by inventory | -14.55% (+5.34% vs index, 94 lots) | +15.51% (+14.40% vs index, 27 lots) | +16.62% (+7.58% vs index, 1442 lots) | 27.2% / 18.2% / 16.8% / 12.0% / 34.5% |
| Drop fills found only on an order row | -15.20% (+5.66% vs index, 90 lots) | +16.74% (+16.17% vs index, 13 lots) | +17.82% (+8.37% vs index, 1441 lots) | 27.2% / 16.6% / 16.8% / 13.9% / 34.1% |
| Keep fills that cannot be trades as trades | -10.59% (+9.37% vs index, 95 lots) | +25.65% (+24.32% vs index, 28 lots) | +16.62% (+7.58% vs index, 1442 lots) | 25.7% / 18.2% / 14.8% / 12.0% / 34.6% |
| Naive: count opening lots as buys at their stated cost | -12.46% (+1.75% vs index, 116 lots) | +30.42% (+28.08% vs index, 48 lots) | +3.63% (+4.84% vs index, 1598 lots) | 50.2% / 18.2% / 50.0% / 12.0% / 36.3% |
| Plain FIFO: older holdings do not go first | -13.43% (+5.78% vs index, 94 lots) | +14.61% (+13.59% vs index, 27 lots) | +15.62% (+6.90% vs index, 1442 lots) | 27.2% / 18.2% / 16.8% / 12.0% / 34.6% |
| Only lots whose order has an order row | -14.18% (+5.31% vs index, 84 lots) | +18.64% (+16.75% vs index, 21 lots) | +18.09% (+9.53% vs index, 13 lots) | as base |
| Without recurring micro-buys | -14.55% (+5.34% vs index, 94 lots) | +15.51% (+14.40% vs index, 27 lots) | +17.31% (+9.00% vs index, 22 lots) | as base |

## KR

Amounts are in KRW. Index: KOSPI (^KS11, Yahoo).

### KR: 2. Positions, against cash and the index

| Scope | Positions | Still open | Realized | Unrealized | Total | Median position | Winners | Hold days (cost-wtd) | Hold days (median) | vs cash (0%) | Index, same dates | vs index |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Account A: bought in window | 22 | 15 | +16.20% | -25.11% | -8.58% | -12.71% | 40.9% | 95 | 126 | -8.58% | -14.64% | +6.06% |
| Account A: opening lots, from the seed-date close | 15 | 11 | +10.11% | -29.25% | -12.22% | -14.87% | 20.0% | 89 | 98 | -12.22% | -8.61% | -3.61% |
| Account C: bought in window | 31 | 22 | -1.14% | -34.21% | -17.86% | +0.57% | 51.6% | 92 | 93 | -17.86% | -22.81% | +4.95% |
| **All: bought in window** | 53 | 37 | +4.23% | -30.60% | -14.55% | -6.45% | 47.2% | 93 | 100 | -14.55% | -19.90% | +5.34% |
| **All: opening lots, from the seed-date close** | 15 | 11 | +10.11% | -29.25% | -12.22% | -14.87% | 20.0% | 89 | 98 | -12.22% | -8.61% | -3.61% |
| Parking instruments (cash-like) | 2 | 2 | n/a | +0.02% | +0.02% | +0.02% | 100.0% | 38 | 35 | +0.02% | -16.49% | +16.51% |

Opening lots measured on the broker's average cost instead: -10.07% (n=15). That cost predates the window, so no index comparison applies to it.

Sells with no known basis, excluded from every return above:

| Scope | Sell pieces | Share of sold notional |
| --- | --- | --- |
| Account A | 6 | 11.2% |
| Account C | 5 | 33.1% |
| **All** | 11 | 20.0% |

### KR: 3. Entry types, fill rates and markouts

Accepted orders: 816 from 928 rows, excluding parking instruments.

| Side | Entry type | Accepted | Filled | Partial | Expired | Cancelled | Open | Fill rate | Fill rate, open removed | Intents | Intent fill rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| buy | deep_limit/proposal | 161 | 1 | 0 | 119 | 7 | 34 | 0.6% | 0.8% | 94 | 1.1% |
| buy | deep_limit/session | 127 | 35 | 0 | 20 | 72 | 0 | 27.6% | 27.6% | 76 | 39.5% |
| buy | near_market/proposal | 43 | 2 | 0 | 29 | 0 | 12 | 4.7% | 6.5% | 40 | 5.0% |
| buy | near_market/session | 117 | 46 | 0 | 17 | 54 | 0 | 39.3% | 39.3% | 78 | 55.1% |
| sell | deep_limit/proposal | 138 | 20 | 0 | 115 | 0 | 3 | 14.5% | 14.8% | 76 | 23.7% |
| sell | deep_limit/session | 100 | 23 | 0 | 49 | 28 | 0 | 23.0% | 23.0% | 47 | 44.7% |
| sell | near_market/proposal | 99 | 40 | 0 | 57 | 0 | 2 | 40.4% | 41.2% | 58 | 51.7% |
| sell | near_market/session | 31 | 20 | 0 | 8 | 3 | 0 | 64.5% | 64.5% | 25 | 64.0% |

Limit distance from the reference close (policy band edge 3%):

| Side | Distance | Accepted | Filled | Partial | Expired | Cancelled | Open | Fill rate | Fill rate, open removed | Intents | Intent fill rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| buy | a. >=12% | 9 | 0 | 0 | 3 | 6 | 0 | 0.0% | 0.0% | 9 | 0.0% |
| buy | b. 3-12% | 279 | 36 | 0 | 136 | 73 | 34 | 12.9% | 14.7% | 168 | 18.5% |
| buy | c. 1-3% | 94 | 21 | 0 | 32 | 31 | 10 | 22.3% | 25.0% | 77 | 26.0% |
| buy | d. 0-1% | 40 | 11 | 0 | 8 | 19 | 2 | 27.5% | 28.9% | 35 | 31.4% |
| buy | e. marketable | 26 | 16 | 0 | 6 | 4 | 0 | 61.5% | 61.5% | 24 | 66.7% |
| sell | a. >=12% | 34 | 3 | 0 | 19 | 11 | 1 | 8.8% | 9.1% | 19 | 15.8% |
| sell | b. 3-12% | 204 | 40 | 0 | 145 | 17 | 2 | 19.6% | 19.8% | 114 | 31.6% |
| sell | c. 1-3% | 81 | 35 | 0 | 42 | 2 | 2 | 43.2% | 44.3% | 55 | 54.5% |
| sell | d. 0-1% | 29 | 13 | 0 | 15 | 1 | 0 | 44.8% | 44.8% | 25 | 48.0% |
| sell | e. marketable | 20 | 12 | 0 | 8 | 0 | 0 | 60.0% | 60.0% | 20 | 60.0% |

By account:

| Side | Account | Accepted | Filled | Partial | Expired | Cancelled | Open | Fill rate | Fill rate, open removed | Intents | Intent fill rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| buy | Account A | 117 | 21 | 0 | 46 | 4 | 46 | 17.9% | 29.6% | 66 | 27.3% |
| buy | Account C | 331 | 63 | 0 | 139 | 129 | 0 | 19.0% | 19.0% | 161 | 30.4% |
| sell | Account A | 130 | 40 | 0 | 85 | 1 | 4 | 30.8% | 31.7% | 65 | 47.7% |
| sell | Account C | 238 | 63 | 0 | 144 | 30 | 1 | 26.5% | 26.6% | 90 | 46.7% |

Markouts of executed orders, mean with the number of orders in brackets. A positive value means the trade looked right. `x` is the same value net of the index. The 95% interval resamples symbols.

| Side | Entry type | Orders | Symbols | 1s | 1s x | 5s | 5s x | 20s | 20s x | 20s 95% CI |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| buy | deep_limit/proposal | 1 | 1 | +0.00% (1) | +5.46% | -18.63% (1) | -8.98% | n/a (0) | n/a | n/a |
| buy | deep_limit/session | 35 | 20 | +1.34% (35) | +2.21% | +0.21% (35) | +1.64% | -9.10% (35) | +8.25% | -14.13% to -3.08% |
| buy | external | 11 | 8 | -1.57% (11) | -1.06% | -3.57% (11) | -3.21% | -2.89% (11) | -9.32% | -16.77% to +9.82% |
| buy | near_market/proposal | 2 | 2 | +2.01% (2) | +1.57% | -6.89% (2) | -0.96% | +4.20% (1) | +3.40% | n/a |
| buy | near_market/session | 46 | 32 | +0.90% (46) | +0.43% | -1.02% (46) | -0.39% | -4.77% (46) | +13.19% | -9.57% to +0.20% |
| sell | deep_limit/proposal | 20 | 13 | +1.32% (20) | +0.82% | +3.65% (20) | +0.47% | +11.10% (18) | +4.46% | +3.85% to +16.44% |
| sell | deep_limit/session | 23 | 17 | -0.37% (23) | -0.80% | +4.61% (23) | +3.07% | +11.44% (23) | -6.72% | +5.62% to +17.28% |
| sell | external | 14 | 9 | +0.70% (14) | +0.27% | +5.79% (14) | +3.85% | +4.81% (14) | +8.07% | -9.00% to +19.45% |
| sell | near_market/proposal | 40 | 17 | +1.02% (40) | +1.15% | +2.41% (40) | +1.18% | +5.12% (36) | +1.47% | -2.71% to +10.85% |
| sell | near_market/session | 20 | 15 | +0.37% (20) | +0.59% | +5.68% (20) | +0.17% | +6.96% (20) | -10.39% | +2.43% to +12.04% |

### KR: 4. Idle cash

| Account | Days | From | To | Avg cash share | Avg cash + parking share | Last cash share | Last cash + parking share | Forgone, % of avg cash | Forgone, % of avg NAV | Index return, same dates | Account return, same dates |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Account A | 121 | 2026-05-04 | 2026-10-30 | 27.2% | 27.8% | 36.5% | 39.6% | -37.50% | -10.11% | -17.39% | -16.96% |
| Account C | 94 | 2026-06-15 | 2026-10-30 | 18.2% | 27.5% | 26.6% | 61.0% | -21.99% | -4.07% | -32.94% | -20.92% |

### KR: 5. Largest winners and losers, and candidate rules

Positions ranked: 62, parking and dust excluded.

**Largest winners**

| Rank | Account | Origin | Return | vs index | Share of gross absolute P&L | Hold days | State | Entry type | Entry rung | Entry tag | Exit type | Exit rung | Exit tag |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| W1 | Account A | fill+seed | +21.91% | +18.58% | 12.6% | 47 | open | seed |  |  | external |  |  |
| W2 | Account A | fill | +20.35% | +7.47% | 2.3% | 6 | closed | deep_limit/session |  | oth_06113e6e… | deep_limit/session |  |  |
| W3 | Account C | fill | +6.24% | +31.90% | 0.6% | 100 | open | near_market/session |  | oth_af90e99f… | deep_limit/proposal | 1 | oth_4f4202f2… |
| W4 | Account A | fill | +23.96% | +36.96% | 0.5% | 52 | closed | near_market/session |  | oth_fdba857b… | deep_limit/session |  |  |
| W5 | Account A | fill | +12.22% | +16.74% | 0.4% | 31 | closed | near_market/session |  | oth_7ba40b5d… | deep_limit/session |  |  |

**Largest losers**

| Rank | Account | Origin | Return | vs index | Share of gross absolute P&L | Hold days | State | Entry type | Entry rung | Entry tag | Exit type | Exit rung | Exit tag |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| L1 | Account A | seed | -35.98% | -12.41% | 9.5% | 173 | open | seed |  |  | held |  |  |
| L2 | Account A | fill+seed | -18.56% | -20.35% | 9.1% | 63 | open | seed |  |  | external |  |  |
| L3 | Account A | fill+seed | -21.74% | -0.33% | 8.6% | 109 | open | seed |  |  | held |  |  |
| L4 | Account A | fill+seed | -30.50% | -1.65% | 6.8% | 136 | open | seed |  |  | held |  |  |
| L5 | Account A | seed | -45.92% | -30.72% | 5.9% | 121 | open | seed |  |  | external |  |  |

**Candidate rules.** In-sample: each number was computed on the same six months it would have changed, so none of them predicts anything.

| Candidate rule | Sample | Counterfactual |
| --- | --- | --- |
| R1 Buy the unfilled `deep_limit` orders at the next open, hold 20 sessions | 68 of 108 reach 20s (99 reach 5s) | 20s -7.56%, net of index +2.71%; 5s -0.97%, net of index +0.57% |
| R1 Buy the unfilled `near_market` orders at the next open, hold 20 sessions | 28 of 52 reach 20s (46 reach 5s) | 20s -6.69%, net of index +10.17%; 5s -2.46%, net of index +0.02% |
| R2 Hold instead of selling: `loss_cut` | 1 of 1 reach 20s (1 reach 5s) | 20s +78.58% of sold notional; 5s +29.97% |
| R2 Hold instead of selling: `no_order_row` | 14 of 14 reach 20s (14 reach 5s) | 20s -18.94% of sold notional; 5s -11.54% |
| R2 Hold instead of selling: `other_proposal` | 51 of 53 reach 20s (53 reach 5s) | 20s -10.67% of sold notional; 5s -4.41% |
| R2 Hold instead of selling: `session` | 43 of 43 reach 20s (43 reach 5s) | 20s -15.85% of sold notional; 5s -4.80% |
| R2 Hold instead of selling: `trim_preplace` | 2 of 6 reach 20s (6 reach 5s) | 20s -13.92% of sold notional; 5s -2.81% |
| R3 Exit every in-window lot 20 sessions after entry | 94 lots, 92 reach the horizon | actual -14.55% of cost, rule -8.32% |
| R4 Never open lots of type `deep_limit/proposal` | 1 lots | removes a return of -23.45% on 0.3% of in-window cost; net of index -13.21% |
| R4 Never open lots of type `deep_limit/session` | 35 lots | removes a return of -14.04% on 40.3% of in-window cost; net of index +2.27% |
| R4 Never open lots of type `external` | 10 lots | removes a return of -18.51% on 8.5% of in-window cost; net of index +5.74% |
| R4 Never open lots of type `near_market/proposal` | 2 lots | removes a return of +8.52% on 0.6% of in-window cost; net of index +11.50% |
| R4 Never open lots of type `near_market/session` | 46 lots | removes a return of -14.52% on 50.2% of in-window cost; net of index +7.78% |
| R5 Buy the index instead, for each in-window lot's money and dates | 94 lots | actual -14.55%, index -19.90% |
| R6 Hold the lower-bound idle cash in the index: Account A | 121 days | -10.11% of average NAV |
| R6 Hold the lower-bound idle cash in the index: Account C | 94 days | -4.07% of average NAV |

## US

Amounts are in USD. Index: S&P 500 (^GSPC, Yahoo).

### US: 2. Positions, against cash and the index

| Scope | Positions | Still open | Realized | Unrealized | Total | Median position | Winners | Hold days (cost-wtd) | Hold days (median) | vs cash (0%) | Index, same dates | vs index |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Account A: bought in window | 8 | 3 | +11.60% | +24.31% | +16.52% | +14.37% | 75.0% | 88 | 80 | +16.52% | +0.29% | +16.23% |
| Account A: opening lots, from the seed-date close | 21 | 15 | +2.05% | +4.76% | +3.48% | +2.47% | 61.9% | 132 | 130 | +3.48% | +2.65% | +0.83% |
| Account C: bought in window | 11 | 4 | +13.80% | +17.88% | +14.68% | +19.94% | 90.9% | 81 | 63 | +14.68% | +1.77% | +12.91% |
| **All: bought in window** | 19 | 7 | +12.95% | +21.71% | +15.51% | +15.60% | 84.2% | 85 | 79 | +15.51% | +1.10% | +14.40% |
| **All: opening lots, from the seed-date close** | 21 | 15 | +2.05% | +4.76% | +3.48% | +2.47% | 61.9% | 132 | 130 | +3.48% | +2.65% | +0.83% |
| Parking instruments (cash-like) | 2 | 2 | n/a | +0.15% | +0.15% | +0.16% | 100.0% | 41 | 42 | +0.15% | -1.49% | +1.63% |

Opening lots measured on the broker's average cost instead: +34.20% (n=21). That cost predates the window, so no index comparison applies to it.

Sells with no known basis, excluded from every return above:

| Scope | Sell pieces | Share of sold notional |
| --- | --- | --- |
| Account A | 7 | 13.2% |
| Account C | 41 | 83.9% |
| **All** | 48 | 47.7% |

### US: 3. Entry types, fill rates and markouts

Accepted orders: 710 from 713 rows, excluding parking instruments.

| Side | Entry type | Accepted | Filled | Partial | Expired | Cancelled | Open | Fill rate | Fill rate, open removed | Intents | Intent fill rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| buy | deep_limit/proposal | 83 | 2 | 0 | 3 | 78 | 0 | 2.4% | 2.4% | 50 | 2.0% |
| buy | deep_limit/session | 20 | 1 | 0 | 1 | 16 | 2 | 5.0% | 5.6% | 18 | 5.6% |
| buy | near_market/proposal | 18 | 0 | 0 | 1 | 17 | 0 | 0.0% | 0.0% | 17 | 0.0% |
| buy | near_market/session | 33 | 18 | 0 | 3 | 11 | 1 | 54.5% | 56.2% | 26 | 57.7% |
| sell | deep_limit/proposal | 102 | 9 | 0 | 40 | 52 | 1 | 8.8% | 8.9% | 66 | 13.6% |
| sell | deep_limit/session | 33 | 2 | 0 | 9 | 3 | 19 | 6.1% | 14.3% | 15 | 13.3% |
| sell | near_market/proposal | 385 | 88 | 0 | 119 | 176 | 2 | 22.9% | 23.0% | 179 | 39.7% |
| sell | near_market/session | 36 | 11 | 0 | 11 | 1 | 13 | 30.6% | 47.8% | 16 | 43.8% |

Limit distance from the reference close (policy band edge 3%):

| Side | Distance | Accepted | Filled | Partial | Expired | Cancelled | Open | Fill rate | Fill rate, open removed | Intents | Intent fill rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| buy | a. >=12% | 1 | 0 | 0 | 0 | 1 | 0 | 0.0% | 0.0% | 1 | 0.0% |
| buy | b. 3-12% | 102 | 3 | 0 | 4 | 93 | 2 | 2.9% | 3.0% | 67 | 3.0% |
| buy | c. 1-3% | 31 | 8 | 0 | 3 | 20 | 0 | 25.8% | 25.8% | 29 | 27.6% |
| buy | d. 0-1% | 8 | 3 | 0 | 0 | 5 | 0 | 37.5% | 37.5% | 8 | 37.5% |
| buy | e. marketable | 12 | 7 | 0 | 1 | 3 | 1 | 58.3% | 63.6% | 12 | 58.3% |
| sell | a. >=12% | 6 | 0 | 0 | 3 | 1 | 2 | 0.0% | 0.0% | 5 | 0.0% |
| sell | b. 3-12% | 129 | 11 | 0 | 46 | 54 | 18 | 8.5% | 9.9% | 79 | 13.9% |
| sell | c. 1-3% | 254 | 43 | 0 | 86 | 115 | 10 | 16.9% | 17.6% | 150 | 26.7% |
| sell | d. 0-1% | 117 | 35 | 0 | 29 | 51 | 2 | 29.9% | 30.4% | 85 | 36.5% |
| sell | e. marketable | 50 | 21 | 0 | 15 | 11 | 3 | 42.0% | 44.7% | 47 | 42.6% |

By account:

| Side | Account | Accepted | Filled | Partial | Expired | Cancelled | Open | Fill rate | Fill rate, open removed | Intents | Intent fill rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| buy | Account A | 15 | 4 | 0 | 8 | 0 | 3 | 26.7% | 33.3% | 11 | 27.3% |
| buy | Account C | 139 | 17 | 0 | 0 | 122 | 0 | 12.2% | 12.2% | 90 | 14.4% |
| sell | Account A | 264 | 52 | 0 | 179 | 0 | 33 | 19.7% | 22.5% | 117 | 37.6% |
| sell | Account C | 292 | 58 | 0 | 0 | 232 | 2 | 19.9% | 20.0% | 108 | 41.7% |

Markouts of executed orders, mean with the number of orders in brackets. A positive value means the trade looked right. `x` is the same value net of the index. The 95% interval resamples symbols.

| Side | Entry type | Orders | Symbols | 1s | 1s x | 5s | 5s x | 20s | 20s x | 20s 95% CI |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| buy | deep_limit/proposal | 2 | 1 | -2.05% (2) | -1.73% | -7.03% (2) | -5.72% | +18.38% (2) | +15.04% | n/a |
| buy | deep_limit/session | 1 | 1 | -2.34% (1) | -0.91% | -3.18% (1) | -2.75% | +11.01% (1) | +10.53% | n/a |
| buy | external | 6 | 6 | -0.84% (6) | -1.00% | -2.04% (6) | -0.66% | +0.62% (6) | +1.03% | -9.56% to +9.98% |
| buy | near_market/session | 18 | 11 | -2.44% (18) | -1.99% | -1.01% (18) | -0.34% | +4.14% (18) | +3.84% | +0.93% to +6.86% |
| sell | deep_limit/proposal | 9 | 7 | +0.54% (9) | +0.03% | -0.18% (9) | +0.09% | -1.82% (8) | -0.66% | -10.65% to +5.31% |
| sell | deep_limit/session | 2 | 2 | +5.20% (2) | +5.28% | +3.57% (2) | +2.93% | +13.43% (2) | +13.60% | n/a |
| sell | external | 14 | 9 | -0.51% (14) | -0.28% | +1.81% (14) | +1.09% | +4.76% (14) | +4.41% | -2.14% to +14.07% |
| sell | near_market/proposal | 88 | 25 | +0.04% (88) | +0.11% | -0.26% (88) | -0.40% | -1.68% (81) | -1.05% | -4.48% to +1.33% |
| sell | near_market/session | 11 | 4 | -0.32% (11) | -0.57% | -1.22% (11) | -1.04% | -3.28% (11) | -3.00% | -7.52% to +13.26% |

### US: 4. Idle cash

| Account | Days | From | To | Avg cash share | Avg cash + parking share | Last cash share | Last cash + parking share | Forgone, % of avg cash | Forgone, % of avg NAV | Index return, same dates | Account return, same dates |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Account A | 124 | 2026-05-06 | 2026-10-30 | 16.8% | 24.8% | 19.6% | 48.4% | +2.75% | +0.47% | +2.92% | +6.27% |
| Account C | 97 | 2026-06-15 | 2026-10-30 | 12.0% | 39.3% | 0.0% | 95.5% | +2.68% | +0.32% | +0.34% | +4.37% |

### US: 5. Largest winners and losers, and candidate rules

Positions ranked: 38, parking and dust excluded.

**Largest winners**

| Rank | Account | Origin | Return | vs index | Share of gross absolute P&L | Hold days | State | Entry type | Entry rung | Entry tag | Exit type | Exit rung | Exit tag |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| W1 | Account A | fill+seed | +23.02% | +20.71% | 20.7% | 157 | open | seed |  |  | near_market/proposal | 1 | oth_2e4c17af… |
| W2 | Account A | fill+seed | +36.35% | +33.65% | 19.6% | 156 | open | seed |  |  | near_market/proposal | 1 | oth_179f3142… |
| W3 | Account A | seed | +12.96% | +11.27% | 4.8% | 71 | open | seed |  |  | near_market/session |  |  |
| W4 | Account A | seed | +10.13% | +7.13% | 4.2% | 130 | open | seed |  |  | near_market/proposal | 1 | oth_0ce8e439… |
| W5 | Account C | fill | +65.61% | +61.82% | 4.1% | 105 | closed | deep_limit/session |  | oth_4545d8a1… | near_market/proposal | 1 | sell.trim_preplace.rsi_confirmed_resistance |

**Largest losers**

| Rank | Account | Origin | Return | vs index | Share of gross absolute P&L | Hold days | State | Entry type | Entry rung | Entry tag | Exit type | Exit rung | Exit tag |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| L1 | Account A | seed | -16.63% | -19.77% | 12.5% | 120 | open | seed |  |  | near_market/proposal | 1 | sell.trim_preplace.ultra_near_resistance |
| L2 | Account A | seed | -9.60% | -12.51% | 6.3% | 142 | open | seed |  |  | near_market/proposal | 1 | oth_d2bf84f3… |
| L3 | Account A | seed | -9.04% | -11.93% | 1.5% | 157 | open | seed |  |  | near_market/proposal | 1 | oth_fd4e0d6a… |
| L4 | Account A | seed | -1.07% | -3.73% | 1.4% | 124 | open | seed |  |  | external |  |  |
| L5 | Account A | seed | -1.90% | -4.29% | 1.3% | 142 | open | seed |  |  | external |  |  |

**Candidate rules.** In-sample: each number was computed on the same six months it would have changed, so none of them predicts anything.

| Candidate rule | Sample | Counterfactual |
| --- | --- | --- |
| R1 Buy the unfilled `deep_limit` orders at the next open, hold 20 sessions | 47 of 61 reach 20s (58 reach 5s) | 20s +4.64%, net of index +3.23%; 5s +0.47%, net of index +0.30% |
| R1 Buy the unfilled `near_market` orders at the next open, hold 20 sessions | 17 of 24 reach 20s (20 reach 5s) | 20s +3.50%, net of index +2.47%; 5s -0.70%, net of index -1.07% |
| R2 Hold instead of selling: `no_order_row` | 14 of 14 reach 20s (14 reach 5s) | 20s -1.97% of sold notional; 5s -1.57% |
| R2 Hold instead of selling: `other_proposal` | 60 of 60 reach 20s (60 reach 5s) | 20s +3.88% of sold notional; 5s +0.59% |
| R2 Hold instead of selling: `session` | 13 of 13 reach 20s (13 reach 5s) | 20s +0.36% of sold notional; 5s +0.57% |
| R2 Hold instead of selling: `trim_preplace` | 29 of 37 reach 20s (37 reach 5s) | 20s +0.51% of sold notional; 5s +0.69% |
| R3 Exit every in-window lot 20 sessions after entry | 27 lots, 27 reach the horizon | actual +15.51% of cost, rule +2.65% |
| R4 Never open lots of type `deep_limit/proposal` | 2 lots | removes a return of +15.96% on 4.7% of in-window cost; net of index +13.47% |
| R4 Never open lots of type `deep_limit/session` | 1 lots | removes a return of +61.67% on 2.2% of in-window cost; net of index +58.58% |
| R4 Never open lots of type `external` | 6 lots | removes a return of +9.63% on 34.8% of in-window cost; net of index +10.01% |
| R4 Never open lots of type `near_market/session` | 18 lots | removes a return of +17.27% on 58.4% of in-window cost; net of index +15.47% |
| R5 Buy the index instead, for each in-window lot's money and dates | 27 lots | actual +15.51%, index +1.10% |
| R6 Hold the lower-bound idle cash in the index: Account A | 124 days | +0.47% of average NAV |
| R6 Hold the lower-bound idle cash in the index: Account C | 97 days | +0.32% of average NAV |

## Crypto

Amounts are in KRW. Index: BTC/KRW (KRW-BTC, Upbit).

### Crypto: 2. Positions, against cash and the index

| Scope | Positions | Still open | Realized | Unrealized | Total | Median position | Winners | Hold days (cost-wtd) | Hold days (median) | vs cash (0%) | Index, same dates | vs index |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Account B: bought in window | 8 | 2 | +19.35% | +0.98% | +16.62% | +3.66% | 50.0% | 80 | 67 | +16.62% | +9.05% | +7.58% |
| Account B: opening lots, from the seed-date close | 6 | 2 | -4.06% | -10.34% | -6.60% | -7.80% | 50.0% | 134 | 117 | -6.60% | -11.40% | +4.80% |
| **All: bought in window** | 8 | 2 | +19.35% | +0.98% | +16.62% | +3.66% | 50.0% | 80 | 67 | +16.62% | +9.05% | +7.58% |
| **All: opening lots, from the seed-date close** | 6 | 2 | -4.06% | -10.34% | -6.60% | -7.80% | 50.0% | 134 | 117 | -6.60% | -11.40% | +4.80% |

Opening lots measured on the broker's average cost instead: -10.83% (n=6). That cost predates the window, so no index comparison applies to it.

Sells with no known basis, excluded from every return above:

| Scope | Sell pieces | Share of sold notional |
| --- | --- | --- |
| Account B | 32 | 28.1% |
| **All** | 32 | 28.1% |

### Crypto: 3. Entry types, fill rates and markouts

Accepted orders: 156 from 156 rows, excluding parking instruments.

| Side | Entry type | Accepted | Filled | Partial | Expired | Cancelled | Open | Fill rate | Fill rate, open removed | Intents | Intent fill rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| buy | deep_limit/proposal | 8 | 1 | 0 | 0 | 7 | 0 | 12.5% | 12.5% | 4 | 25.0% |
| buy | deep_limit/session | 8 | 2 | 0 | 0 | 6 | 0 | 25.0% | 25.0% | 3 | 66.7% |
| buy | near_market/proposal | 2 | 0 | 0 | 0 | 1 | 1 | 0.0% | 0.0% | 2 | 0.0% |
| buy | near_market/session | 9 | 8 | 0 | 0 | 1 | 0 | 88.9% | 88.9% | 8 | 87.5% |
| sell | deep_limit/proposal | 29 | 23 | 0 | 0 | 3 | 3 | 79.3% | 88.5% | 13 | 76.9% |
| sell | deep_limit/session | 8 | 2 | 0 | 0 | 6 | 0 | 25.0% | 25.0% | 3 | 66.7% |
| sell | near_market/proposal | 89 | 69 | 0 | 0 | 1 | 19 | 77.5% | 98.6% | 32 | 84.4% |
| sell | near_market/session | 3 | 3 | 0 | 0 | 0 | 0 | 100.0% | 100.0% | 3 | 100.0% |

Limit distance from the reference close (policy band edge 3%):

| Side | Distance | Accepted | Filled | Partial | Expired | Cancelled | Open | Fill rate | Fill rate, open removed | Intents | Intent fill rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| buy | a. >=12% | 4 | 0 | 0 | 0 | 4 | 0 | 0.0% | 0.0% | 3 | 0.0% |
| buy | b. 3-12% | 12 | 3 | 0 | 0 | 9 | 0 | 25.0% | 25.0% | 6 | 50.0% |
| buy | c. 1-3% | 5 | 5 | 0 | 0 | 0 | 0 | 100.0% | 100.0% | 5 | 100.0% |
| buy | d. 0-1% | 1 | 0 | 0 | 0 | 0 | 1 | 0.0% | n/a | 1 | 0.0% |
| buy | e. marketable | 5 | 3 | 0 | 0 | 2 | 0 | 60.0% | 60.0% | 4 | 50.0% |
| sell | a. >=12% | 6 | 3 | 0 | 0 | 3 | 0 | 50.0% | 50.0% | 4 | 75.0% |
| sell | b. 3-12% | 31 | 22 | 0 | 0 | 6 | 3 | 71.0% | 78.6% | 15 | 73.3% |
| sell | c. 1-3% | 26 | 17 | 0 | 0 | 1 | 8 | 65.4% | 94.4% | 19 | 63.2% |
| sell | d. 0-1% | 29 | 22 | 0 | 0 | 0 | 7 | 75.9% | 100.0% | 22 | 72.7% |
| sell | e. marketable | 37 | 33 | 0 | 0 | 0 | 4 | 89.2% | 100.0% | 20 | 95.0% |

By account:

| Side | Account | Accepted | Filled | Partial | Expired | Cancelled | Open | Fill rate | Fill rate, open removed | Intents | Intent fill rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| buy | Account B | 27 | 11 | 0 | 0 | 15 | 1 | 40.7% | 42.3% | 13 | 53.8% |
| sell | Account B | 129 | 97 | 0 | 0 | 10 | 22 | 75.2% | 90.7% | 40 | 82.5% |

Markouts of executed orders, mean with the number of orders in brackets. A positive value means the trade looked right. `x` is the same value net of the index. The 95% interval resamples symbols.

| Side | Entry type | Orders | Symbols | 1s | 1s x | 5s | 5s x | 20s | 20s x | 20s 95% CI |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| buy | deep_limit/proposal | 1 | 1 | +2.87% (1) | -0.50% | +2.89% (1) | -0.50% | -2.06% (1) | -0.48% | n/a |
| buy | deep_limit/session | 2 | 2 | -0.60% (2) | +0.16% | -0.46% (2) | +1.22% | +9.72% (2) | +5.83% | n/a |
| buy | external | 6 | 5 | -4.25% (6) | -4.43% | -0.40% (6) | -3.29% | -2.16% (6) | +5.90% | -12.24% to +8.23% |
| buy | external_micro | 1561 | 4 | -0.29% (1561) | -0.10% | -1.26% (1561) | +0.02% | -2.46% (1561) | +0.72% | -3.60% to -1.32% |
| buy | near_market/session | 8 | 4 | +1.70% (8) | +2.01% | +2.25% (8) | +3.61% | +7.84% (8) | +10.71% | -0.79% to +21.10% |
| sell | deep_limit/proposal | 23 | 4 | -1.75% (23) | -1.07% | -4.49% (23) | -0.99% | -0.02% (23) | +0.14% | -2.36% to +5.75% |
| sell | deep_limit/session | 2 | 1 | -0.23% (2) | -0.38% | +2.17% (2) | +2.43% | +15.39% (2) | +18.88% | n/a |
| sell | external | 14 | 8 | -0.10% (14) | -0.13% | -1.13% (14) | -1.12% | +8.97% (14) | +3.61% | +2.51% to +14.71% |
| sell | near_market/proposal | 69 | 6 | +0.59% (69) | +0.23% | +0.72% (69) | +0.33% | -2.54% (57) | -0.43% | -7.91% to +3.03% |
| sell | near_market/session | 3 | 2 | -3.42% (3) | -4.67% | +2.34% (3) | +2.79% | +11.24% (3) | +10.18% | n/a |

### Crypto: 4. Idle cash

| Account | Days | From | To | Avg cash share | Avg cash + parking share | Last cash share | Last cash + parking share | Forgone, % of avg cash | Forgone, % of avg NAV | Index return, same dates | Account return, same dates |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Account B | 184 | 2026-05-01 | 2026-10-31 | 34.6% | 34.6% | 79.2% | 79.2% | -25.07% | -9.25% | -9.89% | +10.66% |

### Crypto: 5. Largest winners and losers, and candidate rules

Positions ranked: 10, parking and dust excluded.

**Largest winners**

| Rank | Account | Origin | Return | vs index | Share of gross absolute P&L | Hold days | State | Entry type | Entry rung | Entry tag | Exit type | Exit rung | Exit tag |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| W1 | Account B | fill+seed | +17.27% | +14.84% | 29.8% | 94 | closed | seed |  |  | near_market/proposal | 1 | oth_b16ae9a3… |
| W2 | Account B | fill+seed | +9.53% | -0.36% | 16.1% | 78 | open | near_market/session |  | oth_2616e3e1… | deep_limit/proposal | 1 | oth_dcfdc8df… |
| W3 | Account B | fill+seed | +19.13% | +16.70% | 15.3% | 112 | closed | seed |  |  | near_market/proposal | 1 | oth_d39f952e… |
| W4 | Account B | seed | +8.23% | +18.32% | 6.2% | 167 | open | seed |  |  | deep_limit/proposal | 1 | oth_c7be9155… |
| W5 | Account B | fill | +10.17% | +17.27% | 3.7% | 24 | closed | near_market/session |  | oth_d4e72b19… | deep_limit/session |  |  |

**Largest losers**

| Rank | Account | Origin | Return | vs index | Share of gross absolute P&L | Hold days | State | Entry type | Entry rung | Entry tag | Exit type | Exit rung | Exit tag |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| L1 | Account B | fill+seed | -13.56% | -7.79% | 15.8% | 158 | open | seed |  |  | held |  |  |
| L2 | Account B | seed | -32.38% | -13.02% | 11.2% | 57 | closed | seed |  |  | external |  |  |
| L3 | Account B | fill | -75.40% | -55.43% | 0.9% | 57 | closed | external |  |  | external |  |  |
| L4 | Account B | fill | -8.27% | -3.33% | 0.8% | 40 | closed | near_market/session |  | oth_3bf30d41… | near_market/proposal | 1 | oth_56adb692… |
| L5 | Account B | fill | -13.32% | +7.22% | 0.2% | 58 | closed | external |  |  | external |  |  |

**Candidate rules.** In-sample: each number was computed on the same six months it would have changed, so none of them predicts anything.

| Candidate rule | Sample | Counterfactual |
| --- | --- | --- |
| R1 Buy the unfilled `deep_limit` orders at the next open, hold 20 sessions | 3 of 3 reach 20s (3 reach 5s) | 20s -1.13%, net of index +1.05%; 5s +1.44%, net of index -0.33% |
| R1 Buy the unfilled `near_market` orders at the next open, hold 20 sessions | 3 of 3 reach 20s (3 reach 5s) | 20s +3.73%, net of index -0.09%; 5s +0.88%, net of index -0.08% |
| R2 Hold instead of selling: `loss_cut` | 4 of 4 reach 20s (4 reach 5s) | 20s +13.87% of sold notional; 5s +6.34% |
| R2 Hold instead of selling: `no_order_row` | 14 of 14 reach 20s (14 reach 5s) | 20s -8.77% of sold notional; 5s +0.46% |
| R2 Hold instead of selling: `other_proposal` | 72 of 72 reach 20s (72 reach 5s) | 20s +1.59% of sold notional; 5s +0.89% |
| R2 Hold instead of selling: `session` | 5 of 5 reach 20s (5 reach 5s) | 20s -10.91% of sold notional; 5s -0.54% |
| R2 Hold instead of selling: `trim_preplace` | 4 of 16 reach 20s (16 reach 5s) | 20s -13.30% of sold notional; 5s -3.15% |
| R3 Exit every in-window lot 20 sessions after entry | 1442 lots, 1442 reach the horizon | actual +16.62% of cost, rule +2.39% |
| R4 Never open lots of type `deep_limit/proposal` | 1 lots | removes a return of +12.58% on 0.2% of in-window cost; net of index -0.55% |
| R4 Never open lots of type `deep_limit/session` | 2 lots | removes a return of +30.83% on 8.7% of in-window cost; net of index +12.56% |
| R4 Never open lots of type `external` | 9 lots | removes a return of -12.02% on 1.4% of in-window cost; net of index -11.08% |
| R4 Never open lots of type `external_micro` | 1420 lots | removes a return of +15.78% on 44.8% of in-window cost; net of index +5.83% |
| R4 Never open lots of type `near_market/session` | 10 lots | removes a return of +15.63% on 44.9% of in-window cost; net of index +8.98% |
| R5 Buy the index instead, for each in-window lot's money and dates | 1442 lots | actual +16.62%, index +9.05% |
| R6 Hold the lower-bound idle cash in the index: Account B | 184 days | -9.25% of average NAV |

## Data limits

- **Six months only.** The export window is 2026-05-01 to 2026-11-01, one market regime, with no out-of-sample period.
- **Order records start late.** The first order row is 2026-06-08, so earlier fills count as `external` with no limit price. Proposal links start at 2026-07-11, so `session` against `proposal` is also an early-against-late comparison.
- **Hashed tags.** 875 of 1370 tagged orders carry an `oth_*` hash because Korean strategy names were not exported. They group, but their meaning is gone.
- **Stale order rows.** 76 order rows say `open` while the execution ledger holds their fills, and 26 fills exist only on an order row. The analysis takes the larger quantity from either source. A row still `open` has no recorded ending; that does not prove it is live.
- **Repeated fills.** See Data checks. Fills with no order row can only be tested against inventory, so a repeat that inventory does not expose would stay in.
- **Fill times.** Reconciler rows can carry the order's time, so fill latency is not measured. Fills found only on an order row use the order's date. 6 fills stamped a day away from their order take the order's day. Other fills from the same broker can also be a day early, which shifts their markouts but not their matching.
- **Opening lots.** 51 seed rows carry a broker average cost from an unknown date, and some are plugs added months later for holdings the ledger could not explain. Holding days on them are lower bounds. 147 buys before the 2026-05-10 cutover are already inside a seed and make no lot.
- **Fills that cannot be trades.** 2 ledger fills sit more than 30% from their session's close, at prices like an old average cost. Their quantity reconciles with later holdings, so they count as opening lots and make no markout or cash flow. They are worth a ledger check.
- **Unknown-basis sells.** One account has no seed rows, so sells of holdings older than its ledger have no cost and earn no return. That account's realized figures cover only what it bought in the window. Section 2 shows each account's share.
- **No balances in the export.** Idle cash is a lower bound derived from fills, and deposits, withdrawals, FX conversions and holdings never traded in the window are invisible. It is not the current cash balance. Compare the forgone rate, not the share, against what the operator knows the account holds.
- **Gross returns.** No fees, taxes, FX or dividends. Stock, index and parking instruments are all price return, which understates dividend payers and especially parking instruments, whose yield is mostly distributions.
- **One currency per market.** US figures are in USD and exclude the KRW/USD move. No total across markets is given.
- **Reference price.** Limit distance uses the last complete daily close, not the live quote the policy band uses, so a class can differ from what the session saw.
- **Markouts are close-based.** Session 0 is the fill's session. A fill after the close counts from that same session. A crypto session is one UTC day. Horizons past the last bar are censored, not zero, so 20-session samples are smaller and older.
- **Counterfactuals are in-sample.** They ignore price impact, fees and available cash. R1 assumes a fill at the open that the real limit order did not get. They overlap, so they do not add up. They are candidates to test, not findings.
- **Not an experiment look.** This is a descriptive retrospective of live trading, not a test-set read under `research/experiments`, and it uses no sealed data.
- **Mock and paper excluded.** Only real-money tables are read. Kiwoom mock, NHPLUG mock and Binance demo are outside the export.
- **Index start.** A lot's stock leg starts at its fill price and its index leg at that day's close. The fill's time of day is not reliable, so neither end of the day is exact. The index return on lots bought in the window: KR -19.90% from the close, -20.61% from the open; US +1.10% from the close, +1.11% from the open; Crypto +9.05% from the close, +8.43% from the open.
- **Price coverage.** Symbols with bars: 181. Without: 1. Their orders have no distance or markout and appear as `no_reference` or `n/a`. Open positions with no price to mark: 1, left out of the returns.
- **Splits.** Symbols with a split after a fill: 1, converted to split-adjusted units.

`terminal_outcome` across the live order tables, as exported and after correcting from fills:

| Value | As exported | After correction |
| --- | --- | --- |
| filled | 362 | 438 |
| partial | 0 | 0 |
| expired | 601 | 601 |
| cancelled | 565 | 565 |
| rejected | 91 | 91 |
| open | 191 | 115 |
| replaced | 0 | 0 |
| not_submitted | 0 | 0 |
| anomaly | 0 | 0 |
| unknown | 0 | 0 |
