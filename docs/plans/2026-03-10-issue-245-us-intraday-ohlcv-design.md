# Issue #245: US Intraday OHLCV Extension - Design Document

**Date:** 2026-03-10  
**Status:** Approved for Implementation

---

## Summary

Extend US `get_ohlcv` intraday support to `1m/5m/15m/30m/1h` periods using a dedicated DB-first read service that mirrors the KR pattern but with US-specific adaptations:

- **DB-first architecture**: Query `us_candles_1m/5m/15m/30m` aggregates, with raw-1m re-aggregation for `1h`
- **ET-naive contract**: All timestamps normalized to Eastern Time (naive), with `session` labels (`PRE_MARKET|REGULAR|POST_MARKET`)
- **Source migration**: US intraday changes from `yahoo` to `kis`; US `day/week/month` remains Yahoo-based
- **Dynamic repair**: Abandon KR's fixed 30-minute overlay; use on-demand KIS fallback with self-heal persistence
- **Schema preserved**: Existing `us_candles_*` tables and `candles.us.sync` schedule unchanged

---

## Public Contract (Fixed)

| Aspect | Value |
|--------|-------|
| **Source (intraday)** | `kis` |
| **Source (day/week/month)** | `yahoo` (unchanged) |
| **Timezone** | ET (Eastern Time) naive |
| **Datetime format** | ISO 8601 without timezone offset |
| **Session labels** | `PRE_MARKET` (04:00-09:30), `REGULAR` (09:30-16:00), `POST_MARKET` (16:00-20:00) |
| **Row fields** | `datetime`, `date`, `time`, `open`, `high`, `low`, `close`, `volume`, `value`, `session` |
| **MCP count cap** | 100 |
| **Service count cap** | 200 |

---

## Architecture Decisions

### 1. Reader Service Boundary

Create `read_us_intraday_candles()` with signature:
```python
async def read_us_intraday_candles(
    symbol: str,
    period: str,  # "1m", "5m", "15m", "30m", "1h"
    count: int,
    end_date: datetime | None = None,
    end_date_is_date_only: bool = False,  # True if input was date-only
) -> pd.DataFrame
```

**Location**: `app/services/us_intraday_candles_read_service.py`

**Rationale**: Isolates US intraday complexity (exchange lookup, ET normalization, bucket aggregation, fallback logic) from the shared service layer.

### 2. Period-to-Source Mapping

| Period | Data Source | Implementation |
|--------|-------------|----------------|
| `1m` | `us_candles_1m` | Direct DB query |
| `5m/15m/30m` | `us_candles_5m/15m/30m` | Direct CAGG query |
| `1h` | Raw `us_candles_1m` | Python re-aggregation with ET bucket rules |
| `day/week/month` | Yahoo Finance | Existing path unchanged |

### 3. 1h Bucket Alignment (Clarification #1)

**Intentionally diverges from CAGG**: The existing `us_candles_1h` CAGG uses `offset => INTERVAL '30 minutes'` making all buckets align to :30 boundaries. The new raw-1m re-aggregation uses explicit session-aware boundaries:

| Session | Bucket Start Times (ET) |
|---------|-------------------------|
| Pre-market | 04:00, 05:00, 06:00, 07:00, 08:00 |
| Regular | 09:30, 10:30, 11:30, 12:30, 13:30, 14:30, 15:30 |
| Post-market | 16:00, 17:00, 18:00, 19:00 |

**Why**: The CAGG was designed for regular-session-only aggregation. Supporting pre/post market requires different boundary logic.

### 4. end_date Parsing (Clarification #2)

**MCP layer** (`market_data_quotes.py:650`) must preserve date-only vs timestamp distinction:

```python
# Before (loses distinction):
parsed_end_date = datetime.datetime.fromisoformat(end_date)

# After (preserves distinction):
end_date_is_date_only = len(end_date) == 10  # "YYYY-MM-DD"
if end_date_is_date_only:
    parsed_end_date = datetime.datetime.combine(
        datetime.date.fromisoformat(end_date),
        datetime.time(20, 0)  # 20:00 ET = post-market close
    )
else:
    parsed_end_date = datetime.datetime.fromisoformat(end_date)
```

**Service layer** continues to accept precise `datetime` only.

### 5. MCP and Service Routing (Clarification #3)

