# ROB-426 PR1 — fundamentals JSONB safety + DART estimate-only — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make fundamentals `raw_payload` strict-JSON/`jsonb` safe (non-finite floats → null) and add a true no-fetch `--estimate-only` DART preflight while making `--dry-run`'s budget cost explicit.

**Architecture:** Relocate the proven recursive `sanitize_non_finite` helper from a report-citation schema module to a neutral `app/core/json_safe.py` (re-exported for back-compat), apply it at the two fundamentals `raw_payload` build sites, and thread an `estimate_only` flag through the build request/job/CLI so the job short-circuits before any DART fetch. All changes are additive; no migration, no broker/order/watch mutation.

**Tech Stack:** Python 3.13, dataclasses, pandas, SQLAlchemy (async), pytest (`pytest.mark.asyncio`), ruff.

**Spec:** `docs/superpowers/specs/2026-06-03-rob-426-pr1-fundamentals-jsonb-dart-estimate-design.md`

**Branch:** work continues on `rob-426` (already at `origin/main`, clean; spec committed at `d1db1595`). PR1 opens from `rob-426` → `main`.

---

## File Structure

| File | Create/Modify | Responsibility |
| ---- | ------------- | -------------- |
| `app/core/json_safe.py` | **Create** | Home of `sanitize_non_finite` — shared JSON-safety helper for snapshot/report payload writers |
| `app/schemas/validated_run_card.py` | Modify | Drop local `sanitize_non_finite` def + unused `import math`; re-import from `app.core.json_safe` (back-compat) |
| `app/services/financial_fundamentals_snapshots/builder.py` | Modify | Wrap the two `raw_payload` dicts with `sanitize_non_finite` before redaction |
| `app/jobs/financial_fundamentals_snapshots.py` | Modify | Add `estimate_only` request field + `projected_requests` result field + no-fetch short-circuit |
| `scripts/build_financial_fundamentals_snapshots.py` | Modify | Add `--estimate-only` flag (mutually exclusive with `--commit`), dry-run cost transparency, estimate-only print branch |
| `tests/test_json_safe.py` | **Create** | Unit + re-export-identity tests for the relocated helper |
| `tests/test_financial_fundamentals_jsonb_safety.py` | **Create** | Regression: builder produces `jsonb`-safe `raw_payload` from non-finite frames |
| `tests/test_financial_fundamentals_job.py` | Modify | Add estimate-only no-fetch/no-DB/no-commit test |
| `tests/test_build_financial_fundamentals_cli.py` | Modify | Add `--estimate-only` flag + mutual-exclusion tests |

---

## Task 1: Relocate `sanitize_non_finite` to `app/core/json_safe.py`

**Files:**
- Create: `app/core/json_safe.py`
- Create: `tests/test_json_safe.py`
- Modify: `app/schemas/validated_run_card.py:35-62`

- [ ] **Step 1: Write the failing test**

Create `tests/test_json_safe.py`:

```python
from __future__ import annotations

import json
import math

from app.core.json_safe import sanitize_non_finite


def test_sanitize_non_finite_recursive_and_strict_json_safe():
    raw = {
        "pf": float("inf"),
        "neg": float("-inf"),
        "nan": float("nan"),
        "finite": 1.5,
        "ints": [1, float("inf"), 3],
        "nested": {"a": float("nan"), "b": "ok", "c": 0.0},
        "flag": True,
        "text": "Infinity is a string here",
    }
    out = sanitize_non_finite(raw)
    assert out["pf"] is None
    assert out["neg"] is None
    assert out["nan"] is None
    assert out["finite"] == 1.5
    assert out["ints"] == [1, None, 3]
    assert out["nested"] == {"a": None, "b": "ok", "c": 0.0}
    assert out["flag"] is True  # bool not coerced
    assert out["text"] == "Infinity is a string here"
    json.dumps(out, allow_nan=False)  # raises if any non-finite remains


def test_sanitize_non_finite_does_not_mutate_input():
    raw = {"pf": float("inf")}
    sanitize_non_finite(raw)
    assert math.isinf(raw["pf"])


def test_validated_run_card_reexports_sanitize_non_finite():
    from app.schemas.validated_run_card import sanitize_non_finite as reexported

    assert reexported is sanitize_non_finite
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_json_safe.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.json_safe'`

