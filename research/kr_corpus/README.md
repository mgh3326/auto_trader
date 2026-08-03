# KR Corpus v1 consumer disclosure

This document is the required disclosure for corpus ID kr-corpus-v1.  It
applies to the immutable 2026-08-03 snapshot built from pykrx 1.2.8.  Its
terminal verdict remains BUILT_WITH_GAPS.  It is for exploratory backtest
research only and is not admissible for the current KR-B1 gate.

The original snapshot manifest is immutable and keeps its file-level checksum
architecture intact.  This README is the consumer-facing disclosure addendum;
it does not recollect data, alter an original valid-bar value, fill a gap,
change a denominator, or read a holdout row.  A separately named clamp-admit
sidecar described below does modify high/low only in its own derived rows; it
never overwrites the original valid-bar view.

## Critical consumer warning

OHLC 트리거 로직(스탑·지정가·돌파)은 이 corpus 에서 낙관/비관 어느 쪽으로든 왜곡된다.

The overall backtest-bias risk is MEDIUM.  It is HIGH for OHLC-triggered
logic, including stop-loss and limit-fill simulation, high/low breakout
triggers, intraday-range features, and gap features.  The omission is
directional rather than random.  A close-to-close backtest that bridges a
missing session can capture less of this effect than OHLC-dependent logic, but
it must still treat the result as conditional on this limitation.

## What the 201,805 exclusions mean

The immutable manifest's combined explicit-gap count is 201,805:
171,348 main-scope rows (2015-01-01 through 2024-12-31) plus 30,457 sealed
holdout rows (2025-01-01 through 2026-07-31).  The A/B analysis below was
performed on the 171,348 main rows only.  The holdout was not opened or
classified for this disclosure, so the A/B percentages must not be treated as
a combined-scope composition claim.

| Population, main scope only | Rows | Share of main gaps | Character | Collection treatment |
|---|---:|---:|---|---|
| A. non-traded day | 131935 | 77.00% | open=high=low=0, close>0; 99.99% volume=0 | No valid tradable OHLC exists; exclusion is appropriate. |
| B. tradeable day | 39413 | 23.00% | high is below max(open, close) while prices are real | Adjusted-price rounding artifact; exclusion creates directional loss. |

Population A is how the adjusted source represents a no-trade session: the
reference close is present but O/H/L are zero.  Population B is a source-side
adjusted-price rounding issue: independently rounded integer O/H/L/C values can
put close one tick above high.  The source data were not repaired.  Each
affected membership-day is an explicit gap and its source anomaly is retained.

## Directional bias in the tradeable population

For the 39,413 main-scope tradeable exclusions, close/open minus one is:

| Statistic | Value |
|---|---:|
| Up-day share | 99.99% |
| Mean return | +2.81% |
| p25 / p50 / p75 | +0.45% / +1.42% / +3.16% |
| p95 / p99 | +10.17% / +28.80% |
| Down-day share | 0.01% |

This is mechanically directional: a close above the observed high is much more
likely when close is the day's extreme, so the filter removes the strongest
up-close events rather than a random sample of bars.

### Up-tail loss, main scope only

The tradeable-row loss baseline is 0.708%.  Multiples below are approximate
ratios to that rounded baseline; they are disclosure metrics, not a replacement
for the original membership-day coverage denominator.

| Return threshold | Dropped | Stored | True total | Missing | Approx. baseline multiple |
|---|---:|---:|---:|---:|---:|
| Any up-day | 39410 | 2416299 | 2455709 | 1.60% | 2.3x |
| > +10% | 2009 | 53555 | 55564 | 3.62% | 5.1x |
| > +20% | 911 | 11591 | 12502 | 7.29% | 10.3x |
| > +25% | 718 | 6799 | 7517 | 9.55% | 13.5x |
| > +30% | 165 | 1538 | 1703 | 9.69% | 13.7x |

The often-quoted 10.3x figure applies to the > +20% row.  The more extreme
thresholds are higher when computed against the same rounded 0.708% baseline;
they are shown explicitly so the disclosure does not understate tail loss.

### Time and market concentration, main scope only

39,110 of the 39,413 tradeable losses (99.2%) are in TRAIN
(2015-01-01 through 2022-12-31); only 303 are in VALIDATION
(2023-01-01 through 2024-12-31).  All main-scope gaps (A plus B) split as
KOSDAQ 134,329 and KOSPI 37,019.  Therefore training and validation do not have
comparable omission mechanisms.

## Coverage definitions: retain both

The original immutable measure is membership-day coverage and must remain the
terminal measure:

| Coverage definition | Minimum | Where | Interpretation |
|---|---:|---|---|
| Original: valid bar / every positive membership-day | 0.946542178690388 | KOSDAQ 2020 | Includes non-traded days, so it measures both source collection and whether a trade occurred. |
| Additional: valid bar / tradeable positive membership-day, main scope | 0.983856 | KOSDAQ 2016 | Removes Population A only; it does not repair Population B or change the terminal verdict. |

