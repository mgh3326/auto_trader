# ROB-426 PR2b — snapshot commit guards (write-path) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Block accidental thin-partition commits across the screener/quote/valuation/fundamentals build CLIs, with an explicit `--allow-partial` escape for legitimate partial backfills.

**Architecture:** A shared `assert_min_coverage` (built rows ≥ `active_universe × 0.60`) guards quote/valuation via two-pass guarded wrappers that mirror the existing screener `run_snapshot_build_guarded`; the screener CLI is routed through that existing guarded runner (unchanged); fundamentals (intentionally incremental, per-symbol-per-period) is exempt from a coverage floor and instead fail-fasts before the DART fetch unless `--allow-partial`. Read-only universe count, no migration, no `guards.py` change, no broker mutation.

**Tech Stack:** Python 3.13, SQLAlchemy async, frozen dataclasses, pytest (`pytest.mark.asyncio` + `db_session`/`bind_job_session` fixtures, monkeypatch), ruff.

**Spec:** `docs/superpowers/specs/2026-06-03-rob-426-pr2b-commit-guards-design.md`

**Branch:** `rob-426-pr2b` (off `origin/main` `a22d4192`; PR2a merged). Spec commit already on it.

---

## File Structure

| File | Create/Modify | Responsibility |
| ---- | ------------- | -------------- |
| `app/services/snapshot_commit_guard.py` | **Create** | `PartialCommitBlocked` + `assert_min_coverage` (+ `_MIN_COMMIT_COVERAGE_RATIO`) |
| `app/jobs/market_quote_snapshots.py` | Modify | Add `run_market_quote_snapshot_build_guarded` two-pass wrapper |
| `app/jobs/market_valuation_snapshots.py` | Modify | Add `run_market_valuation_snapshot_build_guarded` two-pass wrapper |
| `app/jobs/financial_fundamentals_snapshots.py` | Modify | Add `allow_partial` request field + fail-fast guard before DART fetch |
| `scripts/build_market_quote_snapshots.py` | Modify | `--allow-partial` + guarded/plain routing + block→exit 2 |
| `scripts/build_market_valuation_snapshots.py` | Modify | Same |
| `scripts/build_financial_fundamentals_snapshots.py` | Modify | `--allow-partial` → request; block→exit 2 |
| `scripts/build_invest_screener_snapshots.py` | Modify | `--allow-partial` + guarded/plain routing + block→exit 2 |
| `tests/test_snapshot_commit_guard.py` | **Create** | `assert_min_coverage` unit tests |
| `tests/test_snapshot_commit_guard_wiring.py` | **Create** | quote/valuation wrappers + fundamentals fail-fast + CLI routing |

---

## Task 1: Shared guard module

**Files:**
- Create: `app/services/snapshot_commit_guard.py`
- Create: `tests/test_snapshot_commit_guard.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_snapshot_commit_guard.py`:

```python
from __future__ import annotations

import pytest

from app.services.snapshot_commit_guard import (
    PartialCommitBlocked,
    assert_min_coverage,
)


def test_passes_when_count_meets_floor():
    # floor = ceil(100 * 0.60) = 60
    assert_min_coverage(60, 100, market="kr") is None
    assert_min_coverage(99, 100, market="kr") is None


def test_blocks_when_count_below_floor():
    with pytest.raises(PartialCommitBlocked) as exc:
        assert_min_coverage(20, 100, market="kr")
    assert exc.value.count == 20
    assert exc.value.universe_count == 100
    assert exc.value.market == "kr"


def test_universe_zero_disables_gate():
    # never block on a missing universe denominator
    assert_min_coverage(0, 0, market="kr") is None
    assert_min_coverage(5, 0, market="us") is None


def test_custom_ratio():
    # floor = ceil(100 * 0.5) = 50
    assert_min_coverage(50, 100, market="kr", min_ratio=0.5) is None
    with pytest.raises(PartialCommitBlocked):
        assert_min_coverage(49, 100, market="kr", min_ratio=0.5)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_snapshot_commit_guard.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.snapshot_commit_guard`

- [ ] **Step 3: Create the module**

Create `app/services/snapshot_commit_guard.py`:

