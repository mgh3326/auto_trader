# ROB-425 — 분기 fundamentals 수집 + 성장 기대주(Toss) parity (`earnings_growth_qoq`)

> Spec authored 2026-06-02 via `/spec`. Branch `rob-425`. Tracker: Linear ROB-425
> (parent ROB-359, related ROB-422 / ROB-330). Next step: `/plan` per PR.
>
> **Code-grounded** against `origin/main` @ `96d159e7` (ROB-422 / PR #1108 merged).
> All 8 infrastructure premises in the Linear issue were verified TRUE before writing.

## Context

ROB-422 (PR #1108) reached `full` parity on 10 of Toss's 11 default fundamentals
presets and honestly classified the 11th — **성장 기대주 (#8)** — as `missing`,
splitting it here. Toss's rule is **3년 평균 순이익 증감률 ≥ 3%** AND **직전분기 대비
순이익 증감률(QoQ) ≥ 10%**. The QoQ leg needs **quarterly** statements, but the live
collector only fetches annual. ROB-422 deliberately built the read-model, derive
logic, and screener plumbing to *accept* quarterly rows, then deferred the actual
quarterly fetch so the collection layer (PR1-shaped) wouldn't bleed into the
screener-wiring PR.

This issue does the deferred half: turn on quarterly collection (with DART-limit
pacing), wire the QoQ metric through to the screener, register the Toss-parity
preset, and flip the parity matrix to **11/11 full**.

**Who cares:** `/invest/screener` users wanting Toss's 성장 기대주 list in
auto_trader; the parity-matrix audit (closes the last `missing`).

## Verified current state (origin/main @ 96d159e7)

| Claim | Status | Evidence |
|---|---|---|
| `financial_fundamentals_snapshots` holds quarterly rows (`period_type`, `discrete_*`) | ✅ | `app/models/financial_fundamentals_snapshot.py:45,74,92-97`; migration `rob422_add_financial_fundamentals_snapshots.py` |
| `derive_fundamentals_metrics` computes `earnings_growth_qoq` | ✅ | `app/services/financial_fundamentals_snapshots/derive.py:217-242`; `_earnings_growth_qoq` `:145-155` |
| `single_quarter_discrete` (YTD diff) | ✅ | `builder.py:126-138` |
| `_payload_from_quarterly` (assembles quarterly upsert, computes discrete) | ✅ | `builder.py:230-277` |
| `default_dart_fetcher` accepts `include_quarterly` but ignores it, returns `quarterly=()` | ✅ | `builder.py:344-410` (`:406`) |
| `_REPRT_CODE_BY_QUARTER = {1:11013, 2:11012, 3:11014, 4:11011}` already defined | ✅ | `builder.py:178` |
| Backfill CLI exists with `--with-quarterly` / `--commit` (dry-run default) / `--concurrency` / `--all`/`--symbol`/`--limit` | ✅ | `scripts/build_financial_fundamentals_snapshots.py:15-59` |
| `FundamentalsPresetSpec` has `min_earnings_growth_3y_avg` but NO `min_earnings_growth_qoq`; `_DERIVE_CHECKS`/`_CARRIED_DERIVE_METRICS` lack `earnings_growth_qoq` | ✅ | `app/services/invest_view_model/fundamentals_screener.py:37-56,149-167` |
| `earnings_growth_qoq` is **computed but dropped** before the screener | ✅ | derive populates it (`derive.py:232`); not in `_DERIVE_CHECKS`/`_CARRIED_DERIVE_METRICS` |
| No `growth_expectation_toss` preset; existing `growth_expectation` (auto_trader_original) stays separate | ✅ | `fundamentals_screener.py:107-117`; `screener_presets.py:161-179` (`parityNote` documents the split) |
| Fundamentals presets auto-route via `preset_id in FUNDAMENTALS_PRESET_SPECS` | ✅ | `screener_service.py:1574-1593`, snapshot-only fallback `:1638-1643` |
| Unique constraint `(market, symbol, fiscal_period, source)` — no `period_type`; quarterly keys on `{year}Q{n}`, distinct from `{year}A` → **no migration** | ✅ | `repository.py:84`; migration `:93-99` |
| Parity matrix #8 = `missing`; summary missing=1, full=10 | ✅ | `docs/invest-screener-toss-parity-matrix.md:61,77-89` |
| No rate-limit / throttle / request-counter anywhere in the collection path | ✅ | none found in `financial_fundamentals_snapshots/`, the CLI, or `disclosures/dart.py` |

**Net:** the read/derive/screener/CLI scaffolding is real and dormant. Three
genuinely-new pieces remain: (1) the fetcher's quarterly branch, (2) DART-limit
pacing, (3) surfacing QoQ to the screener + a fail-closed continuity guard.

## Decisions (locked via `/spec`, 2026-06-02)

- **D1 — 2 PRs.** PR1 = quarterly collection readiness (Scope A). PR2 = preset
  wiring + matrix (Scope B + C). Matches ROB-422/ROB-423 convention and the
  issue's own layer split. PR2 is testable with synthetic `FundamentalPeriod`
  fixtures, so it does not block on PR1's live data; in production the preset
  reads `missing` (fail-closed) until a quarterly backfill runs.
- **D2 — request-budget estimate + hard daily-cap fail-stop** (no token-bucket /
  sleep throttle). dry-run reports the projected request count; a per-run counter
  fail-stops before exceeding a configured daily budget. Real spreading is the
  operator's job via `--limit`/`--symbol`. Full throttle is deferred to the
  production-backfill approval.
- **D3 — fail-closed QoQ continuity guard.** `_earnings_growth_qoq` must require
  the two compared quarters to be **adjacent fiscal quarters** AND the latest to
  be **fresh**; otherwise `unavailable`. Small derive change, lands in PR2 with
  the preset (the only consumer of the metric).
- **D4 — this doc committed to `docs/plans/`, then stop** for review → `/plan`.
  No auto-spawn. Linear ROB-425 already carries the prose intent.

---

# PR1 — Quarterly fundamentals collection readiness (Scope A)

**Goal:** `default_dart_fetcher(include_quarterly=True)` returns populated
quarterly filings (YTD cumulative + `prior_income_statement` for differencing)
with PIT `filing_date`, paced under the DART daily limit, dry-run-first. No
production backfill, no migration, no scheduler.

## A1. Fetcher quarterly branch (`builder.py::default_dart_fetcher`)

Currently `:406` hardcodes `quarterly=()` and the `include_quarterly` arg is
ignored. Replace with a real branch. `_payload_from_quarterly` (`:230-277`) and
`single_quarter_discrete` (`:126-138`) already do all the differencing — PR1 only
needs to **build `RawQuarterlyFiling` tuples with `prior_income_statement` wired**.

KR interim statements are **YTD cumulative**, so the discrete single-quarter value
needs the immediately-prior cumulative within the same fiscal year:

| Quarter | `reprt_code` | cumulative source | `prior_income_statement` | discrete = |
|---|---|---|---|---|
| Q1 | `11013` | Q1 | `None` | Q1 cumulative (standalone) |
| Q2 | `11012` (반기) | H1 | Q1 filing | H1 − Q1 |
| Q3 | `11014` | 9M | Q2 (H1) filing | 9M − H1 |
| Q4 | `11011` (annual, **reuse the annual FY fetch**) | FY | Q3 (9M) filing | FY − 9M |

- Q4 cumulative = the same `finstate_all(symbol, year, "11011", …)` already fetched
  for the annual path; do not double-fetch. This matches `_REPRT_CODE_BY_QUARTER[4]
  == "11011"` and yields a discrete Q4 so QoQ can use the most recent quarter.
- Per quarter, mirror the annual CFS→OFS fallback (`builder.py:367-369`):
  `finstate_all(symbol, year, reprt_code, fs_div="CFS")`, retry `fs_div="OFS"` on
  `None`/empty.
- Populate `RawQuarterlyFiling(bsns_year, quarter, rcept_no, reprt_code,
  income_statement, prior_income_statement)` (`builder.py:150-158`).
- `rcept_no` comes from the per-quarter filing; PIT `filing_date` is resolved by
  the existing `filing_dates` dict (`parse_filing_dates_frame`, `:113-123`,
  consumed in `_payload_from_quarterly`) — **no new PIT code**.
- A quarter whose cumulative frame is `None`/empty is **skipped** (not faked); a
  missing `prior` yields `discrete=None` via `single_quarter_discrete` — that is
  already the correct fail-soft.
- `include_quarterly=False` keeps `quarterly=()` (annual-only default preserved).

## A2. DART daily-limit pacing (D2)

No pacing exists today. Add a **request counter + hard cap**, no throttle.

- **Counter:** wrap each outbound DART call (`finstate_all`, `client.list`) in the
  fundamentals collection path so the job tallies real requests per run. Put the
  counter where both annual and quarterly calls funnel (the fetcher / job runner),
  not in the vendored `OpenDartReader`.
- **Budget:** `DART_DAILY_REQUEST_BUDGET` env (settings), **default `18000`**
  (~90% of the 20,000/day limit, headroom for other DART consumers). `0` or
  negative = explicitly disabled. Document in `env.example` and the runbook.
- **Fail-stop:** before a request that would push the run's tally over the budget,
  raise a typed error (e.g. `DartDailyRequestBudgetExceeded`) and stop — never
  silently truncate. The partial result is reported, not committed past the stop.
- **dry-run estimate:** the dry-run path logs the **projected** request count
  `≈ symbols × years × (1 annual + 3 interim) × ~2 (statement + filing-list, CFS/OFS
  worst case)` so an operator can size `--limit`/`--symbol` before `--commit`.
- Bounded scope stays the operator's lever: existing `--limit` (default 20),
  `--symbol`, `--all`, `--concurrency` (default 4). **No `--all` quarterly without
  an explicit operator decision** (the dry-run estimate makes the cost visible).

## A3. Persistence (no migration)

`_payload_from_quarterly` already emits `fiscal_period=f"{year}Q{quarter}"`,
`period_type="quarterly"`, `discrete_revenue`/`discrete_net_income`, PIT
`filing_date`/`effective_at`, `data_state`. The upsert constraint
`uq_financial_fundamentals_snapshots_msfs = (market, symbol, fiscal_period,
source)` (`repository.py:84`) makes `2025Q3` distinct from `2025A` → quarterly and
annual rows coexist, no schema change.

## A4. PR1 tests (`tests/test_financial_fundamentals_builder_*.py`)

| Layer | What | Count |
|---|---|---|
| Unit | Fetcher builds `RawQuarterlyFiling` Q1–Q4 with correct `prior_income_statement` wiring (Q1 prior=None; Q4 cumulative=annual FY, prior=Q3 9M) | +2 |
| Unit | Discrete differencing end-to-end through `_payload_from_quarterly` (H1−Q1, 9M−H1, FY−9M); missing prior → `discrete=None` | +2 |
| Unit | `include_quarterly=False` → `quarterly=()`; `True` → populated | +1 |
| Unit | Budget fail-stop: run exceeding `DART_DAILY_REQUEST_BUDGET` raises before the over-limit call; partial reported not committed | +2 |
| Unit | dry-run logs a projected request-count estimate; `--commit` not required for estimate | +1 |

Use the existing fetcher-mock pattern (inject a fake fetcher / stub
`finstate_all`); no live DART in unit tests.

## A5. PR1 acceptance criteria

1. `default_dart_fetcher(include_quarterly=True)` returns `RawQuarterlyFiling` for
   Q1–Q4 with `prior_income_statement` wired so `_payload_from_quarterly` produces
   non-null `discrete_*` where the prior cumulative exists; PIT `filing_date` is
   populated from `rcept_no → rcept_dt`.
2. A backfill run that would exceed `DART_DAILY_REQUEST_BUDGET` (default 18000)
   fail-stops with `DartDailyRequestBudgetExceeded` before the over-limit request;
   dry-run reports the projected request count.
3. Annual-only behavior is unchanged when `--with-quarterly` is omitted; no
   migration; quarterly rows upsert distinctly from annual rows.
4. Focused tests above pass; `make lint` clean (`app/` + `tests/`).

---

# PR2 — Toss preset wiring + parity matrix (Scope B + C)

**Goal:** surface `earnings_growth_qoq` to the screener with a fail-closed
continuity guard, register `growth_expectation_toss`, flip the matrix to 11/11.

## B1. Surface QoQ to the screener (`fundamentals_screener.py`)

The metric is computed (`derive.py:232`) but dropped. Three edits:

1. `FundamentalsPresetSpec` (`:37-56`): add
   `min_earnings_growth_qoq: Decimal | None = None  # ratio (0.10)`.
2. `_DERIVE_CHECKS` (`:149-157`): add `("min_earnings_growth_qoq", "earnings_growth_qoq")`.
3. `_CARRIED_DERIVE_METRICS` (`:159-167`): add `"earnings_growth_qoq"`.

The existing `_DERIVE_CHECKS` loop (`:196-206`) then gates on
`derivation.earnings_growth_qoq` (which already exists on `FundamentalsDerivation`,
`derive.py:35-45`): `state != "ok"` → excluded (fail-closed), else compare against
the threshold. No new loop logic.

**Verify in PR2:** the snapshot→`FundamentalPeriod` loader maps `period_type`,
`discrete_revenue`, `discrete_net_income` so quarterly snapshot rows reach
`derive_fundamentals_metrics` as `period_type="quarterly"` periods (the screener
test fixtures already build these fields, implying loader support — confirm the
loader, not just the fixture).

## B2. Fail-closed QoQ continuity guard (D3, `derive.py::_earnings_growth_qoq`)

Today (`:145-155`) it compares `usable[-1]` vs `usable[-2]` where `usable` =
quarters with non-null `discrete_net_income`. A skipped quarter makes `usable[-2]`
non-adjacent (violates "직전분기"); a year-old latest quarter passes as if fresh.
Tighten to fail-closed:

- **Adjacency:** the two compared quarters must be consecutive fiscal quarters.
  Compute a quarter index `idx = year*4 + (quarter-1)` from each period's
  `fiscal_period`/`period_end_date`; require `idx(curr) - idx(prev) == 1`, else
  `unavailable`.
- **Freshness:** `report_date − curr.period_end_date ≤ _QOQ_MAX_STALENESS_DAYS`
  (module constant, **default `183`** ≈ 2 quarters), else `unavailable`. Tunable;
  call it out in review.
- Keep the existing non-positive-base-quarter handling (`_yoy` → `None` →
  `state="partial"`), which the `_DERIVE_CHECKS` loop already treats as excluded.

This only matters once the preset consumes QoQ, so it lands in PR2 with no other
consumer impact.

## B3. Register the Toss-parity preset

- **Spec** (`fundamentals_screener.py`): new `GROWTH_EXPECTATION_TOSS_SPEC =
  FundamentalsPresetSpec(preset_id="growth_expectation_toss",
  min_earnings_growth_3y_avg=Decimal("0.03"),
  min_earnings_growth_qoq=Decimal("0.10"), sort_by="earnings_growth_qoq")`; add it
  to the `FUNDAMENTALS_PRESET_SPECS` registry (`:107-117`). Dispatch is automatic
  (`screener_service.py:1574` `preset_id in FUNDAMENTALS_PRESET_SPECS`) — no
  routing edit; snapshot-only fallback (`:1638-1643`) already forces `missing`
  when no snapshot rows match.
- **Catalog** (`screener_presets.py`): new `ScreenerPreset(id=
  "growth_expectation_toss", name="성장 기대주", presetOrigin=_TOSS,
  parityStatus=_FULL, market="kr", filterChips=[국내, 순이익증가율 "3년평균 3%+",
  순이익 "직전분기 대비 10%+", 데이터 "지연 스냅샷 기반"], metricLabel="순이익증가율")`,
  placed after the existing `growth_expectation` entry (`:161-179`). Add
  `"growth_expectation_toss"` to `_KR_ONLY_PRESET_IDS` (`:27-38`).
- **Do not touch** the existing `growth_expectation` (auto_trader_original
  cap/change self-screen) — it stays separate per its own `parityNote`.

## C1. Parity matrix (`docs/invest-screener-toss-parity-matrix.md`)

- Row #8 (`:61`): preset id `—`→`growth_expectation_toss`; status `**missing**
  (qoq 수집 필요)`→`**full** (ROB-425)`; `earnings_growth_qoq`❌→✅; source `—`→
  `market_valuation_snapshots` + `financial_fundamentals_snapshots`; last col
  `❌ no`→`✅ yes`.
- Summary (`:77-89`): move 성장 기대주 from `missing` to `full`; counts
  **full 10→11, missing 1→0**. Note ROB-425 closes the last `missing` → Toss
  11/11 full.

## B4/C2. PR2 tests (`tests/test_financial_fundamentals_derive.py`, `tests/test_fundamentals_screener.py`)

| Layer | What | Count |
|---|---|---|
| Unit | QoQ guard: adjacent+fresh → `ok`; non-adjacent (gap) → `unavailable`; stale latest quarter → `unavailable`; non-positive base → `partial` | +4 |
| Unit | Preset include: 3y_avg ≥ 3% AND qoq ≥ 10% → included; carries `earnings_growth_qoq` on the output row | +1 |
| Unit | Preset exclude: qoq < 10% (3y ok) excluded; 3y < 3% (qoq ok) excluded | +2 |
| Unit | Fail-closed missing: no quarterly periods → qoq `unavailable` → excluded (never silently passes) | +1 |

Reuse the `_period()` / `_annual()` fixture helpers and pass `report_date` (PIT
gate). Build quarterly fixtures with `period_type="quarterly"` +
`discrete_net_income`.

## PR2 acceptance criteria

1. `growth_expectation_toss` filters on `earnings_growth_3y_avg ≥ 0.03` AND
   `earnings_growth_qoq ≥ 0.10`; with no quarterly data the QoQ leg is
   `unavailable` → the symbol is excluded (fail-closed, never `full`/fresh-faked).
2. QoQ uses **adjacent, fresh** quarters only; a gap or stale latest quarter →
   `unavailable`.
3. The preset auto-routes through the fundamentals snapshot path and is KR-only,
   snapshot-only; `parityStatus=full`.
4. Parity matrix #8 = `full / growth_expectation_toss`; Toss 11/11 full.
5. Focused tests pass; `make lint` clean.

---

## Out of scope / safety boundaries

- Existing `growth_expectation` (auto_trader_original) — **unchanged**.
- No broker / order / watch / order-intent / trade-journal mutation.
- **No production DB quarterly backfill, no migration apply, no scheduler/Prefect
  activation** without separate explicit approval. PR1 is dry-run/no-write
  readiness; quarterly stays gated by `--with-quarterly` (default off) + the
  budget env.
- No real-time throttle / token-bucket (deferred with the production-backfill
  approval).
- QoQ on missing quarterly data → `unavailable` (fail-closed). Never `full`/fresh.
- Q4-discrete reuses the annual FY fetch (no extra DART request for Q4).

## Files reference

| File | PR | Change |
|---|---|---|
| `app/services/financial_fundamentals_snapshots/builder.py:344-410` | PR1 | Quarterly branch in `default_dart_fetcher`; build `RawQuarterlyFiling` Q1–Q4 with `prior_income_statement` |
| `app/jobs/financial_fundamentals_snapshots.py` | PR1 | Request counter + budget fail-stop; dry-run estimate |
| `app/core/config` (settings) + `env.example` | PR1 | `DART_DAILY_REQUEST_BUDGET` (default 18000) |
| `scripts/build_financial_fundamentals_snapshots.py` | PR1 | Surface estimate/budget in output (args already exist) |
| `docs/runbooks/*fundamentals*` | PR1 | Document quarterly readiness + budget pacing |
| `app/services/financial_fundamentals_snapshots/derive.py:145-155` | PR2 | Adjacency + freshness guard in `_earnings_growth_qoq` |
| `app/services/invest_view_model/fundamentals_screener.py:37-167` | PR2 | `min_earnings_growth_qoq` field + `_DERIVE_CHECKS` + `_CARRIED_DERIVE_METRICS` + `GROWTH_EXPECTATION_TOSS_SPEC` + registry |
| `app/services/invest_view_model/screener_presets.py:27-38,161+` | PR2 | `growth_expectation_toss` catalog entry + `_KR_ONLY_PRESET_IDS` |
| `docs/invest-screener-toss-parity-matrix.md:61,77-89` | PR2 | Row #8 → full; summary counts |
| `tests/test_financial_fundamentals_builder_*.py` | PR1 | A4 tests |
| `tests/test_financial_fundamentals_derive.py`, `tests/test_fundamentals_screener.py` | PR2 | B4/C2 tests |

## Effort estimate

- **PR1:** ~2h fetcher quarterly branch + Q4-from-annual wiring · ~1.5h budget
  counter/fail-stop + dry-run estimate · ~2h tests · ~0.5h runbook/env = **~6h**.
- **PR2:** ~0.5h 3-line screener surfacing · ~1.5h QoQ adjacency/freshness guard ·
  ~0.5h preset + catalog · ~0.5h matrix · ~2h tests = **~5h**.

## Rollback

Revert the PR(s). No data migration to undo. Quarterly collection is gated by
`--with-quarterly` (default off) + `DART_DAILY_REQUEST_BUDGET`; the preset
fail-closes to `missing` if no quarterly snapshots exist, so a PR2-only revert
leaves no half-applied filter.

## Sequencing

```
PR1 (collection readiness) ──┐
                             ├─> operator dry-run → (separate approval) → bounded backfill
PR2 (preset + matrix) ───────┘     ↑ until then preset reads `missing` (fail-closed)
```

PR1 and PR2 are independently mergeable (PR2 tests on synthetic fixtures). Ship
PR1 first if you want live data before the preset goes visible; ship PR2 first and
the preset is honestly `missing` until the backfill runs. Recommended order:
PR1 → operator dry-run sanity → PR2.

## Related

- ROB-422 (PR #1108) — built the read-model / derive / screener / CLI scaffolding.
- ROB-359 — Toss parity umbrella (parent).
- ROB-330 — fundamentals PIT panel; quarterly PIT boundary matches ROB-330.
