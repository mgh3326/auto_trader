# ROB-426 PR3 — `/invest/screener` Degraded-State UX (Design Spec)

- **Date**: 2026-06-03
- **Issue**: ROB-426 (`/invest/screener` production data recovery + snapshot pipeline hardening)
- **Stack position**: PR3 of 3. PR1 (fundamentals `raw_payload` JSONB-safe + DART `--estimate-only`) and PR2a (read-path latest-healthy-partition selection) and PR2b (write-path commit guards) are **merged to `main`** (`03ed53bc`, `a22d4192`, `be6b0e69`).
- **Branch**: `rob-426-pr3` (off `origin/main` `be6b0e69`).

## 1. Context & Problem

PR2a added `resolve_healthy_partition()` (`app/services/invest_screener_snapshots/partition_health.py:110-210`), which produces a `HealthyPartition{partition_date, row_count, coverage_ratio, is_fallback, healthy}` and wired it into the 7 screener snapshot loaders. PR2b added write-path commit guards. **But the partition-health intelligence dies at the service boundary**:

- Of `HealthyPartition`'s fields, only `partition_date` reaches the API (becomes `ScreenerFreshnessPrimary.snapshotDate`).
- `is_fallback` and `healthy` are collapsed into one local boolean `partition_degraded = bool(hp and (hp.is_fallback or not hp.healthy))` (`screener_service.py:443`, `:618`; `double_buy_screener.py:67-69`), used only to call `cap_degraded(state)` (`partition_health.py:52-57`), then discarded.
- `row_count` and `coverage_ratio` are computed but read by **zero** downstream callers.
- `cap_degraded()` floors both a thin-newest partition and an older-fallback partition to the single label `"stale"`, so the API's only degraded signal (`ScreenerFreshness.primary.dataState` / `overallState`) **cannot say why** data is degraded.

Simultaneously:
- Empty results render one static string `"표시할 종목이 없습니다."` (`ScreenerResultsTable.tsx:17-18`) regardless of cause.
- The distinct empty conditions are emitted only as free-text Korean `warnings[]` strings (`screener_service.py:1660-1662`) rendered as an undifferentiated `<ul>` (`DesktopScreenerPage.tsx:122-128`) — the UI must pattern-match Korean text to infer cause.
- A healthy partition with **zero qualifiers** is mislabeled `"stale"` (`screener_service.py:1706` `_snapshot_state_override or "stale"`), conflating "no matches today" with "data degraded".

### market_cap reality (verified)

The `시가총액` column shows `-` when valuation data is missing. Verification of the actual code paths:

- **KR valuation presets** — `fundamentals_screener.py` (`:277`), `undervalued_breakout_screener.py` (`:63`), `high_yield_value_screener.py` (`:56`) — already join `MarketValuationSnapshot` **via `resolve_healthy_partition`** and select the typed `market_cap` column. **PR2a already fixed their partition selection** (thin 20-row → 3.8k healthy), so the headline `-` regression for these presets is substantially resolved.
- **Non-valuation KR presets** — `consecutive_gainers` / `investor_flow` (loaders in `screener_service.py:418-722`) and `double_buy` (`double_buy_screener.py`) — **never join valuation and never select `market_cap`** → the `시가총액` column is always empty for them.
- **`raw_payload` fallback is near-no-op**: `builder.py:73` sets the typed column `market_cap = _to_decimal(raw.get("market_cap") or raw.get("marketCap"))` and `:76` sets `raw_payload = dict(raw)` — **same source**. So when the typed column is null, `raw_payload` almost always lacks the value too; only the rare parse-failure sub-case would be recoverable. Rejected as the mechanism.
- **US is null at source**: `app/services/brokers/yahoo/client.py:303-333` fetches PER/PBR/EPS/BPS/DivYield but **never extracts `marketCap`**. US `market_cap` cannot be fixed at the display layer — deferred to a follow-up builder/data PR.

The genuinely valuable lever is therefore **adding a healthy-partition valuation join to the non-valuation KR presets**, not a `raw_payload` fallback.

## 2. Goals / Non-goals