```python
"""ROB-426 PR2b — shared commit-time coverage guard for snapshot builds.

Blocks committing a thin partition (e.g. a --limit 20 smoke run) to production
unless the operator passes --allow-partial. Distinct from the screener-specific
absolute floors in invest_screener_snapshots/guards.py (which PR2b leaves
unchanged). The denominator is the active universe count
(app.services.invest_screener_snapshots.partition_health.active_universe_count),
computed by the caller and passed in as an int — this module is pure arithmetic.

Threshold is locked here; changing it is a separate telemetry-backed PR.
"""

from __future__ import annotations

import math
from typing import Literal

_MIN_COMMIT_COVERAGE_RATIO = 0.60


class PartialCommitBlocked(RuntimeError):
    """Raised when a commit would persist fewer rows than the coverage floor.

    Carries context for the CLI to print and for Stage-6-style alerting.
    """

    def __init__(
        self,
        message: str,
        *,
        count: int | None = None,
        universe_count: int | None = None,
        min_ratio: float | None = None,
        market: str | None = None,
        metric: str = "rows",
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.count = count
        self.universe_count = universe_count
        self.min_ratio = min_ratio
        self.market = market
        self.metric = metric
        self.reason = reason


def assert_min_coverage(
    count: int,
    universe_count: int,
    *,
    market: Literal["kr", "us"] | str,
    min_ratio: float = _MIN_COMMIT_COVERAGE_RATIO,
    metric: str = "rows",
) -> None:
    """Raise :class:`PartialCommitBlocked` when ``count`` is below the floor.

    floor = ceil(universe_count * min_ratio). When ``universe_count <= 0`` the
    gate is disabled (returns without raising) — never block on a missing
    universe denominator (consistent with PR2a fail-open).
    """
    if universe_count <= 0:
        return
    floor = math.ceil(universe_count * min_ratio)
    if count < floor:
        raise PartialCommitBlocked(
            f"{market} commit blocked: built {count} {metric} < floor {floor} "
            f"({min_ratio:.0%} of active universe {universe_count}); "
            f"pass --allow-partial to commit a partial backfill",
            count=count,
            universe_count=universe_count,
            min_ratio=min_ratio,
            market=market,
            metric=metric,
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_snapshot_commit_guard.py -v`
Expected: PASS

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check app/services/snapshot_commit_guard.py tests/test_snapshot_commit_guard.py
uv run ruff format app/services/snapshot_commit_guard.py tests/test_snapshot_commit_guard.py
git add app/services/snapshot_commit_guard.py tests/test_snapshot_commit_guard.py
git commit -m "feat(ROB-426): shared snapshot commit coverage guard (PR2b)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Quote guarded wrapper + CLI routing

**Files:**
- Modify: `app/jobs/market_quote_snapshots.py` (add wrapper after `run_market_quote_snapshot_build`, ends `:271`)
- Modify: `scripts/build_market_quote_snapshots.py` (`parse_args` + `run`)
- Create: `tests/test_snapshot_commit_guard_wiring.py`

- [ ] **Step 1: Write the failing wrapper tests**

Create `tests/test_snapshot_commit_guard_wiring.py`:

```python
from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import pytest

from app.jobs import market_quote_snapshots as quote_job
from app.services.snapshot_commit_guard import PartialCommitBlocked


def _quote_result(*, market="kr", built, committed):
    return quote_job.MarketQuoteSnapshotBuildResult(
        market=market,
        symbols_resolved=built,
        snapshots_built=built,
        committed=committed,
        batches=1,
        started_at=dt.datetime(2026, 6, 3, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 6, 3, tzinfo=dt.UTC),
    )


@pytest.mark.asyncio
async def test_quote_guarded_wrapper_blocks_thin(monkeypatch):
    calls: list[bool] = []  # records each inner run's commit flag

    async def _fake_run(request):
        calls.append(request.commit)
        return _quote_result(built=20, committed=request.commit)

    monkeypatch.setattr(quote_job, "run_market_quote_snapshot_build", _fake_run)
    monkeypatch.setattr(quote_job, "active_universe_count", AsyncMock(return_value=100))

    req = quote_job.MarketQuoteSnapshotBuildRequest(market="kr", all_symbols=True, commit=True)
    with pytest.raises(PartialCommitBlocked):
        await quote_job.run_market_quote_snapshot_build_guarded(req)
    # only the dry pass ran (commit=False); the commit pass was never reached.
    assert calls == [False]


@pytest.mark.asyncio
async def test_quote_guarded_wrapper_commits_when_healthy(monkeypatch):
    calls: list[bool] = []

    async def _fake_run(request):
        calls.append(request.commit)
        return _quote_result(built=80, committed=request.commit)

    monkeypatch.setattr(quote_job, "run_market_quote_snapshot_build", _fake_run)
    monkeypatch.setattr(quote_job, "active_universe_count", AsyncMock(return_value=100))

    req = quote_job.MarketQuoteSnapshotBuildRequest(market="kr", all_symbols=True, commit=True)
    result = await quote_job.run_market_quote_snapshot_build_guarded(req)
    assert calls == [False, True]  # dry pass, then commit pass
    assert result.committed is True


@pytest.mark.asyncio
async def test_quote_guarded_wrapper_skips_crypto(monkeypatch):
    calls: list[bool] = []

    async def _fake_run(request):
        calls.append(request.commit)
        return _quote_result(market="crypto", built=5, committed=request.commit)

    monkeypatch.setattr(quote_job, "run_market_quote_snapshot_build", _fake_run)
    # active_universe_count must NOT be called for crypto.
    monkeypatch.setattr(
        quote_job, "active_universe_count",
        AsyncMock(side_effect=AssertionError("must not be called for crypto")),
    )

    req = quote_job.MarketQuoteSnapshotBuildRequest(market="crypto", all_symbols=True, commit=True)
    result = await quote_job.run_market_quote_snapshot_build_guarded(req)
    assert calls == [False, True]
    assert result.committed is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_snapshot_commit_guard_wiring.py -k quote -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'run_market_quote_snapshot_build_guarded'`

