# U7-KR boosting baseline + frozen controls (ROB-1441)

Experiment-Manifest-Sha256: 0cb94882aecfe2f2ead9b9e6c82fb5fcbc83a881936f5e8dac917c47d30cfb16
Experiment-Manifest-Sha256: 26c32faadd4aa9e6e851111adf944ebccf866e224ab02d062e4d08e5d9c8c73c
Experiment-Test-Look: yes

## Verdict

**STOP. Neither hypothesis passed; U8 TFT does not run for KR.**

| hypothesis | gate verdict | conservative net vs cash, annual | optimistic | G1 cash | G2 controls | G3 stability | G4 bracket |
|---|---|---:|---:|---|---|---|---|
| H-admit | **STOP** | +4.16% | +4.18% | pass | fail | pass | pass |
| H-entry-price | **STOP** | +7.30% | +13.49% | pass | fail | pass | pass |

The verdict rests on the 2019-2022 walk-forward path, conservative fill end. 2023-2024 played no part in it.

## Registry

| hypothesis | status | manifest version | manifest sha256 | k |
|---|---|---|---|---:|
| H-admit | draft | 4 | `0cb94882aecfe2f2ead9b9e6c82fb5fcbc83a881936f5e8dac917c47d30cfb16` | 1 |
| H-entry-price | draft | 4 | `26c32faadd4aa9e6e851111adf944ebccf866e224ab02d062e4d08e5d9c8c73c` | 1 |

`k = 1` on each hypothesis counts the single 2023-2024 diagnostic read below. It is counted because the rules define any metric on those rows as a look; it is not a sealed test. The manifests stay `draft` only because their US and crypto sections are pending for those markets' own U7 units; every KR section is fixed and pinned by the hash.

- code at the run: `a6db3d5075e7902f212237766ffe66be5b8e2236` (tree clean: True)
- data: `kr_daily_causal.v1.parquet` sha256 `8c52275fa01f4e0540aeb4959bd217efd5d184cf135731748753b282c3e74859`, export manifest sha256 `b92840df072bb947ef678b07a5f3c2ee82851e746639aa92f34d063dd8cfffea`
- configuration grid sha256: `e112e9e51cd48cf6c10f5b9577696a3977d46a1741dcd015e214dcaf4054ae76`; seeds [1441, 1442, 1443]
- frozen bundle sha256: `b2a09e79c33b23333c9f8862fbff286a7e7eda0298ae4c224daea95120005a2c`
- run history: manifest v2's run stopped before any fit, on a last-session bug. Manifest v3's stopped in the H-admit simulation with a contract error, on a bug where an entry skipped for cash outlived its intent. Neither wrote a result. Before v4, a throwaway three-configuration grid ran on sessions through 2022 to test the post-fit path; its output was deleted unread.

## Exposure audit of 2023-2024

**Result: not provably untouched. 2023-2024 is diagnostics only and is not called a sealed test.** The test for these manifests is a prospective seal, unopened.

| evidence | line | what it shows |
|---|---:|---|
| `research/kr_corpus/config.py` | 39 | kr-corpus-v1 assigns 2023-2024 to its validation window |
| `research/kr_corpus/backtest/windows.py` | 28 | the corpus backtest treats 2015-2024 as one exploration window |
| `research/underwater_spike_trim_study/README.md` | 73 | the underwater spike trim study read KR through 2024-12-31 |
| `research/kr_corpus/d3_engine/calibration_run.py` | 645 | D3 calibration loaded 2015-2024 as warm-up |
| `research/kr_corpus/d3_engine/primary_corpus.py` | 429 | the D3 primary run reads corpus years 2015 through 2024 |

Rerun with `research/kr_boosting/exposure_audit.py`.

## Method

