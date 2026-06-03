# ROB-426 PR2a — latest-healthy-partition selection (read-path)

- **Linear:** ROB-426 ([workstream] /invest/screener production data recovery + snapshot pipeline hardening)
- **Date:** 2026-06-03
- **Status:** design approved (brainstorming), pre-plan
- **Scope:** PR2 was split into **2a (read-path partition-health selection)** and
  **2b (write-path commit guards)**. This document specs **2a only**. 2b gets its
  own spec → plan → PR. (PR1 — fundamentals JSONB + DART estimate-only — is
  separate and already in PR #1111.)

---

## 0. Problem & grounding (code-verified 2026-06-03)

`/invest/screener` KR loaders select the snapshot partition as the **newest
`snapshot_date`, regardless of how many rows it holds.** A 20-row manual/smoke
partition (0.5% of the ~3,900 active KR universe) therefore *shadows* an older
3.8k-row healthy partition, and presets render empty or thin.

Partition-selection sites (all bare `max(snapshot_date)`), verified:

| # | Site | Table | Presets |
| - | ---- | ----- | ------- |
| 1 | `screener_service.py:429-431` | `InvestScreenerSnapshot` | `consecutive_gainers` |
| 2 | `screener_service.py:600-602` | `InvestorFlowSnapshot` | `investor_flow_momentum` |
| 4 | `double_buy_screener.py:41-46` | `InvestorFlowSnapshot` + `InvestScreenerSnapshot` | `double_buy` |
| 5 | `fundamentals_screener.py:274-280` | `MarketValuationSnapshot` (+ per-symbol fundamentals) | 7 fundamentals presets (`cheap_value`, `steady_dividend`, `growth_expectation_toss`, `profitable_company`, `undervalued_growth`, `stable_growth`, `future_dividend_king`) |
| 6 | `high_yield_value_screener.py:52-57` | `MarketValuationSnapshot` (+ `InvestScreenerSnapshot` LEFT JOIN) | `high_yield_value` |
| 7 | `undervalued_breakout_screener.py:59-64` | `MarketValuationSnapshot` (+ `InvestScreenerSnapshot` LEFT JOIN) | `undervalued_breakout` |
| 10 | `invest_screener_snapshots/repository.py:115-121` `latest_partition(*, market)` | `InvestScreenerSnapshot` | repo chokepoint used by `list_top_candidates`/`breadth` |

Crypto (`screener_service.py:744` → `invest_crypto_screener_snapshots/repository.py`)
**already** gates on `coverage().latest_partition_count` — it is the reference
pattern and is left unchanged.

**Reuse, don't rebuild:**
- Active-universe count is an established query (`KRSymbolUniverse`/`USSymbolUniverse`
  `WHERE is_active = TRUE`), e.g. `coverage_service.py:28-49` and inline in
  several loaders. There is **no** single `_universe_count` helper today — 2a
  adds one small shared helper rather than a 7th copy.
- Freshness layer (`app/services/invest_screener_snapshots/freshness.py`):
  `DataState = Literal["fresh","partial","stale","missing","fallback"]`,
  `classify_state`, `classify_investor_flow_partition`, `aggregate_states`,
  `compute_overall_state`. 2a composes with these; it introduces **no** new enum.

**Critical distinction (must not be confused in implementation):**
*coverage = total row count in a partition (the scored universe)*, **not** the
number of preset **qualifiers**. A healthy 3.8k-row partition with 0 qualifiers
is still healthy and legitimately renders 0 rows. The existing
`screener_service.py:425-431` comment ("prevents older qualifying partitions from
leaking… when the latest partition has zero qualifiers") is about *qualifiers*;
2a gates on *total partition rows* and does not change qualifier filtering.

---

## 1. Goal

When the latest partition's total row count is below a coverage bar, serve the
most recent **healthy** older partition instead (bounded scan-back) and label it
honestly as `stale` with coverage metadata — so a thin smoke partition never
shadows a healthy one. Read-only, fail-open, reversible. No write-path changes.

## 2. Locked design decisions

- **D1 (threshold).** `min_coverage = active_universe_count × 0.50`. A single
  ratio constant `_MIN_HEALTHY_COVERAGE_RATIO = 0.50`, market-agnostic (KR & US
  both derive their floor from their own active-universe count). Cleanly rejects
  20-row (0.5%) and passes 3.8k-row (~99%); more lenient than the 2b commit
  floor (KR 2500/3909 ≈ 64%) so the read path serves slightly-degraded data
  rather than nothing. Changing it is a separate telemetry-backed PR (guards.py
  convention).
- **D2 (fallback).** Bounded **scan-back** to the first healthy older partition.
  `_MAX_PARTITION_SCAN_BACK = 10` partitions (no unbounded scan). If a fallback
  (older-than-latest) partition is served, force `dataState = "stale"` (reuse the
  existing label; do **not** mint a distinct `fallback` value for the badge) and
  set `asOf` to the chosen partition's date.
- **D3 (resolver location).** New module
  `app/services/invest_screener_snapshots/partition_health.py` — table-generic so
  all KR loaders reuse it.
- **D4 (fail-open).** Any resolver error degrades to the pre-2a behavior
  (`max(snapshot_date)`); 2a must never reduce availability. `universe_count == 0`
  disables the gate (returns the latest partition unchanged).
- **D5 (exclude symbol-scoped).** The per-symbol action-readiness loaders
  (`action_readiness_service.py:361-364`, `:467-470`) are **out of scope** —
  per-symbol coverage means ~1 row and the health bar does not apply.

## 3. Components

### 3.1 `app/services/invest_screener_snapshots/partition_health.py` (new)

```
@dataclass(frozen=True)
class HealthyPartition:
    partition_date: date
    row_count: int
    coverage_ratio: float        # row_count / universe_count
    is_fallback: bool            # True if older than the newest partition
    healthy: bool                # True if row_count met the coverage floor

async def active_universe_count(session, *, market) -> int
    # KR/US is_active=TRUE count (shared helper; replaces inline duplication going forward)

async def resolve_healthy_partition(
    session, *, model, date_col, market_col, market,
    universe_count: int,
    min_ratio: float = _MIN_HEALTHY_COVERAGE_RATIO,
    max_scan_back: int = _MAX_PARTITION_SCAN_BACK,
) -> HealthyPartition | None

def cap_degraded(state: DataState) -> DataState
    # never claim better than stale: fresh/partial -> stale; missing/fallback/stale kept
```

Behaviour: select up to `max_scan_back` distinct `date_col` values DESC for
`(model, market_col == market)`; for each, `count()` its rows; return the first
whose `row_count >= ceil(universe_count * min_ratio)` as
`HealthyPartition(healthy=True, is_fallback=(date != newest))`.

**Never reduce availability (D4):** if no scanned partition meets the floor,
return the **newest** partition as a last resort —
`HealthyPartition(healthy=False, is_fallback=False)` — so the loader still serves
whatever data exists (the pre-2a behavior) rather than hiding it. `universe_count
<= 0` likewise returns the newest as `healthy=True` (gate disabled). Return
`None` **only** when the table has no partitions at all (empty) — identical to
pre-2a `max() is None`.

So 2a only ever *prefers a healthy older partition over a thin newer one*; it
never serves *nothing* when data exists. The headline win (3.8k older beats
20-row newer) comes from the scan-back; the valuation-primary presets (no healthy
partition anywhere) keep serving their thin latest but labeled degraded.

Constants `_MIN_HEALTHY_COVERAGE_RATIO = 0.50`, `_MAX_PARTITION_SCAN_BACK = 10`
live at module top with a "change = separate PR" note.

### 3.2 Loader wiring (sites 1, 2, 4, 5, 6, 7; site 10 deferred — see below)

Each replaces its bare `max(snapshot_date)` resolution with:
1. `universe_count = await active_universe_count(session, market=market)`
2. `hp = await resolve_healthy_partition(session, model=<Table>, date_col=<Table>.snapshot_date, market_col=<Table>.market, market=market, universe_count=universe_count)`
3. `chosen_date = hp.partition_date if hp else None`; if `None` (empty table) →
   return `None` (same as pre-2a). Else use `chosen_date` for the existing row
   query (qualifier filtering unchanged) and compute
   `degraded = hp.is_fallback or not hp.healthy`.
4. When `degraded`, wrap each row's freshness with `cap_degraded(...)` so a thin
   *today-dated* partition (which `classify_state` would otherwise call `fresh`)
   is not mislabeled. (For an older fallback partition, `classify_state` already
   returns `stale` because `snapshot_date != today` — `cap_degraded` is a no-op
   there.)
- `double_buy` (site 4) resolves each of its two tables independently;
  `degraded` is the OR across both.
- The `MarketValuationSnapshot`-primary presets (5/6/7): in current prod there is
  **no** healthy valuation partition (only 20 rows ever), so `hp.healthy` is
  `False` and 2a serves the same thin latest partition as today **but labeled
  degraded** — it does not hide the rows, and conjuring real data is an
  operator/data-recovery concern, not 2a.
- **Repo chokepoint 10 (`latest_partition`) is DEFERRED.** Site 1
  (`consecutive_gainers`) inlines its own `max()` and does **not** call
  `repo.latest_partition`; that method's consumers are `list_top_candidates` /
  `breadth`. Wiring it is in 2a scope **only if** planning confirms those flow
  into a user-facing `/invest/screener` result (otherwise adding a `min_coverage`
  param with no caller is dead code). If confirmed, it gains an additive
  `min_coverage: int | None = None` (absolute floor = `universe_count × ratio`,
  computed by the caller), default `None` preserving all existing callers.

### 3.3 Freshness honesty

When `degraded` (`hp.is_fallback or not hp.healthy`), the loader caps each row's
`dataState` at `stale` via `cap_degraded` (never claims better than `stale`),
and `asOf` follows the served `snapshot_date` (= `hp.partition_date`). Two cases:
- **Older fallback** (`is_fallback`): `classify_state` already returns `stale`
  (date ≠ today), so `cap_degraded` is a no-op — correct by construction.
- **Thin today-latest** (`not healthy`, not fallback): `classify_state` could
  return `fresh`/`partial` despite low partition coverage; `cap_degraded` forces
  `stale` so the badge is honest. This is the one case the existing classifiers
  miss (they key on the row's closes-window / date, not partition row count).

No new schema field; `ScreenerFreshness.dataState` already carries `stale`.

## 4. Data flow

```
loader
  └─ universe_count = active_universe_count(market)
  └─ resolve_healthy_partition(model, date_col, market_col, market, universe_count, ratio=0.5)
       ├─ latest healthy           → rows@latest,  classify_state(...)              (fresh/stale as today)
       ├─ latest thin → scanback    → rows@older,   classify_state → stale (asOf=older)
       ├─ no healthy in N back      → rows@latest (last resort), cap_degraded → stale  (NOT hidden)
       └─ table empty (max is None) → None → caller's existing missing path
  └─ qualifier filtering on the chosen partition  (UNCHANGED)
```

## 5. Error handling / fail-open

- Resolver query exception → log + fall back to `max(snapshot_date)` (pre-2a),
  mirroring the existing `try/except` at `screener_service.py:431+`. The loader
  wraps the `active_universe_count` + `resolve_healthy_partition` calls in a
  `try/except` that, on error, recomputes `chosen_date` via the original
  `max(snapshot_date)` query (no `degraded` cap) so availability is preserved.
- `universe_count == 0` (universe table empty / new market) → gate disabled,
  newest partition returned (no behavioral change).
- No healthy partition within scan-back → newest partition still served (degraded
  label), **never** hidden — consistent with D4.
- Resolver is pure read; no writes, no broker/order/watch touch.

## 6. Testing

| ID | Test | Asserts |
| -- | ---- | ------- |
| **T1** | **headline regression**: 20-row newer vs 3.8k-row older | Seed `InvestScreenerSnapshot` partitions D1=3800 rows, D2(>D1)=20 rows; `consecutive_gainers` loader serves D1 rows, `dataState == "stale"`, `asOf == D1` |
| **T2** | resolver: latest healthy | latest partition ≥ floor → returned, `is_fallback False`, `healthy True` |
| **T3** | resolver: thin latest → fallback | latest < floor, older ≥ floor → older returned, `is_fallback True`, `healthy True` |
| **T4** | resolver: all thin within scan-back → last resort | all < floor → returns the **newest** partition, `healthy False`, `is_fallback False` (NOT `None`) |
| **T4b** | resolver: empty table | no partitions → `None` |
| **T5** | resolver: bound respected | only scans ≤ `max_scan_back` partitions (older healthy beyond bound is NOT reached → newest returned as last resort) |
| **T6** | resolver: `universe_count == 0` disables gate | returns newest, `healthy True` |
| **T7** | investor_flow + double_buy loaders route through resolver | thin latest investor_flow partition + healthy older → fallback served, `dataState stale` |
| **T8** | fail-open | resolver raising → loader still returns latest-partition rows (no exception bubbles) |
| **T9** | `cap_degraded` thin today-latest | a thin *today-dated* partition (no healthy older) is served but `dataState == "stale"` (not `fresh`) |

Tests are service/repo-layer with seeded partitions (DB fixture). No live
provider, no network.

## 7. Non-goals / safety

- **No write-path / commit guards** (that is 2b). No CLI build-script changes.
- **No migration** — read-path selection only; schema unchanged.
- **No broker/order/watch/order-intent/trade-journal mutation.** No env/secret
  changes.
- **Symbol-scoped action-readiness loaders excluded** (D5).
- **Crypto loader unchanged** (already coverage-gated).
- **No production backfill** — presets whose only data is a thin partition (e.g.
  valuation-primary) still show honest degraded; real data recovery is
  operator-gated.
- Refactoring the ~6 inline `is_active` universe-count duplications beyond adding
  the one shared helper is **out of scope** (note for a future cleanup).

## 8. Acceptance criteria

1. A 20-row newer partition no longer shadows a 3.8k-row older partition for the
   wired KR presets; the older healthy partition is served with `dataState =
   "stale"` and `asOf` = its date.
2. The health bar is `active_universe_count × 0.50`, derived per market from a
   live `is_active` count; no hardcoded universe constant.
3. Fallback scan-back is bounded (`≤ 10` partitions); unbounded scans impossible.
4. 2a never reduces availability: resolver errors / empty universe / no-healthy
   all serve the newest partition (degraded-labeled when thin); `None` only when
   the table is empty (identical to pre-2a `max() is None`).
5. A thin *today-dated* partition with no healthy older fallback is still served
   but labeled `stale` (not `fresh`) via `cap_degraded`.
6. Tests T1–T9 pass; existing screener tests stay green; no migration.
