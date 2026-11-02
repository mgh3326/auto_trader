# Daily stock-selection research (ROB-1474)

The operator makes investment decisions. This package supplies a causal evaluation
harness and daily up/down rankings for KR, US, and Upbit KRW. It has no database,
account, order, policy, or scheduler wiring. The feature playbook is a thin end-to-end
path: normalized daily bars → features/outcomes → common folds → shared scorer → JSON.

Two designs were considered: reuse the corpus-specific builders, or normalize bars
in an independent pandas panel. The latter keeps calendar, availability and scoring
rules in one small package while accepting future PIT inputs without importing broker
or corpus lifecycle machinery. Existing PIT universe work is inventoried in the report.

## Data contract

One CSV row per observed `(date, symbol)`, plus `__benchmark__` rows, with:
`open, high, low, close, volume, traded_value, available_at, action`.
Symbols use DB dot format for US and six-digit strings for KR; Upbit uses `KRW-*`.
`available_at` is offset-aware and cannot exceed the decision time for its bar date.
`action` explicitly marks split/dividend or other unadjusted corporate-action dates.
The companion `bars.json` identifies market, hash, source, adjustment and coverage.
Use `io.write_json` and `io.digest` when providing a local snapshot through this contract.

`date` means exchange session (XKRX/XNYS), or the **start** of a crypto UTC day.
Equity decisions use the data-plane close plus 30-minute lag (KR 16:00 KST, early
closes and known CSAT dates respected); crypto D closes on D+1 at 00:00 UTC, plus
five-minute lag. Missing sessions are never compressed into an h-*observed-bar* label.
Benchmark gaps fail closed. An unavailable feature row is excluded, with eligibility
recorded; unknown future outcomes never remove an otherwise eligible candidate.

Eligibility: all 252 equity sessions (364 crypto UTC daily sessions) of price history required by the feature set, positive
current volume, then top N by trailing 20-session mean traded value (including today).
Defaults: KR 500 / US 1000 / crypto 100. Ties resolve by symbol. **Equity public sources
use an explicit `close*volume` estimate**, not observed exchange traded value. The
price-unit basis is the provider's historical vintage. Upbit supplies actual quote
turnover. A full historical PIT membership input must include later-delisted symbols;
the downloader's explicit current-name samples do not supply that membership.

Features: 5/20/60-session momentum; 1/5-session reversal; volume/value shock against
the preceding 20 sessions; 20-session return sample volatility; open/previous-close
gap; close/52-week rolling high (252 equity sessions, 364 crypto days) minus one. Each has a benchmark-relative version.
Low volatility is the frozen orientation; the other scores use the feature's sign.
Subtracting the same benchmark scalar cannot change a day's baseline ordering; the
relative columns can still provide time-varying context to a fitted model.
Outcomes are close(t+h)/close(t)-1 for h=1,3,5,10, raw and minus benchmark return.
Any missing intermediate bar or flagged corporate action makes that outcome unknown.
No dividend total-return, delisting payout, or terminal loss is fabricated.
Dataset output carries `prediction_at`. Forward and grade JSON identify
`label_kind="statistical_close_to_close"`, the prediction timestamp, `data_cutoff`,
and `execution_label=null`. An executable label would start at the first tradable
price after `prediction_at` and needs its own fill and cost contract. This scorer
rejects a record relabelled as executable.
In a historical diagnostic replay, `prediction_at` is the simulated decision time;
it does not certify a contemporaneous source vintage.

## Commands

Install normal test dependencies with `uv sync --all-groups`; optional model dependencies
stay outside `pyproject.toml`:

```bash
uv pip install --python .venv/bin/python -r research/stock_selection/requirements-ml.txt
# macOS LightGBM requires OpenMP. An existing compatible libomp can be passed
# through DYLD_LIBRARY_PATH; do not change broker/application dependencies.

python -m research.stock_selection.fetch --market kr \
  --kr-source naver --benchmark naver:KOSPI \
  --symbols-file /path/to/explicit-kr-symbols.txt --start 2020-01-01 --end 2023-01-20
# KR symbols-file: one 005930.KS / 247540.KQ provider symbol per line.
# US: DB symbols (BRK.B converted centrally); crypto: KRW-BTC etc.
# Yahoo is the default equity source; Naver is an explicit whole-series KR
# alternative. Yahoo event dates flag KR action-crossing labels for either source.

python -m research.stock_selection.build --market kr --start 2021-01-01 --end 2022-12-30
python -m research.stock_selection.evaluate --market kr --liquid-n 10 \
  --train-start 2021-01-01 --first-test 2022-01-01 --test-end 2022-12-31 \
  --horizon 1 --output-dir research/stock_selection/.cache/evaluation
# Repeat --horizon 3 / 5 / 10 to bound work, or omit it for all four.

python -m research.stock_selection.forward --market kr --asof 2022-11-01 --liquid-n 10
python -m research.stock_selection.score --market kr --asof 2022-12-01 \
  --lists research/stock_selection/.cache/forward/kr/2022-11-01-momentum_20.json
```