- **Decision**: after the close of session t, orders at 08:30 KST of t+1. Features come only through `feature_join`, which refuses `available_at` after the decision time.
- **Population**: the frozen D3 signal (RSI-14 below 45; fib and Bollinger confluence within -8%..-3% of the close; 120 contiguous bars), over the U2 universe including delisted listings. Bars carry no analyst data, so this is the bar-reproducible part of the live gate.
- **Splits**: train 2015-2018 / validate 2019, expanding to validate 2022. In front of every boundary, purge then gap: 5+5 sessions for H-admit, 25+25 for H-entry-price.
- **Search**: 12 LightGBM configurations per family. The configuration for year Y is chosen on year Y-1 by model loss, so Y does not choose its own model. The top 3 refit with 3 seeds; the top one's seed mean trades.
- **Frame for every policy**: capital 10,000,000 KRW, 300,000 KRW per order, at most 3 new entries per session, one position per listing, 0.215% per side (D3 scalar, tax included), cash earns 0.
- **H-admit**: DAY limit at prior close +2%, exit at the t+5 close. Admit when q50 clears the round trip and q10 is above -10%; rank by q50.
- **H-entry-price**: DAY child orders at one ladder rung for up to 5 sessions, exit 20 sessions after the fill. Rung and rank by P(touch) x (xret - round trip); skip if not positive.
- **Controls**: unchanged policy (D3 rank, nothing skipped; for H-entry-price the D3 L2 anchor), momentum (top 60-session return), mean reversion (lowest 5-session return), both over names with median 20-session traded value of at least 100,000,000 KRW, plus cash.
- **Gate, fixed before the first fit**: conservative end, 2019-2022. G1 annual net vs cash above 2%; G2 every control beaten by more than 2% a year at Holm p ≤ 0.1 (circular block bootstrap, 20-session blocks); G3 ahead of cash and the unchanged policy in at least 3 of 4 years; G4 positive at the optimistic end.

## H-admit: walk-forward 2019-2022

Decisions 2019-01-02 to 2022-12-15. Rows: {"excluded_no_available_at": 0, "label_known": 97001, "total": 97387}.

Net return vs cash at equal capital, primary metric:

| policy | optimistic, annual | conservative, annual | volume share, annual | conservative, total |
|---|---:|---:|---:|---:|
| gbm-baseline | +4.18% | +4.16% | +4.46% | +16.78% |
| control-admit-all | -6.13% | -6.18% | -6.14% | -24.91% |
| control-momentum | -24.23% | -22.33% | -22.71% | -90.04% |
| control-mean-reversion | -6.94% | -6.60% | -6.54% | -26.69% |
| control-cash | +0.00% | +0.00% | +0.00% | +0.00% |

Gate comparisons (verdict **STOP**; ahead of cash in 4 of 4 years, of the unchanged policy in 4):

| gbm-baseline vs | annual difference | p raw | p Holm | passed |
|---|---:|---:|---:|---|
| control-admit-all | +10.34% | 0.0159 | 0.0477 | yes |
| control-cash | +4.16% | 0.3038 | 0.3230 | no |
| control-mean-reversion | +10.76% | 0.1615 | 0.3230 | no |
| control-momentum | +26.49% | 0.0010 | 0.0040 | yes |

P&L by year, share of starting capital, conservative end:

| policy | 2019 | 2020 | 2021 | 2022 |
|---|---:|---:|---:|---:|
| gbm-baseline | +6.76% | +4.51% | +2.89% | +2.63% |
| control-admit-all | +5.44% | -8.04% | -8.59% | -13.72% |
| control-momentum | -37.78% | -30.61% | -16.76% | -4.89% |
| control-mean-reversion | -19.83% | +27.61% | -1.64% | -32.77% |
| control-cash | +0.00% | +0.00% | +0.00% | +0.00% |

Diagnostics:

