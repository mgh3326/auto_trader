# kr-preopen New-Candidate Shadow Section (ROB-918)

## What this is

`research_run_decision_session_service.create_decision_session_from_research_run`
now injects an **advisory-only** `market_brief["new_candidates"]` block into
every `trading_decision_sessions` row created for `market_scope="kr"` +
`stage="preopen"` research runs. It never creates
`trading_decision_proposals` rows and never touches broker/order/watch state
— it is a pure observation record for a 2-week shadow review (see ROB-918).

For any other market/stage combination, `market_brief["new_candidates"]` is
`None`.

## What it contains

```jsonc
{
  "advisory_only": true,
  "market_state": "normal" | "crash_warning" | "unknown",
  "market_state_detail": {"symbol": "069500", "venue": "KRX", "gap_pct": -4.1, ...},
  "consecutive_gainers": [ {...candidate...}, ... ],
  "theme_leaders": [ {...candidate...}, ... ],
  "double_buy": [ {...candidate...}, ... ],
  "omitted_sections": [ {"section": "theme_leaders", "reason": "no_naver_theme_snapshots"}, ... ]
}
```

Each candidate:

```jsonc
{
  "symbol": "042660",
  "name": "한화오션",
  "reason": "consecutive_gainers" | "theme_leader" | "double_buy",
  "advisory_only": true,
  "selection_rationale": "전일(...) 8.7% 상승, 연속상승 1일, 시총≥2,000억·거래대금≥200억 필터 통과",
  "metrics": { ... reason-specific fields ... },
  "baseline_date": "2026-07-16",
  "baseline_close": 50000.0,
  "outcome": {"d1_close_pct": null}
}
```

`outcome.d1_close_pct` starts `null` and is filled in retrospectively by the
shadow aggregation script below — it is never written back into the session
row (this repo's DB-write path stays out of scope; the script only reads).

## Sources and filters

- **consecutive_gainers** — `invest_screener_snapshots` latest healthy `kr`
  partition, filtered to `market_cap >= 2,000억` (joined from
  `market_valuation_snapshots`) and an estimated `trade_value_est =
  daily_volume * latest_close >= 200억` (no direct 거래대금 column exists on
  `invest_screener_snapshots`), sorted by `change_rate` desc, top N.
- **theme_leaders** — `get_theme_events`'s underlying repository
  (`InvestMomentumEventSnapshotsRepository.list_theme_events(event_kind="theme")`
  over `invest_theme_event_snapshots`), flattened to one candidate per leader
  symbol (up to 3 per theme) using `invest_theme_event_snapshot_stocks.price`
  as the baseline close.
- **double_buy** — delegates to the existing
  `load_double_buy_from_snapshots(market="kr")` loader (Toss-parity 쌍끌이
  매수, day-over-day 외국인/기관 순매수 증가) unmodified.

## Crash guard (label only, never blocks)

Compares the latest two `kr_candles_1d` closes for KODEX200 (`069500`,
venue `KRX`). If the gap is `<= -3.0%`, `market_state` is tagged
`"crash_warning"`. This never suppresses candidate generation — it is
advisory context only, per ROB-918 AC #5.

## Graceful degradation

If any source snapshot is missing or a query fails, that section returns an
empty list and an entry is appended to `omitted_sections` with a `reason`
(e.g. `"snapshot_missing"`, `"no_naver_theme_snapshots"`,
`"investor_flow_snapshot_missing"`, `"query_failed"`). The section builder
never raises — a read failure here can never break kr-preopen session
creation.

Structural safety note: `build_new_candidate_section` opens its **own** DB
session (`AsyncSessionLocal()`), never the caller's — so even an unexpected
exception here cannot poison the write transaction that creates the session
and its `research_run` proposals.

## Running the 2-week shadow aggregation report

Read-only — SELECT only, never writes:

```bash
uv run python -m scripts.shadow_new_candidates_report --since-days 14
```

It scans recent `kr` `research_run` sessions' `market_brief.new_candidates`,
joins each candidate's `baseline_date`/`baseline_close` against the next
`kr_candles_1d` close for that symbol (`venue='KRX'`), and prints a
recovered-rate / false-positive-rate / average-D+1-% table per selection
reason (`consecutive_gainers` / `theme_leader` / `double_buy`).

## Safety boundaries

- No `trading_decision_proposals` rows are ever created for these
  candidates — proven by
  `tests/test_research_run_decision_session_service_new_candidates.py::test_kr_preopen_session_gets_new_candidates_section_without_proposals`.
- No migration (JSONB reuse on the existing `market_brief` column).
- No TaskIQ/cron schedule added or changed — the section is computed inline
  whenever `create_decision_session_from_research_run` already runs.
- `scripts/shadow_new_candidates_report.py` never writes to the database.