- [ ] **Step 3: Add the wrapper + imports**

In `app/jobs/market_quote_snapshots.py`, add imports near the top (after line 18):

```python
from dataclasses import replace

from app.services.invest_screener_snapshots.partition_health import (
    active_universe_count,
)
from app.services.snapshot_commit_guard import assert_min_coverage
```

(`replace` may already be importable from `dataclasses`; the existing import is `from dataclasses import dataclass, field` on line 7 — change it to `from dataclasses import dataclass, field, replace`.)

Then append the wrapper at the end of the module (after `run_market_quote_snapshot_build` ends at line 271):

```python
async def run_market_quote_snapshot_build_guarded(
    request: MarketQuoteSnapshotBuildRequest,
) -> MarketQuoteSnapshotBuildResult:
    """Two-pass coverage-guarded commit (ROB-426 PR2b).

    Runs a no-commit pass to count rows, asserts the build covers >= 60% of the
    active KR/US universe, then runs the committing pass. Crypto is skipped (no
    KR/US universe denominator). Raises PartialCommitBlocked on a thin build —
    the committing pass never runs. Callers wanting to bypass the guard call
    run_market_quote_snapshot_build directly (the --allow-partial path).
    """
    dry = await run_market_quote_snapshot_build(replace(request, commit=False))
    if dry.market in ("kr", "us"):
        async with AsyncSessionLocal() as session:
            universe_count = await active_universe_count(session, market=dry.market)
        assert_min_coverage(dry.snapshots_built, universe_count, market=dry.market)
    return await run_market_quote_snapshot_build(request)
```

- [ ] **Step 4: Run the wrapper tests to verify they pass**

Run: `uv run pytest tests/test_snapshot_commit_guard_wiring.py -k quote -v`
Expected: PASS

- [ ] **Step 5: Write the failing CLI test**

Append to `tests/test_snapshot_commit_guard_wiring.py`:

```python
from scripts import build_market_quote_snapshots as quote_cli


def test_quote_cli_allow_partial_arg():
    args = quote_cli.parse_args(["--all", "--commit", "--allow-partial"])
    assert args.allow_partial is True
    assert quote_cli.parse_args(["--all"]).allow_partial is False


@pytest.mark.asyncio
async def test_quote_cli_routes_guarded_by_default(monkeypatch):
    guarded = AsyncMock(return_value=_quote_result(built=3000, committed=True))
    plain = AsyncMock(return_value=_quote_result(built=20, committed=True))
    monkeypatch.setattr(quote_job, "run_market_quote_snapshot_build_guarded", guarded)
    monkeypatch.setattr(quote_job, "run_market_quote_snapshot_build", plain)

    # default commit → guarded
    await quote_cli.run(quote_cli.parse_args(["--all", "--commit"]))
    assert guarded.await_count == 1 and plain.await_count == 0

    guarded.reset_mock()
    plain.reset_mock()
    # --allow-partial commit → plain
    await quote_cli.run(quote_cli.parse_args(["--all", "--commit", "--allow-partial"]))
    assert plain.await_count == 1 and guarded.await_count == 0
```

- [ ] **Step 6: Run to verify it fails**

Run: `uv run pytest tests/test_snapshot_commit_guard_wiring.py -k "quote_cli" -v`
Expected: FAIL — `AttributeError: 'Namespace' object has no attribute 'allow_partial'`

- [ ] **Step 7: Add `--allow-partial` + routing to the quote CLI**

In `scripts/build_market_quote_snapshots.py`, add the argument in `parse_args`
after the `--commit` block (before `args = parser.parse_args(argv)` on line 50):

```python
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Acknowledge and commit a partial/thin build that is below the "
            "coverage floor (skips the commit guard). Use for small "
            "--symbol/--limit backfills."
        ),
    )
```

Replace the `run` body (lines 91-106) with guarded/plain routing + block handling:

```python
async def run(args: argparse.Namespace) -> int:
    from app.jobs import market_quote_snapshots as snapshot_job
    from app.services.snapshot_commit_guard import PartialCommitBlocked

    request = snapshot_job.MarketQuoteSnapshotBuildRequest(
        market=args.market,
        symbols=tuple(args.symbol),
        limit=args.limit,
        all_symbols=args.all,
        batch_size=args.batch_size,
        concurrency=args.concurrency,
        commit=args.commit,
    )
    use_guarded = args.commit and not args.allow_partial
    try:
        if use_guarded:
            result = await snapshot_job.run_market_quote_snapshot_build_guarded(request)
        else:
            result = await snapshot_job.run_market_quote_snapshot_build(request)
    except PartialCommitBlocked as exc:
        print(f"\nCOMMIT BLOCKED: {exc}\n")
        return 2
    _print_result(result)
    return 0
```

- [ ] **Step 8: Run the CLI tests to verify pass**

Run: `uv run pytest tests/test_snapshot_commit_guard_wiring.py -k "quote" -v`
Expected: PASS

- [ ] **Step 9: Lint + commit**

```bash
uv run ruff check app/jobs/market_quote_snapshots.py scripts/build_market_quote_snapshots.py tests/test_snapshot_commit_guard_wiring.py
uv run ruff format app/jobs/market_quote_snapshots.py scripts/build_market_quote_snapshots.py tests/test_snapshot_commit_guard_wiring.py
git add app/jobs/market_quote_snapshots.py scripts/build_market_quote_snapshots.py tests/test_snapshot_commit_guard_wiring.py
git commit -m "feat(ROB-426): quote snapshot commit guard + --allow-partial (PR2b)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Valuation guarded wrapper + CLI routing

**Files:**
- Modify: `app/jobs/market_valuation_snapshots.py` (add wrapper after `run_market_valuation_snapshot_build`)
- Modify: `scripts/build_market_valuation_snapshots.py` (`parse_args` + `run`)
- Modify: `tests/test_snapshot_commit_guard_wiring.py`

- [ ] **Step 1: Write the failing valuation wrapper + CLI tests**

Append to `tests/test_snapshot_commit_guard_wiring.py`:

```python
from app.jobs import market_valuation_snapshots as val_job
from scripts import build_market_valuation_snapshots as val_cli


def _val_result(*, market="kr", built, committed):
    return val_job.MarketValuationSnapshotBuildResult(
        market=market,
        symbols_resolved=built,
        snapshots_built=built,
        committed=committed,
        batches=1,
        started_at=dt.datetime(2026, 6, 3, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 6, 3, tzinfo=dt.UTC),
    )


@pytest.mark.asyncio
async def test_valuation_guarded_wrapper_blocks_thin(monkeypatch):
    calls: list[bool] = []

    async def _fake_run(request):
        calls.append(request.commit)
        return _val_result(built=20, committed=request.commit)

    monkeypatch.setattr(val_job, "run_market_valuation_snapshot_build", _fake_run)
    monkeypatch.setattr(val_job, "active_universe_count", AsyncMock(return_value=100))

    req = val_job.MarketValuationSnapshotBuildRequest(market="kr", all_symbols=True, commit=True)
    with pytest.raises(PartialCommitBlocked):
        await val_job.run_market_valuation_snapshot_build_guarded(req)
    assert calls == [False]


@pytest.mark.asyncio
async def test_valuation_guarded_wrapper_commits_when_healthy(monkeypatch):
    calls: list[bool] = []

    async def _fake_run(request):
        calls.append(request.commit)
        return _val_result(built=80, committed=request.commit)

    monkeypatch.setattr(val_job, "run_market_valuation_snapshot_build", _fake_run)
    monkeypatch.setattr(val_job, "active_universe_count", AsyncMock(return_value=100))

    req = val_job.MarketValuationSnapshotBuildRequest(market="kr", all_symbols=True, commit=True)
    result = await val_job.run_market_valuation_snapshot_build_guarded(req)
    assert calls == [False, True]
    assert result.committed is True


def test_valuation_cli_allow_partial_arg():
    assert val_cli.parse_args(["--all", "--commit", "--allow-partial"]).allow_partial is True
    assert val_cli.parse_args(["--all"]).allow_partial is False