| policy | fill | max drawdown | turnover / yr | mean deployed | entries | fill rate | mean net R5 | mean net R20 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| gbm-baseline | optimistic | -15.30% | 8.0x | 13.1% | 1211 | 99.5% | +0.79% | +2.45% |
| gbm-baseline | conservative | -15.12% | 7.9x | 13.0% | 1208 | 99.3% | +0.79% | +2.49% |
| control-admit-all | optimistic | -38.43% | 21.7x | 38.8% | 2839 | 99.5% | -0.16% | +0.43% |
| control-admit-all | conservative | -38.55% | 21.7x | 38.8% | 2836 | 99.5% | -0.16% | +0.43% |
| control-momentum | optimistic | -97.87% | 31.4x | 56.0% | 1807 | 96.5% | -2.44% | -4.70% |
| control-momentum | conservative | -95.97% | 31.6x | 60.6% | 1846 | 96.4% | -2.34% | -4.43% |
| control-mean-reversion | optimistic | -42.85% | 19.5x | 57.8% | 2808 | 97.7% | -0.32% | -1.05% |
| control-mean-reversion | conservative | -42.78% | 19.6x | 58.1% | 2798 | 97.6% | -0.30% | -1.07% |
| control-cash | optimistic | +0.00% | 0.0x | 0.0% | 0 | n/a | n/a | n/a |
| control-cash | conservative | +0.00% | 0.0x | 0.0% | 0 | n/a | n/a | n/a |

Stress, conservative end:

| policy | fee 0.415% per side, annual | terminal write-off, annual | write-offs |
|---|---:|---:|---|
| control-admit-all | -13.80% | -6.18% | 0 |
| gbm-baseline | +0.71% | +4.16% | 0 |

Calibration: `{"below_q10": 0.12734913248926885, "below_q50": 0.49247916892111243, "below_q90": 0.8958806766944415, "n": 55446, "pinball_q50": 0.02316981857670608}`

Annual net vs cash by seed of the primary configuration: ['+4.46%', '+3.41%', '+4.77%']. By finalist: ['+4.16%', '+3.55%', '+5.52%']. Neither enters the verdict.

Contract report sha256, gbm-baseline: `619c1942a9e123345cb0d2da3c46f821dd0f0077415cf1501f5f8b3e09aa4c3c`.

## H-admit: 2023-2024, diagnostics only (not a sealed test)

Frozen models, no refit. Decisions 2023-01-02 to 2024-12-20. This period does not change the verdict.

| policy | optimistic, annual | conservative, annual | volume share, annual | conservative, total |
|---|---:|---:|---:|---:|
| gbm-baseline | +6.74% | +6.76% | +6.72% | +13.50% |
| control-admit-all | -3.34% | -3.35% | -4.08% | -6.70% |
| control-momentum | -32.85% | -33.55% | -32.94% | -66.97% |
| control-mean-reversion | -15.20% | -17.21% | -16.91% | -34.38% |
| control-cash | +0.00% | +0.00% | +0.00% | +0.00% |

| policy | fill | max drawdown | turnover / yr | mean deployed | entries | fill rate | mean net R5 | mean net R20 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| gbm-baseline | optimistic | -5.24% | 4.4x | 7.2% | 325 | 99.4% | +1.57% | +2.21% |
| gbm-baseline | conservative | -5.24% | 4.4x | 7.2% | 324 | 99.2% | +1.57% | +2.24% |
| control-admit-all | optimistic | -15.69% | 21.0x | 39.6% | 1444 | 99.7% | -0.05% | +0.23% |
| control-admit-all | conservative | -15.71% | 21.0x | 39.5% | 1443 | 99.7% | -0.05% | +0.24% |
| control-momentum | optimistic | -68.91% | 24.2x | 44.9% | 1359 | 97.1% | -2.03% | -4.46% |
| control-momentum | conservative | -70.25% | 24.2x | 45.1% | 1357 | 97.1% | -2.05% | -4.57% |
| control-mean-reversion | optimistic | -40.48% | 23.7x | 56.5% | 1386 | 97.7% | -0.78% | -2.45% |
| control-mean-reversion | conservative | -44.34% | 24.6x | 59.0% | 1376 | 97.5% | -0.85% | -2.47% |
| control-cash | optimistic | +0.00% | 0.0x | 0.0% | 0 | n/a | n/a | n/a |
| control-cash | conservative | +0.00% | 0.0x | 0.0% | 0 | n/a | n/a | n/a |

Calibration: `{"below_q10": 0.10381386772769179, "below_q50": 0.479426464166141, "below_q90": 0.890921442088408, "n": 41218, "pinball_q50": 0.021196932285800045}`