- [ ] **Step 3: Create the new module**

Create `app/core/json_safe.py`:

```python
"""JSON-safety helpers shared across snapshot/report payload writers.

Houses :func:`sanitize_non_finite`, relocated here from
``app/schemas/validated_run_card.py`` (ROB-329) so non-schema callers — e.g.
``financial_fundamentals_snapshots`` (ROB-426) — can reuse it without depending
on a report-citation schema module. ``validated_run_card`` re-exports it for
back-compat.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def sanitize_non_finite(value: Any) -> Any:
    """Recursively replace non-finite floats (``inf``/``-inf``/``nan``) with
    ``None`` so the result is strict-JSON / Postgres-jsonb / JS-JSON.parse
    safe. Returns a new structure; the input is not mutated. Booleans and
    integers are left untouched (``bool`` is intentionally not treated as a
    float here)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {k: sanitize_non_finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_non_finite(v) for v in value]
    return value
```

- [ ] **Step 4: Re-point `validated_run_card.py` at the new module**

In `app/schemas/validated_run_card.py`, change the import block (current lines 35-39):

```python
import math
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
```

to (remove now-unused `import math`; add the core import — `Mapping`/`Any` stay, they are still used elsewhere in the file):

```python
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.json_safe import sanitize_non_finite
```

Then delete the local function definition (current lines 48-62), i.e. remove exactly:

```python
def sanitize_non_finite(value: Any) -> Any:
    """Recursively replace non-finite floats (``inf``/``-inf``/``nan``) with
    ``None`` so the result is strict-JSON / Postgres-jsonb / JS-JSON.parse
    safe. Returns a new structure; the input is not mutated. Booleans and
    integers are left untouched (``bool`` is intentionally not treated as a
    float here)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {k: sanitize_non_finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_non_finite(v) for v in value]
    return value
```

(The blank lines / `RUN_CARD_SCHEMA = ...` constants below it stay as-is.)

- [ ] **Step 5: Run the new test + the existing run-card test to verify both pass**

Run: `uv run pytest tests/test_json_safe.py tests/test_validated_run_card_citation.py -v`
Expected: PASS (new module tests pass; ROB-329 tests still green via re-export)

- [ ] **Step 6: Lint to confirm no unused import remains**

Run: `uv run ruff check app/schemas/validated_run_card.py app/core/json_safe.py tests/test_json_safe.py`
Expected: no errors (in particular no `F401` for `math` in `validated_run_card.py`)

- [ ] **Step 7: Commit**

```bash
git add app/core/json_safe.py app/schemas/validated_run_card.py tests/test_json_safe.py
git commit -m "refactor(ROB-426): relocate sanitize_non_finite to app/core/json_safe (re-export)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Sanitize fundamentals `raw_payload` (JSONB safety)

**Files:**
- Create: `tests/test_financial_fundamentals_jsonb_safety.py`
- Modify: `app/services/financial_fundamentals_snapshots/builder.py:18` (import), `:264` + `:314` (both `raw_payload=` lines)

- [ ] **Step 1: Write the failing test**

Create `tests/test_financial_fundamentals_jsonb_safety.py`:

```python
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import pytest

from app.services.financial_fundamentals_snapshots.builder import (
    RawAnnualFiling,
    RawFundamentalsBundle,
    build_financial_fundamentals_for_symbols,
)