@pytest.mark.asyncio
async def test_valuation_cli_routes_guarded_by_default(monkeypatch):
    guarded = AsyncMock(return_value=_val_result(built=3000, committed=True))
    plain = AsyncMock(return_value=_val_result(built=20, committed=True))
    monkeypatch.setattr(val_job, "run_market_valuation_snapshot_build_guarded", guarded)
    monkeypatch.setattr(val_job, "run_market_valuation_snapshot_build", plain)

    await val_cli.run(val_cli.parse_args(["--all", "--commit"]))
    assert guarded.await_count == 1 and plain.await_count == 0
    guarded.reset_mock(); plain.reset_mock()
    await val_cli.run(val_cli.parse_args(["--all", "--commit", "--allow-partial"]))
    assert plain.await_count == 1 and guarded.await_count == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_snapshot_commit_guard_wiring.py -k "valuation" -v`
Expected: FAIL (wrapper + `allow_partial` arg missing)

- [ ] **Step 3: Add the valuation wrapper + imports**

In `app/jobs/market_valuation_snapshots.py`, change line 7 `from dataclasses import dataclass, field` to `from dataclasses import dataclass, field, replace`, and add after the existing imports (the file imports `AsyncSessionLocal` already; confirm — if not, add `from app.core.db import AsyncSessionLocal`):

```python
from app.services.invest_screener_snapshots.partition_health import (
    active_universe_count,
)
from app.services.snapshot_commit_guard import assert_min_coverage
```

Append the wrapper at the end of the module (after `run_market_valuation_snapshot_build`):

```python
async def run_market_valuation_snapshot_build_guarded(
    request: MarketValuationSnapshotBuildRequest,
) -> MarketValuationSnapshotBuildResult:
    """Two-pass coverage-guarded commit (ROB-426 PR2b). See the quote wrapper."""
    dry = await run_market_valuation_snapshot_build(replace(request, commit=False))
    if dry.market in ("kr", "us"):
        async with AsyncSessionLocal() as session:
            universe_count = await active_universe_count(session, market=dry.market)
        assert_min_coverage(dry.snapshots_built, universe_count, market=dry.market)
    return await run_market_valuation_snapshot_build(request)
```

- [ ] **Step 4: Add `--allow-partial` + routing to the valuation CLI**

In `scripts/build_market_valuation_snapshots.py`, add the `--allow-partial`
argument in `parse_args` (same block as quote Task 2 Step 7) and replace `run`
with the guarded/plain routing + `PartialCommitBlocked` → `return 2` handling
(identical structure to quote Task 2 Step 7, but calling
`run_market_valuation_snapshot_build[_guarded]` and constructing
`MarketValuationSnapshotBuildRequest`). The valuation request has the same fields
as quote except `now` → `today`; do **not** set `today` (leave default).

```python
async def run(args: argparse.Namespace) -> int:
    from app.jobs import market_valuation_snapshots as snapshot_job
    from app.services.snapshot_commit_guard import PartialCommitBlocked

    request = snapshot_job.MarketValuationSnapshotBuildRequest(
        market=args.market,
        symbols=tuple(args.symbol),
        limit=args.limit,
        all_symbols=args.all,
        batch_size=args.batch_size,
        concurrency=args.concurrency,
        commit=args.commit,
    )
    use_guarded = args.commit and not args.allow_partial
    try:
        if use_guarded:
            result = await snapshot_job.run_market_valuation_snapshot_build_guarded(request)
        else:
            result = await snapshot_job.run_market_valuation_snapshot_build(request)
    except PartialCommitBlocked as exc:
        print(f"\nCOMMIT BLOCKED: {exc}\n")
        return 2
    _print_result(result)
    return 0
```

> Implementer note: read `scripts/build_market_valuation_snapshots.py` first to
> confirm the existing `run` signature + request field names (it mirrors the
> quote CLI; `--market` choices may be `kr`/`us` only — if so the crypto branch
> in the wrapper is simply never exercised, which is fine).

- [ ] **Step 5: Run the valuation tests to verify pass**

Run: `uv run pytest tests/test_snapshot_commit_guard_wiring.py -k "valuation" -v`
Expected: PASS

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check app/jobs/market_valuation_snapshots.py scripts/build_market_valuation_snapshots.py tests/test_snapshot_commit_guard_wiring.py
uv run ruff format app/jobs/market_valuation_snapshots.py scripts/build_market_valuation_snapshots.py tests/test_snapshot_commit_guard_wiring.py
git add app/jobs/market_valuation_snapshots.py scripts/build_market_valuation_snapshots.py tests/test_snapshot_commit_guard_wiring.py
git commit -m "feat(ROB-426): valuation snapshot commit guard + --allow-partial (PR2b)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Fundamentals fail-fast guard + CLI

**Files:**
- Modify: `app/jobs/financial_fundamentals_snapshots.py` (request field + fail-fast block after the `estimate_only` short-circuit, before the build at `:230`)
- Modify: `scripts/build_financial_fundamentals_snapshots.py` (`parse_args` + `run`)
- Modify: `tests/test_snapshot_commit_guard_wiring.py`

- [ ] **Step 1: Write the failing fundamentals tests**

Append to `tests/test_snapshot_commit_guard_wiring.py`:

```python
from app.jobs import financial_fundamentals_snapshots as fund_job