## H-entry-price: walk-forward 2019-2022

Decisions 2019-01-02 to 2022-10-20. Rows: {"excluded_no_available_at": 0, "total": 469032, "touch_known": 467104, "xret_known": 259458}.

Net return vs cash at equal capital, primary metric:

| policy | optimistic, annual | conservative, annual | volume share, annual | conservative, total |
|---|---:|---:|---:|---:|
| gbm-baseline | +13.49% | +7.30% | +12.42% | +29.45% |
| control-anchor | -0.14% | -0.97% | -1.37% | -3.90% |
| control-momentum | -21.81% | -21.95% | -21.00% | -88.52% |
| control-mean-reversion | -18.48% | -18.26% | -14.88% | -73.66% |
| control-cash | +0.00% | +0.00% | +0.00% | +0.00% |

Gate comparisons (verdict **STOP**; ahead of cash in 3 of 4 years, of the unchanged policy in 3):

| gbm-baseline vs | annual difference | p raw | p Holm | passed |
|---|---:|---:|---:|---|
| control-anchor | +8.27% | 0.2372 | 0.4744 | no |
| control-cash | +7.30% | 0.3672 | 0.4744 | no |
| control-mean-reversion | +25.57% | 0.0368 | 0.1292 | no |
| control-momentum | +29.25% | 0.0323 | 0.1292 | no |

P&L by year, share of starting capital, conservative end:

| policy | 2019 | 2020 | 2021 | 2022 |
|---|---:|---:|---:|---:|
| gbm-baseline | +13.80% | +25.48% | +9.14% | -18.96% |
| control-anchor | +5.31% | -2.81% | +19.42% | -25.82% |
| control-momentum | -46.32% | -8.29% | -24.56% | -9.35% |
| control-mean-reversion | -19.34% | -8.37% | -24.98% | -20.96% |
| control-cash | +0.00% | +0.00% | +0.00% | +0.00% |

Diagnostics:

| policy | fill | max drawdown | turnover / yr | mean deployed | entries | fill rate | mean net R5 | mean net R20 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| gbm-baseline | optimistic | -42.58% | 9.7x | 83.6% | 1924 | 70.6% | -0.01% | +1.04% |
| gbm-baseline | conservative | -43.64% | 9.8x | 84.0% | 1738 | 67.4% | -0.21% | +0.67% |
| control-anchor | optimistic | -39.51% | 6.4x | 55.0% | 938 | 17.7% | +0.24% | +0.05% |
| control-anchor | conservative | -38.84% | 6.3x | 54.2% | 917 | 17.2% | +0.23% | -0.06% |
| control-momentum | optimistic | -90.64% | 10.8x | 86.2% | 703 | 78.0% | -2.39% | -4.26% |
| control-momentum | conservative | -90.93% | 10.8x | 86.1% | 696 | 76.7% | -2.75% | -4.23% |
| control-mean-reversion | optimistic | -77.14% | 8.0x | 89.4% | 820 | 73.0% | -1.49% | -2.28% |
| control-mean-reversion | conservative | -76.60% | 7.6x | 89.4% | 752 | 72.6% | -2.01% | -2.55% |
| control-cash | optimistic | +0.00% | 0.0x | 0.0% | 0 | n/a | n/a | n/a |
| control-cash | conservative | +0.00% | 0.0x | 0.0% | 0 | n/a | n/a | n/a |

Stress, conservative end:

| policy | fee 0.415% per side, annual | terminal write-off, annual | write-offs |
|---|---:|---:|---|
| control-anchor | -4.04% | -0.97% | 0 |
| gbm-baseline | +0.57% | +7.30% | 0 |

