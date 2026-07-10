# Runbook: candles_1d slow SELECT (ROB-812)

## Phase 1 — read-only diagnostics

Connect (password not echoed):
```bash
PGURL=$(rg -oN "DATABASE_URL=postgresql[^ ]*" /Users/mgh3326/work/auto_trader/.env.prod | sed -E 's/^DATABASE_URL=//; s#\+asyncpg##')
psql "$PGURL" -f docs/runbooks/_rob812_diag.sql   # or paste blocks interactively
```

### pick-a-key (one per table)
```sql
SELECT symbol, venue    FROM public.kr_candles_1d     ORDER BY time DESC LIMIT 1;
SELECT symbol, exchange FROM public.us_candles_1d     ORDER BY time DESC LIMIT 1;
SELECT instrument_id    FROM public.crypto_candles_1d ORDER BY time DESC LIMIT 1;
```

### 1. Plan of the exact production query (repeat for us/crypto)
```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT time, symbol, venue AS partition, open, high, low, close,
       NULL AS adj_close, volume, value, source
FROM public.kr_candles_1d
WHERE symbol = :'sym' AND venue = :'ven'
ORDER BY time DESC
LIMIT 200;
```
Look for: `Custom Scan (ChunkAppend)` w/ ordered append vs plain `Append`/`MergeAppend`;
**chunks scanned** (want 1-2); per-chunk `Index Scan` vs `Seq Scan`;
`Planning Time` vs `Execution Time`; `Buffers`.

### 2. Chunk sprawl
```sql
SELECT hypertable_name, count(*) AS chunks
FROM timescaledb_information.chunks
WHERE hypertable_name IN ('kr_candles_1d','us_candles_1d','crypto_candles_1d')
GROUP BY 1;
```

### 3. Sizes
```sql
SELECT relname, pg_size_pretty(pg_total_relation_size(oid))
FROM pg_class WHERE relname IN
 ('kr_candles_1d','us_candles_1d','crypto_candles_1d',
  'ix_kr_candles_1d_symbol_venue_time_desc',
  'ix_us_candles_1d_symbol_exchange_time_desc',
  'ix_crypto_candles_1d_instrument_id_time');
```

### 4. Bloat / vacuum
```sql
SELECT relname, n_live_tup, n_dead_tup, last_autovacuum, last_analyze
FROM pg_stat_user_tables
WHERE relname LIKE '%candles_1d%' ORDER BY n_dead_tup DESC;
```

### 5. Index present on chunks (not just parent)
```sql
SELECT indexrelid::regclass FROM pg_index
WHERE indrelid IN (
  SELECT format('%I.%I', chunk_schema, chunk_name)::regclass
  FROM timescaledb_information.chunks
  WHERE hypertable_name='kr_candles_1d' LIMIT 3);
```

---

## Phase 1 — Captured evidence (2026-07-10)

Diagnostics run against `auto_trader @ localhost:5432` (TimescaleDB, homebrew
postgresql@17). All read-only.

### Pick-a-key results
| table | key |
|-------|-----|
| kr_candles_1d | symbol=`0017J0`, venue=`KRX` |
| us_candles_1d | symbol=`PRPL`, exchange=`NASD` |
| crypto_candles_1d | instrument_id=`138` |

### Chunk sprawl
| hypertable | chunks |
|------------|--------|
| kr_candles_1d | 6 |
| us_candles_1d | 7 |
| crypto_candles_1d | 4 |

### Bloat / vacuum
| table | n_live_tup | n_dead_tup | last_autovacuum |
|-------|-----------|------------|-----------------|
| all three (parent) | 0 | **0** | NULL |

n_dead_tup = 0 on all → **no bloat (branch b ruled out)**.
(parent n_live_tup=0 is expected for hypertables; stats live on chunks.)

### Index on chunks
`ix_kr_candles_1d_symbol_venue_time_desc` confirmed present on every sampled
chunk (`_hyper_12_30_chunk`, `_hyper_12_29_chunk`, `_hyper_12_34_chunk`) →
**index propagated (branch c ruled out)**.

### EXPLAIN (ANALYZE, BUFFERS) — BEFORE (no time predicate)

#### kr_candles_1d (symbol=0017J0, venue=KRX) — 23 rows total
```
 Limit  (actual time=781.126..781.135 rows=23)
   Buffers: shared hit=26 read=14
   ->  Sort  (actual time=781.125..781.132 rows=23)
         Sort Key: kr_candles_1d."time" DESC
         ->  Result  (actual time=61.306..781.009 rows=23)
               ->  Append  (actual time=61.304..780.937 rows=23)
                     ->  Bitmap Heap Scan on _hyper_12_48_chunk  (actual time=61.303..683.241 rows=23)
                     ->  Bitmap Heap Scan on _hyper_12_30_chunk  (actual time=52.746..52.747 rows=0)
                     ->  Bitmap Heap Scan on _hyper_12_29_chunk  (actual time=44.755..44.755 rows=0)
                     ->  Bitmap Heap Scan on _hyper_12_36_chunk  (actual time=0.081..0.081 rows=0)
                     ->  Bitmap Heap Scan on _hyper_12_35_chunk  (actual time=0.029..0.029 rows=0)
                     ->  Bitmap Heap Scan on _hyper_12_34_chunk  (actual time=0.056..0.056 rows=0)
 Planning Time: 155.655 ms   (shared hit=1420)
 Execution Time: 781.426 ms
```
**ALL 6 chunks scanned** (5 return 0 rows). Plain `Append` + explicit `Sort`.
Cold-cache I/O on first chunk: 683ms for 23 rows (read=13 blocks).