Every command accepts `--cache-dir` (or `STOCK_SELECTION_CACHE`); output commands accept
`--output-dir`. Downloads refuse an existing snapshot. Dated forward files allow
identical replay and reject changed content. Forward defaults to momentum_20;
`--model lightgbm_rank` fits only earlier matured training labels behind the same
10+10-session boundary. Forward output retains the full scored cross-section so its
later IC and deciles are graded by the identical scorer. Score emits `pending`,
`scored`, or `unknown_outcome` per horizon. It refuses revised historical inputs.

## Evaluation contract

Common-date expanding folds, 63 validation sessions each. Before each boundary,
drop ten training decision sessions for label purge and another ten for embargo.
These are unique session dates, independent of the number of symbol rows. The
shared ten-session purge covers each h=1,3,5,10 label and keeps comparison folds
identical. `TimeSeriesSplit(gap=10)` on panel rows is not used.
All rolling features are causal; there is no fitted preprocessing, tuning, early
stopping, or model selection. LightGBM regresses on training-day percentile ranks
of relative returns: 60 trees, 7 leaves, learning rate .05, minimum child 40, L2 1,
seed 1474, one thread. Frozen configuration lives in `models.py`.
This is `LGBMRegressor` on daily percentile ranks, not LambdaRank on raw returns.
Each fit is already scoped to one market and one horizon. A future `LGBMRanker`
must define market/date/horizon query groups and integer relevance buckets with
explicit gains in a separately registered experiment.

`metrics.score_daily` accepts any finite daily scores. Spearman uses average ranks
for ties; ICIR is mean daily IC / sample standard deviation, unannualized (undefined
dispersion is JSON null). h-day outcomes overlap, so ICIR is not a significance test.
Deciles use ceil(10% of names), require ten names, and resolve score ties by symbol.
Up/down hit is strict positive/negative raw return among disjoint top-N lists.
Turnover is half the L1 change in signed decile weights (gross two, first formation
included). Cost-adjusted spread subtracts **four one-way sides** from each independent
h-day long/short vintage: assumed KR25, US10, crypto15 bps/side, with no netting.
This conservative cost proxy is separate from turnover; it is not a daily-rebalanced
portfolio, annualized return, achievable short, or fill simulation. Same-close entry
is a label reference, not a claim that the close can be traded after computing it.

Results include daily values, year splits and regime splits. Regime is trailing
20-session benchmark price return: up >2%, down <-2%, otherwise flat. Selected
missing outcomes null the affected spread/hit; IC reports available-pair coverage.
Summaries average only known daily metrics and expose their measured-day counts.

## Evidence and limitations

Register before historical evaluation in `research/experiments/registry.json`.
Current results are **SURVIVOR-ONLY / BOUNDED** development diagnostics, counted as k=2 (the preliminary crypto 252-day-high view is frozen and superseded);
there is no untouched final test or prospective verdict. The report supplies every
model × market × horizon, source hashes, raw commands, failures and data gaps.
Survivorship usually inflates long returns and hides negative tails; its effect on
long-short spread and IC has no guaranteed sign. Revised price histories and estimated
turnover add unmeasured biases. No market-wide readiness claim follows from these runs.

The survivor label describes these downloaded samples, not all available repository
data. KR already has a delisted-inclusive causal export with primary terminal
classification 555/555; Binance USDT spot has 256 delisted pairs; US PIT listing
intervals use observation brackets. See the [dots follow-up and corpus audit](../../docs/research/rob-1474-dots-follow-up.md).
The registered numerical reports remain frozen. This addendum does not rename the
12-name KR sample as a full-corpus evaluation or substitute Binance for Upbit.

```bash
uv run pytest tests/research/stock_selection/test_harness.py -q
```

The hand fixture has IC 163/165, spread .09 and US cost-adjusted spread .086.
Tests mutate actual future-bar-derived features/labels, verify prefix invariance,
the additive embargo, missing/action outcomes, and forward/score persistence for all
three calendars. New tests are registered in `ci_shards/shard-4.txt`.