@pytest.mark.asyncio
async def test_fundamentals_commit_blocked_without_allow_partial():
    calls: list[str] = []

    async def _spy_fetcher(symbol, *, include_quarterly):
        calls.append(symbol)
        raise AssertionError("fetcher must not run when commit is blocked")

    with pytest.raises(PartialCommitBlocked):
        await fund_job.run_financial_fundamentals_snapshot_build(
            fund_job.FinancialFundamentalsSnapshotBuildRequest(
                market="kr", symbols=("005930",), commit=True, allow_partial=False
            ),
            fetcher=_spy_fetcher,
        )
    assert calls == []  # blocked BEFORE any DART fetch (0 budget)


@pytest.mark.asyncio
async def test_fundamentals_allow_partial_permits_commit(bind_job_session, monkeypatch):
    monkeypatch.setattr(fund_job, "resolve_symbols", _async_return(["005930"]))

    result = await fund_job.run_financial_fundamentals_snapshot_build(
        fund_job.FinancialFundamentalsSnapshotBuildRequest(
            market="kr", symbols=("005930",), commit=True, allow_partial=True
        ),
        fetcher=_fund_fake_fetcher,
    )
    assert result.committed is True
    assert result.snapshots_built >= 1
```

> Implementer note: reuse the existing `bind_job_session`, `_async_return`, and a
> fundamentals fake fetcher from `tests/test_financial_fundamentals_job.py`
> (import them or copy the small `_fake_fetcher` defined there as
> `_fund_fake_fetcher`). The fetcher signature is `(symbol, *, include_quarterly)`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_snapshot_commit_guard_wiring.py -k "fundamentals" -v`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'allow_partial'`

- [ ] **Step 3: Add the request field + fail-fast guard**

In `app/jobs/financial_fundamentals_snapshots.py`, add the field to the request
dataclass (the existing request ends with `estimate_only: bool = False`):

```python
    estimate_only: bool = False
    allow_partial: bool = False
```

Add the import near the top with the other `app.services` imports:

```python
from app.services.snapshot_commit_guard import PartialCommitBlocked
```

Insert the fail-fast guard immediately AFTER the `estimate_only` short-circuit
block (which returns around line 210-225) and BEFORE `reset_request_count()` /
the build call (around `:230`):

```python
    if request.commit and not request.allow_partial:
        raise PartialCommitBlocked(
            "fundamentals commit blocked: fundamentals is an incremental "
            "backfill (DART budget); pass --allow-partial to commit a partial "
            "backfill",
            market=market,
            metric="symbols",
            reason="incremental_backfill",
        )
```

(This runs after symbols are resolved and `estimate_only` has returned, so a
blocked commit consumes **0** DART budget.)

- [ ] **Step 4: Run to verify the block test passes**

Run: `uv run pytest tests/test_snapshot_commit_guard_wiring.py -k "fundamentals_commit_blocked" -v`
Expected: PASS

- [ ] **Step 5: Add `--allow-partial` to the fundamentals CLI**

In `scripts/build_financial_fundamentals_snapshots.py`, add the argument in
`parse_args` (after the `--estimate-only` block):

```python
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Acknowledge and commit a partial fundamentals backfill (required "
            "for any --commit, since fundamentals is incremental by DART budget)."
        ),
    )
```

Pass it into the request in `run` (add after `commit=args.commit,` /
`estimate_only=args.estimate_only,`):

```python
            estimate_only=args.estimate_only,
            allow_partial=args.allow_partial,
```

Wrap the job call in `run` so the block exits non-zero. Change the
`result = await snapshot_job.run_financial_fundamentals_snapshot_build(...)`
call to:

```python
    from app.services.snapshot_commit_guard import PartialCommitBlocked

    try:
        result = await snapshot_job.run_financial_fundamentals_snapshot_build(
            snapshot_job.FinancialFundamentalsSnapshotBuildRequest(
                market=args.market,
                symbols=tuple(args.symbol),
                limit=args.limit,
                all_symbols=args.all,
                include_quarterly=args.include_quarterly,
                concurrency=args.concurrency,
                commit=args.commit,
                estimate_only=args.estimate_only,
                allow_partial=args.allow_partial,
            )
        )
    except PartialCommitBlocked as exc:
        print(f"\nCOMMIT BLOCKED: {exc}\n")
        return 2
    _print_result(result)
    return 0
```

- [ ] **Step 6: Run the fundamentals tests + CLI arg test to verify pass**

Append a CLI arg test to `tests/test_snapshot_commit_guard_wiring.py`:

```python
from scripts import build_financial_fundamentals_snapshots as fund_cli


def test_fundamentals_cli_allow_partial_arg():
    assert fund_cli.parse_args(["--symbol", "005930", "--allow-partial"]).allow_partial is True
    assert fund_cli.parse_args(["--symbol", "005930"]).allow_partial is False
