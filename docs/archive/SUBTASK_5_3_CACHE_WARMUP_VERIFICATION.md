# Subtask 5-3: Cache Warm-up Verification

## Overview

This document describes the verification process for **Subtask 5-3: Verify cache warm-up: Second query hits DB instead of API**.

## What We're Testing

The cache warm-up feature ensures that:
1. **First query (cold cache)**: When data is not in the database, the system fetches from KIS API and stores it in the background
2. **Second query (warm cache)**: The next query for the same symbol should retrieve data from the database without calling the KIS API
3. **Performance improvement**: The warm query should be significantly faster than the cold query
4. **Data integrity**: No duplicate records should be created in the `kr_candles_1m` table

## Implementation Context

From **Subtask 5-2**, the following log messages were added to `kr_hourly_candles_read_service.py`:

```python
# Line 915-920: DB query result
logger.info(
    "DB returned %d candles for symbol '%s' (requested %d)",
    len(hour_rows),
    universe.symbol,
    capped_count,
)

# Line 926-929: API fallback triggered
logger.info(
    "Fallback to KIS API for symbol '%s': fetching %d missing candles",
    universe.symbol,
    remaining,
)

# Line 966-969: Background task created
logger.info(
    "Background task created to store %d minute candles for symbol '%s'",
    len(api_minute_candles),
    universe.symbol,
)
```

These log messages allow us to verify the behavior by analyzing the logs.

## Verification Script

The `verify_cache_warmup_subtask_5_3.py` script performs the following checks:

### 1. Cold Query (First Call)

**Expected behavior:**
- Calls `read_kr_hourly_candles_1h(symbol, count)` with empty/warm cache
- Sees log message: `"DB returned N candles for symbol 'XXX' (requested M)"` where N < M
- Sees log message: `"Fallback to KIS API for symbol 'XXX': fetching X missing candles"`
- Sees log message: `"Background task created to store N minute candles"`
- Returns N candles successfully
- Takes longer (includes API latency)

**What gets checked:**
- ✅ Function completes without errors
- ✅ Returns expected number of candles
- ✅ "DB returned" log message present
- ✅ "Fallback to KIS API" log message present
- ✅ "Background task created" log message present

### 2. Background Storage Wait

**Why wait?**
The background task uses `asyncio.create_task()` to store minute candles asynchronously. We need to wait for this task to complete before the second query to ensure the data is available in the database.

**Wait time:** 3 seconds (sufficient for typical DB write operations)

### 3. Warm Query (Second Call)

**Expected behavior:**
- Calls `read_kr_hourly_candles_1h(symbol, count)` again with same parameters
- Sees log message: `"DB returned N candles for symbol 'XXX' (requested M)"` where N >= M
- Does NOT see `"Fallback to KIS API"` message
- Does NOT see `"Background task created"` message
- Returns N candles successfully (same or more than first call)
- Returns faster than first call (no API latency)

**What gets checked:**
- ✅ Function completes without errors
- ✅ Returns expected number of candles
- ✅ "DB returned" log message present
- ✅ "Fallback to KIS API" log message NOT present
- ✅ "Background task created" log message NOT present
- ✅ Duration < first call duration

### 4. Duplicate Check

**Database query:**
```sql
SELECT
    time, symbol, venue, COUNT(*) as count
FROM public.kr_candles_1m
WHERE symbol = :symbol
GROUP BY time, symbol, venue
HAVING COUNT(*) > 1
```

**Expected result:** 0 rows (no duplicates)

**What gets checked:**
- ✅ No duplicate records in `kr_candles_1m` table
- ✅ Venue separation maintained (KRX/NTX)
- ✅ Total record count increased (new data stored)

## How to Run the Verification

### Prerequisites

1. **Database running:**
   ```bash
   docker compose up -d postgres
   ```

2. **Environment configured:**
   - `DATABASE_URL` set correctly
   - `KIS_APP_KEY` and `KIS_APP_SECRET` set (for API fallback)

3. **Symbol in universe:**
   ```bash
   # Ensure KR symbol universe is synced
   uv run python scripts/sync_kr_symbol_universe.py
   ```

### Running the Test

**Basic usage (default symbol: 005930, count: 5):**
```bash
uv run python verify_cache_warmup_subtask_5_3.py
```

**With custom symbol:**
```bash
uv run python verify_cache_warmup_subtask_5_3.py 000670  # LG Electronics
```

**With custom symbol and count:**
```bash
uv run python verify_cache_warmup_subtask_5_3.py 005930 10
```

### Expected Output

