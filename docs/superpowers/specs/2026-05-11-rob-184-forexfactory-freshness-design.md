# ROB-184 — ForexFactory economic-calendar freshness: design

> Spec authored by planner (Claude Opus). Read this before the implementation
> plan in `docs/superpowers/plans/2026-05-11-rob-184-forexfactory-freshness-plan.md`.
>
> **Branch:** `feature/ROB-184-forexfactory-freshness`
> **Linear:** ROB-184
> **Safety class:** read-mostly market-data ingestion. No broker / order / watch /
> live or paper trading side effects. No production DB write / backfill /
> scheduler activation without 광현님's explicit per-scope approval.

## 1. Problem

Production currently shows ForexFactory economic events on **only 2026-05-07**.
Every other day in the `/invest` calendar surface reads as `missing` (no
partition row) or `empty` (partition succeeded with `event_count=0`). The
Discover "오늘의 이벤트" 경제지표 tab is therefore blank for days other than
2026-05-07.

Goal: deliver durable, fresh ForexFactory coverage of the rolling window served
by the upstream feed, without smashing `nfs.faireconomy.media` with N×per-day
fetches of identical XML, and with explicit operator-visible behavior for dates
the upstream cannot serve.

## 2. Symptom → root-cause analysis

| Observation | Root cause |
| --- | --- |
| Only 2026-05-07 has events in `market_events` for `(source=forexfactory, category=economic, market=global)`. | No scheduled ingestion. Per `docs/runbooks/market-events-ingestion.md`, ROB-128/132 shipped with **no Prefect deployment** — only the manual CLI. Someone ran the CLI on 2026-05-07; nothing has populated subsequent days. |
| Per-day partitions for dates older than the current rolling week succeed with `event_count=0`. | `app/services/market_events/forexfactory_helpers.py::_fetch_xml_documents` only ever requests `ff_calendar_thisweek.xml` (and `ff_calendar_nextweek.xml` when `target_date - today >= 5`). The upstream `thisweek.xml` is **rolling**: backfilling 2026-05-04 on 2026-05-11 returns zero rows for that date. The partition is marked succeeded → freshness service reports `empty`, not `failed`, so the operator can't distinguish "FF has no events" from "FF can't serve that date." |
| Every per-day backfill (e.g. `--month 2026-05`) issues ~30 HTTP fetches of the same payload. | `_fetch_xml_documents` is called from `fetch_forexfactory_events_for_date(target_date)` once per partition iteration. There is no per-CLI-run or per-week memoization. This is wasteful and creates a real 429 risk on `nfs.faireconomy.media`. |
| A single transient `429` or 5xx fails the partition for that day. | `_fetch_xml_documents` uses a bare `httpx.AsyncClient.get(url)` with `raise_for_status()`. No retry, no `Retry-After` honoring, no jittered backoff. The partition gets `status="failed"` with the raw HTTPStatusError message; operator has to re-run by hand. |
| `_fetch_xml_documents` "next-week" branch only triggers for **future** dates `(target_date - today).days >= 5`. | This is correct — but it tells you the implementation is fundamentally targeted at "ingest today/upcoming," not at any historical backfill. Past-week dates are an out-of-scope concept for the upstream. |
| Frontend `/invest` calendar reads via `/trading/api/market-events/today` and `/range`. | No SPA request-path FF fetch today (good). The legacy `app/services/external/economic_calendar.py` cache exists for the n8n / Telegram path and is unaffected by this work. |

### 2.1 What "fresh" actually means for ForexFactory

ForexFactory publishes exactly two XML payloads:

* `ff_calendar_thisweek.xml` — current ISO week, **Sunday-anchored ET**.
* `ff_calendar_nextweek.xml` — next ISO week, **Sunday-anchored ET**.

The upstream window from any given moment is therefore approximately:

```
[start_of_this_week_et, end_of_next_week_et]
```

Dates outside that window cannot be served by FF at all. Any "backfill"
deeper than that is impossible from this provider and should be surfaced as
such rather than recorded as `succeeded(event_count=0)`.

## 3. Non-goals

* No alternative upstream provider (Investing.com, TradingEconomics, etc.). This
  spec keeps ForexFactory as the single source for `(forexfactory, economic,
  global)` partitions.
* No frontend request-path FF fetch. The SPA must continue to read only via
  `/trading/api/market-events/*`.
