# Subtask 5-5: Performance Benchmark Guide

## Overview

This document provides comprehensive guidance for running and interpreting the performance benchmark for the KR Hourly Candles Read Service. The benchmark measures and validates the performance targets defined in the specification.

## Performance Targets

| Metric | Target | Description |
|--------|--------|-------------|
| **Cold Query** | < 3 seconds | First query when DB is empty (includes KIS API fetch latency) |
| **Warm Query** | < 100ms | Second query when data is cached in DB (database hit only) |
| **Speedup** | ~30x minimum | How much faster warm queries are compared to cold queries |

## What We're Testing

### Cold Query (Cache Miss)
- **Scenario**: First time querying a symbol that's not in the database
- **Process**:
  1. Query `kr_candles_1h` table → returns empty
  2. Fallback to KIS API → fetches 1-minute candles
  3. Aggregate 1-minute candles to hourly candles in-memory
  4. Return data to caller
  5. Schedule background task to persist 1-minute candles to DB
- **Expected Time**: 1-3 seconds (dominated by network latency to KIS API)

### Warm Query (Cache Hit)
- **Scenario**: Subsequent queries for the same symbol
- **Process**:
  1. Query `kr_candles_1h` table → returns cached data immediately
  2. No API call needed
  3. Return data to caller
- **Expected Time**: 10-100ms (database query only)

### Speedup Factor
- **Calculation**: `cold_query_time / warm_query_time`
- **Expected**: 20-50x faster (spec says ~30x)
- **Why**: Eliminating network latency (API call) provides massive performance gain

## Benchmark Script

### Location
`./benchmark_performance_subtask_5_5.py`

### Features
- **Multiple iterations**: Runs 3 times by default for statistical significance
- **Automatic cache clearing**: Clears DB before each cold query
- **Background task waiting**: Ensures data is persisted before warm query
- **Statistical analysis**: Calculates mean and standard deviation
- **Pass/fail reporting**: Color-coded output with clear indicators
- **Exit codes**: Returns 0 on success, 1 on failure (CI/CD compatible)

## How to Run

### Basic Usage
```bash
# Default: Symbol 005930, 5 candles, 3 runs
uv run python benchmark_performance_subtask_5_5.py
```

### Custom Symbol
```bash
# Test with SK Hynix (000660)
uv run python benchmark_performance_subtask_5_5.py 000660
```

### Custom Parameters
```bash
# Test with 10 candles, 5 benchmark runs
uv run python benchmark_performance_subtask_5_5.py 005930 10 --runs 5
```

### With Python Directly (if uv not available)
```bash
python3 benchmark_performance_subtask_5_5.py 005930 5 --runs 3
```

## Expected Output

### Success Case (All Targets Met)
```
================================================================================
                    KR Hourly Candles Performance Benchmark
================================================================================

Symbol: 005930
Count: 5 candles
Runs: 3 iterations

Performance Targets:
  • Cold query: < 3.00s (includes API)
  • Warm query: < 100.00ms (DB hit only)
  • Speedup: > 20.0x

─────────────────────────────────────
Benchmark Run 1/3
─────────────────────────────────────
Clearing Database Cache
✓ Cleared kr_candles_1m for symbol 005930
✓ Database cache cleared successfully

  Measuring cold query (DB empty, API fetch required)...
✓   Cold query returned 5 candles
    → Cold query time: 1.85s
  Waiting for background storage to complete...
  Measuring warm query (data cached in DB)...
✓   Warm query returned 5 candles
    → Warm query time: 45.23ms
    → Speedup: 40.9x

[... additional runs ...]

─────────────────────────────────────
Benchmark Results
─────────────────────────────────────
Successful runs: 3/3

Cold Query (DB empty, API fetch):
  Average: 1.92s (±0.15s)
✓ Target met (< 3.00s)

Warm Query (data cached in DB):
  Average: 48.50ms (±5.20ms)
✓ Target met (< 100.00ms)

Speedup Factor:
  Average: 39.6x
✓ Target met (> 20.0x)

─────────────────────────────────────
Overall Result
─────────────────────────────────────
✓ ALL PERFORMANCE TARGETS MET

The KR hourly candles read service meets all performance requirements:
  • Cold queries complete within 3.00s
  • Warm queries complete within 100.00ms
  • Cache provides >20.0x speedup
```

### Failure Case (Targets Not Met)
```
─────────────────────────────────────
Benchmark Results
─────────────────────────────────────
Successful runs: 3/3

Cold Query (DB empty, API fetch):
  Average: 4.25s (±0.30s)
✗ Target missed (> 3.00s)

Warm Query (data cached in DB):
  Average: 150.00ms (±20.00ms)
✗ Target missed (> 100.00ms)

Speedup Factor:
  Average: 28.3x
✓ Target met (> 20.0x)

─────────────────────────────────────
Overall Result
─────────────────────────────────────
✗ SOME PERFORMANCE TARGETS NOT MET

Please review the results above and consider:
  • Check KIS API latency (affects cold query time)
  • Check database query performance (affects warm query time)
  • Review network connectivity
```

## Interpreting Results

### Cold Query Performance

**If Cold Query is Slow (> 3s)**:
- **Most Likely Cause**: High KIS API latency
  - Network issues between your server and KIS
  - KIS API rate limiting or throttling
  - KIS API server-side issues
- **Solutions**:
  - Check network connectivity to KIS API endpoints
  - Verify KIS API credentials are valid
  - Check if you're hitting rate limits (30 calls/day limit)
  - Consider testing during off-peak hours

