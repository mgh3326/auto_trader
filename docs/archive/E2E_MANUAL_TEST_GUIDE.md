# E2E Manual Test Guide for subtask-5-2

## Overview

This document provides the manual E2E test verification for subtask-5-2: "Query non-held stock, verify API called, data returned, background task created"

## Test Environment Requirements

### Prerequisites
- PostgreSQL database running (via Docker Compose)
- Redis running (optional, for rate limiting)
- Valid KIS API credentials in `.env` file:
  - `KIS_APP_KEY`
  - `KIS_APP_SECRET`
- KR symbol universe synced (`scripts/sync_kr_symbol_universe.py`)

### Start Services
```bash
# Start PostgreSQL and Redis
docker compose up -d

# Verify services are running
docker compose ps

# Run database migrations
uv run alembic upgrade head

# Sync KR symbol universe
uv run python scripts/sync_kr_symbol_universe.py
```

## Test Case: Non-Held Stock Query

### Objective
Verify that querying a stock symbol with no data in the database triggers:
1. KIS API fallback call
2. Background task creation for data persistence
3. Successful data return to caller

### Test Symbol
- **Primary**: `005930` (Samsung Electronics)
- **Alternative**: `000660` (SK Hynix)
- **Alternative**: Any symbol in `kr_symbol_universe` table with no `kr_candles_1m` records

### Test Steps

#### Step 1: Verify Initial State
```sql
-- Connect to database
docker compose exec postgres psql -U auto_trader -d auto_trader

-- Check if symbol has any 1m candle data
SELECT COUNT(*) FROM public.kr_candles_1m WHERE symbol = '005930';

-- Expected: 0 or minimal count (for true cold query test, should be 0)
```

#### Step 2: Run Query (Python)
```python
# Create test script: test_query.py
import asyncio
import datetime
import logging
from app.services.kr_hourly_candles_read_service import read_kr_hourly_candles_1h

# Enable logging
logging.basicConfig(level=logging.INFO)

async def test():
    result = await read_kr_hourly_candles_1h(
        symbol='005930',
        count=5,
        end_date=None,
        now_kst=datetime.datetime.now()
    )
    print(f"Returned {len(result)} candles")
    print(result.head())

asyncio.run(test())
```

Run the script:
```bash
uv run python test_query.py
```

#### Step 3: Verify Logs
Check console output for:
- ✅ `Fallback to KIS API` or `Calling KIS API` message
- ✅ `Background task created` message
- ✅ `Returned N candles` (N should be > 0)
- ❌ No `ValueError` or exceptions raised

#### Step 4: Wait for Background Task
```bash
# Wait 2-3 seconds for background storage to complete
sleep 3
```

#### Step 5: Verify Data Persistence
```sql
-- Check minute candles were persisted
SELECT
    COUNT(*) as total_records,
    COUNT(DISTINCT venue) as venue_count
FROM public.kr_candles_1m
WHERE symbol = '005930';

-- Expected:
-- - total_records > 0 (should have multiple 1m candles)
-- - venue_count = 1 or 2 (KRX only, or KRX + NTX)

-- View latest candles
SELECT
    time,
    venue,
    open,
    high,
    low,
    close,
    volume
FROM public.kr_candles_1m
WHERE symbol = '005930'
ORDER BY time DESC
LIMIT 10;
```

#### Step 6: Verify Cache Warm-up (Optional)
Query the same symbol again:
```bash
uv run python test_query.py
```

Expected:
- ✅ Logs show `DB returned N candles` (NOT "Fallback to KIS API")
- ✅ Query returns faster (no API latency)
- ✅ Same number of candles returned

## Success Criteria

The test passes when:
1. ✅ First query triggers API fallback (check logs)
2. ✅ No ValueError raised (graceful degradation)
3. ✅ Data returned (DataFrame with N candles, N > 0)
4. ✅ Background task created (check logs)
5. ✅ Minute candles persisted to `kr_candles_1m` table
6. ✅ Second query hits cache (no API call)

## Troubleshooting

### Issue: "Symbol not in kr_symbol_universe"
**Solution**: Run the sync script
```bash
uv run python scripts/sync_kr_symbol_universe.py
```

### Issue: "KIS API authentication failed"
**Solution**: Verify credentials in `.env`:
```bash
grep KIS_APP_KEY .env
grep KIS_APP_SECRET .env
```

### Issue: "No data returned"
**Possible causes**:
- Market is closed (test during market hours: 9:00-15:30 KST)
- Symbol delisted or suspended
- API rate limit exceeded

**Solution**: Try a different symbol or test during market hours

### Issue: "No minute candles persisted"
**Possible causes**:
- Background task failed (check logs)
- Database transaction not committed
- Incorrect venue constraint

**Solution**:
- Check logs for background task errors
- Verify `venue` is 'KRX' or 'NTX' (not other values)
- Check database constraints: `ck_kr_candles_1m_venue`

## Test Results Template

```
Date: [YYYY-MM-DD]
Time: [HH:MM:SS] KST
Symbol: 005930

Step 1 - Initial State:
  - Initial 1m candle count: [N]

Step 2 - Query Execution:
  - Candles returned: [N]
  - Execution time: [N] seconds
  - API fallback triggered: Yes/No
  - Background task created: Yes/No
  - Exceptions raised: None/[Error message]

Step 3 - Logs Verification:
  - "Fallback to KIS API": Yes/No
  - "Background task created": Yes/No
  - "DB returned": Yes/No

Step 4 - Data Persistence:
  - Final 1m candle count: [N]
  - Venues present: [KRX] / [KRX, NTX]
  - Latest candle time: [YYYY-MM-DD HH:MM:SS]

Step 5 - Cache Warm-up:
  - Second query time: [N] seconds
  - API called again: No/Yes
  - Cache hit: Yes/No

Overall Result: PASSED/FAILED
Notes: [Any observations or issues]
```

## Automated Test Script

A Python script `test_e2e_subtask_5_2.py` has been provided with:
- Database state checking
- Query execution with timing
- Log capture and verification
- Background task completion waiting
- Data persistence verification
- Results summary

Run with:
```bash
uv run python test_e2e_subtask_5_2.py
```

Expected output:
```
================================================================================
E2E Test Results
================================================================================
Symbol: 005930
Duration: 2.3 seconds
API Called: True
Background Task Created: True
DB Hit: False
Total Logs Captured: 15

Requirement Verification:
  ✓ API called: True
  ✓ Background task created: True
  ✓ Data returned: 5 candles
  ✓ 1m candles persisted: 300 records

✅ TEST PASSED

All requirements verified:
  1. API fallback triggered
  2. Background task created
  3. Data returned successfully
  4. Minute candles persisted to DB
================================================================================
```

## Notes

- This test requires network access to KIS API (`https://openapi.koreainvestment.com:9443`)
- For accurate results, test during KRX market hours (09:00-15:30 KST on weekdays)
- Background task may take 1-3 seconds to complete (depends on data volume)
- Subsequent queries should be significantly faster (< 100ms vs 2-3 seconds)