On the additional tradeable denominator, every main-scope 2021-2024
market-year clears 0.995; KOSDAQ 2024 is 0.999902.  2015-2020 still do not
clear it because the rounding-artifact population is concentrated in older
history.  The additional statistic is diagnostic only.  It does not redefine
MIN_MARKET_YEAR_COVERAGE, convert BUILT_WITH_GAPS to READY_FOR_RESEARCH, or
permit denominator manipulation.

## Clamp-admit derived sensitivity view

An independently checksummed, main-scope-only sidecar exists at
derived-views/clamp-admit-v1.  Its manifest is separate from the immutable
source snapshot and records:

| Relationship | Rows |
|---|---:|
| Original valid-bar view, unchanged | 5525302 |
| Tradeable rounding rows admitted with clamp | 39413 |
| Clamp-admit dataset total | 5564715 |
| Non-traded rows classified no_trade and excluded | 131935 |

The derived dataset carries these row-level Parquet data columns, not merely
metadata: clamped, clamp_delta_high, clamp_delta_low,
clamp_delta_high_relative, clamp_delta_low_relative, source_high, source_low,
clamp_classification, and admitted.  For every clamped row:

| Field | Meaning |
|---|---|
| high | max(source_high, open, close) |
| low | min(source_low, open, close) |
| clamp_delta_high | high minus source_high, a non-negative magnitude |
| clamp_delta_low | source_low minus low, a non-negative magnitude |

The 39,413 admitted rows are marked
clamp_classification=tradeable_adjusted_rounding and clamped=true.  The
131,935 rows with open=high=low=0 and close>0 are instead emitted only under
the sidecar's no_trade/ path with clamp_classification=no_trade,
clamped=false, admitted=false, and zero deltas.  They are never admitted to
the clamp dataset.

The clamp view is a sensitivity view, not a ground-truth replacement:
modifying high/low can create a stop/limit/breakout trigger that did not occur
or remove a trigger that did occur.  Consumers must retain the original
valid-bar view, report clamp-sensitive results separately, and use clamped and
delta columns to filter a tolerated correction magnitude.

## Scope labels and holdout custody

The main snapshot's top-level manifest contains combined main-plus-holdout
aggregates even though its physical rows are main scope only.  Interpret these
existing fields as combined: metrics.bar_rows=6556270,
metrics.membership_rows=6758075, coverage, minimum_market_year_coverage,
explicit_gap_count=201805, gap_reason_counts, and crosscheck_mismatches=1160.
The physical main snapshot has 5,525,302 bar rows, 5,696,650 membership rows,
and 171,348 gaps.  Do not compare the combined totals directly to only the main
snapshot's files.

For future builds, the collector emits explicit field_scopes metadata with
main_plus_holdout labels.  It also omits the former
holdout_written_not_read field.  That field remains in this immutable v1
manifest but is not an audited measurement: its backing counter was never
incremented.  The true protection for v1 is architectural separation, not that
tautological field.  No final holdout row was opened for this disclosure.

## Frozen KIS crosscheck: denominator and main-scope gap

The frozen KIS sample denominator is 1,160 and mismatches are 1,160, for a 0%
match rate.  Its sessions are entirely holdout scope (2025: 592; 2026: 568)
across four symbols.  The sample therefore provides zero independent external
corroboration for the main 2015-2024 corpus.  The mismatch cause remains an open
validation question; it was not investigated by opening the holdout.  The
crosscheck is diagnostic-only and made no overwrite, correction, or fallback
decision.

## Other known limitations and dispositions

- Value/turnover is NULL in 100% of current corpus rows because the adjusted
  pykrx/Naver path does not supply 거래대금.  Do not use this corpus for
  turnover, liquidity, participation, or value-based filters.
- source-anomalies.jsonl uses market=UNMAPPED.  To slice an anomaly by market,
  join its (session, ticker) key to the gap artifact.  This is documented rather
  than rewritten because the snapshot is immutable.
- The current text redactor has a token-collision limitation for any future
  credential value that also appears in a required path or identifier.  No
  secret exposure was found in v1.  A structured field-aware redactor is
  deferred to a separately scoped security change; this disclosure does not
  claim to solve it retroactively.
- This README addresses the prior absence of a snapshot-level consumer guide.

## Required downstream use

Carry this disclosure into any backtest handoff.  Report results separately for
close-to-close and OHLC-triggered logic, and do not use a favorable result from
the latter as evidence of robust execution behavior without an independent
source or sensitivity analysis.  No data values, gap records, membership
denominators, or holdout rows are changed by this disclosure.
