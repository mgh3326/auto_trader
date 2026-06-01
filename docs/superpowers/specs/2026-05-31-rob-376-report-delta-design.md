# ROB-376 PR1 — `investment_report_delta_get` (read-only 장중 델타 도구)

**Status:** Design approved 2026-05-31
**Issue:** ROB-376 (split from ROB-375). This spec covers **PR1 only** — the read-only
delta MCP tool. The `intraday_update` report policy/type (issue item 2) is a deferred
follow-up issue, out of scope here.

## 1. Problem & context

ROB-375 (a bug umbrella, all merged) fixed report **continuity** (advisory drafts now flow
into `investment_report_context_get`, journal `enrich_live`, frozen numeric baselines).
ROB-376 is the **feature** layer: an intraday follow-up report should automatically surface
what changed vs the open/prior report — "무엇이 목표/손절/트리거를 터치했고, 지수·보유 P/L이
어떻게 변했나".

### Verified current state (code-checked)

- `investment_report_context_get` returns `triggered_events`, `active_watches`,
  `recent_decisions` (`app/services/investment_reports/query_service.py:262`). `triggered_events`
  is **only** populated by the background `InvestmentWatchScanner` job
  (`app/jobs/investment_watch_scanner.py`); advisory drafts never activate a watch, so it is
  effectively always empty for them. This is a **side-effect-job dependency**, not a hard
  "advisory is forbidden" guard.
- **Correction to the issue's premise:** advisory flows are **not** code-barred from
  `activate_watch`/`decide_item`. `decide_item` has no service guard; `activate_watch` guards
  only on item kind/status/condition. `draft_policy` (`"exclude"` | `"advisory_only"`,
  discriminated by `created_by_profile == "HERMES_ADVISOR"`) only selects which prior drafts
  are returned as context — it does not gate calls. Therefore the delta must be derived from
  baseline-snapshot-vs-live and must **not** depend on scanner/watch state.
- `previous_report_uuid` is a **trace hint only** — no consumer computes a delta today.
- `get_trade_journal(enrich_live=True)` (`app/mcp_server/tooling/trade_journal_tools.py:240`)
  **already** computes per-entry `target_reached`, `stop_reached`, `pnl_pct_live`,
  `current_price` from live quotes (ROB-375 Bug3). The delta tool **reuses** these — it does
  not recompute level touches.
- Reports carry frozen `market_snapshot` and `portfolio_snapshot` dicts
  (`app/schemas/investment_reports.py:344-345`), built by
  `generator.py::_section_snapshot_descriptors` as `{ "provenance": {...}, "baseline": {...} }`
  (or `{ "status": "unavailable", "reason": ... }`), frozen at row-snapshot time by
  ROB-375 Bug2. **Critical asymmetry (code-verified):**
  - `market_snapshot["baseline"]["indices"]` IS present — a dict keyed by index symbol:
    `{ "<symbol>": { "change_percent": float, "name": str|None, "current": float|None } }`
    (`generator.py:80,89` whitelist `("market","from_date","to_date","indices")`). Usable
    baseline for the **index delta**.
  - `portfolio_snapshot["baseline"]` whitelists **aggregates only**
    (`primary_source`, `cash`, `buying_power`, `sellable_summary`, `holdings_count`) and
    **deliberately omits the per-symbol `holdings` list** (`generator.py:81-106`). So there is
    **no per-symbol P/L baseline on the report row**.
- The per-symbol P/L baseline lives in the **snapshot bundle's** `portfolio` payload, reachable
  via `report.snapshot_bundle_uuid`. Each `holdings[]` entry carries `ticker` + `pnl_rate`
  (`collectors/portfolio.py:144-168`, `_reader_holding_to_dict`). Live `get_holdings` positions
  carry `symbol` + `profit_rate` (`portfolio_helpers.py`). The holdings-P/L delta joins
  baseline `pnl_rate` (by `ticker`) to live `profit_rate` (by `symbol`).
