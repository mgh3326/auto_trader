# Toss Symbol Master Sync Runbook

ROB-534 adds a dry-run-first sync for Toss Open API symbol master metadata and market-cap valuation rows.

## Dry Run

~~~bash
uv run python -m scripts.sync_toss_symbol_master --market kr --limit 20
uv run python -m scripts.sync_toss_symbol_master --market us --limit 20
~~~

Expected output includes requested symbols, matched stocks, missing stocks, master updates, and market-cap payload count. Dry-run writes no rows.

## Commit

Run only after reviewing dry-run coverage and after ROB-534 stronger-model/CTO migration review clears the change.

~~~bash
uv run python -m scripts.sync_toss_symbol_master --market kr --all --commit
uv run python -m scripts.sync_toss_symbol_master --market us --all --commit
~~~

### Gap-fill semantics & ordering (ROB-546)

Toss valuation rows carry **market_cap only** (PER/PBR/ROE/dividend/52w are
NULL). To keep these metric-sparse rows from shadowing metric-rich
`naver_finance`/`yahoo` rows in readers (fundamentals evidence, screener
market-cap join), the sync **gap-fills**: for a given `(market, symbol,
snapshot_date)` it skips the Toss `market_cap` row whenever another source
already has a row for that key. Master metadata columns (shares_outstanding,
security_type, etc.) are always updated regardless.

The dry-run/commit packet reports `market_cap_skipped_existing` — the number of
symbols whose Toss market_cap was skipped because a richer source already
covered them.

**Build order:** run the `naver_finance` / `yahoo` fundamentals valuation build
for a `snapshot_date` **before** the Toss `--all --commit` for that same date.
Doing so keeps Toss in pure gap-fill mode (it only adds market_cap for symbols
no other source covers) and prevents a Toss-only metric-sparse partition from
being picked as the "latest healthy" partition by screener loaders. The
reader-side partition-selection hardening for the out-of-order case is tracked
separately; this runbook ordering is the operational guard.

`is_common_stock` (US): Toss `is_common_share` only **fills** this column when
it is currently NULL — it never flips an existing NASDAQ-Trader classification
(which drives screener partition denominators). The commit packet prints a
`warning:` line with the NULL→TRUE fill count for the guard.

## Rollback

This migration is additive for universe fields. To remove Toss-derived market-cap rows for one date:

~~~sql
DELETE FROM market_valuation_snapshots
WHERE source = 'toss_openapi'
  AND snapshot_date = DATE 'YYYY-MM-DD';
~~~

Do not delete existing `naver_finance` or `yahoo` valuation rows.

## 시가총액 행 보존 (hk 5751)

`--commit`은 이제 써 넣은 심볼의 더 오래된 `toss_openapi` 행을 같은 트랜잭션에서 지운다. 따라서 Toss 행은 `(market, symbol)`마다 최신 한 개만 남는다. 다른 소스(`naver_finance`·`yahoo`·`tvscreener`) 행은 건드리지 않는다. 과거 누적분은 `scripts/prune_nonprice_history.py --only toss_valuation` 으로 정리한다(`docs/runbooks/nonprice-history-prune.md`).