```

Run: `uv run pytest tests/test_snapshot_commit_guard_wiring.py -k "fundamentals" -v`
Expected: PASS

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check app/jobs/financial_fundamentals_snapshots.py scripts/build_financial_fundamentals_snapshots.py tests/test_snapshot_commit_guard_wiring.py
uv run ruff format app/jobs/financial_fundamentals_snapshots.py scripts/build_financial_fundamentals_snapshots.py tests/test_snapshot_commit_guard_wiring.py
git add app/jobs/financial_fundamentals_snapshots.py scripts/build_financial_fundamentals_snapshots.py tests/test_snapshot_commit_guard_wiring.py
git commit -m "feat(ROB-426): fundamentals --allow-partial fail-fast commit gate (PR2b)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Screener CLI routing through the existing guarded runner

**Files:**
- Modify: `scripts/build_invest_screener_snapshots.py` (`parse_args` + `run`, ends `:149+`)
- Modify: `tests/test_snapshot_commit_guard_wiring.py`

- [ ] **Step 1: Write the failing screener routing test**

Append to `tests/test_snapshot_commit_guard_wiring.py`:

```python
from app.jobs import invest_screener_snapshots as screener_job
from scripts import build_invest_screener_snapshots as screener_cli


def _screener_result(*, built, committed):
    return screener_job.SnapshotBuildResult(
        market="kr",
        symbols_resolved=built,
        snapshots_built=built,
        committed=committed,
        batches=1,
        started_at=dt.datetime(2026, 6, 3, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 6, 3, tzinfo=dt.UTC),
    )


def test_screener_cli_allow_partial_arg():
    assert screener_cli.parse_args(["--all", "--commit", "--allow-partial"]).allow_partial is True
    assert screener_cli.parse_args(["--all"]).allow_partial is False


@pytest.mark.asyncio
async def test_screener_cli_routes_guarded_by_default(monkeypatch):
    guarded = AsyncMock(return_value=_screener_result(built=3000, committed=True))
    plain = AsyncMock(return_value=_screener_result(built=20, committed=True))
    monkeypatch.setattr(screener_cli.snapshot_job, "run_snapshot_build_guarded", guarded)
    monkeypatch.setattr(screener_cli.snapshot_job, "run_snapshot_build", plain)

    await screener_cli.run(screener_cli.parse_args(["--all", "--commit"]))
    assert guarded.await_count == 1 and plain.await_count == 0
    guarded.reset_mock(); plain.reset_mock()
    await screener_cli.run(screener_cli.parse_args(["--all", "--commit", "--allow-partial"]))
    assert plain.await_count == 1 and guarded.await_count == 0
```

> Implementer note: confirm the screener result class name is `SnapshotBuildResult`
> and its constructor fields by reading `app/jobs/invest_screener_snapshots.py`
> (the result built around `:259-284`). Confirm `scripts/build_invest_screener_snapshots.py`
> imports the job module as `snapshot_job` (it references `snapshot_job.run_snapshot_build`
> at `:138`). Adjust the monkeypatch target / result fields to match.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_snapshot_commit_guard_wiring.py -k "screener" -v`
Expected: FAIL — `allow_partial` arg missing / wrong routing

- [ ] **Step 3: Add `--allow-partial` + routing to the screener CLI**

In `scripts/build_invest_screener_snapshots.py`, add the `--allow-partial`
argument in `parse_args` (after the `--commit` block):

```python
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Acknowledge and commit a partial/thin screener build, bypassing the "
            "row-count + dominant-partition guards (for small --symbol/--limit "
            "backfills)."
        ),
    )
```

Replace the runner call in `run` (line 138) so it routes through the guarded
wrapper by default:

```python
async def run(args: argparse.Namespace) -> int:
    from app.services.invest_screener_snapshots.guards import (
        InsufficientRowsError,
        SuspiciousDistributionError,
    )

    request = snapshot_job.SnapshotBuildRequest(
        market=args.market,
        symbols=tuple(args.symbol),
        limit=args.limit,
        all_symbols=args.all,
        batch_size=args.batch_size,
        concurrency=args.concurrency,
        commit=args.commit,
        common_stocks_only=args.common_stocks_only,
    )
    use_guarded = args.commit and not args.allow_partial
    try:
        if use_guarded:
            result = await snapshot_job.run_snapshot_build_guarded(request)
        else:
            result = await snapshot_job.run_snapshot_build(request)
    except (SuspiciousDistributionError, InsufficientRowsError) as exc:
        print(f"\nCOMMIT BLOCKED: {exc}\n")
        return 2
```

(Keep the existing `_print_result(result)` / `return 0` tail that follows the
original `run_snapshot_build` call at lines 149+.)

