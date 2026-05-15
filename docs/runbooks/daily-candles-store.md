# Daily Candle Store Runbook

## What this is

Durable per-day OHLCV store for US (`us_candles_1d`), KR (`kr_candles_1d`),
and crypto (`crypto_candles_1d`) markets, populated by KIS-primary scheduled
sync jobs and read by `_fetch_ohlcv_for_indicators` (and its 12+ downstream
callers: invest_screener_snapshots, sell_signal, n8n indicators, analyzers,
portfolio_holdings, etc.).

Tables are TimescaleDB hypertables with a unique constraint on
`(time, symbol, venue/partition)`. All writes go through
`app/services/daily_candles/repository.py::DailyCandlesRepository.upsert_rows()`.
Reads are via `DailyCandlesRepository.fetch_recent()`, which `_fetch_ohlcv_for_indicators`
calls as a cache-first step before falling back to the external API.

---

## Schedule (Asia/Seoul)

Cron entries are registered in `app/tasks/daily_candles_tasks.py` using
`cron_offset: "Asia/Seoul"`.

| Market  | Cron expression         | Wall-clock time  | Rationale                                              |
|---------|-------------------------|------------------|--------------------------------------------------------|
| KR      | `30 16 * * 1-5`         | 16:30 KST Mon-Fri | 1 h after KOSPI close (15:30 KST)                   |
| US      | `0 7 * * 2-6`           | 07:00 KST Tue-Sat | ~1 h after NYSE close (05:xx KST) on the prior US trading day |
| Crypto  | `0 9 * * *`             | 09:00 KST daily  | Upbit 24/7; daily snapshot at a quiet hour            |

**Note:** The plan document mentions UTC cron times — those are superseded by
the Asia/Seoul-local times shown above, which are what the code actually uses.

---

## Sources

| Market | Primary source             | `source` column value | Fallback                                      |
|--------|----------------------------|-----------------------|-----------------------------------------------|
| KR     | KIS domestic daily         | `kis`                 | None                                          |
| US     | KIS overseas daily         | `kis`                 | Yahoo Finance when KIS returns empty rows (`yahoo_fallback`) |
| Crypto | Upbit day candles          | `upbit`               | None                                          |

For US symbols, the Yahoo fallback runs automatically inside
`app/services/daily_candles/yahoo_us_fallback.py` when the KIS fetch returns
an empty DataFrame. The `source` column on the inserted rows is set to
`yahoo_fallback` so the fallback usage is visible in operational queries.

---

## Wrapper safety clamp vs batch horizon

The wrapper-level safety clamp at
`app/services/brokers/kis/domestic_market_data.py::normalize_daily_chart_lookback`
(currently 200) protects ad-hoc display/MCP calls. Batch horizon is governed
separately by `app/services/daily_candles/constants.py::DAILY_CANDLE_BACKFILL_BARS_*`
(currently 400). These two are independent on purpose; raising one does not
raise the other. The 200-bar value is a wrapper default, **not** a KIS upstream
cap.

| Path                                | Horizon control                             | Current value |
|-------------------------------------|---------------------------------------------|---------------|
| Ad-hoc display / MCP calls          | `normalize_daily_chart_lookback` clamp      | 200 bars      |
| Batch ingest / backfill CLI         | `DAILY_CANDLE_BACKFILL_BARS_{KR,US,CRYPTO}` | 400 bars each |

---

## Initial backfill

After applying the three migrations and before enabling cron jobs, run a
targeted historical backfill per market.

**KR (plain 6-digit code, e.g. `005930`):**
```bash
uv run python scripts/backfill_daily_candles.py --market kr --symbols 005930,000660 --horizon-bars 400
```

**US (DB-canonical dot notation — `BRK.B` not `BRK-B`):**
```bash
uv run python scripts/backfill_daily_candles.py --market us --symbols AAPL,MSFT,NVDA --horizon-bars 500
```

**Crypto (full Upbit market string, e.g. `KRW-BTC`):**
```bash
uv run python scripts/backfill_daily_candles.py --market crypto --symbols KRW-BTC,KRW-ETH --horizon-bars 400
```

Symbol format notes:
- KR: plain numeric code (`005930`), no exchange prefix.
- US: DB-canonical dot notation (`BRK.B`). The CLI/service translates to KIS slash notation (`BRK/B`) or Yahoo hyphen notation (`BRK-B`) internally.
- Crypto: full Upbit market string (`KRW-BTC`), including the quote-currency prefix.

---

## Operational queries (smoke)

### Per-day row count with fallback breakdown (last 7 days)

Run after any scheduled tick or backfill to verify coverage:

```sql
SELECT 'us' AS market, time::date AS day, COUNT(*) AS rows,
       COUNT(*) FILTER (WHERE source='yahoo_fallback') AS fb_rows
FROM public.us_candles_1d
WHERE time >= now() - INTERVAL '7 days'
GROUP BY 1, 2
UNION ALL
SELECT 'kr', time::date, COUNT(*), 0
FROM public.kr_candles_1d
WHERE time >= now() - INTERVAL '7 days'
GROUP BY 2
UNION ALL
SELECT 'crypto', time::date, COUNT(*), 0
FROM public.crypto_candles_1d
WHERE time >= now() - INTERVAL '7 days'
GROUP BY 2
ORDER BY 1, 2 DESC;
```

### Fallback ratio for US (alert if > 5% for any recent day)

```sql
SELECT time::date AS day,
       COUNT(*) AS total,
       COUNT(*) FILTER (WHERE source='yahoo_fallback') AS fb,
       ROUND(100.0 * COUNT(*) FILTER (WHERE source='yahoo_fallback') / COUNT(*), 2) AS fb_pct
FROM public.us_candles_1d
WHERE time >= now() - INTERVAL '14 days'
GROUP BY 1
ORDER BY 1 DESC;
```

Alert threshold: fb_pct > 5% on any single day is unusual and may indicate
a KIS API availability or credential issue for US overseas endpoint.

### Stale symbols (active KR universe symbols with no row in the last 5 days)

```sql
WITH expected AS (
    SELECT DISTINCT symbol, 'KRX' AS partition
    FROM public.kr_symbol_universe
    WHERE is_active = TRUE
)
SELECT e.symbol
FROM expected e
LEFT JOIN public.kr_candles_1d c
  ON c.symbol = e.symbol AND c.venue = e.partition
  AND c.time >= now() - INTERVAL '5 days'
WHERE c.symbol IS NULL
ORDER BY e.symbol
LIMIT 50;
```

A large number of stale symbols on a weekday indicates the KR sync job failed.
Check TaskIQ logs and KIS token health (`app/services/redis_token_manager.py`).

---

## Production deploy smoke

1. Apply all pending migrations on the prod replica first:
   ```bash
   uv run alembic upgrade head
   ```
   Verify TimescaleDB extension is ≥ 2.15:
   ```sql
   SELECT extversion FROM pg_extension WHERE extname='timescaledb';
   ```

2. Run a single-symbol backfill as a sanity check:
   ```bash
   uv run python scripts/backfill_daily_candles.py --market us --symbols AAPL --horizon-bars 50
   ```

3. Verify a row appeared in the table:
   ```sql
   SELECT * FROM us_candles_1d WHERE symbol='AAPL' ORDER BY time DESC LIMIT 1;
   ```

4. Restart the TaskIQ scheduler so the three daily cron entries register:
   ```bash
   # Depending on your process manager:
   supervisorctl restart taskiq-scheduler
   # or: systemctl restart auto-trader-taskiq-scheduler
   ```

5. Wait one scheduled tick per market; re-run the operational query above and
   confirm row counts > 0 for each market.

---

## Rollback

```bash
uv run alembic downgrade <rev-before-us_candles_1d>
```

The revision immediately before the first daily candle migration is `9f1a2b3c4d5e`
(the alembic head prior to this PR). To roll back all three daily candle tables:

```bash
uv run alembic downgrade 9f1a2b3c4d5e
```

Cron jobs are effectively no-ops after downgrade because `_fetch_ohlcv_for_indicators`
falls back to the external-API path when the DB tables are absent — the
`fetch_recent` call returns empty, and the external API call still proceeds
normally. No service restart is required after rollback.

---

## Known limitations

### Rowcount metric always reports 0

`DailyCandlesRepository.upsert_rows()` (and `sync_market_universe`'s
`rows_upserted` total) returns 0 even when rows are successfully inserted.
This is due to asyncpg's `executemany`-style batch execute returning
`rowcount = -1` for batch statements, so the Python-side count is always 0.
The same behaviour affects the existing intraday `us_candles_sync_service`.

Log output from the CLI will show:
```
backfill done symbol=005930 upserted=0 fallback=False
```
The `upserted=0` here reflects the asyncpg rowcount quirk only — rows are
committed. Query the table directly
(`SELECT COUNT(*) FROM public.kr_candles_1d WHERE symbol='005930'`) to verify
coverage.

### adj_close enrichment is not yet run by any scheduled task

The `adj_close` column exists on `us_candles_1d` for storing split/dividend-
adjusted close prices. Populating it from Yahoo Finance (without changing
`source='kis'` for existing rows) is a planned follow-up. Until then the
column will be NULL for all rows.

### `_fetch_ohlcv_for_volume_profile` still uses the legacy path for KR

The `_fetch_ohlcv_for_volume_profile` helper in the MCP tooling still uses
the legacy `kis_ohlcv_cache` path for KR market data. It was not migrated to
the durable store in this PR. Migration is a separate follow-up.
