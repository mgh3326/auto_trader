# ROB-426 PR2b — snapshot commit guards (write-path)

- **Linear:** ROB-426 ([workstream] /invest/screener production data recovery + snapshot pipeline hardening)
- **Date:** 2026-06-03
- **Status:** design approved (brainstorming), pre-plan
- **Scope:** PR2 was split into **2a (read-path partition-health selection — merged, #1112)** and **2b (this — write-path commit guards)**. PR1 (#1111) and PR2a (#1112) already merged.

---

## 0. Problem & grounding (code-verified 2026-06-03, branch off main `a22d4192`)

Every snapshot build CLI defaults `--limit=20` and gates writes with `--commit`
(`dry_run = not commit`). The proven defect: a `--limit 20 --commit` run persists
a 20-row partition that PR2a then has to defend against on read. PR2b closes the
write side across all four families that feed `/invest/screener`.

| Family | CLI | Job runner (commit block) | Guard today |
| ------ | --- | ------------------------- | ----------- |
| **screener** | `scripts/build_invest_screener_snapshots.py:138` → `run_snapshot_build` (UNGUARDED) | `app/jobs/invest_screener_snapshots.py:197` `run_snapshot_build` (commit `:268-269`); guarded wrapper `run_snapshot_build_guarded` `:287-326` | guards EXIST (`guards.py`) but only the **scheduled** task uses them (`app/tasks/invest_screener_snapshot_tasks.py:183`). **Manual CLI bypasses.** |
| **quote** | `scripts/build_market_quote_snapshots.py:94` | `app/jobs/market_quote_snapshots.py:208` `run_market_quote_snapshot_build` (commit `:256-257`, `snapshot_at` distribution `:253`) | **NONE, any path** |
| **valuation** | `scripts/build_market_valuation_snapshots.py:94` | `app/jobs/market_valuation_snapshots.py:193` `run_market_valuation_snapshot_build` (commit `:242-243`, `snapshot_date` distribution `:239`) | **NONE** |
| **fundamentals** | `scripts/build_financial_fundamentals_snapshots.py:134` (+`--estimate-only` from PR1) | `app/jobs/financial_fundamentals_snapshots.py:164` `run_financial_fundamentals_snapshot_build` (commit `:251-252`, `symbols_resolved=len(symbols)` `:256`) | **NONE** (DART budget is an API-quota gate, not a quality guard) |

Key facts:
- **No `AsyncSession` is live at commit time** in any family — `_commit_payloads`
  opens its own `AsyncSessionLocal()`. A coverage-ratio guard therefore opens a
  short-lived session just to read the universe count.
- `guards.py` thresholds (`KR≥2500 / US≥3500`, `≥70%` dominant) are **locked**
  ("change = separate PR" per the module docstring) — PR2b does **not** touch them.
- `active_universe_count(session, *, market)` (`app/services/invest_screener_snapshots/partition_health.py:60-86`, merged in PR2a) is the shared coverage denominator.
- Fundamentals rows are **per-symbol-per-period** (e.g. 10 symbols × ~5 periods),
  and fundamentals backfill is **intentionally incremental** (DART daily budget;
  ROB-425 deliberately did 10 symbols). A row/universe coverage floor would block
  legitimate backfills → fundamentals is exempt from a coverage floor.
- No `*_commit_enabled` / `*_schedule_enabled` config flags exist for quote /
  valuation / fundamentals (only screener has them).

---

## 1. Goal

Prevent accidental production commits of thin partitions across all four
families, while letting operators commit a legitimate partial backfill via an
explicit `--allow-partial` acknowledgment. Close the screener CLI bypass by
routing it through the existing guarded runner.

## 2. Locked design decisions

- **D1 (quote/valuation guard).** A shared coverage-ratio guard:
  `committed_rows ≥ ceil(active_universe_count × 0.60)`. Single constant
  `_MIN_COMMIT_COVERAGE_RATIO = 0.60`, market-agnostic. Stricter than PR2a's
  read-path bar (0.50) — committing data the read path would call degraded is
  worse than serving it.
- **D2 (screener).** Leave `guards.py` (absolute floors + dominant-partition)
  **unchanged**. Route the manual CLI and manual task through the existing
  `run_snapshot_build_guarded`. `--allow-partial` reverts to the unguarded
  `run_snapshot_build` (explicit operator bypass for small `--symbol`/`--limit`
  builds).
- **D3 (fundamentals).** **Exempt from a coverage floor** (always-incremental).
  Guarded by requiring `--allow-partial` for any commit: `commit and not
  allow_partial` → blocked with a message directing the operator to acknowledge
  the partial backfill. PR1's `--estimate-only` remains the budget preflight.
- **D4 (escape flag).** A single `--allow-partial` flag on all four CLIs,
  threaded as `allow_partial: bool = False` on each job request. It does **not**
  combine with `--estimate-only` (fundamentals) or change `--dry-run` semantics.
- **D5 (no new config flags).** These are manual, dry-run-first CLIs; PR2b adds
  **no** `*_commit_enabled` flags (YAGNI). Recorded as a deferred follow-up if
  scheduled paths are ever added for quote/valuation/fundamentals.
- **D6 (fail-closed on block).** A blocked guard does **not** commit; it raises a
  typed exception the CLI catches, prints, and exits non-zero. Failure-only
  output (no success noise).

## 3. Components

### 3.1 `app/services/snapshot_commit_guard.py` (new)

```python
_MIN_COMMIT_COVERAGE_RATIO = 0.60   # locked; change = separate PR

class PartialCommitBlocked(RuntimeError):
    # carries: count, universe_count, min_ratio, market, metric, reason
    ...

def assert_min_coverage(
    count: int, universe_count: int, *, market: str,
    min_ratio: float = _MIN_COMMIT_COVERAGE_RATIO, metric: str = "rows",
) -> None:
    """Raise PartialCommitBlocked when count < ceil(universe_count * min_ratio).
    universe_count <= 0 disables the gate (returns without raising) — never
    block on a missing universe denominator."""
```

Pure arithmetic (no session); the **job** opens a short-lived session, calls
`active_universe_count`, and passes the two integers in. Reuses the
`active_universe_count` denominator from `partition_health.py`.

### 3.2 Per-family job wiring

**Escape mechanism is uniform: the CLI picks the runner.** For the three
row-count families (screener/quote/valuation) `--allow-partial` selects the plain
(unguarded) runner; otherwise the guarded wrapper runs. The guarded wrappers
therefore do **not** need to know about `allow_partial`. Only fundamentals (no
two-pass) carries `allow_partial` on its request.

- **quote** (`market_quote_snapshots.py`) & **valuation** (`market_valuation_snapshots.py`):
  these commit **per-batch inside the build loop** (`market_quote_snapshots.py:256-257`,
  `market_valuation_snapshots.py:242-243`), so there is no single
  "after-build / before-commit" seam. Add a **two-pass guarded wrapper**
  mirroring screener's `run_snapshot_build_guarded` (`:287-326`):
  ```python
  async def run_market_quote_snapshot_build_guarded(request):
      dry = await run_market_quote_snapshot_build(replace(request, commit=False))
      async with AsyncSessionLocal() as s:
          uc = await active_universe_count(s, market=dry.market)
      assert_min_coverage(dry.snapshots_built, uc, market=dry.market)  # raises → no commit pass
      return await run_market_quote_snapshot_build(request)  # original commit flag
  ```
  First pass is `commit=False` (no write); the guard runs on `dry.snapshots_built`;
  only then does the committing pass run. The extra build pass is acceptable —
  manual, infrequent, dry-run-first operator runs. Valuation gets the identical
  wrapper.
- **fundamentals** (`financial_fundamentals_snapshots.py`): single
  build-then-commit; add `allow_partial: bool = False` to its request.
  **Fail fast before the DART fetch** (so a blocked commit consumes **0**
  budget): right after symbol resolution and the `estimate_only` short-circuit
  (`:210-225`), if `request.commit and not request.allow_partial`, raise
  `PartialCommitBlocked(reason="fundamentals is an incremental backfill; pass --allow-partial to commit")`.
  No coverage math; the `estimate_only` path is unaffected.
- **screener**: no job change. The CLI picks `run_snapshot_build_guarded`
  (default) vs `run_snapshot_build` (`--allow-partial`). The scheduled task
  already uses the guarded runner.

### 3.3 CLI wiring (all four `scripts/build_*.py`)

- Add `--allow-partial` (store_true) with help describing the explicit-partial
  acknowledgment.
- **screener / quote / valuation**: branch on `args.allow_partial` to call the
  plain runner (`run_*_build`) when set, else the guarded wrapper
  (`run_*_build_guarded`).
- **fundamentals**: pass `allow_partial=args.allow_partial` into the request
  (the runner fail-fasts internally).
- Wrap the job call so `PartialCommitBlocked`, `SuspiciousDistributionError`, and
  `InsufficientRowsError` are caught, printed clearly, and the process exits
  non-zero (e.g. `return 2`) without a partial write.

## 4. Data flow

```
build (dry-run-first default) ── --commit ──▶  CLI picks runner per --allow-partial
  ├─ screener  : guarded wrapper (default) | run_snapshot_build (--allow-partial)
  │               guarded → assert_dominant_partition + assert_min_row_count (unchanged)
  ├─ quote/val : *_build_guarded (default)  | *_build (--allow-partial)
  │               guarded → dry pass → assert_min_coverage(built ≥ universe×0.60) → commit pass
  └─ fund      : request.allow_partial; runner fail-fasts (commit & !allow_partial → block, 0 DART)
  → guard passes (or --allow-partial) → _commit_payloads
  → guard blocks → typed exception → CLI prints + exit 2, NO write
```

## 5. Error handling

- Block → typed exception (`PartialCommitBlocked` / screener's existing
  `SuspiciousDistributionError` / `InsufficientRowsError`) → CLI catch → message
  + non-zero exit; **no** commit.
- `universe_count <= 0` → `assert_min_coverage` does not raise (gate disabled) —
  never block on a missing universe count (consistent with PR2a fail-open).
- The short-lived universe-count session is read-only; no broker/order/watch
  touch anywhere in PR2b.

## 6. Testing

| ID | Test | Asserts |
| -- | ---- | ------- |
| **T1** | `assert_min_coverage` pass | `count ≥ ceil(uc*0.6)` → no raise |
| **T2** | `assert_min_coverage` block | `count < floor` → `PartialCommitBlocked` (carries count/uc/ratio) |
| **T3** | `assert_min_coverage` universe 0 | `uc=0` → no raise (gate disabled) |
| **T4** | quote guarded wrapper blocks thin | `run_market_quote_snapshot_build_guarded` with built < floor → `PartialCommitBlocked`, **0 rows written** (the commit pass never runs) |
| **T5** | quote plain runner commits thin | `run_market_quote_snapshot_build(commit=True)` (the `--allow-partial` path) → commits (no guard) |
| **T6** | valuation guarded wrapper / plain runner | as T4/T5 for valuation |
| **T7** | fundamentals job: commit blocked without flag | `commit=True, allow_partial=False` → `PartialCommitBlocked` **before any DART fetch** (spy fetcher not called), no write |
| **T8** | fundamentals job: `--allow-partial` commits | `allow_partial=True` → builds + commits |
| **T9** | CLI routing (screener/quote/valuation) | default `--commit` → calls `run_*_build_guarded`; `--commit --allow-partial` → calls `run_*_build` (assert via monkeypatched spies) |
| **T10** | CLI arg parsing (all 4) | `--allow-partial` sets the flag; default `False` |

Job tests use the existing `bind_job_session` / `db_session` fixtures; guard-unit
tests are pure. No live provider, no network.

## 7. Non-goals / safety

- **No change to `guards.py`** (screener absolute floors + dominant-partition)
  and no change to the read-path resolver (PR2a).
- **No migration**; **no** new config flags (D5); **no** scheduled-task
  registration for quote/valuation/fundamentals.
- **No** broker/order/watch/order-intent/trade-journal mutation. No env/secret
  changes.
- **No production backfill/refresh** — PR2b only governs *how* a commit is
  allowed; running the actual full backfill stays operator-gated (issue
  Non-goals). Once PR2b lands, the operator runbook (PR-C / a follow-up) covers
  the dependency refresh order.

## 8. Acceptance criteria

1. A `--limit 20 --commit` run is **blocked** (no write) for screener (via
   guarded routing), quote, and valuation; fundamentals `--commit` is blocked
   without `--allow-partial`.
2. `--allow-partial` lets an operator commit a legitimate partial/symbol-scoped
   build in every family.
3. The shared quote/valuation bar is `active_universe × 0.60`, derived per market
   from a live `is_active` count; `universe_count == 0` disables the gate.
4. `guards.py` and the PR2a read path are untouched; no migration; no new config
   flags; no broker/order mutation.
5. Tests T1–T10 pass; existing screener/quote/valuation/fundamentals job + CLI
   tests stay green.