async def _nan_fetcher(
    symbol: str, *, include_quarterly: bool
) -> RawFundamentalsBundle:
    # DART finstate_all frames carry non-finite numeric cells for missing
    # divisions/periods; DataFrame.to_dict(orient="records") preserves them as
    # float('nan')/float('inf'), which Postgres jsonb rejects.
    df = pd.DataFrame(
        [
            {
                "account_id": "ifrs-full_Revenue",
                "account_nm": "매출액",
                "sj_div": "IS",
                "thstrm_amount": "1,000",
                "frgn_amount": float("nan"),
            },
            {
                "account_id": "ifrs-full_ProfitLoss",
                "account_nm": "당기순이익",
                "sj_div": "CIS",
                "thstrm_amount": "100",
                "frgn_amount": float("inf"),
            },
        ]
    )
    return RawFundamentalsBundle(
        symbol=symbol,
        currency="KRW",
        annual=(
            RawAnnualFiling(bsns_year=2024, rcept_no="r1", income_statement=df),
        ),
        quarterly=(),
        filing_dates={"r1": dt.date(2025, 3, 20)},
    )


@pytest.mark.asyncio
async def test_raw_payload_is_strict_json_safe_when_frame_has_non_finite():
    result = await build_financial_fundamentals_for_symbols(
        market="kr",
        symbols=["005930"],
        collected_at=dt.datetime(2026, 6, 3, tzinfo=dt.UTC),
        fetcher=_nan_fetcher,
    )
    assert result.payloads
    for p in result.payloads:
        # Postgres-jsonb / JS JSON.parse contract: rejects NaN/Infinity.
        json.dumps(p.raw_payload, allow_nan=False)
        records = p.raw_payload["income_statement"]
        assert any("frgn_amount" in r for r in records)
        assert all(r.get("frgn_amount") is None for r in records)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_financial_fundamentals_jsonb_safety.py -v`
Expected: FAIL — `ValueError: Out of range float values are not JSON compliant` raised by `json.dumps(..., allow_nan=False)` (NaN/inf still present in `raw_payload`).

- [ ] **Step 3: Add the import in the builder**

In `app/services/financial_fundamentals_snapshots/builder.py`, after line 18 (`from app.services.market_quote_snapshots.builder import redact_sensitive_payload`), add:

```python
from app.core.json_safe import sanitize_non_finite
```

(Place it with the other `app.` imports; ruff's import sorter will order `app.core` before `app.services` — run `uv run ruff check --fix` in Step 5 if needed.)

- [ ] **Step 4: Wrap both `raw_payload` build sites**

In the same file, both the annual (line 264) and quarterly (line 314) builders contain the identical line:

```python
        raw_payload=redact_sensitive_payload(raw),
```

Change **both** occurrences to:

```python
        raw_payload=redact_sensitive_payload(sanitize_non_finite(raw)),
```

- [ ] **Step 5: Run test + lint to verify pass**

Run: `uv run pytest tests/test_financial_fundamentals_jsonb_safety.py -v && uv run ruff check app/services/financial_fundamentals_snapshots/builder.py`
Expected: PASS, no lint errors.

- [ ] **Step 6: Confirm no regression in existing builder tests**

Run: `uv run pytest tests/test_financial_fundamentals_builder_orchestration.py tests/test_financial_fundamentals_builder_parse.py -v`
Expected: PASS (existing payloads unaffected; `raw_payload` for finite data is unchanged because `sanitize_non_finite` is a no-op on finite values).

- [ ] **Step 7: Commit**

```bash
git add tests/test_financial_fundamentals_jsonb_safety.py app/services/financial_fundamentals_snapshots/builder.py
git commit -m "fix(ROB-426): sanitize non-finite floats in fundamentals raw_payload (jsonb-safe)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Estimate-only no-fetch short-circuit (request/result/job)