* No expansion of the `MarketEvent` schema (no new column, no new table).
* No production schedule activation. The plan describes the Prefect deployment
  contract but ships gated behind explicit 광현님 approval.
* No broker / order / watch / live or paper trading code touched.
* Historical (pre-rolling-window) backfill from FF is **not** in scope — by
  upstream limitation.

## 4. Proposed approach

Three coordinated changes, each gated by tests:

### 4.1 Weekly-aware fetch with per-run cache

Replace the per-call `_fetch_xml_documents` with a `ForexFactoryWeeklyCache`
that:

* Computes the upstream rolling window once per run: `(this_week_start_et,
  next_week_end_et)`.
* Lazily fetches `thisweek.xml` and `nextweek.xml` **at most once each per
  process lifetime** (in-memory `dict[Literal["this","next"], list[dict]]`).
* Is keyed by the cache's "as-of UTC date" so a long-running process spanning a
  Sunday→Monday rollover invalidates and refetches.
* Exposes a single `get_events_for_date(target_date: date) -> list[dict] | None`
  return contract: rows matching `target_date`, or `None` if the date is
  outside the rolling window (the caller decides what status to record).

**Cache layer:** in-memory only. No Redis, no disk artifacts. Rationale:
ingestion runs are short-lived (CLI + future Prefect task), all per-day
partitions for a given run live in the same process, and a Redis dependency on
the ingest path introduces failure modes (Redis outage breaking ingestion) that
the simpler in-memory approach avoids. A `forexfactory_weekly_cache` test seam
allows tests to assert exact fetch counts.

### 4.2 Out-of-rolling-window partition handling

When `get_events_for_date(target_date)` returns `None` (target outside
`[this_week_start_et, next_week_end_et]`), `ingest_economic_events_for_date`
short-circuits and records the partition with:

* `status="failed"` (re-use existing schema; no new status column).
* `last_error="forexfactory_out_of_rolling_window"` — sentinel reason string
  matched by tests and by a follow-up freshness mapping.

Rationale for `failed` (vs. `succeeded(0)`):

* Operator running `--from-date 2026-04-01 --to-date 2026-05-11` immediately
  sees which partitions FF cannot serve, instead of a sea of "succeeded with 0
  events" that mask the upstream gap.
* `freshness_service` already maps `failed` → calendar `error` state. A
  follow-up (out of scope for this PR) can split `failed (out-of-window)` into
  a softer label such as "데이터 제공 범위 밖"; the sentinel string is the
  contract that enables it.
* No schema migration required — keeps this PR small.

### 4.3 429 + transient-error protection

Wrap each XML fetch in a retry policy local to `forexfactory_helpers`:

* `httpx.AsyncClient(timeout=10.0)` with explicit per-call attempts, not
  `httpx-retry-transport` (the project does not pull that in for one site).
* Retry on: connection errors, `httpx.TimeoutException`, status `429`, `500`,
  `502`, `503`, `504`.
* Backoff: `min(retry_after_header, base * 2**attempt)` with ±25% jitter,
  capped at 30s. `attempt` starts at 0.
* Max **3 attempts** per URL per partition iteration (so worst case = 3 ×
  thisweek + 3 × nextweek per CLI run, not per day).
* On final failure: raise a typed `ForexFactoryFetchError`. Caller marks
  partition `failed` with sentinel `forexfactory_rate_limited` /
  `forexfactory_upstream_5xx` / `forexfactory_network_error`.

The cache layer's "fetch at most once" guarantee means a transient 429 backs
off **once per CLI run**, not 30 times.

### 4.4 Scheduler contract (documentation only — no activation)

Document — in `docs/runbooks/market-events-ingestion.md` and as a Prefect
deployment stub `app/flows/forexfactory_calendar_flow.py` (deployment
unscheduled, manually triggerable only) — the operator contract:

* Cadence: every 4 hours during US/EU business hours (08:00–22:00 KST), once
  per night.
