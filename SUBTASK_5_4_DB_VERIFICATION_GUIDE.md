# Subtask 5-4: Database Verification Guide

## Overview

This document explains the database verification process for the `kr_candles_1m` table after implementing the KR 1h candle fallback feature with KIS API integration and background storage.

## What We're Verifying

After the E2E test (Subtask 5-2) and cache warm-up verification (Subtask 5-3), minute candle data should be stored in the `kr_candles_1m` table. This verification ensures:

1. **Data Structure**: Table has correct schema with all required columns
2. **Venue Separation**: KRX and NTX venues are properly separated
3. **No Duplicates**: UPSERT logic prevents duplicate records
4. **Time Format**: Times stored in KST naive format (as per implementation)
5. **Valid OHLCV**: Price and volume data are valid
6. **Continuous Aggregate**: TimescaleDB hourly view is accessible

## Implementation Context

### Background Storage Function

The `_store_minute_candles_background()` function (lines 767-836 in `kr_hourly_candles_read_service.py`) stores minute candles using:

```python
# UPSERT SQL (lines 98-111)
_INSERT INTO public.kr_candles_1m (symbol, time, venue, open, high, low, close, volume, value)
VALUES (:symbol, :time, :venue, :open, :high, :low, :close, :volume, :value)
ON CONFLICT (time, symbol, venue)
DO UPDATE SET
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume,
    value = EXCLUDED.value
```

### Time Format

**Critical**: The implementation stores times as **KST naive** (line 806):
```python
time_naive = _to_kst_naive(time_val)  # Removes timezone info
```

This is different from the spec which mentioned UTC, but matches the existing database format.

### Venue Handling

Venues are converted from market codes:
- Market 'J' → Venue 'KRX'
- Market 'NX' → Venue 'NTX'

The `_to_venue()` function (lines 132-146) validates venues and returns None for invalid values.

## Verification Methods

### Method 1: Automated Python Script

Run the comprehensive verification script:

```bash
# Default symbol (005930)
python verify_db_kr_candles_1m_subtask_5_4.py

# Custom symbol
python verify_db_kr_candles_1m_subtask_5_4.py 000670
```

**Expected Output:**
```
Database Verification for Symbol: 005930
==================================================
ℹ Checking kr_candles_1m table structure and data...

1. Verifying Table Structure
==================================================
✓ Table structure correct with 9 columns

2. Verifying Data Exists
==================================================
✓ Found 600 records for symbol '005930'

3. Verifying Sample Data
==================================================
✓ Retrieved 10 sample rows

  Row 1:
    Time: 2026-03-06 14:59:00
    Symbol: 005930
    Venue: KRX
    OHLC: 80500.00 / 80800.00 / 80400.00 / 80700.00
    Volume: 150000
  ✓ Venue 'KRX' is valid
  ✓ OHLC values are valid
  ✓ Volume is valid

... (more rows)

✓ All sample data is valid

4. Verifying Venue Separation
==================================================
✓ Found venues: ['KRX', 'NTX']
✓ All venues are valid (KRX/NTX)

5. Verifying No Duplicates
==================================================
✓ No duplicate records found

6. Verifying Time Format
==================================================
✓ Time is KST naive (no timezone info)
ℹ Sample time: 2026-03-06 14:59:00

7. Verifying Continuous Aggregate
==================================================
✓ Found 10 hourly candles in continuous aggregate

Verification Summary
==================================================
Table Structure: PASS
Data Exists: PASS
Sample Data: PASS
Venue Separation: PASS
No Duplicates: PASS
Time Format: PASS
Continuous Aggregate: PASS

Total: 7/7 checks passed

✓ All verifications passed!
```

### Method 2: Direct SQL Commands

If Docker access is available, run these commands directly:

#### Check Sample Data
```bash
docker compose exec -T postgres psql -U auto_trader -d auto_trader -c "
SELECT time, symbol, venue, open, high, low, close, volume
FROM public.kr_candles_1m
WHERE symbol = '005930'
ORDER BY time DESC
LIMIT 10;
"
```

**Expected:**
- 10 rows returned
- Venue column shows 'KRX' or 'NTX'
- Times are in KST naive format (no timezone suffix)
- OHLC values are valid (high >= low)
- Volume >= 0

#### Check Venue Separation
```bash
docker compose exec -T postgres psql -U auto_trader -d auto_trader -c "
SELECT DISTINCT venue
FROM public.kr_candles_1m
WHERE symbol = '005930';
"
```