Both layers require independent updates:

| Layer | File | Change |
|-------|------|--------|
| MCP | `market_data_quotes.py:659` | Update `source_map` to be period-aware |
| MCP | `market_data_quotes.py:679` | Route US intraday to new reader |
| Service | `market_data/service.py:240` | Route US intraday to new reader |
| Validation | `market_data/constants.py:43` | Add minute periods to `US_OHLCV_PERIODS` |

### 6. Fallback and Self-Heal (Clarification #4)

**Current sync limitation**: `us_candles_sync_service.py` only syncs regular XNYS sessions (09:30-16:00 ET).

**Mitigation**: Comprehensive fallback logic:

1. Query DB for requested range
2. If insufficient rows OR pre/post gaps detected:
   - Call `inquire_overseas_minute_chart()` with backward pagination
   - Normalize KIS timestamps to ET-naive
   - Upsert fetched 1m rows to `us_candles_1m` (self-heal)
3. Re-query or aggregate to fulfill request

**Key insight**: Pre/post market data will initially come from fallback. Over time, self-heal persistence builds DB coverage.

### 7. Timezone Normalization Pipeline

```
DB (TIMESTAMPTZ) ──► UTC-aware datetime ──► astimezone(ET) ──► replace(tzinfo=None) ──► ET-naive
                                    │
KIS API (ET-naive) ─────────────────┘
```

**Session classification** happens AFTER normalization to ET-naive.

### 8. Exchange Resolution

Use existing `get_us_exchange_by_symbol()` from `us_symbol_universe_service.py`:

- Returns `NASD`, `NYSE`, or `AMEX`
- Raises `USSymbolNotRegisteredError` with sync-hint message if symbol not found
- Error propagated as MCP error payload with `source="kis"`

---

## Data Flow

```
User Request (MCP or Service)
    │
    ▼
Period-aware Routing ──► Intraday? ──Yes──► read_us_intraday_candles()
    │                                    │
    No                                   ▼
    │                            Exchange Lookup
    ▼                                    │
Yahoo Finance                    DB Query (1m/5m/15m/30m/1h)
    │                            │
    │                            ▼
    │                    Sufficient Data?
    │                            │
    │                    Yes ────┴─── No
    │                    │             │
    │                    │             ▼
    │                    │     KIS Fallback + Self-Heal
    │                    │             │
    │                    └─────────────┘
    │                            │
    ▼                            ▼
ET-Naive Normalization ◄─── Aggregation (if needed)
    │
    ▼
Session Classification
    │
    ▼
Indicator Enrichment (if requested)
    │
    ▼
Response (Candle for service, extended row for MCP)
```

---

## File Inventory

### New Files
- `app/services/us_intraday_candles_read_service.py` - Core reader implementation
- `tests/test_us_intraday_candles_read_service.py` - Reader unit tests

### Modified Files
- `app/services/market_data/constants.py:43` - Add minute periods to `US_OHLCV_PERIODS`
- `app/services/market_data/service.py:240` - Route US intraday to new reader
- `app/mcp_server/tooling/market_data_quotes.py:650` - Preserve date-only distinction
- `app/mcp_server/tooling/market_data_quotes.py:659` - Period-aware source map
- `app/mcp_server/tooling/market_data_quotes.py:679` - Route US intraday to new reader
- `app/mcp_server/README.md:36` - Update documentation (split intraday vs daily sources)
- `tests/test_mcp_ohlcv_tools.py` - Update US 1h expectations, add intraday tests
- `tests/test_market_data_service.py` - Update US period validation tests

### Unchanged (Reference Only)
- `app/services/us_candles_sync_service.py` - Sync behavior unchanged
- `app/tasks/us_candles_tasks.py` - Schedule unchanged
- `alembic/versions/e7a5b7c9d1f2_add_us_candles_timescale.py` - Schema unchanged
- `tests/test_us_candles_sync.py` - Sync tests unchanged (regression only)

---

## Test Strategy