* Window per run: `today` through `today + 14d` (the upstream's hard ceiling).
  Anything older is by construction excluded.
* Concurrency: 1. The in-memory cache lives inside the run; cross-run sharing
  is unnecessary and would be a Redis decision deferred to a future task.
* Activation: explicit 광현님 approval via Linear/Discord trail before any
  Prefect schedule is enabled or any production backfill executes.

A `--dry-run` smoke flow is the first step of any production activation,
following the ROB-178 pattern.

## 5. Component map

```
app/services/market_events/
├── forexfactory_helpers.py
│   ├── ForexFactoryWeeklyCache (NEW)
│   │     .get_events_for_date(target_date) -> list[dict] | None
│   │     ._fetch_this_week() / ._fetch_next_week()  (httpx with retry+jitter)
│   ├── ForexFactoryFetchError (NEW typed exception)
│   ├── rolling_window_for_today(now_utc) -> (date, date)  (NEW pure helper)
│   ├── _parse_one_xml(xml_text) -> list[dict]            (UNCHANGED)
│   └── fetch_forexfactory_events_for_date(target_date, *, cache=None)
│         (UPDATED to delegate to the cache; back-compat default cache per
│         module-level singleton so existing callers/tests continue to work)
└── ingestion.py
    └── ingest_economic_events_for_date(db, target_date, fetch_rows=None)
          (UPDATED to recognize None → "out of rolling window" path and the
          new ForexFactoryFetchError → reason-tagged failed partition)

app/flows/                              (NEW directory, only if not already
└── forexfactory_calendar_flow.py        present)
      Prefect-flow stub — unscheduled,
      manually triggerable only.
```

No frontend file changes. No DB schema changes. No new env vars.

## 6. Data flow

1. Operator (or future Prefect task) calls
   `scripts.ingest_market_events --source forexfactory --category economic
   --market global --from-date X --to-date Y [--dry-run]`.
2. `run_ingest` iterates per-day partitions as today, calling
   `ingest_economic_events_for_date(db, d)`.
3. Inside the orchestrator, a **single** `ForexFactoryWeeklyCache` instance
   (constructed once for the loop and threaded through, or via a module-level
   singleton invalidated by date) is reused across all `d` values.
4. For each `d`:
   * If `d` is inside the rolling window → cache returns rows; normalize +
     upsert; partition `succeeded(event_count=N)`.
   * If `d` is outside → cache returns `None`; partition `failed` with reason
     `forexfactory_out_of_rolling_window`.
   * If the underlying HTTP fetch raises `ForexFactoryFetchError` → partition
     `failed` with reason `forexfactory_rate_limited` /
     `forexfactory_upstream_5xx` / `forexfactory_network_error` derived from
     the exception class.
5. `db.commit()` happens once per partition (already the case).

The frontend reads the result via `/trading/api/market-events/today` and
`/range`. **No FF HTTP from the request path** — verified by an existing
codebase grep guard (Section 8).

## 7. Failure-mode taxonomy after this change

| Calendar UI state | Trigger after this work |
| --- | --- |
| `loaded` | Partition `succeeded`, `event_count > 0`. |
| `empty` | Partition `succeeded`, `event_count = 0` — FF served the rolling window but had no events for that date (e.g. some weekend KST days). |
| `partial` | Mixed mode across other sources (Finnhub/DART); ForexFactory side reports `succeeded` or one of the failed reasons below. |
| `missing` | No partition row yet (ingest never ran for that day). |
| `error` | Partition `failed`. New sentinel reasons make this drilldown actionable: `out_of_rolling_window`, `rate_limited`, `upstream_5xx`, `network_error`, or the legacy generic error. A follow-up task may map `out_of_rolling_window` to a softer "데이터 제공 범위 밖" UI label. |
| `stale` | Unchanged (`STALE_AFTER_HOURS = 36`). |

## 8. Test surface

All tests stay offline (no live `nfs.faireconomy.media` calls).

### 8.1 New / updated unit tests (pytest, async)

* `tests/services/test_market_events_forexfactory_helpers.py` (UPDATE)
  * `test_weekly_cache_fetches_each_url_at_most_once_for_loop_of_dates` —
    assert `_fetch_xml_documents` mock is called exactly twice (this + next)
    when iterating a range that spans both weeks; once when only thisweek is
    needed.
  * `test_get_events_for_date_returns_none_outside_rolling_window` — feed a
    date 30 days in the past and assert `None`.
  * `test_get_events_for_date_handles_sunday_monday_rollover` — drive the
    cache with two different "now"s across a week boundary; assert refetch.
  * `test_retry_respects_retry_after_header` — `httpx_mock` returns 429 with
    `Retry-After: 1` then 200; assert exactly two attempts and the parsed
    rows match.
  * `test_retry_exhausts_then_raises_forexfactory_fetch_error` — 3× 429;
    assert `ForexFactoryFetchError` with `reason="rate_limited"`.
  * `test_retry_on_5xx_then_success` — 503, 200; assert success after one
    retry.
  * `test_no_retry_on_4xx_other_than_429` — 403 raises immediately.
* `tests/services/test_market_events_ingestion.py` (UPDATE)
  * `test_economic_ingestion_marks_failed_with_out_of_rolling_window_reason`
    — patch the cache so `get_events_for_date` returns `None`; assert
    `partition.status == "failed"` and
    `partition.last_error.startswith("forexfactory_out_of_rolling_window")`.
  * `test_economic_ingestion_marks_failed_with_rate_limited_reason` — patch
    the cache to raise `ForexFactoryFetchError(reason="rate_limited")`; assert
    the matching `last_error` prefix.
  * `test_economic_ingestion_reuses_cache_across_per_day_partitions` —
    construct a multi-day range; assert the patched fetcher is called once
    per URL across the loop.
* `tests/test_market_events_cli.py` (UPDATE)
  * `test_cli_dry_run_does_not_call_forexfactory_fetch` — assert no httpx
    calls escape during `--dry-run`.

### 8.2 Frontend guard

* `tests/test_frontend_no_forexfactory_request_path.py` (NEW, simple
  static-source grep): assert `frontend/invest/src` contains zero references
  to `nfs.faireconomy.media` and zero references to the FF helpers. This is a
  cheap regression guard against accidental request-path fetches.

### 8.3 Backwards-compatibility tests preserved

* `tests/test_services_forexfactory_calendar.py` (n8n / Telegram path) — must
  remain green. The legacy `app/services/external/forexfactory_calendar.py`
  is **not** refactored in this PR; the cache changes are confined to the
  market-events helper.

## 9. Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Module-level singleton cache leaks state across pytest tests. | Tests construct an explicit `ForexFactoryWeeklyCache()` instance and inject it; the orchestrator accepts `cache=None` and constructs one if not provided. A `pytest` fixture in `conftest.py` resets the module singleton if we keep one. |
| Misidentifying a date as "out of window" near the Sunday→Monday ET boundary. | `rolling_window_for_today` uses ET (`America/New_York`) explicitly and is exhaustively tested at the boundary. |
| `httpx_mock` not yet a project dependency. | Plan uses `unittest.mock.AsyncMock` against `httpx.AsyncClient.get` instead — pattern already used elsewhere in this repo. |
| 광현님 has not approved production schedule activation. | This PR ships the helper + cache + tests + runbook update **only**. The Prefect flow stub is unscheduled and manually triggerable; activation is a separate approval-gated task. |
| `out_of_rolling_window` partitions flood `error` in the freshness UI. | Sentinel reason string allows a small follow-up task to remap it to a softer state. This PR documents the contract but does not block on the follow-up. |
| Long-running test suite hits live FF if a mock leaks. | All tests assert on `httpx.AsyncClient.get` being patched; a `pytest` autouse fixture in `tests/conftest.py` can deny network for the `forexfactory_helpers` test module if desired (optional in the plan). |

## 10. Out-of-scope follow-ups (explicit, not blocking this PR)

1. **Prefect deployment activation** for the rolling window — gated behind
   explicit 광현님 approval. Deliverable: a follow-up Linear task with a
   `--dry-run` smoke from a deployed runner first, then a graduated rollout.
2. **Freshness UI label** for `out_of_rolling_window` — small task in
   `app/services/market_events/freshness_service.py` +
   `frontend/invest/src/components/calendar/vm.ts` to distinguish "수집 실패"
   (transient) from "데이터 제공 범위 밖" (structural).
3. **Cross-process cache** in Redis if and only if we later run multiple
   parallel Prefect workers per cycle. Not justified by current load.
4. **Production manual backfill** (e.g. one-time recovery for missing days
   inside the rolling window) — must be approval-gated and run with
   `--dry-run` first; treat as a separate task.

## 11. Acceptance criteria

* Every new behavior in §4 is gated by a test in §8.
* `uv run pytest tests/services/test_market_events_forexfactory_helpers.py
  tests/services/test_market_events_ingestion.py tests/test_market_events_cli.py
  tests/test_services_forexfactory_calendar.py -q` passes.
* `uv run ruff check . && uv run ruff format --check .` passes.
* Linear ROB-184 comment trail includes: branch + PR URL, commit SHAs, the
  exact test commands and their output, and a statement that **no live FF HTTP
  fetch occurred during CI**.
* No production DB write / backfill / scheduler activation is performed by
  this PR; the runbook section explicitly calls out approval gating for any
  later activation.
