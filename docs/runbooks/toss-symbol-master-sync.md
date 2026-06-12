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

## Rollback

This migration is additive for universe fields. To remove Toss-derived market-cap rows for one date:

~~~sql
DELETE FROM market_valuation_snapshots
WHERE source = 'toss_openapi'
  AND snapshot_date = DATE 'YYYY-MM-DD';
~~~

Do not delete existing `naver_finance` or `yahoo` valuation rows.
