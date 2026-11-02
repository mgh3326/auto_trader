# ROB-1474: daily stock-selection harness and bounded diagnostics

2026-11-02. The operator makes investment decisions; this research package supplies the honest evaluation harness and baselines that later models must beat.

Experiment-Manifest-Sha256: c698f8b0af75eff96788de35d61d6e8f1593a783e5c734994fea47abda12d4d3
Experiment-Test-Look: yes

The preliminary diagnostic view was registered before fetching prices; the corrected second view was reserved before running its metrics (k=2; the superseded preliminary 252-day crypto-high view is frozen in `research/experiments/reports/rob-1474-diagnostic-v1.md`). This is a development comparison on explicit survivor samples, with one fixed model configuration and no model/feature selection. There is no pristine sealed final test, prospective verdict, market-wide readiness claim, order, broker-account endpoint, database connection, or scheduler. The operator-desk accepted bounded diagnostics after the data-gap escalation and owns CI gating after push.

## 1. Data inventory

Repository search covered `app/`, `research/`, `scripts/`, models/migrations, `docs/research/`, runbooks, and public-source adapters, including OHLCV/candle/value/turnover, universe/listing/delisting, and corporate-action terms. The existing PIT work was found and is not described as absent. The following are source inventories, not survivor performance tables.

### KR: KOSPI + KOSDAQ

| Source / repository evidence | Start / available history | Delisted coverage | Adjustment / traded value | Gaps and limitations |
|---|---|---|---|---|
| `research/kr_corpus/`, `research/kr_delist_repair/data/export_manifest.json` | Main scope 2015-01-02–2024-12-30; 5,696,650 causal rows / 3,148 symbols documented | 562 lifecycle-resolution listings; 404 ended main-scope listings resolved | Adjusted pykrx/Naver bars; original traded value is null, not native turnover | Existing 2025+ holdout was not opened; endpoint credentials needed by the current pykrx adapter; payout amounts not established |
| `research/kr_delist_repair/kind_fetch.py` and `kind_parse.py` | Public KIND date-range delisting lists; this run probed 2022 | Exact-code delisting dates/reasons, not candle histories or payouts | No price adjustment / no OHLCV | Read-only 2022 probes returned 7 KOSPI and 37 KOSDAQ rows; dates alone cannot recreate prices |
| Yahoo chart (`research/data_plane/sources.py`, new `fetch.py`) | Symbol-dependent; requested 2020-01-01–2023-01-20 exclusive | No complete archive guarantee; 091990.KQ returned 404 | Current-vintage historical prices, Yahoo action events; **close*volume estimate** | Every requested KR series lacked 2022-01-03 and 2022-05-09; strict benchmark validation refused the run |
| Naver public fchart, fixed `https://fchart.stock.naver.com/sise.nhn` (`fetch.py`) | Bounded 5,000 returned sessions; this run retains 2020–2023 only; public KOSPI 2,000-row probe began 2018-09-04 | No certified complete delisted archive or listing intervals | Current provider vintage, adjustment basis not independently certified; **close*volume estimate**, not actual turnover; Yahoo event dates retained independently of missing Yahoo prices | Explicit whole-series KR alternative; 22 nonpositive-OHLC rows and 24 inconsistent-OHLC rows rejected and recorded as gaps, no price repair |
| KIS/Toss/Kiwoom daily adapters; `research/data_plane/broker_adapters.py`, broker inventory | Endpoint/entitlement-specific, no universal start date established | KIS current master is not lifecycle history | Raw/adjusted options; authenticated | Inventoried only, never called here. No account endpoints or credentials used |
| KRX public listing/delisted query endpoints (`MDCSTAT01901`, `MDCSTAT02001`) | History not established from this host | Potential official lifecycle/price history | Official raw/adjusted semantics require endpoint contract | Both credential-free probes returned HTTP 403, not proof that KRX lacks data |

### US

| Source / repository evidence | Start / available history | Delisted coverage | Adjustment / traded value | Gaps and limitations |
|---|---|---|---|---|
| `research/us_corpus/README.md` | Exploration 2016–2024; 2025–2026 sealed | Frozen 5,355-symbol survivor universe | Yahoo adjusted OHLCV; no actual traded value | Existing sealed data was not opened; no market-wide inference from this corpus |
| `research/us_universe/` and `data_manifests/` | NASDAQ Trader/Wayback captures from 2008 | Listing intervals include later-delisted observations, with bracketed event dates | Membership evidence only, no prices | Sparse capture gaps; external PIT CSV absent on this host; source states dates as evidence intervals, not exact lifecycle dates |
| Public Yahoo chart; `research/data_plane/` and new `fetch.py` | Symbol-dependent; actual sample 2020–2023 | Requested current survivors; not complete delisted history | Current-vintage split-adjusted prices, separate actions; **close*volume estimate** | Revised history is not a contemporaneous vintage; action-crossing labels remain unknown |
| KIS/Toss authenticated daily adapters, Alpaca data research tooling | Entitlement/source dependent | No complete delisted archive established here | Provider options and authenticated data | Inventoried only, no broker/account request; not necessary for this bounded sample |

### Crypto: Upbit KRW (Binance listed only as an inventory comparator)