**Files:**
- Modify: `app/jobs/financial_fundamentals_snapshots.py:25-34` (request), `:49-59` (result), `:199-208` (job short-circuit)
- Modify: `tests/test_financial_fundamentals_job.py` (append test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_financial_fundamentals_job.py`:

```python
@pytest.mark.asyncio
async def test_estimate_only_does_not_fetch_or_commit():
    # No bind_job_session fixture on purpose: estimate-only must short-circuit
    # BEFORE any AsyncSessionLocal use, so this test proves it never touches DB.
    calls: list[str] = []

    async def _spy_fetcher(symbol: str, *, include_quarterly: bool):
        calls.append(symbol)
        raise AssertionError("fetcher must not be called in estimate-only mode")

    result = await job.run_financial_fundamentals_snapshot_build(
        job.FinancialFundamentalsSnapshotBuildRequest(
            market="kr",
            symbols=("005930",),
            estimate_only=True,
            include_quarterly=False,
        ),
        fetcher=_spy_fetcher,
    )
    assert calls == []
    assert result.projected_requests == 11  # 1 symbol * 11 (annual-only)
    assert result.committed is False
    assert result.snapshots_built == 0
    assert any("estimate-only" in w for w in result.warnings)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_financial_fundamentals_job.py::test_estimate_only_does_not_fetch_or_commit -v`
Expected: FAIL — `TypeError: ... got an unexpected keyword argument 'estimate_only'` (request field does not exist yet).

- [ ] **Step 3: Add the request field**

In `app/jobs/financial_fundamentals_snapshots.py`, the request dataclass (lines 25-34) currently ends:

```python
@dataclass(frozen=True)
class FinancialFundamentalsSnapshotBuildRequest:
    market: str = "kr"
    symbols: tuple[str, ...] = ()
    limit: int | None = 20
    all_symbols: bool = False
    include_quarterly: bool = False
    concurrency: int = 4
    commit: bool = False
    collected_at: dt.datetime | None = None
```

Add a trailing field:

```python
    collected_at: dt.datetime | None = None
    estimate_only: bool = False
```

- [ ] **Step 4: Add the result field**

The result dataclass (lines 49-59) currently ends with `warnings`:

```python
    idempotency: dict[str, int] = field(default_factory=dict)
    samples: tuple[FinancialFundamentalsSnapshotSample, ...] = ()
    warnings: tuple[str, ...] = ()
```

Add a trailing field:

```python
    warnings: tuple[str, ...] = ()
    projected_requests: int | None = None
```

- [ ] **Step 5: Add the short-circuit in the job**

In `run_financial_fundamentals_snapshot_build`, immediately after the projected-logging block (the `logger.info("Projected DART requests ...", ...)` call ending at line 206) and **before** `reset_request_count()` (line 208), insert:

```python
    if request.estimate_only:
        finished_at = dt.datetime.now(dt.UTC)
        return FinancialFundamentalsSnapshotBuildResult(
            market=market,
            symbols_resolved=len(symbols),
            snapshots_built=0,
            committed=False,
            started_at=started_at,
            finished_at=finished_at,
            idempotency={"wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": 0},
            warnings=(
                f"estimate-only: projected {projected} DART requests; "
                "no fetch performed",
            ),
            projected_requests=projected,
        )
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_financial_fundamentals_job.py::test_estimate_only_does_not_fetch_or_commit -v`
Expected: PASS

- [ ] **Step 7: Confirm no regression in the rest of the job tests**

Run: `uv run pytest tests/test_financial_fundamentals_job.py -v`
Expected: PASS (existing dry-run/commit/budget tests unaffected — `estimate_only` defaults to `False`).

- [ ] **Step 8: Commit**

```bash
git add app/jobs/financial_fundamentals_snapshots.py tests/test_financial_fundamentals_job.py
git commit -m "feat(ROB-426): estimate-only no-fetch preflight for fundamentals DART build

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: CLI `--estimate-only` flag + dry-run cost transparency

**Files:**
- Modify: `scripts/build_financial_fundamentals_snapshots.py` (arg, mutual-exclusion, help text, `run()` print + request kwarg, `_print_result` branch)
- Modify: `tests/test_build_financial_fundamentals_cli.py` (append tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_build_financial_fundamentals_cli.py`:

```python
def test_estimate_only_sets_flag():
    args = parse_args(["--symbol", "005930", "--estimate-only"])
    assert args.estimate_only is True
    assert args.commit is False


def test_estimate_only_mutually_exclusive_with_commit():
    with pytest.raises(SystemExit):
        parse_args(["--symbol", "005930", "--estimate-only", "--commit"])


def test_estimate_only_defaults_false():
    args = parse_args(["--symbol", "005930"])
    assert args.estimate_only is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_build_financial_fundamentals_cli.py -v`
Expected: FAIL — `AttributeError: 'Namespace' object has no attribute 'estimate_only'`.

- [ ] **Step 3: Add the `--estimate-only` argument + mutual exclusion**

In `scripts/build_financial_fundamentals_snapshots.py`, add this argument inside `parse_args` (e.g. directly after the `--commit` argument block, before `args = parser.parse_args(argv)` on line 51):

```python
    parser.add_argument(
        "--estimate-only",
        action="store_true",
        help=(
            "Print the projected DART request count and exit WITHOUT fetching "
            "(0 budget consumed). Mutually exclusive with --commit. Contrast "
            "with --dry-run, which fetches to validate and DOES consume budget."
        ),
    )
```

Then add the mutual-exclusion check right after the existing `--all`/`--symbol` check (after line 53):

```python
    if args.estimate_only and args.commit:
        parser.error("--estimate-only is mutually exclusive with --commit")
```

- [ ] **Step 4: Update the `--commit` help text for dry-run transparency**

Change the `--commit` argument's `help` (line 49) from:

```python
        help="Actually write to the database. Default is --dry-run/no writes.",
```

to:

```python
        help=(
            "Actually write to the database (fetches from DART). Default is "
            "--dry-run: fetch-validate, no writes (still consumes DART budget). "
            "Use --estimate-only for a no-fetch projection."
        ),
```

- [ ] **Step 5: Make `run()` cost-explicit and thread the flag**

In `run()`, replace the projected-print block (lines 96-100):

```python
    projected = len(symbols) * (41 if args.include_quarterly else 11)
    budget = settings.opendart_daily_request_budget
    print(
        f"Projected DART requests for {len(symbols)} symbols: {projected} (daily budget: {budget})"
    )
```

with:

```python
    projected = len(symbols) * (41 if args.include_quarterly else 11)
    budget = settings.opendart_daily_request_budget
    if args.estimate_only:
        print(
            f"--estimate-only: projected {projected} DART requests for "
            f"{len(symbols)} symbols (daily budget: {budget}); no fetch performed."
        )
    else:
        print(
            f"Projected DART requests for {len(symbols)} symbols: {projected} "
            f"(daily budget: {budget}). NOTE: --dry-run still fetches from DART "
            f"and consumes ~{projected} requests."
        )
```

Then add `estimate_only=args.estimate_only,` to the request constructed just below (the `FinancialFundamentalsSnapshotBuildRequest(...)` call, after `commit=args.commit,` on line 110):

```python
            commit=args.commit,
            estimate_only=args.estimate_only,
```

- [ ] **Step 6: Add an estimate-only branch to `_print_result`**

In `_print_result` (lines 62-82), add this early-return at the very top of the function body (before the existing `print(f"\nbuilt ...")`):

```python
def _print_result(result) -> None:
    if result.projected_requests is not None:
        print(
            f"\n--estimate-only: projected {result.projected_requests} DART "
            f"requests for {result.symbols_resolved} {result.market.upper()} "
            f"symbols; no fetch, no rows written.\n"
        )
        for warning in result.warnings:
            print(f"  - {warning}")
        return
    print(
        f"\nbuilt {result.snapshots_built} fundamentals snapshots "
        f"for {result.symbols_resolved} {result.market.upper()} symbols "
        f"(dry_run={not result.committed}):"
    )
```

(The rest of `_print_result` is unchanged. `projected_requests` is `None` for every non-estimate path, so existing output is untouched.)

- [ ] **Step 7: Run tests + lint to verify pass**

Run: `uv run pytest tests/test_build_financial_fundamentals_cli.py -v && uv run ruff check scripts/build_financial_fundamentals_snapshots.py`
Expected: PASS, no lint errors.

- [ ] **Step 8: Commit**

```bash
git add scripts/build_financial_fundamentals_snapshots.py tests/test_build_financial_fundamentals_cli.py
git commit -m "feat(ROB-426): --estimate-only CLI flag + dry-run budget-cost transparency

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full PR1 test surface**

Run:

```bash
uv run pytest \
  tests/test_json_safe.py \
  tests/test_validated_run_card_citation.py \
  tests/test_financial_fundamentals_jsonb_safety.py \
  tests/test_financial_fundamentals_builder_orchestration.py \
  tests/test_financial_fundamentals_builder_parse.py \
  tests/test_financial_fundamentals_job.py \
  tests/test_build_financial_fundamentals_cli.py \
  -v
```

Expected: ALL PASS.

- [ ] **Step 2: Run the touched-file lint + format check**

Run:

```bash
uv run ruff check app/core/json_safe.py app/schemas/validated_run_card.py \
  app/services/financial_fundamentals_snapshots/builder.py \
  app/jobs/financial_fundamentals_snapshots.py \
  scripts/build_financial_fundamentals_snapshots.py tests/test_json_safe.py \
  tests/test_financial_fundamentals_jsonb_safety.py
uv run ruff format --check app/ tests/ scripts/
```

Expected: no errors. (CI runs `ruff check` **and** `ruff format --check` over `app/`, `tests/`, and `scripts/` — run both to avoid the lint-only-app pitfall noted in prior PRs.)

- [ ] **Step 3: Sanity-check the CLI end-to-end estimate-only path (no DB, no fetch)**

Run:

```bash
uv run python -m scripts.build_financial_fundamentals_snapshots --symbol 005930 --estimate-only
```

Expected: prints `--estimate-only: projected 11 DART requests ... no fetch performed.` and exits 0 without any DART call or DB write. (If credentials/DB are absent in the dev shell this still succeeds because estimate-only never fetches and never opens a session.)

- [ ] **Step 4: Confirm no migration / no stray changes**

Run: `git status --short && git log --oneline origin/main..HEAD`
Expected: only the four feature commits (Tasks 1-4) above the spec commit; no files under `alembic/versions/`; no changes to broker/order/watch modules.

---

## Self-Review

**1. Spec coverage** — every PR1 spec section maps to a task:
- Spec §3.1 (relocate + re-export) → Task 1.
- Spec §3.2 (apply sanitize at both builder sites) → Task 2.
- Spec §3.3 (estimate_only request/result + job short-circuit) → Task 3.
- Spec §3.4 (CLI `--estimate-only`, mutual exclusion, dry-run transparency, `_print_result`) → Task 4.
- Spec §6 tests: T1 → Task 2; T1b/T5 → Task 1 (`tests/test_json_safe.py` + existing `test_validated_run_card_citation.py`); T2 → Task 3; T3 → existing `test_dry_run_builds_but_writes_nothing` (kept green in Task 3 Step 7 / Task 5); T4 → Task 4.
- Spec §8 acceptance criteria 1-5 → covered by Tasks 2,3,4,1 and verified in Task 5.

**2. Placeholder scan** — no TBD/TODO/"handle edge cases"; every code step shows complete code and exact commands.

**3. Type consistency** — `sanitize_non_finite` signature identical across the new module, re-export, and call sites. New dataclass fields `estimate_only: bool` (request) and `projected_requests: int | None` (result) are referenced consistently in the job short-circuit, the CLI request kwarg, `_print_result`, and the tests. `RawFundamentalsBundle`/`RawAnnualFiling` kwargs (`symbol`, `currency`, `annual`, `quarterly`, `filing_dates`, `bsns_year`, `rcept_no`, `income_statement`) match the existing builder/job test usage. The fetcher signature `(symbol, *, include_quarterly)` matches existing fakes.

**Deferred (recorded, not in PR1):** the same latent non-finite bug in `market_quote_snapshots` / `market_valuation_snapshots` / `investor_flow` / crypto builders that share `redact_sensitive_payload` — picked up in PR2/PR3 via `app.core.json_safe.sanitize_non_finite`.
