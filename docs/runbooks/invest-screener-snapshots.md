# Runbook: `invest_screener_snapshots` (ROB-170)

## 1. Purpose

`invest_screener_snapshots` is a per-symbol, per-trading-day table that precomputes price-derived metrics (`consecutive_up_days`, `week_change_rate`, `latest_close`, `change_amount`, `change_rate`) for the `/invest/screener` `consecutive_gainers` preset.

**Read path:** `/invest/api/screener/results?presetId=consecutive_gainers` calls `_enrich_consecutive_up_days` which reads from `invest_screener_snapshots` first (snapshot-first). If a snapshot is fresh, OHLCV fetch is skipped. If missing/stale, the existing on-demand OHLCV path is used transparently (ROB-168 fallback).

**Write path:** Operator-driven CLI only (no recurring scheduler — see §5).

---

## 2. Operator Workflow

### Build snapshots (default: dry-run, no writes)

```bash
# KR — preview top 20 active universe symbols
uv run python -m scripts.build_invest_screener_snapshots --market kr --limit 20

# KR — persist (write to DB)
uv run python -m scripts.build_invest_screener_snapshots --market kr --limit 20 --commit

# US — persist
uv run python -m scripts.build_invest_screener_snapshots --market us --limit 20 --commit

# Specific symbols
uv run python -m scripts.build_invest_screener_snapshots \
    --market kr --symbol 005930 --symbol 000660 --commit
```

`--dry-run` (default) prints payloads and exits with no DB writes. `--commit` actually persists rows via `INSERT ON CONFLICT DO UPDATE`.

---

## 3. Coverage Check

### Via CLI (read-only, no writes)

```bash
uv run python -m scripts.diagnose_invest_screener_snapshots --market kr
uv run python -m scripts.diagnose_invest_screener_snapshots --market us
```

Output includes: universe size, coveringToday, stale, missing, lastComputedAt, dataState.

### Via HTTP endpoint

```bash
curl "http://localhost:8000/invest/api/screener/snapshots/coverage?market=kr"
curl "http://localhost:8000/invest/api/screener/snapshots/coverage?market=us"
```

Returns 200 even when the table is empty (`snapshotsCoveringToday=0`, `dataState="missing"`).

---

## 4. Fallback Semantics

`ScreenerFreshness.dataState` on the screener response indicates snapshot read quality:

| `dataState` | Meaning |
|-------------|---------|
| `fresh`     | All rows served from snapshots dated today with `≥5` closes |
| `partial`   | Rows served but `closes_window` has 2–4 entries (week_change_rate not computable) |
| `stale`     | Snapshot exists but is older than today's trading date or computed >36h ago |
| `missing`   | No snapshot rows found; all data came from on-demand OHLCV fallback |
| `fallback`  | Mixed: some rows used snapshots, ≥1 row used on-demand fallback |

If the table is empty, the screener response is byte-equivalent to the ROB-168 baseline — only `freshness.dataState` differs (it becomes `"missing"`).

---

## 5. Scheduler — Deferred

**No recurring scheduler entry is active in this PR.** The table is filled on-demand by the operator CLI.

Recurring automation (e.g. nightly TaskIQ/Prefect job) requires:
- A separate approval ticket
- At least one or two days of operator-run smoke evidence

---

## 6. Safety Boundary

- **Read/model/UI/data-layer only.** No broker, order, watch, or order-intent mutations.
- The CLI defaults to `--dry-run`. Accidental invocation without `--commit` is a no-op.
- Migration is table-create only — no `ALTER` of existing tables.
- The repository's `upsert` is the only write path; direct `INSERT/UPDATE/DELETE` is forbidden.