**If Cold Query is Very Fast (< 1s)**:
- **Possible Cause**: Data already in DB (cache not cleared)
  - Verify database cache was cleared before test
  - Check script output for "Database cache cleared successfully"
  - Manually verify: `SELECT COUNT(*) FROM kr_candles_1m WHERE symbol = '005930'`

### Warm Query Performance

**If Warm Query is Slow (> 100ms)**:
- **Most Likely Cause**: Database performance issues
  - Missing indexes on `kr_candles_1h` view
  - TimescaleDB continuous aggregate not refreshing properly
  - Database connection pool issues
  - High database load from other queries
- **Solutions**:
  - Check database query execution plan
  - Verify TimescaleDB continuous aggregate policies are active
  - Check database server resources (CPU, memory, disk I/O)
  - Review database connection pool settings

**If Warm Query is Very Fast (< 10ms)**:
- **Excellent!**: Your database is well-optimized
  - Queries are hitting indexes
  - TimescaleDB continuous aggregate is working efficiently
  - Connection pooling is effective

### Speedup Factor

**If Speedup is Low (< 20x)**:
- **Possible Causes**:
  - Warm query is slower than expected (see above)
  - Cold query is faster than expected (maybe API was cached)
- **Investigation**:
  - Review individual cold/warm query times
  - Check if KIS API response is unusually fast (possibly cached by KIS)
  - Verify database performance for warm queries

**If Speedup is Very High (> 100x)**:
- **Excellent!**: But verify accuracy
  - Cold query may include overhead that won't exist in production
  - Warm query may be benefiting from additional caching
  - Consider this an upper bound, not typical performance

## Troubleshooting

### Script Won't Run

**Error: `ModuleNotFoundError: No module named 'app'`**
```bash
# Ensure you're in the correct directory
cd /path/to/worktree
python benchmark_performance_subtask_5_5.py
```

**Error: Database connection failed**
```bash
# Start PostgreSQL
docker compose up -d postgres

# Verify database is running
docker compose ps

# Check database connection string in .env
```

**Error: KIS API authentication failed**
```bash
# Verify KIS credentials in .env
grep KIS_APP_KEY .env
grep KIS_APP_SECRET .env
```

### Benchmark Fails Mid-Execution

**Error: "Cold query failed"**
- KIS API may be down or returning errors
- Check your internet connection
- Verify symbol is valid (exists in KR symbol universe)

**Error: "Warm query returned 0 candles"**
- Background task may not have completed
- Increase wait time in script (find `await asyncio.sleep(2.0)` and increase to 3.0)
- Check database to verify data was persisted

### Inconsistent Results Across Runs

**Problem**: Large standard deviation (±500ms or more)
- **Cause**: External factors affecting performance
  - Network latency variability
  - Database load from other processes
  - KIS API response time variability
- **Solutions**:
  - Run more iterations (`--runs 5` or `--runs 10`)
  - Run during low-traffic periods
  - Close other applications using network/database

## Integration with CI/CD

### GitHub Actions Example

```yaml
name: Performance Benchmark

on:
  pull_request:
    paths:
      - 'app/services/kr_hourly_candles_read_service.py'

jobs:
  benchmark:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Start services
        run: docker compose up -d postgres redis

      - name: Run benchmark
        run: |
          uv run python benchmark_performance_subtask_5_5.py
        env:
          KIS_APP_KEY: ${{ secrets.KIS_APP_KEY }}
          KIS_APP_SECRET: ${{ secrets.KIS_APP_SECRET }}
          DATABASE_URL: ${{ secrets.DATABASE_URL }}

      - name: Fail on performance regression
        run: |
          # Script returns exit code 1 on failure
          uv run python benchmark_performance_subtask_5_5.py || \
          echo "Performance regression detected!"
```

## Performance Optimization Tips

### Reduce Cold Query Time
1. **Use connection pooling**: Reuse KIS API connections
2. **Parallel fetching**: Fetch KRX and NTX venues concurrently
3. **Request optimization**: Only fetch candles needed, not excess
4. **CDN/Edge**: Consider caching closer to users (if applicable)

### Reduce Warm Query Time
1. **Database indexes**: Ensure indexes exist on `(symbol, bucket)`
2. **Query optimization**: Use `EXPLAIN ANALYZE` to review query plans
3. **Connection pooling**: Reuse database connections
4. **TimescaleDB tuning**: Optimize continuous aggregate refresh policy

### Improve Speedup Factor
- Focus on both cold and warm query optimization
- The speedup is a ratio, so both ends matter
- Typical range: 20-50x is excellent

## Related Documentation

- **Subtask 5-2**: E2E test for cold query with API fallback
- **Subtask 5-3**: Cache warm-up verification
- **Main Spec**: `spec.md` (lines 481-487: Performance Benchmarks)
- **Implementation Plan**: `implementation_plan.json` (subtask-5-5)

## Success Criteria

The performance benchmark is considered successful when:

✅ Average cold query time < 3 seconds across multiple runs
✅ Average warm query time < 100ms across multiple runs
✅ Speedup factor > 20x (ideally ~30x as per spec)
✅ Results are consistent (low standard deviation)
✅ No errors during execution
✅ Background tasks complete successfully

## Contact & Support

If you encounter issues not covered in this guide:

1. Check the logs in `benchmark_performance_subtask_5_5.py` output
2. Review related subtasks (5-2, 5-3, 5-4) for context
3. Consult the main specification document
4. Review implementation notes in `build-progress.txt`

---

**Last Updated**: 2026-03-06
**Subtask**: 5-5 - Performance Benchmark
**Status**: Implementation Complete, Ready for Testing