**Expected:**
- Returns 'KRX' and/or 'NTX'
- No other venue values

#### Check for Duplicates
```bash
docker compose exec -T postgres psql -U auto_trader -d auto_trader -c "
SELECT time, symbol, venue, COUNT(*) as count
FROM public.kr_candles_1m
WHERE symbol = '005930'
GROUP BY time, symbol, venue
HAVING COUNT(*) > 1;
"
```

**Expected:**
- Zero rows (no duplicates)

#### Check Record Count
```bash
docker compose exec -T postgres psql -U auto_trader -d auto_trader -c "
SELECT COUNT(*) as total_records,
       COUNT(DISTINCT venue) as unique_venues
FROM public.kr_candles_1m
WHERE symbol = '005930';
"
```

**Expected:**
- `total_records` > 0 (after E2E test)
- `unique_venues` = 1 or 2

#### Check Continuous Aggregate
```bash
docker compose exec -T postgres psql -U auto_trader -d auto_trader -c "
SELECT COUNT(*) as hourly_records
FROM public.kr_candles_1h
WHERE symbol = '005930';
"
```

**Expected:**
- May be 0 if TimescaleDB hasn't refreshed yet
- Should be > 0 after refresh (automatic every 5 minutes)

## Common Issues and Solutions

### Issue 1: No Data Found

**Symptom:** `No data found for symbol '005930'`

**Cause:** E2E test has not been run yet

**Solution:**
1. Run the E2E test from Subtask 5-2:
   ```bash
   python test_e2e_subtask_5_2.py
   ```
2. Wait for background storage to complete (2-3 seconds)
3. Run verification again

### Issue 2: Timezone-Aware Times

**Symptom:** `Time is timezone-aware: Asia/Seoul`

**Cause:** Data stored with timezone info (not matching implementation)

**Solution:** This is expected if using external data sources. The implementation converts to KST naive before storage.

### Issue 3: Duplicate Records Found

**Symptom:** `Found N duplicate records`

**Cause:** UPSERT constraint not working, or duplicate API calls

**Solution:**
1. Check that the unique constraint exists:
   ```sql
   SELECT conname
   FROM pg_constraint
   WHERE conrelid = 'public.kr_candles_1m'::regclass
   AND contype = 'u';
   ```
2. Expected constraint: `kr_candles_1m_time_symbol_venue_key`

### Issue 4: Invalid Venue Values

**Symptom:** `Invalid venues found: ['J', 'NX']`

**Cause:** Market codes not converted to venue codes

**Solution:** Check `_to_venue()` function is being called correctly in the API fallback logic.

### Issue 5: No Data in Continuous Aggregate

**Symptom:** `No data in kr_candles_1h continuous aggregate`

**Cause:** TimescaleDB hasn't refreshed the view yet

**Solution:**
1. Wait 5 minutes for automatic refresh
2. Or manually refresh:
   ```sql
   CALL refresh_continuous_aggregate('public.kr_candles_1h', NULL, NULL);
   ```

## Success Criteria

The verification passes when:

- [x] Table has correct structure (9 columns)
- [x] Data exists for test symbol (005930)
- [x] Sample data shows valid OHLCV values
- [x] Venue separation maintained (KRX/NTX only)
- [x] No duplicate records on (time, symbol, venue)
- [x] Times in KST naive format
- [x] Continuous aggregate accessible

## Next Steps

After successful verification:

1. **Subtask 5-5**: Performance benchmark (cold vs warm query latency)
2. **QA Sign-off**: Review all acceptance criteria
3. **Deployment**: Merge to main branch

## References

- Implementation: `app/services/kr_hourly_candles_read_service.py` (lines 767-836)
- Database Schema: `alembic/versions/87541fdbc954_add_kr_candles_timescale.py`
- Spec: `.auto-claude/specs/006-feat-kr-1h-candle-fallback-db-kis-api/spec.md`
- Plan: `.auto-claude/specs/006-feat-kr-1h-candle-fallback-db-kis-api/implementation_plan.json`

## Author Notes

This verification script was created for Subtask 5-4 of the KR 1h Candle Fallback feature. The script is designed to work in the isolated worktree environment and provides comprehensive checking of the database state after the E2E test and cache warm-up verification have been completed.

**Note:** The verification assumes that the E2E test (Subtask 5-2) has been run and that background storage has completed. If no data is found, run the E2E test first and wait 2-3 seconds for background storage to finish.
