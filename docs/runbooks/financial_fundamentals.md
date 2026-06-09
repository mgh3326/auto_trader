# Runbook: Financial Fundamentals Collection & Toss Parity (ROB-425)

## 1. Purpose

The financial fundamentals snapshots system (`financial_fundamentals_snapshots`) stores durable, point-in-time (PIT) financial statement metrics and dividend details sourced from DART (OpenDART) for KR symbols.

These snapshots back the `/invest/screener` fundamentals-based presets (such as Toss-parity presets). ROB-425 implements the deferred **성장 기대주 (growth_expectation_toss)** preset by enabling **quarterly/interim** statement collection, introducing DART daily-limit request budgeting, and wiring the QoQ net-income growth metric to the screener with fail-closed continuity guards.

---

## 2. Operator Workflow

### Collection Jobs (default: dry-run, no writes)

Operators can run the collection job manually using the CLI. The job defaults to `--dry-run` to log projected request estimates without hitting DART or writing to the database.

```bash
# KR — preview/dry-run first 5 active KR universe symbols (default, annual only)
uv run python -m scripts.build_financial_fundamentals_snapshots --limit 5

# KR — preview/dry-run with quarterly/interim filings enabled
uv run python -m scripts.build_financial_fundamentals_snapshots --limit 5 --with-quarterly

# KR — execute and commit to database (REQUIRES OPERATOR APPROVAL)
uv run python -m scripts.build_financial_fundamentals_snapshots --limit 5 --with-quarterly --commit

# KR — surgical refresh for specific symbols
uv run python -m scripts.build_financial_fundamentals_snapshots --symbol 005930 --symbol 000660 --with-quarterly --commit
```

---

## 3. DART Daily Request Budget & Pacing

To safeguard OpenDART API keys from hitting the daily limit (20,000 requests/day), the fundamentals collector has built-in metered budgeting:

- **Configurable Budget:** Controlled by the `OPENDART_DAILY_REQUEST_BUDGET` environment variable (Settings field: `opendart_daily_request_budget`). Defaults to `18000` (90% headroom). Values `≤ 0` explicitly disable budgeting.
- **Fail-Stop Gate:** Outbound calls (`finstate_all`, `report`, `list`) are dynamically metered per run. If a request is about to push the tally over the configured budget, the fetcher immediately raises a `DartDailyRequestBudgetExceeded` exception.
- **Transactional Safety:** Upon hitting the budget, the job immediately aborts any remaining symbols, **disables all database writes (cancels `--commit`)**, and safely returns the partial payloads gathered before the threshold was reached as a dry-run result.
- **Projected Estimate:** The CLI calculates and displays the projected request count before starting the fetch so the operator can size `--limit` accordingly:
  - **Annual-only:** `~11 requests per symbol`
  - **With Quarterly:** `~41 requests per symbol`

---

## 4. Toss Parity: 성장 기대주 (growth_expectation_toss)

The growth expectation preset (**성장 기대주**) requires:
1. **3년 평균 순이익 증감률 ≥ 3%** (annual historical metrics).
2. **직전분기 대비 순이익 증감률 (QoQ) ≥ 10%** (interim quarterly metrics).

### Continuity Guards (Fail-Closed)

To prevent lookahead/staleness leakage, the `earnings_growth_qoq` metric implements strict fail-closed guards:
- **Adjacency:** The compared quarters must be consecutive fiscal quarters (e.g., `2025Q3` compared to `2025Q2`). A missing quarter/gap resolves the metric to `unavailable`.
- **Freshness:** The latest quarter must be fresh. If `report_date - latest_quarter_end_date > 183 days` (module constant), the metric resolves to `unavailable` (preventing old data from passing as active growth).
- **Missing Data Fallback:** If quarterly database records are missing, the screener fails closed and excludes the symbol from the preset. It never silently passes or fakes the metric.

---

## 5. Parity Matrix

With the addition of `growth_expectation_toss`, Toss parity is **11/11 Full** at the
code level (preset implemented with Toss-equivalent semantics):
- **성장 기대주 (#8):** Fully implemented with PIT quarterly differencing and active screener registry integration.

> **Operational caveat:** `full` here means the preset and its QoQ metric are
> code-complete. The quarterly snapshots it reads are populated **only after an
> operator runs the paced quarterly backfill** (`--with-quarterly --commit`,
> §2–§3). Until then the preset fails closed and the screener surfaces it as
> `missing`/empty in production — by design, never faked.
