# KR Candles Local Backfill Validation Design

## Context
- PR #186 scope introduced KR minute-candle storage (`kr_candles_1m`) and hourly continuous aggregate (`kr_candles_1h`).
- Local runtime validation target is real KIS-backed ingestion, not mock or dry-run execution.
- Execution path should stay identical to production entrypoint behavior.

## Goal
- Validate local `backfill` ingestion for recent 3 trading sessions using real API calls.
- Confirm data exists in `kr_candles_1m` and is materialized/available in `kr_candles_1h`.

## Scope
- In scope:
  - Run `scripts/sync_kr_candles.py` with `--mode backfill --sessions 3`.
  - Verify 1m and 1h data presence using SQL queries.
  - Report pass/fail strictly against agreed success criteria.
- Out of scope:
  - TaskIQ scheduler/worker orchestration validation.
  - Incremental mode behavior checks.
  - Production deploy/migration workflow changes.

## Execution Architecture
1. Preconditions:
   - DB connection and Timescale extension available.
   - `kr_candles_1m` / `kr_candles_1h` objects exist.
   - `kr_symbol_universe` has active symbols for target holdings.
   - Local KIS credentials are valid.
2. Ingestion:
   - Run:
     - `uv run python scripts/sync_kr_candles.py --mode backfill --sessions 3`
3. Validation:
   - Query `kr_candles_1m` grouped by `symbol/venue` for count and time bounds.
   - Query `kr_candles_1h` for recent buckets.
   - Optionally inspect a representative symbol (for example `005930`) in both tables.

## Success Criteria
1. `public.kr_candles_1m` contains backfilled rows for the executed scope.
2. `public.kr_candles_1h` returns hourly aggregated rows for symbols present in minute data.

## Failure Criteria And Handling
- Preconditions fail (`kr_symbol_universe` empty/inactive/missing, Timescale object missing):
  - Stop execution and report specific blocker.
- Script result fails (`status != completed` or non-zero exit):
  - Report primary error message and stop.
- Script succeeds but table checks fail:
  - Mark overall validation as failed with data-level diagnosis.

## Validation Queries
```sql
SELECT symbol, venue, COUNT(*) AS cnt, MIN(time) AS min_time, MAX(time) AS max_time
FROM public.kr_candles_1m
GROUP BY symbol, venue
ORDER BY cnt DESC
LIMIT 20;
```

```sql
SELECT symbol, bucket, open, high, low, close, volume, value, venues
FROM public.kr_candles_1h
ORDER BY bucket DESC
LIMIT 50;
```

```sql
SELECT
  (SELECT COUNT(*) FROM public.kr_candles_1m WHERE symbol = '005930') AS m1_rows,
  (SELECT COUNT(*) FROM public.kr_candles_1h WHERE symbol = '005930') AS h1_rows;
```