| Source / repository evidence | Start / available history | Delisted coverage | Adjustment / traded value | Gaps and limitations |
|---|---|---|---|---|
| `research/crypto_universe/data/coverage.md` / U4 Upbit | 2017-09-25–2026-09-30 documented | 287 live pairs; 0 delisted recovered; existing 544 delisted probes returned 404 | Unadjusted daily OHLCV; actual quote turnover in archive builder | REST-only delisted gap is established by those probes; the underlying external candle artifact was absent here |
| Public Upbit `/v1/candles/days`, new `fetch.py` | Symbol-dependent, requested 2020–2023; 200 rows/page | Current requested markets; delisted REST markets may return 404 | Unadjusted, native `candle_acc_trade_price` quote turnover | No-trade days have no candle and stay missing; UTC-day close is D+1, not D 00:00 ([official daily-candle contract](https://docs.upbit.com/kr/reference/list-candles-days)) |
| Official [Upbit historical-data portal](https://www.upbit.com/historical_data/download) | Portal found and HTTP 200 checked in this run | Delisted/date completeness **UNVERIFIED** | Portal advertises candle/trade downloads; not integrated | The prior repository assertion of no public archive must not be generalized to this current portal. Bootstrap loaded, but archive contents/terms/completeness were not established |
| U4 Binance public spot archive comparator | 2017-08-17–2026-09-30 documented | 756 traded pairs / 256 delisted (33.9%) | Unadjusted OHLCV + quote volume; public checksum verification | Does not substitute for Upbit KRW venue/price history; no Binance data used in evaluation |

Serving tables (`*_candles_1d`, symbol universes) overwrite current state and are not certified PIT model inputs. `research/data_plane/` already has write-once vintage partitions, availability, source, corporate-action and hash contracts. Its external root was absent here and the generic daily-bar schema lacks native equity turnover. These sources were inventoried without opening a database. Local KR causal-export existence/schema documentation was inspected; its sealed rows were not read.

### Credential-free KRX checks and licensed gap-closing options

The three initial read-only POST probes sent no credentials and followed no redirects. Listing and delisting bld queries to `https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd` returned 403 (2,613-byte HTML, sha256 `2c860edd6d3458284e3b7f2f727385462a5e2c59d3f32ec4244da90780c0dfa9`). KIND's `searchDelCompanySub` download returned 200: KOSPI 7 parsed rows (sha256 `cb1a7ddb1b95cb55be086b75c6128dd8d9acd9fbcdb8be7161f38baa0d1d9d0c`), then KOSDAQ 37 parsed rows (sha256 `c797d964072a62a1bff93b1543d31a610eafead0a88e73f645bcdc7ef3a68203`). Query forms, bodies and hashes are under `.cache/public-probes/`. These prove limited public delisting-history reachability. They do not prove historical listing membership, prices or full coverage. The existing pykrx adapter auto-authenticates on import, so it was not invoked without credentials.

No purchase, trial activation, login, vendor contact, or credential request was made. The precise products and required deliverables are:

| Gap | Named licensed product / required contract | What it closes, and what remains unverified |
|---|---|---|
| KR missing actual turnover and certified corporate actions | FnGuide **DataGuide stock + event datasets**, under an explicit bulk-data/research-storage agreement (or a separately contracted data-feed delivery of those fields) | Vendor documents stock prices/adjusted prices and events from 1980, and stock volume/turnover updates. Require exact-code listing/delisting intervals, delisted OHLCV, actual traded value, corporate-action effective/known timestamps and full historical coverage in the contract. Those delisted/vintage guarantees are **not verified** by the public product page. The ordinary DataGuide subscription forbids mechanical bulk extraction/database building, so it alone does not authorize this harness. [Coverage](https://help-dataguide.fnguide.com/ko/articles/제공-데이터-안내-5c5347da), [terms](https://help-dataguide.fnguide.com/ko/articles/이용-및-요금-안내-48b18a4b) |
| US survivor universe, delisted outcomes and action history | **CRSP US Stock & Indexes Database**, licensed direct or through WRDS | Survivor-bias-free security history, corporate actions and delisting-return information. Missing delisting outcomes can still exist and must remain unknown. [CRSP product](https://www.crsp.org/research/crsp-us-stock-databases-2025-layout/), [delisting definitions](https://www.crsp.org/crsp_pdf/crsp-us-stock-indexes-databases-calculations-index-methodologies-guide-flat-file-format-2-0/) |
| US actual dollar turnover instead of close*volume | **NYSE Daily TAQ Trades + Master historical files** | Consolidated US trades/master, history from 1993; aggregate valid trade price*size after corrections/conditions. This addresses actual traded value, not corporate-action payouts. [Product and coverage](https://www.nyse.com/data-products/catalog/daily-taq/), [licensed file specification](https://www.nyse.com/publicdocs/nyse/data/Daily_TAQ_Client_Spec_v4.1.pdf) |
| Alternative US daily survivorship repair | **Norgate US Stock Market Platinum or Diamond**, including delisted securities | Named tiers include delisted stocks and adjustment options; validate exact security histories and turnover field semantics before treating turnover as tape notional. [Content](https://norgatedata.com/data-content-tables.php), [export access](https://norgatedata.com/accessibility.php) |
| Upbit KRW delisted daily bars / actual turnover after 2021-03-03 | **Tardis.dev Upbit historical trades** | Vendor documents all currency pairs from 2021-03-03; reconstruct UTC daily OHLCV and sum price*size, retaining capture incidents/gaps and inactive pairs. Require exact market/date coverage and listing evidence; this does **not** close pre-2021-03-03 history or establish payouts. [Upbit coverage and trades](https://docs.tardis.dev/historical-data-details/upbit) |
| Upbit 2017–2021 delisted history; certified historical publication vintages | No verified complete licensed product identified in this check | Operator would need an explicit venue/vendor backfile agreement guaranteeing those exact pairs/dates and correction/publication history. Do not label Kaiko/CoinAPI or the official portal as a proven complete repair without a coverage audit |

## 2. Method and bias

**Every performance table below is SURVIVOR-ONLY / BOUNDED.** Twelve predeclared current-name instruments per market, then the ten most liquid among feature-ready names each day; KR includes six KOSPI and six KOSDAQ names. Defaults for a supplied full universe are KR500/US1000/crypto100. This is not the full market. The public price vintage was fetched now, with nominal session availability, not observed in 2022. A true PIT run needs contemporaneous availability/vintages and historical membership including delisted names.

Expected survivorship bias: omitting failures usually inflates long-list raw returns/hit rates and truncates negative tails; it can understate down-list opportunities and the risks of holding doomed securities. IC and long-minus-short spread have no guaranteed direction because both rankings and the short tail change. Current large/liquid-name sampling also narrows dispersion/capacity characteristics. `close*volume` overstates true notional when close exceeds VWAP and understates it below VWAP; historical price/volume adjustment mismatch can additionally distort liquidity ranks. Its net bias direction is unmeasured. None of these tables establish market-wide readiness.

Features, labels, costs and math are specified in `research/stock_selection/README.md`. Labels use XKRX/XNYS/UTC sessions; full missing/action-crossing outcomes remain unknown. Features use only closed prefixes. A trailing 52-week high requires complete history (252 equity sessions, 364 crypto daily sessions); exclusions use no future labels. Price and value shocks use prior 20-session denominators. Every baseline and ranker uses identical eligible names, labels and common-date folds, with **10 purge + 10 embargo sessions** before each boundary, expanding training from 2021, 63 validation sessions per fold in 2022. No fitted preprocessing or hyperparameter search; fixed rank regression uses seed1474, 60 trees, 7 leaves, lr .05, child40, L2 1, one thread.

ICIR is unannualized mean daily IC / sample std. Horizons overlap; no independence, significance, superiority or deployment verdict is claimed. Decile spread is raw h-session long-minus-short close return. Up/down hit rates use strict raw signs. Cost-adjusted spread subtracts four one-way sides, KR25/US10/crypto15 bps each, for independent fully round-tripped gross-two vintages. It does not use turnover netting and is not an achievable short, same-close fill, or daily NAV backtest. Turnover is .5*L1 signed decile-weight change, including initial formation. Year/regime tables below show the ranker; full daily/year/regime breakdowns for **every** model and raw/relative target are in the reproducible evaluation JSON. Regime uses preceding 20-session index return, up >2%, down <-2%, else flat; crypto benchmark is BTC/KRW, a proxy rather than a broad crypto index.

## 3. Real runs and input identity

The commands below were actually executed. Full-market pulls/training were not attempted. `runtug heavy` returned `unknown command`; each fetch or per-horizon training/evaluation subprocess was bounded at 60 seconds and killed/waited on timeout. Initial all-horizon US/crypto runs timed out; successful final runs use the same fixed configuration split by horizon. Failed KR source attempts (Yahoo missing sessions, ETF proxy gaps, native zero/rounded-inconsistent OHLC) were refused before any KR metric; no invariants were relaxed. Final KR source drops and records rejected native bars rather than fabricating prices. A requested `091990.KQ` returned 404 and was replaced with `036930.KQ` before evaluating any KR outcome; this reinforces, rather than solves, the survivor limitation.

```bash
uv sync --all-groups --inexact
uv pip install --python .venv/bin/python -r research/stock_selection/requirements-ml.txt
# macOS on this host: use the compatible OpenMP runtime already in the sklearn wheel.
export DYLD_LIBRARY_PATH="$PWD/.venv/lib/python3.13/site-packages/sklearn/.dylibs"

uv run python -m research.stock_selection.fetch --market kr --kr-source naver --benchmark naver:KOSPI --cache-dir research/stock_selection/.cache/kr-verified --symbols 005930.KS,000660.KS,035420.KS,051910.KS,005380.KS,068270.KS,247540.KQ,086520.KQ,035900.KQ,041510.KQ,263750.KQ,036930.KQ --start 2020-01-01 --end 2023-01-20
uv run python -m research.stock_selection.fetch --market us --symbols AAPL,MSFT,AMZN,GOOGL,META,NVDA,JPM,V,JNJ,XOM,KO,WMT --start 2020-01-01 --end 2023-01-20
uv run python -m research.stock_selection.fetch --market crypto --symbols KRW-BTC,KRW-ETH,KRW-XRP,KRW-ADA,KRW-ETC,KRW-BCH,KRW-XLM,KRW-TRX,KRW-LINK,KRW-BAT,KRW-QTUM,KRW-IOST --start 2020-01-01 --end 2023-01-20

# Executed once for every market and h=1,3,5,10 (KR uses --cache-dir .../kr-verified).
uv run python -m research.stock_selection.evaluate --market kr --cache-dir research/stock_selection/.cache/kr-verified --liquid-n 10 --horizon 1 --output-dir research/stock_selection/.cache/evaluation
uv run python -m research.stock_selection.evaluate --market us --liquid-n 10 --horizon 1 --output-dir research/stock_selection/.cache/evaluation
uv run python -m research.stock_selection.evaluate --market crypto --liquid-n 10 --horizon 1 --output-dir research/stock_selection/.cache/evaluation
# Repeat those exact evaluate commands with --horizon 3, 5, 10.

# Build, forward and grade executed for all three markets, with the same KR cache override.
uv run python -m research.stock_selection.build --market kr --cache-dir research/stock_selection/.cache/kr-verified --liquid-n 10 --start 2021-01-01 --end 2022-12-30
uv run python -m research.stock_selection.forward --market kr --cache-dir research/stock_selection/.cache/kr-verified --liquid-n 10 --asof 2022-11-01 --top-n 5 --output-dir research/stock_selection/.cache/forward-v2
uv run python -m research.stock_selection.score --market kr --cache-dir research/stock_selection/.cache/kr-verified --liquid-n 10 --asof 2022-12-01 --lists research/stock_selection/.cache/forward-v2/kr/2022-11-01-momentum_20.json --output-dir research/stock_selection/.cache/grades-v2
# Repeat build/forward/score with --market us / crypto and the default cache.
# Additional actual fitted forward check:
uv run python -m research.stock_selection.forward --market us --asof 2022-11-01 --model lightgbm_rank --liquid-n 10 --top-n 5 --output-dir research/stock_selection/.cache/forward-v2
```

Re-fetch into fresh cache directories: existing snapshots and forward records refuse changed content. Downloaded prices, models and row-level JSON stay in the ignored cache, not in git. Tables are rendered by `evaluate.render_tables` directly from the real JSON; no values are hand-entered.

### Snapshot / dataset audit

| Market | Bar rows | Snapshot SHA-256 | Dataset rows / eligible (2021–2022) | Rejected native bars |
|---|---:|---|---:|---:|
| kr | 9782 | `75ef730f21ca979daf31d334b5e27cae5381ed00d8579f6073489053269ae6d8` | 5896 / 4910 | 46 |
| us | 9984 | `13c275831f15dec68b9ae3311dcb7fb468f30ed16477be608896c5a96ee9d3ca` | 6036 / 5030 | 0 |
| crypto | 14279 | `9e192d254d464d1e9da4213ff1e9d0cb717ea4122148d7e148aea2ba84992afd` | 8748 / 7290 | 0 |

## 4. Complete model tables — SURVIVOR-ONLY / BOUNDED

All returns/hit rates are fractions (0.01 = 1%). `UNKNOWN` is not zero. IC/spread days and outcome coverage expose missing/action outcomes. Benchmark-relative baselines have the same daily ordering by construction; subtracting a common benchmark return also leaves daily IC and decile spread unchanged. Relative targets are retained for training and audit.

# kr — SURVIVOR-ONLY / BOUNDED diagnostic walk-forward

Folds: `[{'test': ['2022-01-03', '2022-04-06'], 'train': ['2021-01-04', '2021-12-02']}, {'test': ['2022-04-07', '2022-07-07'], 'train': ['2021-01-04', '2022-03-08']}, {'test': ['2022-07-08', '2022-10-11'], 'train': ['2021-01-04', '2022-06-09']}, {'test': ['2022-10-12', '2022-12-29'], 'train': ['2021-01-04', '2022-09-07']}]`

## h=1 — SURVIVOR-ONLY / BOUNDED

| Model | IC | ICIR | Spread | Up hit | Down hit | Turnover | Net spread | IC/spread days | Coverage |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gap | 0.034699 | 0.094868 | 0.003113 | 0.457851 | 0.504132 | 1.784553 | -0.006887 | 245/244 | 0.993496 |
| high_distance_52w | 0.007049 | 0.019041 | 0.001283 | 0.441322 | 0.493004 | 0.146341 | -0.008717 | 245/245 | 0.993496 |
| lightgbm_rank | -0.010885 | -0.030729 | 0.001464 | 0.445455 | 0.494650 | 1.447154 | -0.008536 | 245/244 | 0.993496 |
| momentum_20 | 0.002122 | 0.005569 | -0.001297 | 0.454321 | 0.497521 | 0.463415 | -0.011297 | 245/245 | 0.993496 |
| momentum_5 | -0.012205 | -0.030235 | -0.000228 | 0.456790 | 0.505350 | 0.910569 | -0.010228 | 245/244 | 0.993496 |
| momentum_60 | 0.002686 | 0.007540 | -0.003056 | 0.460331 | 0.509917 | 0.268293 | -0.013056 | 245/244 | 0.993496 |
| relative_gap | 0.034699 | 0.094868 | 0.003113 | 0.457851 | 0.504132 | 1.784553 | -0.006887 | 245/244 | 0.993496 |
| relative_high_distance_52w | 0.007049 | 0.019041 | 0.001283 | 0.441322 | 0.493004 | 0.146341 | -0.008717 | 245/245 | 0.993496 |
| relative_momentum_20 | 0.002122 | 0.005569 | -0.001297 | 0.454321 | 0.497521 | 0.463415 | -0.011297 | 245/245 | 0.993496 |
| relative_momentum_5 | -0.012205 | -0.030235 | -0.000228 | 0.456790 | 0.505350 | 0.910569 | -0.010228 | 245/244 | 0.993496 |
| relative_momentum_60 | 0.002686 | 0.007540 | -0.003056 | 0.460331 | 0.509917 | 0.268293 | -0.013056 | 245/244 | 0.993496 |
| relative_reversal_1 | 0.018041 | 0.048151 | -0.000504 | 0.442623 | 0.499174 | 1.731707 | -0.010504 | 245/245 | 0.993496 |
| relative_reversal_5 | 0.012205 | 0.030235 | 0.000228 | 0.446914 | 0.495473 | 0.910569 | -0.009772 | 245/244 | 0.993496 |
| relative_value_shock | 0.012413 | 0.034751 | -0.000213 | 0.454545 | 0.507819 | 1.304878 | -0.010213 | 245/245 | 0.993496 |
| relative_volatility_20 | -0.014759 | -0.040791 | -0.000035 | 0.439669 | 0.502857 | 0.207317 | -0.010035 | 245/242 | 0.993496 |
| relative_volume_shock | 0.014311 | 0.040526 | -0.000855 | 0.456198 | 0.509465 | 1.325203 | -0.010855 | 245/244 | 0.993496 |
| reversal_1 | 0.018041 | 0.048151 | -0.000504 | 0.442623 | 0.499174 | 1.731707 | -0.010504 | 245/245 | 0.993496 |
| reversal_5 | 0.012205 | 0.030235 | 0.000228 | 0.446914 | 0.495473 | 0.910569 | -0.009772 | 245/244 | 0.993496 |
| value_shock | 0.012413 | 0.034751 | -0.000213 | 0.454545 | 0.507819 | 1.304878 | -0.010213 | 245/245 | 0.993496 |
| volatility_20 | -0.014759 | -0.040791 | -0.000035 | 0.439669 | 0.502857 | 0.207317 | -0.010035 | 245/242 | 0.993496 |
| volume_shock | 0.014311 | 0.040526 | -0.000855 | 0.456198 | 0.509465 | 1.325203 | -0.010855 | 245/244 | 0.993496 |

## h=3 — SURVIVOR-ONLY / BOUNDED

| Model | IC | ICIR | Spread | Up hit | Down hit | Turnover | Net spread | IC/spread days | Coverage |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gap | 0.034748 | 0.093610 | 0.004178 | 0.459072 | 0.556410 | 1.784553 | -0.005822 | 243/241 | 0.980488 |
| high_distance_52w | 0.015950 | 0.041374 | 0.003927 | 0.439316 | 0.536134 | 0.146341 | -0.006073 | 243/243 | 0.980488 |
| lightgbm_rank | -0.034078 | -0.100497 | -0.002905 | 0.431224 | 0.522689 | 1.170732 | -0.012905 | 243/239 | 0.980488 |
| momentum_20 | 0.000305 | 0.000833 | -0.003523 | 0.446218 | 0.549573 | 0.463415 | -0.013523 | 243/243 | 0.980488 |
| momentum_5 | -0.008942 | -0.022749 | 0.000710 | 0.460759 | 0.554043 | 0.910569 | -0.009290 | 243/241 | 0.980488 |
| momentum_60 | -0.022063 | -0.060925 | -0.007898 | 0.437288 | 0.535319 | 0.268293 | -0.017898 | 243/242 | 0.980488 |
| relative_gap | 0.034748 | 0.093610 | 0.004178 | 0.459072 | 0.556410 | 1.784553 | -0.005822 | 243/241 | 0.980488 |
| relative_high_distance_52w | 0.015950 | 0.041374 | 0.003927 | 0.439316 | 0.536134 | 0.146341 | -0.006073 | 243/243 | 0.980488 |
| relative_momentum_20 | 0.000305 | 0.000833 | -0.003523 | 0.446218 | 0.549573 | 0.463415 | -0.013523 | 243/243 | 0.980488 |
| relative_momentum_5 | -0.008942 | -0.022749 | 0.000710 | 0.460759 | 0.554043 | 0.910569 | -0.009290 | 243/241 | 0.980488 |
| relative_momentum_60 | -0.022063 | -0.060925 | -0.007898 | 0.437288 | 0.535319 | 0.268293 | -0.017898 | 243/242 | 0.980488 |
| relative_reversal_1 | -0.019347 | -0.050764 | -0.004598 | 0.424370 | 0.523729 | 1.731707 | -0.014598 | 243/241 | 0.980488 |
| relative_reversal_5 | 0.008942 | 0.022749 | -0.000710 | 0.428085 | 0.518143 | 0.910569 | -0.010710 | 243/241 | 0.980488 |
| relative_value_shock | 0.032737 | 0.096708 | -0.000076 | 0.435043 | 0.528270 | 1.304878 | -0.010076 | 243/241 | 0.980488 |
| relative_volatility_20 | 0.006479 | 0.017389 | -0.004572 | 0.411111 | 0.518182 | 0.207317 | -0.014572 | 243/234 | 0.980488 |
| relative_volume_shock | 0.044666 | 0.135817 | 0.002819 | 0.440851 | 0.533333 | 1.325203 | -0.007181 | 243/240 | 0.980488 |
| reversal_1 | -0.019347 | -0.050764 | -0.004598 | 0.424370 | 0.523729 | 1.731707 | -0.014598 | 243/241 | 0.980488 |
| reversal_5 | 0.008942 | 0.022749 | -0.000710 | 0.428085 | 0.518143 | 0.910569 | -0.010710 | 243/241 | 0.980488 |
| value_shock | 0.032737 | 0.096708 | -0.000076 | 0.435043 | 0.528270 | 1.304878 | -0.010076 | 243/241 | 0.980488 |
| volatility_20 | 0.006479 | 0.017389 | -0.004572 | 0.411111 | 0.518182 | 0.207317 | -0.014572 | 243/234 | 0.980488 |
| volume_shock | 0.044666 | 0.135817 | 0.002819 | 0.440851 | 0.533333 | 1.325203 | -0.007181 | 243/240 | 0.980488 |

## h=5 — SURVIVOR-ONLY / BOUNDED

| Model | IC | ICIR | Spread | Up hit | Down hit | Turnover | Net spread | IC/spread days | Coverage |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gap | 0.031432 | 0.082042 | 0.001385 | 0.458874 | 0.535398 | 1.784553 | -0.008615 | 241/236 | 0.967480 |
| high_distance_52w | 0.018250 | 0.047638 | 0.004141 | 0.444053 | 0.517949 | 0.146341 | -0.005859 | 241/241 | 0.967480 |
| lightgbm_rank | -0.036176 | -0.103046 | -0.003202 | 0.442795 | 0.521739 | 0.995935 | -0.013202 | 241/231 | 0.967480 |
| momentum_20 | -0.002948 | -0.008392 | -0.007087 | 0.457265 | 0.533628 | 0.463415 | -0.017087 | 241/240 | 0.967480 |
| momentum_5 | -0.010850 | -0.028499 | -0.002915 | 0.466667 | 0.540969 | 0.910569 | -0.012915 | 241/237 | 0.967480 |
| momentum_60 | -0.032768 | -0.091812 | -0.012728 | 0.439130 | 0.523348 | 0.268293 | -0.022728 | 241/240 | 0.967480 |
| relative_gap | 0.031432 | 0.082042 | 0.001385 | 0.458874 | 0.535398 | 1.784553 | -0.008615 | 241/236 | 0.967480 |
| relative_high_distance_52w | 0.018250 | 0.047638 | 0.004141 | 0.444053 | 0.517949 | 0.146341 | -0.005859 | 241/241 | 0.967480 |
| relative_momentum_20 | -0.002948 | -0.008392 | -0.007087 | 0.457265 | 0.533628 | 0.463415 | -0.017087 | 241/240 | 0.967480 |
| relative_momentum_5 | -0.010850 | -0.028499 | -0.002915 | 0.466667 | 0.540969 | 0.910569 | -0.012915 | 241/237 | 0.967480 |
| relative_momentum_60 | -0.032768 | -0.091812 | -0.012728 | 0.439130 | 0.523348 | 0.268293 | -0.022728 | 241/240 | 0.967480 |
| relative_reversal_1 | 0.010779 | 0.028231 | 0.003491 | 0.464935 | 0.542609 | 1.731707 | -0.006509 | 241/237 | 0.967480 |
| relative_reversal_5 | 0.010850 | 0.028499 | 0.002915 | 0.440529 | 0.516883 | 0.910569 | -0.007085 | 241/237 | 0.967480 |
| relative_value_shock | 0.018847 | 0.056144 | -0.002649 | 0.455507 | 0.529870 | 1.304878 | -0.012649 | 241/238 | 0.967480 |
| relative_volatility_20 | 0.013730 | 0.039898 | -0.009949 | 0.436283 | 0.529707 | 0.207317 | -0.019949 | 241/228 | 0.967480 |
| relative_volume_shock | 0.030569 | 0.093051 | 0.001088 | 0.455507 | 0.531602 | 1.325203 | -0.008912 | 241/235 | 0.967480 |
| reversal_1 | 0.010779 | 0.028231 | 0.003491 | 0.464935 | 0.542609 | 1.731707 | -0.006509 | 241/237 | 0.967480 |
| reversal_5 | 0.010850 | 0.028499 | 0.002915 | 0.440529 | 0.516883 | 0.910569 | -0.007085 | 241/237 | 0.967480 |
| value_shock | 0.018847 | 0.056144 | -0.002649 | 0.455507 | 0.529870 | 1.304878 | -0.012649 | 241/238 | 0.967480 |
| volatility_20 | 0.013730 | 0.039898 | -0.009949 | 0.436283 | 0.529707 | 0.207317 | -0.019949 | 241/228 | 0.967480 |
| volume_shock | 0.030569 | 0.093051 | 0.001088 | 0.455507 | 0.531602 | 1.325203 | -0.008912 | 241/235 | 0.967480 |

## h=10 — SURVIVOR-ONLY / BOUNDED

| Model | IC | ICIR | Spread | Up hit | Down hit | Turnover | Net spread | IC/spread days | Coverage |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gap | 0.030096 | 0.081868 | 0.003257 | 0.455140 | 0.552657 | 1.784553 | -0.006743 | 236/224 | 0.934959 |
| high_distance_52w | 0.004303 | 0.011300 | 0.005693 | 0.428019 | 0.528571 | 0.146341 | -0.004307 | 236/236 | 0.934959 |
| lightgbm_rank | 0.007623 | 0.019879 | 0.001609 | 0.458095 | 0.556938 | 0.752033 | -0.008391 | 236/220 | 0.934959 |
| momentum_20 | -0.013018 | -0.038079 | -0.009316 | 0.440889 | 0.547573 | 0.463415 | -0.019316 | 236/235 | 0.934959 |
| momentum_5 | -0.028300 | -0.069820 | -0.005335 | 0.450237 | 0.550239 | 0.910569 | -0.015335 | 236/228 | 0.934959 |
| momentum_60 | -0.063016 | -0.184571 | -0.023067 | 0.419535 | 0.528910 | 0.268293 | -0.033067 | 236/235 | 0.934959 |
| relative_gap | 0.030096 | 0.081868 | 0.003257 | 0.455140 | 0.552657 | 1.784553 | -0.006743 | 236/224 | 0.934959 |
| relative_high_distance_52w | 0.004303 | 0.011300 | 0.005693 | 0.428019 | 0.528571 | 0.146341 | -0.004307 | 236/236 | 0.934959 |
| relative_momentum_20 | -0.013018 | -0.038079 | -0.009316 | 0.440889 | 0.547573 | 0.463415 | -0.019316 | 236/235 | 0.934959 |
| relative_momentum_5 | -0.028300 | -0.069820 | -0.005335 | 0.450237 | 0.550239 | 0.910569 | -0.015335 | 236/228 | 0.934959 |
| relative_momentum_60 | -0.063016 | -0.184571 | -0.023067 | 0.419535 | 0.528910 | 0.268293 | -0.033067 | 236/235 | 0.934959 |
| relative_reversal_1 | 0.002995 | 0.008342 | -0.002733 | 0.456604 | 0.553052 | 1.731707 | -0.012733 | 236/228 | 0.934959 |
| relative_reversal_5 | 0.028300 | 0.069820 | 0.005335 | 0.443062 | 0.537441 | 0.910569 | -0.004665 | 236/228 | 0.934959 |
| relative_value_shock | 0.019543 | 0.057371 | -0.000964 | 0.452885 | 0.549296 | 1.304878 | -0.010964 | 236/229 | 0.934959 |
| relative_volatility_20 | 0.013924 | 0.042184 | -0.021144 | 0.418447 | 0.540773 | 0.207317 | -0.031144 | 236/212 | 0.934959 |
| relative_volume_shock | 0.026101 | 0.079506 | -0.001070 | 0.457692 | 0.553488 | 1.325203 | -0.011070 | 236/226 | 0.934959 |
| reversal_1 | 0.002995 | 0.008342 | -0.002733 | 0.456604 | 0.553052 | 1.731707 | -0.012733 | 236/228 | 0.934959 |
| reversal_5 | 0.028300 | 0.069820 | 0.005335 | 0.443062 | 0.537441 | 0.910569 | -0.004665 | 236/228 | 0.934959 |
| value_shock | 0.019543 | 0.057371 | -0.000964 | 0.452885 | 0.549296 | 1.304878 | -0.010964 | 236/229 | 0.934959 |
| volatility_20 | 0.013924 | 0.042184 | -0.021144 | 0.418447 | 0.540773 | 0.207317 | -0.031144 | 236/212 | 0.934959 |
| volume_shock | 0.026101 | 0.079506 | -0.001070 | 0.457692 | 0.553488 | 1.325203 | -0.011070 | 236/226 | 0.934959 |


# us — SURVIVOR-ONLY / BOUNDED diagnostic walk-forward

Folds: `[{'test': ['2022-01-03', '2022-04-01'], 'train': ['2021-01-04', '2021-12-02']}, {'test': ['2022-04-04', '2022-07-05'], 'train': ['2021-01-04', '2022-03-04']}, {'test': ['2022-07-06', '2022-10-03'], 'train': ['2021-01-04', '2022-06-03']}, {'test': ['2022-10-04', '2022-12-30'], 'train': ['2021-01-04', '2022-09-02']}]`

## h=1 — SURVIVOR-ONLY / BOUNDED

| Model | IC | ICIR | Spread | Up hit | Down hit | Turnover | Net spread | IC/spread days | Coverage |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gap | -0.007902 | -0.019983 | 0.000710 | 0.489270 | 0.506224 | 1.713147 | -0.003290 | 251/244 | 0.988048 |
| high_distance_52w | 0.048509 | 0.099243 | 0.002726 | 0.505532 | 0.530000 | 0.127490 | -0.001274 | 251/246 | 0.988048 |
| lightgbm_rank | 0.006953 | 0.018744 | -0.001743 | 0.484746 | 0.510924 | 1.235060 | -0.005743 | 251/245 | 0.988048 |
| momentum_20 | 0.009154 | 0.022125 | 0.003757 | 0.488034 | 0.509544 | 0.418327 | -0.000243 | 251/247 | 0.988048 |
| momentum_5 | 0.028154 | 0.064710 | 0.002322 | 0.492437 | 0.518987 | 0.892430 | -0.001678 | 251/245 | 0.988048 |
| momentum_60 | 0.005654 | 0.012701 | 0.000651 | 0.494017 | 0.515833 | 0.231076 | -0.003349 | 251/246 | 0.988048 |
| relative_gap | -0.007902 | -0.019983 | 0.000710 | 0.489270 | 0.506224 | 1.713147 | -0.003290 | 251/244 | 0.988048 |
| relative_high_distance_52w | 0.048509 | 0.099243 | 0.002726 | 0.505532 | 0.530000 | 0.127490 | -0.001274 | 251/246 | 0.988048 |
| relative_momentum_20 | 0.009154 | 0.022125 | 0.003757 | 0.488034 | 0.509544 | 0.418327 | -0.000243 | 251/247 | 0.988048 |
| relative_momentum_5 | 0.028154 | 0.064710 | 0.002322 | 0.492437 | 0.518987 | 0.892430 | -0.001678 | 251/245 | 0.988048 |
| relative_momentum_60 | 0.005654 | 0.012701 | 0.000651 | 0.494017 | 0.515833 | 0.231076 | -0.003349 | 251/246 | 0.988048 |
| relative_reversal_1 | 0.008708 | 0.021856 | -0.001568 | 0.487288 | 0.509623 | 1.681275 | -0.005568 | 251/245 | 0.988048 |
| relative_reversal_5 | -0.028154 | -0.064710 | -0.002322 | 0.480169 | 0.505882 | 0.892430 | -0.006322 | 251/245 | 0.988048 |
| relative_value_shock | -0.024780 | -0.070252 | -0.000746 | 0.490756 | 0.510549 | 1.286853 | -0.004746 | 251/247 | 0.988048 |
| relative_volatility_20 | 0.045329 | 0.096501 | 0.002990 | 0.491379 | 0.517842 | 0.139442 | -0.001010 | 251/245 | 0.988048 |
| relative_volume_shock | -0.029008 | -0.080911 | -0.001873 | 0.493277 | 0.511864 | 1.290837 | -0.005873 | 251/248 | 0.988048 |
| reversal_1 | 0.008708 | 0.021856 | -0.001568 | 0.487288 | 0.509623 | 1.681275 | -0.005568 | 251/245 | 0.988048 |
| reversal_5 | -0.028154 | -0.064710 | -0.002322 | 0.480169 | 0.505882 | 0.892430 | -0.006322 | 251/245 | 0.988048 |
| value_shock | -0.024780 | -0.070252 | -0.000746 | 0.490756 | 0.510549 | 1.286853 | -0.004746 | 251/247 | 0.988048 |
| volatility_20 | 0.045329 | 0.096501 | 0.002990 | 0.491379 | 0.517842 | 0.139442 | -0.001010 | 251/245 | 0.988048 |
| volume_shock | -0.029008 | -0.080911 | -0.001873 | 0.493277 | 0.511864 | 1.290837 | -0.005873 | 251/248 | 0.988048 |

## h=3 — SURVIVOR-ONLY / BOUNDED

| Model | IC | ICIR | Spread | Up hit | Down hit | Turnover | Net spread | IC/spread days | Coverage |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gap | 0.012072 | 0.029385 | 0.001805 | 0.498551 | 0.533645 | 1.713147 | -0.002195 | 251/234 | 0.964542 |
| high_distance_52w | 0.062685 | 0.121816 | 0.004766 | 0.518841 | 0.532110 | 0.127490 | 0.000766 | 251/238 | 0.964542 |
| lightgbm_rank | 0.035252 | 0.089053 | 0.001619 | 0.493953 | 0.523902 | 0.976096 | -0.002381 | 251/237 | 0.964542 |
| momentum_20 | 0.008216 | 0.019257 | 0.004630 | 0.500000 | 0.524201 | 0.418327 | 0.000630 | 251/238 | 0.964542 |
| momentum_5 | 0.018410 | 0.043159 | 0.004788 | 0.512963 | 0.540777 | 0.892430 | 0.000788 | 251/233 | 0.964542 |
| momentum_60 | -0.023097 | -0.052954 | -0.002287 | 0.495000 | 0.513636 | 0.231076 | -0.006287 | 251/232 | 0.964542 |
| relative_gap | 0.012072 | 0.029385 | 0.001805 | 0.498551 | 0.533645 | 1.713147 | -0.002195 | 251/234 | 0.964542 |
| relative_high_distance_52w | 0.062685 | 0.121816 | 0.004766 | 0.518841 | 0.532110 | 0.127490 | 0.000766 | 251/238 | 0.964542 |
| relative_momentum_20 | 0.008216 | 0.019257 | 0.004630 | 0.500000 | 0.524201 | 0.418327 | 0.000630 | 251/238 | 0.964542 |
| relative_momentum_5 | 0.018410 | 0.043159 | 0.004788 | 0.512963 | 0.540777 | 0.892430 | 0.000788 | 251/233 | 0.964542 |
| relative_momentum_60 | -0.023097 | -0.052954 | -0.002287 | 0.495000 | 0.513636 | 0.231076 | -0.006287 | 251/232 | 0.964542 |
| relative_reversal_1 | 0.018319 | 0.045996 | 0.000967 | 0.469608 | 0.503670 | 1.681275 | -0.003033 | 251/238 | 0.964542 |
| relative_reversal_5 | -0.018410 | -0.043159 | -0.004788 | 0.457282 | 0.485185 | 0.892430 | -0.008788 | 251/233 | 0.964542 |
| relative_value_shock | -0.030526 | -0.082077 | -0.002852 | 0.482126 | 0.502326 | 1.286853 | -0.006852 | 251/240 | 0.964542 |
| relative_volatility_20 | 0.074807 | 0.157646 | 0.008577 | 0.514286 | 0.550450 | 0.139442 | 0.004577 | 251/233 | 0.964542 |
| relative_volume_shock | -0.031440 | -0.085873 | -0.004673 | 0.477451 | 0.505069 | 1.290837 | -0.008673 | 251/237 | 0.964542 |
| reversal_1 | 0.018319 | 0.045996 | 0.000967 | 0.469608 | 0.503670 | 1.681275 | -0.003033 | 251/238 | 0.964542 |
| reversal_5 | -0.018410 | -0.043159 | -0.004788 | 0.457282 | 0.485185 | 0.892430 | -0.008788 | 251/233 | 0.964542 |
| value_shock | -0.030526 | -0.082077 | -0.002852 | 0.482126 | 0.502326 | 1.286853 | -0.006852 | 251/240 | 0.964542 |
| volatility_20 | 0.074807 | 0.157646 | 0.008577 | 0.514286 | 0.550450 | 0.139442 | 0.004577 | 251/233 | 0.964542 |
| volume_shock | -0.031440 | -0.085873 | -0.004673 | 0.477451 | 0.505069 | 1.290837 | -0.008673 | 251/237 | 0.964542 |

## h=5 — SURVIVOR-ONLY / BOUNDED

| Model | IC | ICIR | Spread | Up hit | Down hit | Turnover | Net spread | IC/spread days | Coverage |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gap | 0.014798 | 0.035657 | 0.006099 | 0.476757 | 0.538860 | 1.713147 | 0.002099 | 251/220 | 0.940637 |
| high_distance_52w | 0.065695 | 0.133221 | 0.005080 | 0.493548 | 0.538384 | 0.127490 | 0.001080 | 251/232 | 0.940637 |
| lightgbm_rank | 0.016094 | 0.044124 | -0.003354 | 0.492632 | 0.543478 | 0.772908 | -0.007354 | 251/223 | 0.940637 |
| momentum_20 | -0.002630 | -0.006151 | 0.002341 | 0.464804 | 0.530348 | 0.418327 | -0.001659 | 251/228 | 0.940637 |
| momentum_5 | -0.010696 | -0.026564 | 0.002393 | 0.473575 | 0.548634 | 0.892430 | -0.001607 | 251/216 | 0.940637 |
| momentum_60 | -0.034451 | -0.080132 | -0.007240 | 0.470115 | 0.517073 | 0.231076 | -0.011240 | 251/219 | 0.940637 |
| relative_gap | 0.014798 | 0.035657 | 0.006099 | 0.476757 | 0.538860 | 1.713147 | 0.002099 | 251/220 | 0.940637 |
| relative_high_distance_52w | 0.065695 | 0.133221 | 0.005080 | 0.493548 | 0.538384 | 0.127490 | 0.001080 | 251/232 | 0.940637 |
| relative_momentum_20 | -0.002630 | -0.006151 | 0.002341 | 0.464804 | 0.530348 | 0.418327 | -0.001659 | 251/228 | 0.940637 |
| relative_momentum_5 | -0.010696 | -0.026564 | 0.002393 | 0.473575 | 0.548634 | 0.892430 | -0.001607 | 251/216 | 0.940637 |
| relative_momentum_60 | -0.034451 | -0.080132 | -0.007240 | 0.470115 | 0.517073 | 0.231076 | -0.011240 | 251/219 | 0.940637 |
| relative_reversal_1 | 0.000785 | 0.001979 | -0.003688 | 0.446739 | 0.512821 | 1.681275 | -0.007688 | 251/223 | 0.940637 |
| relative_reversal_5 | 0.010696 | 0.026564 | -0.002393 | 0.451366 | 0.526425 | 0.892430 | -0.006393 | 251/216 | 0.940637 |
| relative_value_shock | -0.044731 | -0.119424 | -0.007296 | 0.461453 | 0.514000 | 1.286853 | -0.011296 | 251/230 | 0.940637 |
| relative_volatility_20 | 0.073737 | 0.164619 | 0.007910 | 0.502326 | 0.561353 | 0.139442 | 0.003910 | 251/221 | 0.940637 |
| relative_volume_shock | -0.037355 | -0.101472 | -0.009486 | 0.460571 | 0.515000 | 1.290837 | -0.013486 | 251/228 | 0.940637 |
| reversal_1 | 0.000785 | 0.001979 | -0.003688 | 0.446739 | 0.512821 | 1.681275 | -0.007688 | 251/223 | 0.940637 |
| reversal_5 | 0.010696 | 0.026564 | -0.002393 | 0.451366 | 0.526425 | 0.892430 | -0.006393 | 251/216 | 0.940637 |
| value_shock | -0.044731 | -0.119424 | -0.007296 | 0.461453 | 0.514000 | 1.286853 | -0.011296 | 251/230 | 0.940637 |
| volatility_20 | 0.073737 | 0.164619 | 0.007910 | 0.502326 | 0.561353 | 0.139442 | 0.003910 | 251/221 | 0.940637 |
| volume_shock | -0.037355 | -0.101472 | -0.009486 | 0.460571 | 0.515000 | 1.290837 | -0.013486 | 251/228 | 0.940637 |

## h=10 — SURVIVOR-ONLY / BOUNDED

| Model | IC | ICIR | Spread | Up hit | Down hit | Turnover | Net spread | IC/spread days | Coverage |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gap | 0.001298 | 0.003213 | -0.003901 | 0.443478 | 0.555705 | 1.713147 | -0.007901 | 251/196 | 0.881673 |
| high_distance_52w | 0.061868 | 0.135649 | 0.004551 | 0.460256 | 0.567105 | 0.127490 | 0.000551 | 251/213 | 0.881673 |
| lightgbm_rank | 0.097282 | 0.249229 | 0.024190 | 0.487952 | 0.563636 | 0.701195 | 0.020190 | 251/199 | 0.881673 |
| momentum_20 | -0.006368 | -0.015403 | -0.000151 | 0.456522 | 0.552564 | 0.418327 | -0.004151 | 251/191 | 0.881673 |
| momentum_5 | 0.002311 | 0.005766 | 0.004610 | 0.463576 | 0.579562 | 0.892430 | 0.000610 | 251/190 | 0.881673 |
| momentum_60 | -0.085030 | -0.196936 | -0.023116 | 0.427941 | 0.525301 | 0.231076 | -0.027116 | 251/196 | 0.881673 |
| relative_gap | 0.001298 | 0.003213 | -0.003901 | 0.443478 | 0.555705 | 1.713147 | -0.007901 | 251/196 | 0.881673 |
| relative_high_distance_52w | 0.061868 | 0.135649 | 0.004551 | 0.460256 | 0.567105 | 0.127490 | 0.000551 | 251/213 | 0.881673 |
| relative_momentum_20 | -0.006368 | -0.015403 | -0.000151 | 0.456522 | 0.552564 | 0.418327 | -0.004151 | 251/191 | 0.881673 |
| relative_momentum_5 | 0.002311 | 0.005766 | 0.004610 | 0.463576 | 0.579562 | 0.892430 | 0.000610 | 251/190 | 0.881673 |
| relative_momentum_60 | -0.085030 | -0.196936 | -0.023116 | 0.427941 | 0.525301 | 0.231076 | -0.027116 | 251/196 | 0.881673 |
| relative_reversal_1 | -0.017455 | -0.041639 | -0.004221 | 0.430216 | 0.540136 | 1.681275 | -0.008221 | 251/198 | 0.881673 |
| relative_reversal_5 | -0.002311 | -0.005766 | -0.004610 | 0.420438 | 0.536424 | 0.892430 | -0.008610 | 251/190 | 0.881673 |
| relative_value_shock | -0.026525 | -0.069768 | -0.001406 | 0.438571 | 0.565563 | 1.286853 | -0.005406 | 251/200 | 0.881673 |
| relative_volatility_20 | 0.103237 | 0.237328 | 0.007608 | 0.454015 | 0.550617 | 0.139442 | 0.003608 | 251/195 | 0.881673 |
| relative_volume_shock | -0.028780 | -0.075285 | -0.004929 | 0.437681 | 0.571053 | 1.290837 | -0.008929 | 251/199 | 0.881673 |
| reversal_1 | -0.017455 | -0.041639 | -0.004221 | 0.430216 | 0.540136 | 1.681275 | -0.008221 | 251/198 | 0.881673 |
| reversal_5 | -0.002311 | -0.005766 | -0.004610 | 0.420438 | 0.536424 | 0.892430 | -0.008610 | 251/190 | 0.881673 |
| value_shock | -0.026525 | -0.069768 | -0.001406 | 0.438571 | 0.565563 | 1.286853 | -0.005406 | 251/200 | 0.881673 |
| volatility_20 | 0.103237 | 0.237328 | 0.007608 | 0.454015 | 0.550617 | 0.139442 | 0.003608 | 251/195 | 0.881673 |
| volume_shock | -0.028780 | -0.075285 | -0.004929 | 0.437681 | 0.571053 | 1.290837 | -0.008929 | 251/199 | 0.881673 |


# crypto — SURVIVOR-ONLY / BOUNDED diagnostic walk-forward

Folds: `[{'test': ['2022-01-01', '2022-03-04'], 'train': ['2021-01-01', '2021-12-11']}, {'test': ['2022-03-05', '2022-05-06'], 'train': ['2021-01-01', '2022-02-12']}, {'test': ['2022-05-07', '2022-07-08'], 'train': ['2021-01-01', '2022-04-16']}, {'test': ['2022-07-09', '2022-09-09'], 'train': ['2021-01-01', '2022-06-18']}, {'test': ['2022-09-10', '2022-11-11'], 'train': ['2021-01-01', '2022-08-20']}, {'test': ['2022-11-12', '2022-12-31'], 'train': ['2021-01-01', '2022-10-22']}]`

## h=1 — SURVIVOR-ONLY / BOUNDED

| Model | IC | ICIR | Spread | Up hit | Down hit | Turnover | Net spread | IC/spread days | Coverage |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gap | -0.016721 | -0.051139 | -0.002390 | 0.475616 | 0.520000 | 1.695890 | -0.008390 | 363/365 | 1.000000 |
| high_distance_52w | 0.019954 | 0.052481 | -0.001792 | 0.470685 | 0.507397 | 0.178082 | -0.007792 | 365/365 | 1.000000 |
| lightgbm_rank | 0.056166 | 0.148130 | -0.002047 | 0.471781 | 0.510137 | 1.183562 | -0.008047 | 365/365 | 1.000000 |
| momentum_20 | -0.022667 | -0.058516 | 0.001408 | 0.466301 | 0.503562 | 0.504110 | -0.004592 | 365/365 | 1.000000 |
| momentum_5 | -0.037294 | -0.091316 | 0.004030 | 0.473425 | 0.511233 | 0.939726 | -0.001970 | 365/365 | 1.000000 |
| momentum_60 | -0.048702 | -0.125217 | 0.000720 | 0.468493 | 0.506849 | 0.293151 | -0.005280 | 365/365 | 1.000000 |
| relative_gap | -0.016721 | -0.051139 | -0.002390 | 0.475616 | 0.520000 | 1.695890 | -0.008390 | 363/365 | 1.000000 |
| relative_high_distance_52w | 0.019954 | 0.052481 | -0.001792 | 0.470685 | 0.507397 | 0.178082 | -0.007792 | 365/365 | 1.000000 |
| relative_momentum_20 | -0.022667 | -0.058516 | 0.001408 | 0.466301 | 0.503562 | 0.504110 | -0.004592 | 365/365 | 1.000000 |
| relative_momentum_5 | -0.037294 | -0.091316 | 0.004030 | 0.473425 | 0.511233 | 0.939726 | -0.001970 | 365/365 | 1.000000 |
| relative_momentum_60 | -0.048702 | -0.125217 | 0.000720 | 0.468493 | 0.506849 | 0.293151 | -0.005280 | 365/365 | 1.000000 |
| relative_reversal_1 | 0.008474 | 0.020585 | -0.001971 | 0.471781 | 0.513425 | 1.698630 | -0.007971 | 365/365 | 1.000000 |
| relative_reversal_5 | 0.037294 | 0.091316 | -0.004030 | 0.469041 | 0.511233 | 0.939726 | -0.010030 | 365/365 | 1.000000 |
| relative_value_shock | -0.010041 | -0.027035 | 0.003232 | 0.472877 | 0.514521 | 1.052055 | -0.002768 | 365/365 | 1.000000 |
| relative_volatility_20 | 0.061079 | 0.141644 | -0.003069 | 0.467945 | 0.509589 | 0.216438 | -0.009069 | 365/365 | 1.000000 |
| relative_volume_shock | -0.001658 | -0.004513 | 0.001864 | 0.473425 | 0.513973 | 1.057534 | -0.004136 | 365/365 | 1.000000 |
| reversal_1 | 0.008474 | 0.020585 | -0.001971 | 0.471781 | 0.513425 | 1.698630 | -0.007971 | 365/365 | 1.000000 |
| reversal_5 | 0.037294 | 0.091316 | -0.004030 | 0.469041 | 0.511233 | 0.939726 | -0.010030 | 365/365 | 1.000000 |
| value_shock | -0.010041 | -0.027035 | 0.003232 | 0.472877 | 0.514521 | 1.052055 | -0.002768 | 365/365 | 1.000000 |
| volatility_20 | 0.061079 | 0.141644 | -0.003069 | 0.467945 | 0.509589 | 0.216438 | -0.009069 | 365/365 | 1.000000 |
| volume_shock | -0.001658 | -0.004513 | 0.001864 | 0.473425 | 0.513973 | 1.057534 | -0.004136 | 365/365 | 1.000000 |

## h=3 — SURVIVOR-ONLY / BOUNDED

| Model | IC | ICIR | Spread | Up hit | Down hit | Turnover | Net spread | IC/spread days | Coverage |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gap | -0.007508 | -0.022594 | -0.003348 | 0.439452 | 0.540274 | 1.695890 | -0.009348 | 363/365 | 1.000000 |
| high_distance_52w | 0.022204 | 0.059357 | -0.007724 | 0.446575 | 0.546301 | 0.178082 | -0.013724 | 365/365 | 1.000000 |
| lightgbm_rank | 0.091048 | 0.265011 | 0.006860 | 0.449315 | 0.547397 | 1.043836 | 0.000860 | 365/365 | 1.000000 |
| momentum_20 | -0.018155 | -0.044827 | 0.003231 | 0.448219 | 0.548493 | 0.504110 | -0.002769 | 365/365 | 1.000000 |
| momentum_5 | -0.021355 | -0.053716 | 0.009073 | 0.444932 | 0.542466 | 0.939726 | 0.003073 | 365/365 | 1.000000 |
| momentum_60 | -0.051443 | -0.126644 | 0.000648 | 0.437260 | 0.536986 | 0.293151 | -0.005352 | 365/365 | 1.000000 |
| relative_gap | -0.007508 | -0.022594 | -0.003348 | 0.439452 | 0.540274 | 1.695890 | -0.009348 | 363/365 | 1.000000 |
| relative_high_distance_52w | 0.022204 | 0.059357 | -0.007724 | 0.446575 | 0.546301 | 0.178082 | -0.013724 | 365/365 | 1.000000 |
| relative_momentum_20 | -0.018155 | -0.044827 | 0.003231 | 0.448219 | 0.548493 | 0.504110 | -0.002769 | 365/365 | 1.000000 |
| relative_momentum_5 | -0.021355 | -0.053716 | 0.009073 | 0.444932 | 0.542466 | 0.939726 | 0.003073 | 365/365 | 1.000000 |
| relative_momentum_60 | -0.051443 | -0.126644 | 0.000648 | 0.437260 | 0.536986 | 0.293151 | -0.005352 | 365/365 | 1.000000 |
| relative_reversal_1 | -0.009284 | -0.024446 | -0.004305 | 0.439452 | 0.542466 | 1.698630 | -0.010305 | 365/365 | 1.000000 |
| relative_reversal_5 | 0.021355 | 0.053716 | -0.009073 | 0.442740 | 0.544110 | 0.939726 | -0.015073 | 365/365 | 1.000000 |
| relative_value_shock | 0.009229 | 0.025426 | 0.006598 | 0.438356 | 0.535890 | 1.052055 | 0.000598 | 365/365 | 1.000000 |
| relative_volatility_20 | 0.072547 | 0.175759 | -0.007265 | 0.444932 | 0.547945 | 0.216438 | -0.013265 | 365/365 | 1.000000 |
| relative_volume_shock | 0.013179 | 0.036327 | 0.004908 | 0.440548 | 0.538082 | 1.057534 | -0.001092 | 365/365 | 1.000000 |
| reversal_1 | -0.009284 | -0.024446 | -0.004305 | 0.439452 | 0.542466 | 1.698630 | -0.010305 | 365/365 | 1.000000 |
| reversal_5 | 0.021355 | 0.053716 | -0.009073 | 0.442740 | 0.544110 | 0.939726 | -0.015073 | 365/365 | 1.000000 |
| value_shock | 0.009229 | 0.025426 | 0.006598 | 0.438356 | 0.535890 | 1.052055 | 0.000598 | 365/365 | 1.000000 |
| volatility_20 | 0.072547 | 0.175759 | -0.007265 | 0.444932 | 0.547945 | 0.216438 | -0.013265 | 365/365 | 1.000000 |
| volume_shock | 0.013179 | 0.036327 | 0.004908 | 0.440548 | 0.538082 | 1.057534 | -0.001092 | 365/365 | 1.000000 |

## h=5 — SURVIVOR-ONLY / BOUNDED

| Model | IC | ICIR | Spread | Up hit | Down hit | Turnover | Net spread | IC/spread days | Coverage |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gap | -0.016625 | -0.051071 | -0.001208 | 0.427397 | 0.553425 | 1.695890 | -0.007208 | 363/365 | 1.000000 |
| high_distance_52w | 0.032052 | 0.086408 | -0.013125 | 0.437260 | 0.566575 | 0.178082 | -0.019125 | 365/365 | 1.000000 |
| lightgbm_rank | 0.080979 | 0.254304 | 0.012575 | 0.446575 | 0.574247 | 0.958904 | 0.006575 | 365/365 | 1.000000 |
| momentum_20 | -0.018121 | -0.044996 | 0.003648 | 0.442192 | 0.568219 | 0.504110 | -0.002352 | 365/365 | 1.000000 |
| momentum_5 | 0.005320 | 0.013839 | 0.014732 | 0.443288 | 0.569315 | 0.939726 | 0.008732 | 365/365 | 1.000000 |
| momentum_60 | -0.069388 | -0.171904 | 0.000409 | 0.419178 | 0.549589 | 0.293151 | -0.005591 | 365/365 | 1.000000 |
| relative_gap | -0.016625 | -0.051071 | -0.001208 | 0.427397 | 0.553425 | 1.695890 | -0.007208 | 363/365 | 1.000000 |
| relative_high_distance_52w | 0.032052 | 0.086408 | -0.013125 | 0.437260 | 0.566575 | 0.178082 | -0.019125 | 365/365 | 1.000000 |
| relative_momentum_20 | -0.018121 | -0.044996 | 0.003648 | 0.442192 | 0.568219 | 0.504110 | -0.002352 | 365/365 | 1.000000 |
| relative_momentum_5 | 0.005320 | 0.013839 | 0.014732 | 0.443288 | 0.569315 | 0.939726 | 0.008732 | 365/365 | 1.000000 |
| relative_momentum_60 | -0.069388 | -0.171904 | 0.000409 | 0.419178 | 0.549589 | 0.293151 | -0.005591 | 365/365 | 1.000000 |
| relative_reversal_1 | 0.008007 | 0.022505 | -0.004208 | 0.428493 | 0.558904 | 1.698630 | -0.010208 | 365/365 | 1.000000 |
| relative_reversal_5 | -0.005320 | -0.013839 | -0.014732 | 0.418630 | 0.548493 | 0.939726 | -0.020732 | 365/365 | 1.000000 |
| relative_value_shock | 0.017074 | 0.047645 | 0.009363 | 0.423014 | 0.549041 | 1.052055 | 0.003363 | 365/365 | 1.000000 |
| relative_volatility_20 | 0.085784 | 0.206472 | -0.008384 | 0.428493 | 0.557808 | 0.216438 | -0.014384 | 365/365 | 1.000000 |
| relative_volume_shock | 0.021027 | 0.058111 | 0.008967 | 0.428493 | 0.554521 | 1.057534 | 0.002967 | 365/365 | 1.000000 |
| reversal_1 | 0.008007 | 0.022505 | -0.004208 | 0.428493 | 0.558904 | 1.698630 | -0.010208 | 365/365 | 1.000000 |
| reversal_5 | -0.005320 | -0.013839 | -0.014732 | 0.418630 | 0.548493 | 0.939726 | -0.020732 | 365/365 | 1.000000 |
| value_shock | 0.017074 | 0.047645 | 0.009363 | 0.423014 | 0.549041 | 1.052055 | 0.003363 | 365/365 | 1.000000 |
| volatility_20 | 0.085784 | 0.206472 | -0.008384 | 0.428493 | 0.557808 | 0.216438 | -0.014384 | 365/365 | 1.000000 |
| volume_shock | 0.021027 | 0.058111 | 0.008967 | 0.428493 | 0.554521 | 1.057534 | 0.002967 | 365/365 | 1.000000 |

## h=10 — SURVIVOR-ONLY / BOUNDED

| Model | IC | ICIR | Spread | Up hit | Down hit | Turnover | Net spread | IC/spread days | Coverage |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gap | -0.028236 | -0.087205 | -0.003132 | 0.384658 | 0.599452 | 1.695890 | -0.009132 | 363/365 | 1.000000 |
| high_distance_52w | 0.047306 | 0.124768 | -0.026060 | 0.408767 | 0.622466 | 0.178082 | -0.032060 | 365/365 | 1.000000 |
| lightgbm_rank | 0.060468 | 0.188352 | 0.021848 | 0.391233 | 0.603288 | 0.819178 | 0.015848 | 365/365 | 1.000000 |
| momentum_20 | -0.018514 | -0.043718 | 0.010941 | 0.393973 | 0.607671 | 0.504110 | 0.004941 | 365/365 | 1.000000 |
| momentum_5 | 0.008057 | 0.020380 | 0.013868 | 0.400000 | 0.614795 | 0.939726 | 0.007868 | 365/365 | 1.000000 |
| momentum_60 | -0.080448 | -0.179048 | -0.003618 | 0.374247 | 0.589589 | 0.293151 | -0.009618 | 365/365 | 1.000000 |
| relative_gap | -0.028236 | -0.087205 | -0.003132 | 0.384658 | 0.599452 | 1.695890 | -0.009132 | 363/365 | 1.000000 |
| relative_high_distance_52w | 0.047306 | 0.124768 | -0.026060 | 0.408767 | 0.622466 | 0.178082 | -0.032060 | 365/365 | 1.000000 |
| relative_momentum_20 | -0.018514 | -0.043718 | 0.010941 | 0.393973 | 0.607671 | 0.504110 | 0.004941 | 365/365 | 1.000000 |
| relative_momentum_5 | 0.008057 | 0.020380 | 0.013868 | 0.400000 | 0.614795 | 0.939726 | 0.007868 | 365/365 | 1.000000 |
| relative_momentum_60 | -0.080448 | -0.179048 | -0.003618 | 0.374247 | 0.589589 | 0.293151 | -0.009618 | 365/365 | 1.000000 |
| relative_reversal_1 | -0.010477 | -0.028844 | -0.008716 | 0.385205 | 0.600548 | 1.698630 | -0.014716 | 365/365 | 1.000000 |
| relative_reversal_5 | -0.008057 | -0.020380 | -0.013868 | 0.380822 | 0.594521 | 0.939726 | -0.019868 | 365/365 | 1.000000 |
| relative_value_shock | 0.001245 | 0.003438 | 0.002964 | 0.387945 | 0.601096 | 1.052055 | -0.003036 | 365/365 | 1.000000 |
| relative_volatility_20 | 0.102964 | 0.246008 | -0.004546 | 0.392329 | 0.608219 | 0.216438 | -0.010546 | 365/365 | 1.000000 |
| relative_volume_shock | 0.004201 | 0.011650 | 0.003854 | 0.387945 | 0.600548 | 1.057534 | -0.002146 | 365/365 | 1.000000 |
| reversal_1 | -0.010477 | -0.028844 | -0.008716 | 0.385205 | 0.600548 | 1.698630 | -0.014716 | 365/365 | 1.000000 |
| reversal_5 | -0.008057 | -0.020380 | -0.013868 | 0.380822 | 0.594521 | 0.939726 | -0.019868 | 365/365 | 1.000000 |
| value_shock | 0.001245 | 0.003438 | 0.002964 | 0.387945 | 0.601096 | 1.052055 | -0.003036 | 365/365 | 1.000000 |
| volatility_20 | 0.102964 | 0.246008 | -0.004546 | 0.392329 | 0.608219 | 0.216438 | -0.010546 | 365/365 | 1.000000 |
| volume_shock | 0.004201 | 0.011650 | 0.003854 | 0.387945 | 0.600548 | 1.057534 | -0.002146 | 365/365 | 1.000000 |


## 5. Comparison and regime decomposition — SURVIVOR-ONLY / BOUNDED

The best baseline below is identified after the fact for description only, not chosen for forward use or asserted superior under multiple testing.

| Market | h | Ranker IC | Highest baseline IC | Baseline | Ranker net spread |
|---|---:|---:|---:|---|---:|
| kr | 1 | -0.010885 | 0.034699 | gap | -0.008536 |
| kr | 3 | -0.034078 | 0.044666 | relative_volume_shock | -0.012905 |
| kr | 5 | -0.036176 | 0.031432 | gap | -0.013202 |
| kr | 10 | 0.007623 | 0.030096 | gap | -0.008391 |
| us | 1 | 0.006953 | 0.048509 | high_distance_52w | -0.005743 |
| us | 3 | 0.035252 | 0.074807 | relative_volatility_20 | -0.002381 |
| us | 5 | 0.016094 | 0.073737 | relative_volatility_20 | -0.007354 |
| us | 10 | 0.097282 | 0.103237 | relative_volatility_20 | 0.020190 |
| crypto | 1 | 0.056166 | 0.061079 | relative_volatility_20 | -0.008047 |
| crypto | 3 | 0.091048 | 0.072547 | relative_volatility_20 | 0.000860 |
| crypto | 5 | 0.080979 | 0.085784 | relative_volatility_20 | 0.006575 |
| crypto | 10 | 0.060468 | 0.102964 | relative_volatility_20 | 0.015848 |

The fixed ranker has higher mean IC than the strongest reported baseline in 1/12 market-horizon comparisons. This is mostly a negative result for the ranker, not a reason to hide baseline rows. There is no significance test, acceptance gate, or TFT promotion.

### Ranker by year and regime — SURVIVOR-ONLY / BOUNDED

| Market | h | Split | Days / IC days | IC | ICIR | Net spread | Up / down hit |
|---|---:|---|---:|---:|---:|---:|---:|
| kr | 1 | by_year:2022 | 246 / 245 | -0.010885 | -0.030729 | -0.008536 | 0.445455 / 0.494650 |
| kr | 1 | by_regime:down | 129 / 128 | -0.040693 | -0.119470 | -0.012731 | 0.452381 / 0.495238 |
| kr | 1 | by_regime:flat | 62 / 62 | -0.000501 | -0.001379 | -0.004675 | 0.464516 / 0.516129 |
| kr | 1 | by_regime:up | 55 / 55 | 0.046783 | 0.125258 | -0.003201 | 0.407407 / 0.469091 |
| kr | 3 | by_year:2022 | 246 / 243 | -0.034078 | -0.100497 | -0.012905 | 0.431224 / 0.522689 |
| kr | 3 | by_regime:down | 129 / 126 | -0.036682 | -0.110751 | -0.017123 | 0.445528 / 0.523967 |
| kr | 3 | by_regime:flat | 62 / 62 | -0.117912 | -0.335657 | -0.021997 | 0.406557 / 0.487097 |
| kr | 3 | by_regime:up | 55 / 55 | 0.066391 | 0.206260 | 0.007050 | 0.426415 / 0.560000 |
| kr | 5 | by_year:2022 | 246 / 241 | -0.036176 | -0.103046 | -0.013202 | 0.442795 / 0.521739 |
| kr | 5 | by_regime:down | 129 / 124 | -0.031086 | -0.087368 | -0.008130 | 0.463793 / 0.513793 |
| kr | 5 | by_regime:flat | 62 / 62 | -0.049074 | -0.136030 | -0.021920 | 0.403333 / 0.546667 |
| kr | 5 | by_regime:up | 55 / 55 | -0.033114 | -0.098884 | -0.013886 | 0.441509 / 0.511111 |
| kr | 10 | by_year:2022 | 246 / 236 | 0.007623 | 0.019879 | -0.008391 | 0.458095 / 0.556938 |
| kr | 10 | by_regime:down | 129 / 119 | 0.060007 | 0.154336 | -0.010783 | 0.536735 / 0.512871 |
| kr | 10 | by_regime:flat | 62 / 62 | -0.075170 | -0.183874 | -0.018864 | 0.382759 / 0.629091 |
| kr | 10 | by_regime:up | 55 / 55 | -0.012386 | -0.037952 | 0.007895 | 0.396296 / 0.566038 |
| us | 1 | by_year:2022 | 251 / 251 | 0.006953 | 0.018744 | -0.005743 | 0.484746 / 0.510924 |
| us | 1 | by_regime:down | 127 / 127 | 0.026870 | 0.073469 | -0.006481 | 0.506557 / 0.506667 |
| us | 1 | by_regime:flat | 45 / 45 | -0.057493 | -0.136807 | -0.007773 | 0.418605 / 0.536364 |
| us | 1 | by_regime:up | 79 / 79 | 0.011643 | 0.033305 | -0.003337 | 0.487324 / 0.502703 |
| us | 3 | by_year:2022 | 251 / 251 | 0.035252 | 0.089053 | -0.002381 | 0.493953 / 0.523902 |
| us | 3 | by_regime:down | 127 / 127 | 0.045873 | 0.118433 | 0.003929 | 0.529730 / 0.475926 |
| us | 3 | by_regime:flat | 45 / 45 | -0.073745 | -0.202797 | -0.020224 | 0.430769 / 0.610811 |
| us | 3 | by_regime:up | 79 / 79 | 0.080264 | 0.191209 | -0.002572 | 0.470769 / 0.556667 |
| us | 5 | by_year:2022 | 251 / 251 | 0.016094 | 0.044124 | -0.007354 | 0.492632 / 0.543478 |
| us | 5 | by_regime:down | 127 / 127 | 0.023162 | 0.061717 | -0.001315 | 0.572632 / 0.465306 |
| us | 5 | by_regime:flat | 45 / 45 | -0.020952 | -0.069250 | -0.019618 | 0.372973 / 0.662857 |
| us | 5 | by_regime:up | 79 / 79 | 0.025834 | 0.067544 | -0.011734 | 0.437931 / 0.611765 |
| us | 10 | by_year:2022 | 251 / 251 | 0.097282 | 0.249229 | 0.020190 | 0.487952 / 0.563636 |
| us | 10 | by_regime:down | 127 / 127 | 0.148749 | 0.370553 | 0.043720 | 0.591011 / 0.441509 |
| us | 10 | by_regime:flat | 45 / 45 | -0.022540 | -0.069125 | -0.038400 | 0.406061 / 0.671429 |
| us | 10 | by_regime:up | 79 / 79 | 0.082796 | 0.210143 | 0.014362 | 0.340909 / 0.650000 |
| crypto | 1 | by_year:2022 | 365 / 365 | 0.056166 | 0.148130 | -0.008047 | 0.471781 / 0.510137 |
| crypto | 1 | by_regime:down | 196 / 196 | 0.035917 | 0.095065 | -0.008702 | 0.508163 / 0.470408 |
| crypto | 1 | by_regime:flat | 69 / 69 | 0.033441 | 0.096457 | -0.012729 | 0.426087 / 0.544928 |
| crypto | 1 | by_regime:up | 100 / 100 | 0.111534 | 0.278311 | -0.003534 | 0.432000 / 0.564000 |
| crypto | 3 | by_year:2022 | 365 / 365 | 0.091048 | 0.265011 | 0.000860 | 0.449315 / 0.547397 |
| crypto | 3 | by_regime:down | 196 / 196 | 0.079582 | 0.236948 | 0.002096 | 0.477551 / 0.506122 |
| crypto | 3 | by_regime:flat | 69 / 69 | 0.044408 | 0.135212 | -0.009700 | 0.397101 / 0.600000 |
| crypto | 3 | by_regime:up | 100 / 100 | 0.145703 | 0.399651 | 0.005722 | 0.430000 / 0.592000 |
| crypto | 5 | by_year:2022 | 365 / 365 | 0.080979 | 0.254304 | 0.006575 | 0.446575 / 0.574247 |
| crypto | 5 | by_regime:down | 196 / 196 | 0.065703 | 0.202738 | 0.004005 | 0.479592 / 0.535714 |
| crypto | 5 | by_regime:flat | 69 / 69 | 0.051972 | 0.199546 | -0.012048 | 0.347826 / 0.631884 |
| crypto | 5 | by_regime:up | 100 / 100 | 0.130933 | 0.384789 | 0.024465 | 0.450000 / 0.610000 |
| crypto | 10 | by_year:2022 | 365 / 365 | 0.060468 | 0.188352 | 0.015848 | 0.391233 / 0.603288 |
| crypto | 10 | by_regime:down | 196 / 196 | 0.017194 | 0.053229 | 0.002406 | 0.378571 / 0.588776 |
| crypto | 10 | by_regime:flat | 69 / 69 | 0.057215 | 0.195260 | -0.005252 | 0.376812 / 0.631884 |
| crypto | 10 | by_regime:up | 100 / 100 | 0.147530 | 0.459509 | 0.056753 | 0.426000 / 0.612000 |

## 6. Forward / score files actually written

Each list contains five disjoint up/down names for all four horizons plus the full scored cross-section. Printed paths from the CLI runs:

```text
research/stock_selection/.cache/forward-v2/kr/2022-11-01-momentum_20.json
research/stock_selection/.cache/grades-v2/kr/2022-11-01-momentum_20-graded-2022-12-01.json
research/stock_selection/.cache/forward-v2/us/2022-11-01-momentum_20.json
research/stock_selection/.cache/grades-v2/us/2022-11-01-momentum_20-graded-2022-12-01.json
research/stock_selection/.cache/forward-v2/crypto/2022-11-01-momentum_20.json
research/stock_selection/.cache/grades-v2/crypto/2022-11-01-momentum_20-graded-2022-12-01.json
research/stock_selection/.cache/forward-v2/us/2022-11-01-lightgbm_rank.json
```

| Market | h=1 | h=3 | h=5 | h=10 |
|---|---|---|---|---|
| kr | scored | scored | scored | scored |
| us | scored | unknown_outcome | unknown_outcome | unknown_outcome |
| crypto | scored | scored | scored | scored |

US action-crossing outcomes are intentionally `unknown_outcome` after maturity; they are not fabricated grades or failures hidden as success. Tests separately exercise pending and fully matured fixtures on all three calendars. Files and grades are research logs only; they do not propose or place orders.

## 7. Verification and acceptance

The final local command `uv run pytest tests/research/stock_selection/test_harness.py -q` reported:

```text
24 passed in 56.09s
ROB-1296 external HTTP boundary: 0 blocked requests
ROB-1880 socket guard: active=True blocked_attempts=0 workers=0
```

The initial test-only 52-week fixture used a string in a pandas query and selected no row (3 failed, 21 passed); the fixture now compares a Timestamp and the final 24-test run passes. The production calculation was unchanged by that test correction. The independent fixed verifier also ran the same changed test file and accepted the implementation after the corporate-action correction. New file is listed in `ci_shards/shard-4.txt`.

Hand fixture: rank vectors differ by one adjacent swap, sum(d²)=2, so IC = 1-6*2/(10*(10²-1)) = 163/165. The top decile returns .05 and the bottom -.04, so spread = .09; US four-side cost .004 gives .086. Initial signed-weight turnover =1, unchanged next-day turnover=0. These values are asserted to floating-point precision by the real scorer test.

Leakage proof has both outcomes: clean prefix/lineage tests pass; injecting an actual future bar at an earlier decision raises `LeakageError: bar available_at exceeds its session decision time`. Replacing a feature with actual t+3 future-bar return, or a nominal t+3 label with actual t+5 return, raises `LeakageError: feature/label lineage does not match closed bars`. The deliberate failure output is retained in the external report. Embargo is explicitly additive; a missing whole validation day is audited against the complete exchange calendar.

Changed-source Ruff checks, `git diff --check`, and `python3 scripts/ci/experiment_registry_lint.py` are the local static checks. No full suite, integration DB test, local database server, fixture service, scheduler, order or deploy was started. Bounded children have exited or were killed and waited on by `subprocess.run`.

CI is intentionally not watched or awaited: operator-desk corrected the brief to push and emit READY, then mergewake/desk gates CI. PR status and exact remote head are recorded in the external delivery report; this document does not claim CI success.
