# Execution Ledger Backfill Runbook

## Review Hold

Do not run commit mode until ROB-478~481 are reviewed under `high_risk_change` and `needs_stronger_model_review`.

## Phase 1: KIS Dry Run

```bash
uv run python -m scripts.reconcile_execution_ledger \
  --broker kis \
  --start-date 2026-02-01 \
  --end-date 2026-06-10 \
  --max-pages 100 \
  --dry-run
```

Archive JSON output with `would_insert`, `would_update`, `unchanged`, and sample rows.

## Phase 2: Upbit Dry Run

```bash
uv run python -m scripts.reconcile_execution_ledger \
  --broker upbit \
  --start-date 2026-02-01 \
  --end-date 2026-06-10 \
  --dry-run
```

Archive JSON output and confirm no truncation error.

## Phase 3: Coverage SQL

Run this SQL before and after dry-run planning against the target DB. Archive the
result sets with the dry-run JSON. The date range matches the commands above:
`2026-02-01T00:00:00Z <= filled_at < 2026-06-11T00:00:00Z`.

```sql
-- 1. Ledger coverage by FIFO match key and source.
SELECT
  broker,
  account_mode,
  venue,
  instrument_type::text AS instrument_type,
  symbol,
  currency,
  source,
  COUNT(*) AS fill_count,
  COUNT(*) FILTER (WHERE side = 'buy') AS buy_fill_count,
  COUNT(*) FILTER (WHERE side = 'sell') AS sell_fill_count,
  SUM(CASE WHEN side = 'buy' THEN filled_qty ELSE -filled_qty END) AS net_qty,
  MIN(filled_at) AS first_fill_at,
  MAX(filled_at) AS last_fill_at
FROM review.execution_ledger
WHERE filled_at >= TIMESTAMPTZ '2026-02-01 00:00:00+00'
  AND filled_at < TIMESTAMPTZ '2026-06-11 00:00:00+00'
GROUP BY
  broker,
  account_mode,
  venue,
  instrument_type::text,
  symbol,
  currency,
  source
ORDER BY broker, venue, symbol, currency, source;

-- 2. Sells that still lack enough earlier buy quantity for realized P/L.
WITH sells AS (
  SELECT
    s.id,
    s.broker,
    s.account_mode,
    s.venue,
    s.instrument_type::text AS instrument_type,
    s.symbol,
    s.currency,
    s.broker_order_id,
    s.fill_seq,
    s.filled_at,
    s.filled_qty AS sell_qty,
    COALESCE((
      SELECT SUM(b.filled_qty)
      FROM review.execution_ledger b
      WHERE b.side = 'buy'
        AND b.broker = s.broker
        AND b.account_mode = s.account_mode
        AND b.venue = s.venue
        AND b.instrument_type = s.instrument_type
        AND b.symbol = s.symbol
        AND b.currency = s.currency
        AND (b.filled_at, b.id) <= (s.filled_at, s.id)
    ), 0) AS buy_qty_to_sell,
    COALESCE((
      SELECT SUM(prev_s.filled_qty)
      FROM review.execution_ledger prev_s
      WHERE prev_s.side = 'sell'
        AND prev_s.broker = s.broker
        AND prev_s.account_mode = s.account_mode
        AND prev_s.venue = s.venue
        AND prev_s.instrument_type = s.instrument_type
        AND prev_s.symbol = s.symbol
        AND prev_s.currency = s.currency
        AND (prev_s.filled_at, prev_s.id) < (s.filled_at, s.id)
    ), 0) AS prior_sell_qty
  FROM review.execution_ledger s
  WHERE s.side = 'sell'
    AND s.filled_at >= TIMESTAMPTZ '2026-02-01 00:00:00+00'
    AND s.filled_at < TIMESTAMPTZ '2026-06-11 00:00:00+00'
)
SELECT
  broker,
  account_mode,
  venue,
  instrument_type,
  symbol,
  currency,
  broker_order_id,
  fill_seq,
  filled_at,
  sell_qty,
  buy_qty_to_sell - prior_sell_qty AS available_buy_qty_before_sell,
  CASE
    WHEN buy_qty_to_sell - prior_sell_qty >= sell_qty THEN 'coverable'
    ELSE 'uncovered'
  END AS coverage_state
FROM sells
WHERE buy_qty_to_sell - prior_sell_qty < sell_qty
ORDER BY filled_at DESC, id DESC;

-- 3. Reconcile run audit trail; error_summary must stay NULL before commit.
SELECT
  broker,
  dry_run,
  window_start,
  window_end,
  started_at,
  finished_at,
  would_insert,
  would_update,
  unchanged,
  committed_insert,
  committed_update,
  error_summary,
  notes
FROM review.execution_ledger_reconcile_runs
WHERE window_start >= TIMESTAMPTZ '2026-02-01 00:00:00+00'
  AND window_end < TIMESTAMPTZ '2026-06-11 00:00:00+00'
ORDER BY started_at DESC
LIMIT 50;

-- 4. Manual opening lots after Phase 6 only.
SELECT
  broker,
  account_mode,
  venue,
  instrument_type::text AS instrument_type,
  symbol,
  currency,
  COUNT(*) AS opening_lot_count,
  SUM(filled_qty) AS opening_lot_qty,
  SUM(filled_notional) AS opening_lot_notional,
  MIN(filled_at) AS first_opening_lot_at,
  MAX(filled_at) AS last_opening_lot_at
FROM review.execution_ledger
WHERE source = 'manual_import'
GROUP BY
  broker,
  account_mode,
  venue,
  instrument_type::text,
  symbol,
  currency
ORDER BY broker, venue, symbol, currency;
```