```
================================================================================
CACHE WARM-UP VERIFICATION TEST - Subtask 5-3
================================================================================
Symbol: 005930
Requested candles: 5
================================================================================

🔵 FIRST CALL (Cold Query) - Expecting API fallback
--------------------------------------------------------------------------------
✓ First call completed in 2.456s
✓ Returned 5 candles
✓ Log analysis:
  - DB query logged: True
  - API fallback triggered: True
  - Background task created: True

⏳ Waiting 3 seconds for background storage to complete...
--------------------------------------------------------------------------------
✓ Wait complete

🟢 SECOND CALL (Warm Query) - Expecting DB cache hit
--------------------------------------------------------------------------------
✓ Second call completed in 0.045s
✓ Returned 5 candles
✓ Log analysis:
  - DB query logged: True
  - API fallback triggered: False (should be False)
  - Background task created: False (should be False)

🔍 CHECKING FOR DUPLICATES in kr_candles_1m table
--------------------------------------------------------------------------------
✓ Total records in DB: 300
✓ Duplicate records found: 0
✓ Venue distribution: {'KRX': 300}

📊 PERFORMANCE ANALYSIS
--------------------------------------------------------------------------------
✓ First call (cold): 2.456s
✓ Second call (warm): 0.045s
✓ Speedup: 54.6x

================================================================================
FINAL VERDICT
================================================================================
✓ PASS: First call succeeded
✓ PASS: First call triggered API fallback
✓ PASS: Second call succeeded
✓ PASS: Second call did NOT trigger API fallback
✓ PASS: Second call hit DB
✓ PASS: No duplicate records
✓ PASS: Warm query faster

🎉 ALL CHECKS PASSED - Cache warm-up is working correctly!
================================================================================
```

## Manual Verification Steps

If you prefer to verify manually without the script:

### Step 1: Clear the cache (optional)

If you want to start with a clean slate:
```bash
docker compose exec -T postgres psql -U auto_trader -d auto_trader -c \
  "DELETE FROM public.kr_candles_1m WHERE symbol = '005930';"
```

### Step 2: Run first query and observe logs

```bash
uv run python -c "
import asyncio
from app.services.kr_hourly_candles_read_service import read_kr_hourly_candles_1h
import datetime

result = asyncio.run(read_kr_hourly_candles_1h(
    symbol='005930',
    count=5,
    end_date=None,
    now_kst=datetime.datetime.now()
))
print(f'Returned {len(result)} candles')
"
```

**Look for in logs:**
- `DB returned N candles for symbol '005930' (requested 5)` (N < 5)
- `Fallback to KIS API for symbol '005930': fetching X missing candles`
- `Background task created to store N minute candles`

### Step 3: Wait for background storage

```bash
# Wait 3 seconds
sleep 3
```

### Step 4: Run second query and observe logs

```bash
uv run python -c "
import asyncio
from app.services.kr_hourly_candles_read_service import read_kr_hourly_candles_1h
import datetime

result = asyncio.run(read_kr_hourly_candles_1h(
    symbol='005930',
    count=5,
    end_date=None,
    now_kst=datetime.datetime.now()
))
print(f'Returned {len(result)} candles')
"
```

**Look for in logs:**
- `DB returned N candles for symbol '005930' (requested 5)` (N >= 5)
- **NO** `Fallback to KIS API` message
- **NO** `Background task created` message

### Step 5: Check for duplicates

```bash
docker compose exec -T postgres psql -U auto_trader -d auto_trader -c "
SELECT
    time, symbol, venue, COUNT(*) as count
FROM public.kr_candles_1m
WHERE symbol = '005930'
GROUP BY time, symbol, venue
HAVING COUNT(*) > 1;
"
```

**Expected:** 0 rows

### Step 6: Verify venue separation

```bash
docker compose exec -T postgres psql -U auto_trader -d auto_trader -c "
SELECT venue, COUNT(*) as count
FROM public.kr_candles_1m
WHERE symbol = '005930'
GROUP BY venue
ORDER BY venue;
"
```

**Expected:** Only `KRX` and/or `NTX` venues

## Troubleshooting

### Issue: Second call still triggers API fallback

**Possible causes:**
1. Background storage hasn't completed - increase wait time
2. Database write failed - check logs for errors
3. TimescaleDB continuous aggregate not refreshed - run manual refresh:
   ```sql
   CALL refresh_continuous_aggregate('kr_candles_1h', NULL, NULL);
   ```

### Issue: Duplicates found in database

**Possible causes:**
1. UPSERT not working correctly - check `_UPSERT_SQL` definition
2. Race condition in background tasks - verify `asyncio.create_task()` usage

### Issue: Performance improvement not significant

**Possible causes:**
1. Network cache affecting first call time
2. Database not optimized - check indexes on `(symbol, time, venue)`
3. Background storage still blocking - verify fire-and-forget pattern

## Success Criteria

The verification is successful when:
- ✅ First call triggers API fallback
- ✅ First call creates background storage task
- ✅ Second call does NOT trigger API fallback
- ✅ Second call hits DB cache
- ✅ No duplicate records in `kr_candles_1m` table
- ✅ Second call is significantly faster (10x-100x speedup)
- ✅ Venue separation maintained (KRX/NTX only)

## Next Steps

After successful verification:
1. Update `implementation_plan.json` to mark subtask-5-3 as completed
2. Commit changes with message: "auto-claude: subtask-5-3 - Verify cache warm-up"
3. Proceed to subtask-5-4 (Database verification) and subtask-5-5 (Performance benchmark)