**Goals**
1. Surface a structured `degradationReason` so the UI can distinguish the distinct empty/degraded reasons (esp. older-fallback vs thin-today).
2. Surface a server-formatted `coverageLabel` (e.g. `"20 / 3,800 (0.5%)"`) when coverage is below floor — transparency for the headline regression.
3. Stop mislabeling a healthy-but-zero-qualifiers partition as `stale`.
4. Make the `시가총액` column meaningful on the non-valuation KR presets by joining the healthy `MarketValuationSnapshot` partition; mark provenance with `marketCapSource`.
5. Render distinct, honest empty/degraded states (desktop) instead of one static string.

**Non-goals (explicitly out of scope)**
- US `market_cap` source fix (Yahoo builder change) — separate data PR.
- Mobile screener page — none exists today (`DesktopScreenerPage` is the sole entry); building one is speculative scope. Component designed to be reusable for a later parity issue.
- A new `DataState` badge value — PR2a spec line 75 says do not mint one; chip stays `stale`, the *why* lives in `degradationReason`. `freshness.py` and all `DataState` consumers stay unchanged.
- Exposing PR2b commit-guard internals on the read path.
- Operator backfill/refresh of snapshot partitions (issue Non-goal; operator-gated).

## 3. Empty / degraded reasons (canonical set)

| `degradationReason` | Trigger (code) | Chip (`dataState`) | UX tone |
|---|---|---|---|
| `snapshot_missing` | `resolve_healthy_partition` → `None`; snapshot-only loader returns `None`; `_snapshot_state_override="missing"` (`screener_service.py:1618-1651`, `:1701-1706`) | `missing` | "스냅샷 준비중" (recovery pending; no retry-clears-cache promise) |
| `coverage_below_floor` | `hp.healthy is False` (newest partition < 50% floor; `partition_health.py:181-187`) | `stale` | "오늘 커버리지 얇음" + `coverageLabel` |
| `older_fallback` | `hp.is_fallback is True` (older healthy partition served; `partition_health.py:165-171`) | `stale` | "이전 파티션 기준 (asOf 날짜)" |
| `healthy_no_matches` | partition healthy, loader returns `[]` (rows empty, not `None`) | **not** `stale` — partition's real state (fresh/partial) | neutral/informational: "조건에 맞는 종목 없음" |
| `live` | snapshot path not taken; `primary_kind="live"` (`screener_service.py:1733-1734`) | (existing) | "실시간 결과 (스냅샷 아님)" — lowest priority |
| `null` | fresh partition with results | (existing) | no banner |

## 4. Design

Three well-bounded units + tests.

### Unit A — Contract (Pydantic ↔ TS lockstep)

All `ScreenerResult*` models use `ConfigDict(extra="forbid")`, so Pydantic and TS must change together.

`ScreenerFreshnessPrimary` (`app/schemas/invest_screener.py:138-145` + `frontend/invest/src/types/screener.ts:113-120`):
- `degradationReason: Optional[Literal["snapshot_missing","coverage_below_floor","older_fallback","healthy_no_matches","live"]] = None`
- `coverageLabel: Optional[str] = None` — pre-formatted, populated only when `degradationReason == "coverage_below_floor"`.

`ScreenerResultRow` (`app/schemas/invest_screener.py:108-129` + `frontend/invest/src/types/screener.ts:87-108`):
- `marketCapSource: Optional[Literal["primary","fallback"]] = None`

No change to `DataState` (`freshness.py`) or `ScreenerResultsResponse` root.

### Unit B — Backend plumbing (re-thread the dropped intelligence)

- `_SnapshotLoadResult` (`screener_service.py:112-120`): add `degradation_reason: str | None = None` and `coverage_label: str | None = None`.
- In each snapshot loader, derive the reason from the `hp` already in hand (do not recompute health):
  - `hp is None` → `snapshot_missing`
  - `hp.is_fallback` → `older_fallback`
  - `not hp.healthy` → `coverage_below_floor`; build `coverage_label` from `hp.row_count` / `active_universe_count(...)` (`partition_health.py:60-86`), formatted server-side as `"{rows:,} / {universe:,} ({pct:.1f}%)"`.
  - rows empty but `hp` healthy and not fallback → `healthy_no_matches`
  - (live path) → `live`
