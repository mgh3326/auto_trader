# ROB-812 — `*_candles_1d` daily SELECT slow query investigation & fix

- **Issue**: ROB-812 `[perf] kr_candles_1d SELECT가 쿼리당 avg 945ms — 인덱스/플랜 조사`
- **Date**: 2026-07-10
- **Scope (approved)**: kr + us + crypto `*_candles_1d` — all three share the same
  `ORDER BY time DESC LIMIT n` daily-read pattern.
- **Branch**: `rob-812`

## Problem statement

Sentry 24h prod telemetry (2026-07-10) shows the daily-candle read inside
`analyze_stock_batch` costs **avg 945 ms/query, 80 queries/day, sum ~76 s/day**:

```sql
SELECT time, symbol, venue AS partition, open, high, low, close,
       NULL AS adj_close, volume, value, source
FROM public.kr_candles_1d
WHERE symbol = $1 AND venue = $2
ORDER BY time DESC
LIMIT $3
```

An equality filter on `(symbol, venue)` plus `ORDER BY time DESC LIMIT n` should be
single-digit ms if a matching composite index is used.

## Key finding that reframes the issue

**The composite index the issue assumes is missing already exists on all three
tables.** So this is *not* a missing-index problem.

| table | daily read filter | existing index | daily retention |
|---|---|---|---|
| `kr_candles_1d` | `symbol=? AND venue=?` ORDER BY time DESC | `ix_kr_candles_1d_symbol_venue_time_desc (symbol, venue, time DESC)` | **none** → chunks grow forever |
| `us_candles_1d` | `symbol=? AND exchange=?` ORDER BY time DESC | `ix_us_candles_1d_symbol_exchange_time_desc (symbol, exchange, time DESC)` | none (daily) |
| `crypto_candles_1d` | `instrument_id=?` ORDER BY time DESC | PK `(instrument_id, time)` + `ix_crypto_candles_1d_instrument_id_time (instrument_id, time DESC)` | — |

All three are **TimescaleDB hypertables** (`kr`/`us` 90-day chunks; retention
policies were added only to *intraday* tables, not daily). Sources:
- `alembic/versions/bad6e17e4115_add_kr_candles_1d.py` (index lines 90-93)
- `alembic/versions/142b01f2eba0_add_us_candles_1d.py` (index line 91)
- `alembic/versions/f974ac12e573_add_crypto_candles_1d.py` +
  `6acbc5e7fc93_migrate_crypto_candles_1d_step1_add_.py` +
  `5fa5a347d85b_migrate_crypto_candles_1d_step3_finalize.py`
- Query source: `app/services/daily_candles/repository.py` (kr/us at 449-457, crypto at 403-414)

Because the index exists, the real suspects are Timescale-side:

- **(a)** Ordered-append optimization not firing → planner opens an index scan on
  **every** chunk and MergeAppends them. `ORDER BY time DESC LIMIT n` with **no time
  predicate** across an ever-growing chunk set is the classic trigger. *Most likely.*
- **(b)** Stale planner stats and/or table+index bloat (no autovacuum tuning).
- **(c)** Index genuinely unused / not propagated to chunks. *Unlikely.*
- **(d)** Planning-time overhead from chunk sprawl (no retention on daily tables).

The actual cause can only be settled by `EXPLAIN (ANALYZE, BUFFERS)` on prod data —
the dev DB (`auto_trader_dev`) will not reproduce the prod data volume.

## Approach: diagnosis-first decision tree

Do **not** pre-commit to a fix. Run read-only diagnostics on prod, then branch to the
fix the plan output justifies.

### Phase 1 — Read-only diagnostics on prod (authorized run)

Read-only only. The auto-mode classifier gates direct `psql` against `.env.prod`;
these are executed by the operator / with explicit authorization. Target DB:
`auto_trader` @ localhost:5432. Run for **each** of the 3 tables (substitute a real
key that exists):

1. `EXPLAIN (ANALYZE, BUFFERS)` on the exact production query. Inspect:
   - node type: `Custom Scan (ChunkAppend)` with ordered-append vs plain
     `Append`/`MergeAppend`;
   - **number of chunks scanned** (want: 1–2, not all);
   - per-chunk `Index Scan` vs `Seq Scan`;
   - `Planning Time` vs `Execution Time` split;
   - `Buffers` (shared hit/read).
2. Chunk count per hypertable:
   `SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = '<t>';`
3. Sizes: `pg_relation_size` / `pg_total_relation_size` for table + each index.
4. Bloat/vacuum: `pg_stat_user_tables` (`n_dead_tup`, `last_autovacuum`,
   `last_analyze`) for the table and its chunks.
5. Confirm indexes exist on the underlying **chunks**, not just the parent hypertable.

The exact SQL bundle lives in the implementation plan; capture raw output as the
empirical before/after evidence.

### Phase 2 — Root-cause → fix mapping (branch on Phase 1)

- **(a) Ordered-append not firing / all-chunk scan** → add a **bounded time
  predicate** at the query layer: `AND time >= now() - interval 'N days'`, N sized so
  the window always covers the largest `LIMIT` (daily reads cap ~200 rows → ~1 trading
  year; pick a safe margin, e.g. 400–550 days, and derive N from the max requested
  count rather than hard-coding blindly). Lets Timescale exclude old chunks.
  Change in `app/services/daily_candles/repository.py`; **no migration**. Applies to
  kr/us/crypto query builders.
- **(b) Stale stats / bloat** → operator `VACUUM (ANALYZE)` + optional
  `REINDEX INDEX CONCURRENTLY`; consider per-table autovacuum tuning. Runbook, no
  schema change.
- **(c) Index unused / wrong** → covering index `CREATE INDEX CONCURRENTLY`; one
  alembic additive migration.
- **(d) Chunk sprawl / planning overhead** → larger `chunk_time_interval` for daily
  tables and/or retention/compression policy. **Bigger change → flagged as a
  follow-up issue, not forced into this PR.**

### Phase 3 — Implement + verify

- Apply the branch-selected fix.
- If query-layer bounded-time predicate (most likely): add a regression test
  asserting the emitted SQL carries the predicate **and** that `read_service` returns
  row-equivalent results vs the current query. Cover kr/us/crypto builders.
- Any schema change = **one alembic additive migration**, index built `CONCURRENTLY`;
  operator runs `alembic upgrade head` separately (repo cutover convention).
- Re-run Phase 1 `EXPLAIN` to confirm **945 ms → single-digit ms** and that chunks
  scanned drops.

## Safety boundaries

- Read-only investigation first; live prod `EXPLAIN` is read-only and authorization-gated.
- No broker / order / watch / order-intent mutation on any path.
- Index additions via `CONCURRENTLY` (minimal locking); migration additive-only,
  operator-applied.
- Branch (d) (chunk-interval/retention rework) is out of scope for this PR → follow-up.
- The crypto `_resolve_instrument_id` extra round-trip in `repository.py` is noted but
  out of scope.

## Testing

- Unit/regression: repository query-shape test (bounded-time predicate present for
  kr/us/crypto) + row-equivalence via `read_service`.
- Empirical: Phase-1 `EXPLAIN (ANALYZE, BUFFERS)` before/after captured on prod.

## Expected impact

945 ms → ~single-digit ms/query. Saves ~76 s/day in `analyze_stock_batch` and
co-improves every other daily-candle reader (`get_ohlcv` day, forecast read windows,
etc.).