- Input tools all exist: `get_trade_journal`, `get_holdings`
  (`portfolio_holdings.py:1215`), `get_market_index` (`fundamentals_handlers.py:271`,
  returns `{"indices": [{symbol, current, change_pct, ...}]}`),
  `investment_report_get`/`get_bundle`. Bundle payloads are read via
  `InvestmentSnapshotsRepository.list_bundle_items_with_snapshots` (the same read path the
  generator uses) — `investment_snapshots` are reusable evidence artifacts.

## 2. Goals & non-goals

**Goals**
- One new read-only MCP tool that, given a baseline report, returns 3 deterministic deltas:
  1. **levels_delta** — target/stop touch (journal × live), reusing `get_trade_journal` output.
  2. **holdings_pnl_delta** — current holdings P/L vs baseline `portfolio_snapshot` P/L.
  3. **index_delta** — current `get_market_index` vs baseline `market_snapshot` index.
- Per-signal fail-open: one signal's failure never kills the others.
- `migration 0`; no broker/order/watch/order-intent mutation; no in-process LLM.

**Non-goals (deferred)**
- `intraday_update` report policy / `report_type` / freshness policy (issue item 2 → separate issue).
- New 급변주 (`screen_stocks`) and 뉴스 이슈 (`get_market_issues`) signals (the "5종" option; not chosen).
- Persisting per-report expected-level snapshots (would need a migration; rejected in favor of
  journal-derived levels).
- HTTP transport surface (MCP tool only for PR1).

## 3. Tool contract

```
investment_report_delta_get(
    report_uuid: str,            # baseline = 개장/직전 리포트
    near_pct: float = 1.0,       # 목표/손절 근접 임계(%), passed through to journal near-flags
    account_type: str = "live",  # 저널 조회 스코프 ("live" | "paper")
) -> dict
```

Return (deterministic; snake_case keys, serialized `by_alias`/`mode="json"`):

```jsonc
{
  "success": true,
  "baseline_report_uuid": "<uuid>",
  "market": "us",                       // echoed from baseline report
  "computed_at_kst": "2026-05-31T13:05:00+09:00",
  "levels_delta": {
    "entries": [
      { "symbol": "AAPL", "side": "buy", "target_price": 230.0, "stop_loss": 200.0,
        "current_price": 231.2, "pnl_pct_live": 4.1,
        "target_reached": true, "stop_reached": false,
        "near_target": false, "near_stop": false }
    ],
    "summary": { "near_target": 1, "near_stop": 0, "target_hit": 1, "stop_hit": 0 }
  },
  "holdings_pnl_delta": {                 // baseline = snapshot-bundle portfolio payload
    "entries": [
      { "symbol": "AAPL", "baseline_pnl_pct": 1.0, "live_pnl_pct": 4.1, "delta_pp": 3.1 }
    ],
    "summary": { "symbols_compared": 1, "symbols_baseline_only": 0, "symbols_live_only": 0 }
  },
  "index_delta": {                        // baseline = report market_snapshot["baseline"]["indices"]
    "entries": [
      { "index_symbol": "^GSPC", "baseline_value": 5500.0, "live_value": 5533.0, "change_pct": 0.6 }
    ]
  },
  "unavailable": { "holdings": "baseline_snapshot_absent" }   // present only when a signal degraded
}
```

- `success:false` only for top-level failures: `{ "error": "baseline_not_found" }` (bad/missing
  `report_uuid`) or `{ "error": "invalid_report_uuid" }` (unparseable).
- Per-signal degradation does **not** flip `success`; the block is set to `null` and the reason
  recorded under `unavailable[<signal>]`.

## 4. Components & data flow

New file: `app/services/investment_reports/delta_service.py` (one focused service, one public
method `compute_delta(...)`). Handler in
`app/mcp_server/tooling/investment_reports_handlers.py` (`investment_report_delta_get_impl`),
registered alongside the other investment-report tools.

Flow inside `compute_delta`:

1. **Baseline load** — `InvestmentReportQueryService.get_bundle(report_uuid)` (returns a dict
   `{report, items, decisions_by_item, alerts, events}` or `None`). If `None` →
   `baseline_not_found`. From `bundle["report"]` read `market`, `market_snapshot`,
   `snapshot_bundle_uuid`. The **symbol set** = de-duped non-null `item.symbol` over
   `bundle["items"]`.