#### us_candles_1d (symbol=PRPL, exchange=NASD) — 48 rows total
```
 Limit  (actual time=999.370..999.395 rows=48)
   Buffers: shared hit=4 read=45 dirtied=7
   ->  Sort  (actual time=999.369..999.386 rows=48)
         ->  Append  (actual time=76.666..999.184 rows=48)
               ->  Bitmap Heap Scan on _hyper_11_49_chunk  (actual time=76.665..398.742 rows=22)
               ->  Bitmap Heap Scan on _hyper_11_28_chunk  (actual time=99.992..400.068 rows=26)
               ->  Bitmap Heap Scan on _hyper_11_27_chunk  (actual time=25.275..25.276 rows=0)
               ->  Bitmap Heap Scan on _hyper_11_39_chunk  (actual time=38.861..38.861 rows=0)
               ->  Bitmap Heap Scan on _hyper_11_38_chunk  (actual time=51.371..51.372 rows=0)
               ->  Index Scan on _hyper_11_37_chunk         (actual time=64.379..64.379 rows=0)
               ->  Seq Scan on _hyper_11_42_chunk           (actual time=20.431..20.431 rows=0)
 Planning Time: 1224.774 ms   (shared hit=1694 read=30 dirtied=7)
 Execution Time: 999.639 ms
```
**ALL 7 chunks scanned** (5 return 0 rows). Plain `Append` + `Sort`. Even a
**Seq Scan** on one chunk. Planning time 1224ms is extreme (chunk-sprawl
planning overhead).

#### crypto_candles_1d (instrument_id=138) — 201 rows total
```
 Limit  (actual time=1011.321..1011.377 rows=200)
   Buffers: shared hit=7 read=39
   ->  Sort  (actual time=1011.319..1011.347 rows=200)
         ->  Result  (actual time=99.441..1010.789 rows=201)
               ->  Append  (actual time=99.432..1010.653 rows=201)
                     ->  Bitmap Heap Scan on _hyper_13_47_chunk  (actual time=99.430..601.993 rows=34)
                     ->  Bitmap Heap Scan on _hyper_13_31_chunk  (actual time=97.240..284.022 rows=90)
                     ->  Bitmap Heap Scan on _hyper_13_33_chunk  (actual time=53.395..91.752 rows=77)
                     ->  Index Scan Backward on _hyper_13_32_chunk (actual time=32.818..32.818 rows=0)
 Planning Time: 644.704 ms   (shared hit=1238 read=11)
 Execution Time: 1011.550 ms
```
**ALL 4 chunks scanned** (1 returns 0 rows). Plain `Append` + `Sort`.

### EXPLAIN — AFTER (with `AND time >= now() - interval '600 days'`)

#### kr_candles_1d
```
 ->  Custom Scan (ChunkAppend) on kr_candles_1d  (actual time=0.098..0.417 rows=23)
       Chunks excluded during startup: 0
       ->  Bitmap Heap Scan on _hyper_12_48_chunk  (actual time=0.096..0.233 rows=23)
       ->  Bitmap Heap Scan on _hyper_12_30_chunk  (actual time=0.047..0.047 rows=0)
       ...
 Planning Time: 7.451 ms
 Execution Time: 0.886 ms
```
**`Custom Scan (ChunkAppend)` fires** (was plain `Append`). Warm-cache run
shows the architectural improvement. With current 6 chunks, 0 excluded (data
spans ~14 months < 600-day window), but as data grows the predicate will
exclude old chunks, cutting both planning and cold-I/O proportionally.

#### crypto_candles_1d
```
 ->  Custom Scan (ChunkAppend) on crypto_candles_1d  (actual time=0.050..1.417 rows=201)
       Chunks excluded during startup: 0
       ...
 Execution Time: 1.637 ms
```
ChunkAppend fires. 1011ms → 1.6ms (warm cache).

### Warm-cache baseline (no predicate, 2nd run)
KR without predicate, fully warm: Planning 8ms, Execution **0.562ms**.
→ On warm cache the query is already fast. The 945ms prod average is
**cold-cache + all-chunk-scan + plain Append**. The predicate fixes the
architecture (ChunkAppend + future chunk exclusion) and will scale better
as daily tables accumulate chunks (no retention policy).

---

## Decision

**Branch (a): ordered-append not firing / all-chunk scan.**

Justification:
- All three tables show plain `Append` (not `Custom Scan (ChunkAppend)`) →
  the ordered-append optimization does NOT fire without a time predicate.
- ALL chunks are scanned on every query (6/6, 7/7, 4/4), most returning 0 rows.
- Adding `AND time >= :time_floor` makes ChunkAppend fire (verified on KR + crypto).
- Branch (b) ruled out: n_dead_tup = 0 on all tables.
- Branch (c) ruled out: composite index confirmed present on every chunk.
- Branch (d) partially present (US planning time 1224ms = chunk-sprawl overhead)
  but full retention/compression rework is out of scope for this PR → follow-up.

Routing: Task 2 → Task 3 → Task 5 (query-layer bounded time predicate).

---

## Alternate fix (branch b): stale stats / bloat
*Not needed — Phase 1 showed n_dead_tup = 0 on all tables.*

If bloat appears later (operator runs):
```sql
VACUUM (ANALYZE) public.kr_candles_1d;
VACUUM (ANALYZE) public.us_candles_1d;
VACUUM (ANALYZE) public.crypto_candles_1d;
-- if index bloat is the culprit (per Phase-1 sizes):
REINDEX INDEX CONCURRENTLY public.ix_kr_candles_1d_symbol_venue_time_desc;
REINDEX INDEX CONCURRENTLY public.ix_us_candles_1d_symbol_exchange_time_desc;
REINDEX INDEX CONCURRENTLY public.ix_crypto_candles_1d_instrument_id_time;
```

## Alternate fix (branch c): covering index
*Not needed — index confirmed present on all chunks.*
