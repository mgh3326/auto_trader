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

# KR — full active universe, dry-run (RECOMMENDED before any --commit)
uv run python -m scripts.build_invest_screener_snapshots --market kr --all

# KR — full active universe, persist (REQUIRES OPERATOR APPROVAL)
uv run python -m scripts.build_invest_screener_snapshots --market kr --all --commit

# US — full active universe, persist
uv run python -m scripts.build_invest_screener_snapshots --market us --all --commit

# Specific symbols (small surgical refresh)
uv run python -m scripts.build_invest_screener_snapshots \
    --market kr --symbol 005930 --symbol 000660 --commit
```

`--dry-run` (default) prints payloads without writing. `--commit` persists rows
via `INSERT ON CONFLICT DO UPDATE`. `--all` iterates the full active universe in
`--batch-size` chunks (default 200), committing per batch when `--commit` is set.
`--all` is mutually exclusive with `--symbol` and `--limit`.

**Operator approval gating:** never run `--all --commit` against production
without explicit human approval citing dry-run evidence. The recommended
sequence is:

1. `--all` (no commit) → review log of total/built counts and a sample of payloads.
2. Inspect coverage before commit: `curl /invest/api/screener/snapshots/coverage`.
3. Wait for explicit "approved to commit" from a reviewer.
4. `--all --commit` → re-check coverage; expect `dataState="fresh"`.

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

**No recurring scheduler entry is active.** The table is filled on operator
demand. Recurring automation (e.g. nightly TaskIQ/Prefect job) requires:

- A separate Linear ticket
- At least one or two days of operator-run smoke evidence (coverage diagnostic
  output captured before/after `--all --commit`)
- Explicit reviewer approval citing that evidence

Do not introduce a recurring scheduler in the same PR as the snapshot read-path
wiring or the operator-CLI changes — they are intentionally split so the
scheduler activation can be reviewed against a known-stable manual baseline.

---

## 6. Safety Boundary

- **Read/model/UI/data-layer only.** No broker, order, watch, or order-intent mutations.
- The CLI defaults to `--dry-run`. Accidental invocation without `--commit` is a no-op.
- Migration is table-create only — no `ALTER` of existing tables.
- The repository's `upsert` is the only write path; direct `INSERT/UPDATE/DELETE` is forbidden.