Calibration: `{"base_rate": 0.5619765892653217, "bins": [{"bin": "0.0-0.2", "n": 20247, "observed": 0.18145898157751766, "predicted": 0.1495028322724154}, {"bin": "0.2-0.4", "n": 61833, "observed": 0.32348422363462875, "predicted": 0.3019924551029192}, {"bin": "0.4-0.6", "n": 55527, "observed": 0.5128135861832982, "predicted": 0.4969295754643594}, {"bin": "0.6-0.8", "n": 60194, "observed": 0.7044555935807556, "predicted": 0.7068925087842067}, {"bin": "0.8-1.0", "n": 54391, "observed": 0.8672574506811789, "predicted": 0.8728859095594594}], "n": 252192}`

Annual net vs cash by seed of the primary configuration: ['+10.51%', '+9.79%', '+8.83%']. By finalist: ['+7.30%', '+8.57%', '+3.89%']. Neither enters the verdict.

Contract report sha256, gbm-baseline: `ff87608a081e901ed05fefb40fc9773c8a4d2e1bef0e3d21acd35b1bf6e280a1`.

## H-entry-price: 2023-2024, diagnostics only (not a sealed test)

Frozen models, no refit. Decisions 2023-01-02 to 2024-11-22. This period does not change the verdict.

| policy | optimistic, annual | conservative, annual | volume share, annual | conservative, total |
|---|---:|---:|---:|---:|
| gbm-baseline | +14.14% | +6.15% | +11.18% | +12.25% |
| control-anchor | -1.84% | -1.10% | -0.91% | -2.22% |
| control-momentum | -27.30% | -27.81% | -27.39% | -55.52% |
| control-mean-reversion | -15.82% | -9.95% | -8.05% | -19.92% |
| control-cash | +0.00% | +0.00% | +0.00% | +0.00% |

| policy | fill | max drawdown | turnover / yr | mean deployed | entries | fill rate | mean net R5 | mean net R20 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| gbm-baseline | optimistic | -19.88% | 10.0x | 88.0% | 833 | 73.9% | -0.22% | +1.11% |
| gbm-baseline | conservative | -20.64% | 9.9x | 87.6% | 801 | 69.6% | -0.04% | +0.48% |
| control-anchor | optimistic | -17.98% | 6.3x | 62.4% | 413 | 17.1% | -0.58% | -0.23% |
| control-anchor | conservative | -17.79% | 6.4x | 60.0% | 423 | 16.6% | -0.68% | -0.09% |
| control-momentum | optimistic | -66.56% | 10.2x | 88.3% | 623 | 77.2% | -1.57% | -3.44% |
| control-momentum | conservative | -66.45% | 10.3x | 87.5% | 619 | 76.8% | -1.81% | -3.55% |
| control-mean-reversion | optimistic | -41.80% | 7.9x | 90.6% | 437 | 76.3% | -0.44% | -3.08% |
| control-mean-reversion | conservative | -38.02% | 7.6x | 91.1% | 424 | 77.1% | -0.13% | -2.19% |
| control-cash | optimistic | +0.00% | 0.0x | 0.0% | 0 | n/a | n/a | n/a |
| control-cash | conservative | +0.00% | 0.0x | 0.0% | 0 | n/a | n/a | n/a |

Calibration: `{"base_rate": 0.5473800020196758, "bins": [{"bin": "0.0-0.2", "n": 17821, "observed": 0.14870097076482802, "predicted": 0.1401175397744476}, {"bin": "0.2-0.4", "n": 39199, "observed": 0.28908900737263704, "predicted": 0.3019443168965751}, {"bin": "0.4-0.6", "n": 40140, "observed": 0.46066268061783755, "predicted": 0.4993992338108203}, {"bin": "0.6-0.8", "n": 40964, "observed": 0.6631188360511668, "predicted": 0.7031710713366462}, {"bin": "0.8-1.0", "n": 50025, "observed": 0.8666066966516741, "predicted": 0.8799389183604192}], "n": 188149}`

## Payout sensitivity of the terminal labels

The dots audit of `delist_resolution.v1` (ROB-1437) matched 100 of 100 sampled rows to KIND on symbol, final delist date and broad event class (Wilson 95% 96.3-100%). **Payouts were not verified.** It also names 003415 (weakly supported payout assumption), seven rows labelled 해산 that are absorption-merger contexts, 002250 (compound reason) and same-code relistings. Its data fix (hk 2535) is not in this run, which stays pinned to the hash above.