2. **levels_delta** — call `get_trade_journal(enrich_live=True, account_type=account_type,
   market=<baseline market>)`; filter `entries` to the baseline symbol set; project the already-
   computed `target_reached`/`stop_reached`/`pnl_pct_live`/`current_price` plus `near_*` flags.
3. **holdings_pnl_delta** — baseline P/L from the **snapshot bundle**: load the bundle's
   `portfolio` payload via `InvestmentSnapshotsRepository.list_bundle_items_with_snapshots(
   bundle.id)` (bundle resolved from `snapshot_bundle_uuid`), build `{ticker → pnl_rate}` from
   `payload["holdings"]`. Live P/L from `get_holdings(...)` → `{symbol → profit_rate}` over all
   `accounts[].positions[]`. `delta_pp = live_profit_rate − baseline_pnl_rate` for symbols
   present in **both**; count `symbols_baseline_only` / `symbols_live_only` in `summary`.
   **Missing ≠ zero** — never fabricate a 0 P/L for a symbol absent on one side (ROB-375 Bug2
   lesson). If `snapshot_bundle_uuid` is null or no `portfolio` payload is present →
   `unavailable["holdings"]="baseline_snapshot_absent"`.
4. **index_delta** — baseline from `market_snapshot["baseline"]["indices"]` (dict keyed by index
   symbol; per-symbol `current`). If `market_snapshot` is the `{"status":"unavailable"}` shape or
   lacks `baseline.indices` → `unavailable["index"]="baseline_snapshot_absent"`. Live from
   `get_market_index(market=<baseline market>)` → match `indices[].symbol`, take `current`.
   `change_pct = (live_current − baseline_current) / baseline_current * 100` only when both
   finite and `baseline_current != 0`; otherwise `change_pct` is `null` (never fabricated).

Each of steps 2–4 is wrapped in its own `try/except` (fail-open). Live-data helpers and the
snapshots repository are imported lazily inside the service to keep the handler/CLI import path
free of heavy/Settings-backed modules.

## 5. Error handling

| Condition | Behavior |
|---|---|
| Unparseable `report_uuid` | `{success:false, error:"invalid_report_uuid"}` |
| Baseline report not found | `{success:false, error:"baseline_not_found"}` |
| A signal's collector raises | That block `null`, `unavailable[signal]=<short reason>`, others continue |
| Baseline snapshot key absent (legacy report) | `unavailable[signal]="baseline_snapshot_absent"` |
| Symbol present on only one side (P/L) | Counted in summary; **no** fabricated entry |
| Non-finite / divide-by-zero in a numeric delta | Field `null`, never `NaN`/`Infinity` in JSON |

## 6. Testing (pytest, DB-integration where a report row is needed)

- **Happy path:** seed a baseline report (bundle items + frozen snapshots) + active journals;
  mock live quotes/holdings/index → all 3 deltas populated with correct values.
- **Fail-open isolation:** force each signal's collector to raise; assert the other two still
  populate and `unavailable[<signal>]` is set, `success` stays `true`.
- **Missing ≠ zero:** baseline `portfolio_snapshot` lacks symbol X present live (and vice-versa)
  → no fabricated P/L entry; counted in `summary`.
- **Legacy report:** empty `market_snapshot`/`portfolio_snapshot` → `unavailable` path, no crash.
- **Top-level errors:** bad UUID → `invalid_report_uuid`; unknown UUID → `baseline_not_found`.
- **Registration/schema:** tool is registered and its response round-trips serialization.

## 7. Safety boundaries (recap)

- Read-only: no broker/order/watch/order-intent mutation; does not touch the scanner or
  `activate_watch`/`decide_item` paths.
- `migration 0`.
- No in-process LLM — output is deterministic evidence for Hermes to pull/compose/push
  (consistent with `/invest/reports has no internal LLM`).
- Independent of watch/scanner state — derives purely from baseline snapshot vs live.
