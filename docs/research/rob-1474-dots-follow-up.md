# ROB-1474: dots follow-up and existing corpus audit

2026-11-02. Source: operator-desk's dots note,
`hk:doc dots/results/ROB-1474/2026-11-02-8e69c7df`, body sha256
`667e627218f15ae987d08e5bb3c2b948fc64e6ca335c1e4a33d6fd11ea8adb28`.
The full note was read before this follow-up.

Experiment-Manifest-Sha256: c698f8b0af75eff96788de35d61d6e8f1593a783e5c734994fea47abda12d4d3
Experiment-Test-Look: no

This follow-up clarifies the existing implementation, adds statistical-label
metadata and regression assertions, and audits existing KR membership data.
It does not train another model, choose a model, or open a new performance period.
The two counted numerical reports remain frozen at k=2.

## Changes against the seven notes

| Dots note | Implementation or correction |
|---|---|
| Purge and embargo count session dates, not panel rows | `walk_forward` deduplicates session dates. Every market's calendar supplies the common boundaries, and all symbols move together. Ten purge sessions plus ten embargo sessions cover each h=1,3,5,10 while keeping comparison folds identical. An uneven-density panel test now proves duplicate row counts cannot shorten the gap, and checks each horizon's label end. `TimeSeriesSplit` is not used. |
| LambdaRank query groups and integer relevance | The current model is `LGBMRegressor` on training-day percentile ranks, which the brief permits. One fit has one market and one horizon, so its date groups are already market/date/horizon groups. It does not call `LGBMRanker` or pass continuous returns as LambdaRank relevance. A future LambdaRank experiment must register integer gain buckets and query sizes before evaluation. No silent objective change was made from these diagnostics. |
| KR delisted history exists | Confirmed and used for the membership audit below: the local causal export matches its registered file hash and contains 555/555 primary terminal classifications. The survivor label in the earlier numerical report applies to that report's 12 requested current names, not to all KR data in this repository. The earlier inventory omitted the distinct 555/555 primary frame; this addendum makes it explicit. |
| Binance delisted-inclusive crypto panel exists | `research/crypto_universe` supplies Binance USDT spot history from 2017-08-17 through 2026-09-30, including 256 delisted pairs among 756 traded pairs. It is a usable delisted-inclusive crypto panel for an explicitly scoped USDT experiment. It does not stand in for Upbit KRW. The 287-pair Upbit corpus recovered no delisted prices; the existing REST probes and the newly found official portal are separate evidence. |
| US PIT intervals exist | `research/us_universe` supplies observation-bracket intervals. Use `listed_from <= t < delisted_by` when a bound exists; do not change it to `t <= listed_to`, invent exact event dates, or equate membership evidence with delisted price or terminal-return coverage. The full local CSV is absent on this host; the committed build manifests remain available. |
| Statistical and executable labels differ | Dataset output now carries `prediction_at`. Forward and grade logs include `label_kind=statistical_close_to_close`, `prediction_at`, `data_cutoff`, and `execution_label=null`. The existing close(t+h)/close(t)-1 label remains statistical. An executable label would begin at the first tradable price after prediction and needs a separate fill/cost contract. Grading rejects an executable relabel or an inconsistent timestamp/cutoff. |
| Unknown forward outcomes never become zero | Pending horizons retain `pending`; mature missing outcomes retain `unknown_outcome`. A test now removes every future bar of a selected up-list symbol, as for a delisted or missing series, and verifies unknown state, partial observed coverage, and null up-hit rather than a zero return. Existing v1 statistical records without the added metadata remain readable under their original statistical contract. |

For retrospective replay, `prediction_at` is the simulated decision timestamp,
not evidence that today's revised public price history was observed then.
The new fields do not convert those diagnostics into an executable backtest.

## Read-only KR corpus audit

Source: `research/kr_delist_repair/data/export_manifest.json` and the local
`kr_daily_causal.v1.parquet` it identifies. The entire file's SHA-256 was checked:

```text
8c52275fa01f4e0540aeb4959bd217efd5d184cf135731748753b282c3e74859
```

The file has 5,696,650 rows. Its manifest reports:

- Primary terminal classification: 555 resolved / 555, zero unresolved.
- Membership-ended frame: 404 resolved / 404, zero unresolved.
- Resolution table: 562 listing rows. This is a separate denominator.
- Payout semantics: 57 ambiguous entries. Classification coverage does not
  establish complete payouts, corporate actions, or executable liquidation prices.

Only membership and session-state columns for 2022 were read for the following
audit. No price return, trading result, or model metric was computed.

| Audit | Count |
|---|---:|
| 2022 membership rows | 617,757 |
| Distinct symbols in those rows | 2,603 |
| Terminal membership rows in 2022 | 45 |
| Valid-price session state | 599,445 |
| No-trade session state | 17,066 |
| Invalid-OHLC session state | 1,246 |
| 2022-11-01 membership rows | 2,539 |
| KOSPI membership on that date | 940 |
| KOSDAQ membership on that date | 1,599 |

The audit used `pyarrow.parquet.read_table` with session filters
`2022-01-01 <= session <= 2022-12-31` and these columns only:
`symbol, listing_id, market, session, session_state, is_terminal_session`.
The local counts are stored in the ignored
`research/stock_selection/.cache/kr-pit-preflight.json`.
No sealed holdout, database, broker, credential, or scheduler was used.

These counts establish that a much broader KR membership panel is available.
They do not replace the earlier sample's model tables with uncomputed full-panel
results. A full KR run should use the existing listing identities and lifecycle
evidence, preserve no-trade and invalid rows as explicit exclusions or unknown
outcomes, declare the turnover estimate, and register its input and evaluation
before producing a new performance look. The current 12-name results cannot be
renamed as that run.

## Binance and US evidence boundaries

The committed Binance manifest labels its archive
`INCLUDES_DELISTED_WHERE_ARCHIVED`. It records no fetch-error exclusions,
but eight pairs have internal no-bar days and 19 currently listed pairs had no
monthly archive yet at the build. Its coverage ends on 2026-09-30 and cannot be
called the November current universe. Listing episodes, stablecoin/leveraged-token
scope, quote currency, and the 2025 holdout boundary must stay explicit.
The external candle-file index is absent on this host; the manifest and PIT tables
are present. This follow-up does not download the archive or read its holdout.

The US manifest documents listing-history reconstruction, including inactive
symbols. Sparse observations and ticker reuse can leave intervals or short-lived
listings unresolved. Membership brackets cannot provide missing OHLCV, corporate
actions, or terminal payouts on their own. A full US evaluation must combine
the intervals with separately verified price and action data.

## Verification

The changed test file exercises date-group embargoes for all four horizons,
statistical-label metadata, executable-label rejection, inconsistent cutoff
rejection, and selected-symbol future-data loss on all three calendars.
The delivery report records the actual pytest and static-check outputs.
Independent review of the compact code delta returned PASS by inspection.
CI is not watched or awaited, per operator-desk's instruction.