### Unit Tests (New File)
- `test_read_us_intraday_candles_1m_from_db()` - Direct DB query path
- `test_read_us_intraday_candles_5m_from_cagg()` - CAGG query path
- `test_read_us_intraday_candles_1h_bucket_boundaries()` - ET bucket alignment
- `test_read_us_intraday_candles_dst_transition()` - DST boundary handling
- `test_read_us_intraday_candles_session_classification()` - PRE/REGULAR/POST labels
- `test_read_us_intraday_candles_fallback_trigger()` - KIS fallback on insufficient data
- `test_read_us_intraday_candles_self_heal_write()` - UTC-aware upsert verification
- `test_read_us_intraday_candles_end_date_date_only()` - Date-only cursor (20:00 ET)
- `test_read_us_intraday_candles_end_date_timestamp()` - Precise timestamp cursor
- `test_read_us_intraday_candles_exchange_not_found()` - Sync-hint error propagation

### Integration Tests (Existing Files)
- `test_mcp_ohlcv_tools.py`:
  - Replace Yahoo 1h expectations with KIS intraday
  - Convert US 5m reject test to positive path
  - Verify `source="kis"` for intraday, `source="yahoo"` for daily
  - Verify ET-naive `datetime`, `session` field presence
  - Verify `include_indicators` behavior preserved
  - Verify error payload `source` is period-aware

- `test_market_data_service.py`:
  - Remove US minute period rejection tests
  - Verify service `get_ohlcv()` routes to new reader
  - Verify return type remains plain `Candle` (no `session` field)

---

## DST and Edge Cases

### DST Transitions
- **Spring forward** (2nd Sunday March, 02:00→03:00): No data for 02:00-03:00 ET. Affects `PRE_MARKET` 04:00-09:30 only (no buckets in gap).
- **Fall back** (1st Sunday November, 02:00→01:00): Ambiguous hour 01:00-02:00 ET occurs twice. DST transitions are in `CLOSED` hours, so session boundaries (04:00, 09:30, 16:00, 20:00) remain unambiguous.

### Early Close Days
- Thanksgiving Friday, Christmas Eve: Market closes 13:00 ET
- Regular session buckets: 09:30, 10:30, 11:30, 12:30 (13:00 is close, not a bucket start)
- Post-market: No data (market closed)

### Symbol Not in Universe
- `get_us_exchange_by_symbol()` raises `USSymbolNotRegisteredError`
- Error message includes sync hint: `"Sync required: uv run python scripts/sync_us_symbol_universe.py"`
- MCP returns error payload with `source="kis"`, `error_type="USSymbolNotRegisteredError"`

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| 1h buckets diverge from CAGG | Documented in design; raw-1m re-aggregation is intentional |
| Pre/post data initially sparse | Comprehensive fallback + self-heal builds coverage over time |
| Date-only vs timestamp confusion | Explicit parsing logic with `end_date_is_date_only` flag |
| MCP/Service routing mismatch | Both layers updated in same PR; tests verify both paths |
| Exchange lookup failures | Clear error messages with sync instructions |

---

## Dependencies

- `app/services/us_symbol_universe_service.py` - Exchange lookup
- `app/services/brokers/kis/client.py` - `inquire_overseas_minute_chart()`
- `app/services/us_candles_sync_service.py` - Self-heal upsert pattern (reference)
- `app/services/kr_hourly_candles_read_service.py` - KR pattern (reference)
- `zoneinfo.ZoneInfo("America/New_York")` - ET timezone handling

---

## Success Criteria

1. US `get_ohlcv` with `period="1m/5m/15m/30m/1h"` returns data with `source="kis"`
2. US `get_ohlcv` with `period="day/week/month"` returns data with `source="yahoo"`
3. All intraday timestamps are ET-naive ISO 8601 format
4. `session` field present in MCP responses for intraday periods
5. `session` field NOT present in service-layer `Candle` responses
6. Date-only `end_date` (e.g., `"2024-06-30"`) interpreted as post-market close (20:00 ET)
7. Timestamp `end_date` (e.g., `"2024-06-30T14:30:00"`) interpreted as exact cursor
8. All existing tests pass (regression)
9. New test coverage >90% for reader service

---

## Implementation Sequence

1. **Add reader service** with core query logic
2. **Add unit tests** for reader (failing initially)
3. **Implement fallback + self-heal**
4. **Update validation constants** (US minute periods)
5. **Update service routing**
6. **Update MCP routing** + date-only parsing
7. **Update MCP documentation**
8. **Update integration tests**
9. **Final verification** - all tests pass

---

**Ready for Implementation**