Features and labels read no payout field, but the admit and `xret` labels and the simulator settle a delisted position at its last close, so they rest on that assumption. `payout_semantics` is read only by the write-off stress. The table gives the most a wrong payout could take on 2019-2022 at the conservative end, before any reinvestment effect. Flags are by symbol: all 해산 rows count, since the seven are not separated here, and a flagged symbol may have settled in a listing the audit did not question. A control's exposure moves that control's result too, so compare rows rather than reading one alone.

H-admit:

| policy | settled at last close | value, KRW | if all paid nothing, annual | flagged rows settled | flagged value, KRW | if flagged paid nothing, annual | flagged symbols |
|---|---:|---:|---:|---:|---:|---:|---|
| gbm-baseline | 0 | 0 | -0.00% | 0 | 0 | -0.00% | none |
| control-admit-all | 1 | 275,000 | -0.69% | 0 | 0 | -0.00% | none |
| control-momentum | 1 | 177,450 | -0.44% | 1 | 177,450 | -0.44% | 003415 |
| control-mean-reversion | 6 | 933,341 | -2.33% | 2 | 453,990 | -1.13% | 003415, 079660 |
| control-cash | 0 | 0 | -0.00% | 0 | 0 | -0.00% | none |

H-entry-price:

| policy | settled at last close | value, KRW | if all paid nothing, annual | flagged rows settled | flagged value, KRW | if flagged paid nothing, annual | flagged symbols |
|---|---:|---:|---:|---:|---:|---:|---|
| gbm-baseline | 0 | 0 | -0.00% | 0 | 0 | -0.00% | none |
| control-anchor | 0 | 0 | -0.00% | 0 | 0 | -0.00% | none |
| control-momentum | 2 | 137,200 | -0.34% | 1 | 126,750 | -0.32% | 003415 |
| control-mean-reversion | 4 | 706,585 | -1.77% | 1 | 284,920 | -0.71% | 079660 |
| control-cash | 0 | 0 | -0.00% | 0 | 0 | -0.00% | none |

## Limits

- **Prices are adjusted** (`price_mode=adjusted`). Ticks on adjusted prices are approximate, and the tick table is the seven-band table from the evaluation contract's fixtures, because the frozen D3 YAML is not on this host. Every policy shares this.
- **`invalid_ohlc` sessions are unobserved.** U2 keeps no prices for them, and kr-corpus-v1 discloses they lean strongly to up-closes (+2.81% mean). Labels ending on one are unknown and orders cannot fill on one, which biases returns down for every policy.
- **Terminal payout is an assumption.** U2 asserts no amount, so a delisted position settles at its last close; the write-off stress zeroes `residual_after_liquidation_trading` and `ambiguous`. A listing's known delisting is not used to block entries, because U2 dates many events only at the delist date.
- **Fills at the daily open and close are idealised.** Near-marketable H-admit orders barely separate the bracket, so its cost and open-price risk are not captured by fill uncertainty; the fee stress is the relevant check. Volume share caps at 10% of the session's whole volume, not the auction's.
- **No T+2 model.** Proceeds are usable next session.
- **D3 is not the whole live gate.** No analyst upside, no NXT session, no per-symbol policy.
- **Validation years are not independent of this work's design**: the frame and rules were chosen knowing ROB-1383's findings about the same market.

## Reproduce

```bash
uv venv --python 3.13 "$ROOT/venv"
VIRTUAL_ENV="$ROOT/venv" uv pip install -r research/kr_boosting/requirements-ml.txt
"$ROOT/venv/bin/python" -m research.kr_boosting.run validate --export-dir "$EXPORT" --root "$ROOT/run"
"$ROOT/venv/bin/python" -m research.kr_boosting.run diagnostic --export-dir "$EXPORT" --root "$ROOT/run"
"$ROOT/venv/bin/python" -m research.kr_boosting.run report --export-dir "$EXPORT" --root "$ROOT/run" --report research/experiments/reports/rob-1441-kr-boosting-baseline.md
```