> Implementer note: read `scripts/build_invest_screener_snapshots.py:137-152` to
> preserve the exact tail after the runner call (the `_print_result(result)` and
> `return 0`). Only the runner selection + try/except is new.

- [ ] **Step 4: Run the screener tests to verify pass**

Run: `uv run pytest tests/test_snapshot_commit_guard_wiring.py -k "screener" -v`
Expected: PASS

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check scripts/build_invest_screener_snapshots.py tests/test_snapshot_commit_guard_wiring.py
uv run ruff format scripts/build_invest_screener_snapshots.py tests/test_snapshot_commit_guard_wiring.py
git add scripts/build_invest_screener_snapshots.py tests/test_snapshot_commit_guard_wiring.py
git commit -m "feat(ROB-426): route screener manual CLI through guarded runner (PR2b)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full PR2b + adjacent test surface**

```bash
uv run pytest \
  tests/test_snapshot_commit_guard.py \
  tests/test_snapshot_commit_guard_wiring.py \
  tests/test_financial_fundamentals_job.py \
  tests/test_build_financial_fundamentals_cli.py \
  tests/test_build_invest_screener_snapshots_cli.py \
  -q
```

Expected: ALL PASS. (Also run any existing `test_build_market_quote*`/`test_build_market_valuation*` CLI tests if present — `ls tests/ | grep -E "market_quote|market_valuation"`.)

- [ ] **Step 2: Lint + format (CI scope is `app/ tests/`)**

```bash
uv run ruff check app/services/snapshot_commit_guard.py app/jobs/ scripts/ tests/test_snapshot_commit_guard.py tests/test_snapshot_commit_guard_wiring.py
uv run ruff format --check app/ tests/
```

Expected: no errors. (Run `ruff format` over any flagged file. Note: CI checks `app/ tests/` only, not `scripts/`, but format scripts anyway for hygiene.)

- [ ] **Step 3: Confirm no out-of-scope changes**

```bash
git status --short && git diff --stat origin/main..HEAD
```

Expected: only `snapshot_commit_guard.py`, the 3 job files, the 4 build scripts, and the 2 test files; **no** `alembic/versions/` change; **no** edit to `app/services/invest_screener_snapshots/guards.py` or the PR2a read path; no broker/order/watch/config edits.

- [ ] **Step 4: Sanity-check a blocked commit end-to-end (no DB write)**

```bash
uv run python -m scripts.build_market_quote_snapshots --limit 20 --commit
```

Expected: prints `COMMIT BLOCKED: kr commit blocked: built ... < floor ...` and exits non-zero, **without** writing (if the universe count is unavailable in the dev shell the guard is disabled and it may commit — that is the documented `universe_count <= 0` fail-open; prefer running where the KR universe table is populated, or trust the unit tests).

---

## Self-Review

**1. Spec coverage:**
- Spec §3.1 (`PartialCommitBlocked` + `assert_min_coverage` + ratio) → Task 1.
- §3.2 quote/valuation two-pass guarded wrappers → Tasks 2, 3. fundamentals fail-fast → Task 4. screener no job change → Task 5 (CLI only).
- §3.3 CLI `--allow-partial` + guarded/plain routing (screener/quote/valuation) + request flag (fundamentals) + block→exit 2 → Tasks 2-5.
- §6 tests T1-T3 → Task 1; T4/T5 → Task 2; T6 → Task 3; T7/T8 → Task 4; T9 → Tasks 2/3/5; T10 → Tasks 2-5 arg tests.
- §7 non-goals / §8 acceptance → enforced in Task 6 Steps 3-4.

**Gap noted & accepted:** the spec's crypto exclusion is implemented in the
quote/valuation wrappers (`if dry.market in ("kr","us")`) and tested
(`test_quote_guarded_wrapper_skips_crypto`); valuation crypto is only exercised
if its CLI allows `--market crypto` (it may not — harmless either way).

**2. Placeholder scan:** No TBD/TODO. Implementer notes instruct confirming exact
existing names (valuation `run` body, screener `SnapshotBuildResult` fields,
`bind_job_session`/`_async_return` reuse) before editing — verification steps,
not placeholders; transformation code is fully shown.

**3. Type consistency:** `assert_min_coverage(count, universe_count, *, market, min_ratio=0.60, metric="rows")` and `PartialCommitBlocked(message, *, count, universe_count, min_ratio, market, metric, reason)` are used identically across Tasks 1-5. Wrapper names `run_market_quote_snapshot_build_guarded` / `run_market_valuation_snapshot_build_guarded` and the existing `run_snapshot_build_guarded` are referenced consistently in jobs, CLIs, and tests. Request field `allow_partial: bool = False` (fundamentals) matches its test + CLI usage.