- `_build_freshness` (`screener_service.py:1309`): add keyword-only `primary_degradation_reason: str | None = None` and `primary_coverage_label: str | None = None` (same defaulted-keyword pattern as the ROB-277 params at `:1316-1321`); pass them into both `ScreenerFreshnessPrimary(...)` constructions (`:1411`, `:1422`).
- **Empty-reason 3 mislabel fix** (`screener_service.py:1701-1706`): the loader records `degradation_reason="healthy_no_matches"` (and the partition's real state) on `_SnapshotLoadResult` when `hp` is healthy/non-fallback but the row list is empty (it already surfaces `partition_date` there even with 0 rows, per ROB-277). `build_screener_results` then consumes that `degradation_reason` so that, instead of flooring `_aggregated_data_state` to `"stale"` via `_snapshot_state_override or "stale"`, it keeps the partition's real state (fresh/partial). Net: a single `degradation_reason` flows loader → `_SnapshotLoadResult` → aggregation → `_build_freshness`; the `:1706` change only stops the false `stale` for this one reason.

### Unit C — market_cap join for non-valuation KR presets

For `consecutive_gainers`, `investor_flow` (loaders at `screener_service.py:418-722`), and `double_buy` (`double_buy_screener.py`):
- Resolve the healthy `MarketValuationSnapshot` partition with `resolve_healthy_partition` (reuse PR2a infra; same call shape as `fundamentals_screener.py:277-281`).
- `LEFT JOIN` it on `(market, symbol)` and populate `row["market_cap"]`.
- **KR + snapshot-path only.** US path, live fallback path, and crypto path are unchanged.
- Set `marketCapSource = "primary"` when the value comes from the newest healthy valuation partition, `"fallback"` when it comes from an older fallback valuation partition (`is_fallback`).
- US `market_cap` stays `-` (source-null) — intended; not a regression.

### Unit D — Frontend (desktop only)

- New `ScreenerEmptyState` (a.k.a. degraded banner) component: switches on `degradationReason` to render distinct copy (table 3). Replaces the static `"표시할 종목이 없습니다."` at `ScreenerResultsTable.tsx:17-18`. `healthy_no_matches` renders neutral/informational (not a warning).
- `ScreenerFreshnessLine.tsx`: when `primary.coverageLabel` is present, render it on the data-basis line; surface `degradationReason` as secondary text/tooltip beside the chip. Chip class still keyed on `dataState`.
- `ScreenerResultsTable.tsx:92`: render a quiet badge/tooltip on the `시가총액` cell only when `marketCapSource === "fallback"`. Keep rows terse (do not dump `row.warnings` inline for kr/us).
- `screener.css`: styles for empty-state/banner/badge.

### Unit E — Tests

- Backend unit: reason mapping (all 5 + null), `coverage_label` formatting, **`healthy_no_matches` is not `stale`** (regression), market_cap join on the 3 presets (KR populated; US unchanged; live/crypto unchanged), schema round-trip (no `extra="forbid"` violation).
- Frontend: empty-state render per `degradationReason`, `coverageLabel` display, fallback badge, and no regression for fresh-with-results.
- Invariants: migration 0; broker/order/watch mutation 0; `partition_health.py`, `guards.py`, `snapshot_commit_guard.py`, `freshness.py` unchanged.

## 5. Risks / verify-at-implementation

- **Does the healthy KR valuation partition actually have populated `market_cap`?** Unit C assumes the 3.8k healthy partition's `market_cap` column is populated (a data assumption underlying the whole recovery). If even the healthy partition is `market_cap`-sparse, the join still yields `-` for those symbols — but the `marketCapSource`/`coverageLabel` transparency keeps the UI honest. Runtime/operator concern, not a code defect.
- **`active_universe_count` cost for the coverage label**: it is computed only when `degradationReason == "coverage_below_floor"` (degraded path), not on the hot fresh path.
- **`extra="forbid"` lockstep**: any field added to Pydantic without the matching TS field (or vice versa) breaks the contract; tests assert round-trip.

## 6. Data flow (for reference)

`MarketValuationSnapshot / InvestScreenerSnapshot / InvestorFlowSnapshot` → `resolve_healthy_partition()` → 7 loaders (now also threading `degradation_reason`/`coverage_label`, and for 3 presets a market_cap join) → `_SnapshotLoadResult` → `build_screener_results()` → `_build_freshness()` → `ScreenerResultsResponse` (Pydantic) → router `/invest/api/screener/results` → `fetchScreenerResults` → TS `ScreenerResultsResponse` → `DesktopScreenerPage.tsx` → `ScreenerFreshnessLine.tsx` + `ScreenerResultsTable.tsx` (+ new `ScreenerEmptyState`).