## Phase 4: KIS/Upbit Commit

Only after reviewer approval:

```bash
EXECUTION_LEDGER_COMMIT_ENABLED=true uv run python -m scripts.reconcile_execution_ledger \
  --broker kis \
  --start-date 2026-02-01 \
  --end-date 2026-06-10 \
  --max-pages 100 \
  --commit

EXECUTION_LEDGER_COMMIT_ENABLED=true uv run python -m scripts.reconcile_execution_ledger \
  --broker upbit \
  --start-date 2026-02-01 \
  --end-date 2026-06-10 \
  --commit
```

## Phase 5: Opening Lot Seed Dry Run

Run only after Phase 4 commits:

```bash
uv run python -m scripts.seed_execution_ledger_opening_lots \
  --cutover 2026-05-10 \
  --dry-run
```

Archive skipped rows. Modified Upbit average prices and ambiguous/non-positive prices must remain skipped.

## Phase 6: Opening Lot Seed Commit

Only after reviewer approval:

```bash
EXECUTION_LEDGER_COMMIT_ENABLED=true uv run python -m scripts.seed_execution_ledger_opening_lots \
  --cutover 2026-05-10 \
  --commit
```

## Filtered Re-Seed (--symbol)

When a fresh cutover proposes inserts for every current holding (ledger net
since the new cutover is zero) but the operator approved re-seeding only
specific symbols, restrict the plan with `--symbol`. The flag is repeatable
and also accepts comma-separated lists; the filter applies identically to
dry-run and commit.

Matching is **exact equality after normalization** — never prefix or
substring. Normalization is uppercase + `app/core/symbol.to_db_symbol`:

- KR 6-digit codes pass through unchanged: `005930`
- US tickers accept any separator spelling: `BRK.B`, `BRK-B`, `BRK/B`,
  `brk.b` all compare as `BRK.B`
- Upbit crypto accepts the currency code (`BTC`) or the raw market key
  (`KRW-BTC`)

Every requested symbol that is not an opening-lot candidate is reported in
the JSON under `symbol_filter.unmatched` and in `skipped` with reason
`no_matching_candidate` — it is never silently ignored. If **none** of the
requested symbols matches a candidate the command prints a stderr message
and exits 1.

Dry-run first — verify `would_seed`, the `sample_seed_rows` symbols, and
that `symbol_filter.unmatched`/`skipped` list every rejected request:

```bash
uv run python -m scripts.seed_execution_ledger_opening_lots \
  --cutover 2026-05-10 \
  --broker kis \
  --symbol 005930 \
  --symbol 196170 \
  --dry-run
```

Equivalent comma-list form:

```bash
uv run python -m scripts.seed_execution_ledger_opening_lots \
  --cutover 2026-05-10 \
  --broker kis \
  --symbol 005930,196170 \
  --dry-run
```

Commit only the identical filtered plan after reviewer approval — the
`EXECUTION_LEDGER_COMMIT_ENABLED` gate is unchanged:

```bash
EXECUTION_LEDGER_COMMIT_ENABLED=true uv run python -m scripts.seed_execution_ledger_opening_lots \
  --cutover 2026-05-10 \
  --broker kis \
  --symbol 005930,196170 \
  --commit
```

Post-commit verification: `committed`/`committed_insert` must equal the
number of approved symbols, and re-run the Phase 3 opening-lot SQL (query 4)
to confirm only the requested symbols gained `manual_import` rows.

## Phase 7: UI Verification

Open `/invest/my?tab=sellHistory` and confirm matched rows show 판매수익/수익률 and currency summary cards render.
