# ROB-352 Slice C — candidate_universe collector hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two contract-hygiene defects in the snapshot-backed `candidate_universe` collector — duplicate symbols and silent truncation — without touching candidate strategy (ranking/filtering/classification stays with ROB-346).

**Architecture:** Two localized changes in `app/services/action_report/snapshot_backed/collectors/candidate_universe.py`: (1) an order-preserving `_dedupe_rows` helper applied to fetched screener rows before evidence is built (equity keys on `to_db_symbol`, crypto on raw symbol); (2) two additive payload/coverage fields `universe_count` + `capped` derived from the freshness counts the collector already has. No migration, no new dependency, no broker/order/watch mutation.

**Tech Stack:** Python 3.13, pytest against the real-Postgres `db_session` fixture + hand-rolled fake repositories (existing test style). `uv run pytest ...`. Spec: `docs/superpowers/specs/2026-05-29-rob-352-slice-c-candidate-universe-hygiene-design.md`. Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## File Structure

| File | Change |
|------|--------|
| `app/services/action_report/snapshot_backed/collectors/candidate_universe.py` | `_dedupe_rows` helper; dedupe in `_collect_equity`/`_collect_crypto`; `universe_count`+`capped` in `_build_candidate_result` |
| `tests/services/action_report/test_candidate_universe_collector_evidence.py` | dedup test + cap-surfacing test |

---

## Task C1: Dedupe candidates by normalized symbol

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/candidate_universe.py`
- Test: `tests/services/action_report/test_candidate_universe_collector_evidence.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/action_report/test_candidate_universe_collector_evidence.py`:

```python
@pytest.mark.asyncio
async def test_equity_collector_dedupes_symbol_format_variants(db_session):
    """ROB-352 Slice C — BRK.B / BRK-B collapse to one candidate; ranks contiguous."""

    class _DupRepo(_FakeEquityRepository):
        def __init__(self) -> None:
            super().__init__()
            self.rows = [
                _EquityRow(symbol="BRK.B", change_rate=Decimal("9.0")),
                _EquityRow(symbol="BRK-B", change_rate=Decimal("8.5")),
                _EquityRow(symbol="AAPL", change_rate=Decimal("8.0")),
            ]

    repo = _DupRepo()
    collector = CandidateUniverseSnapshotCollector(db_session, equity_repository=repo)
    results = await collector.collect(
        CollectorRequest(
            market="us",
            account_scope=None,
            symbols=[],
            candidate_limit=10,
            policy_snapshot={},
        )
    )
    payload = results[0].payload_json
    symbols = [c["symbol"] for c in payload["candidates"]]
    # BRK.B and BRK-B normalize to the same DB symbol → one survives (first/highest).
    assert symbols == ["BRK.B", "AAPL"]
    assert payload["candidates"][0]["candidate_rank"] == 1
    assert payload["candidates"][1]["candidate_rank"] == 2
    assert results[0].coverage_json["candidate_count"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py -k dedupes -v`
Expected: FAIL — both `BRK.B` and `BRK-B` present (`symbols == ["BRK.B", "BRK-B", "AAPL"]`), `candidate_count == 3`.

- [ ] **Step 3: Add the dedupe helper + import**

In `candidate_universe.py`, add the import near the other `app.` imports (after the `from app.models...` block, around line 20):

```python
from app.core.symbol import to_db_symbol
```

Add a module-level helper after `_candidate_limit` (around line 65):

```python
def _dedupe_rows(rows: list[Any], *, key: Any) -> list[Any]:
    """Order-preserving dedupe on ``key(row)``.

    ROB-352 Slice C — screener rows can repeat one instrument under symbol
    format variants (BRK.B / BRK-B / BRK/B). Rows arrive ordered by
    ``change_rate DESC``, so keeping the first occurrence keeps the
    highest-ranked one. This is hygiene only — no ranking/filter changes.
    """
    seen: set[Any] = set()
    out: list[Any] = []
    for row in rows:
        k = key(row)
        if k in seen:
            continue
        seen.add(k)
        out.append(row)
    return out
```

- [ ] **Step 4: Apply dedupe in both branches**

In `_collect_equity`, replace the `rows = await self._equity_repo.list_top_candidates(...)` assignment's downstream usage — insert a dedupe line immediately after the fetch:

```python
        rows = await self._equity_repo.list_top_candidates(
            market=request.market, limit=limit
        )
        rows = _dedupe_rows(rows, key=lambda r: to_db_symbol(r.symbol))
        evidence = build_candidate_evidence(
            market=request.market,
            preset="top_gainers",
            rows=[_equity_row_to_input(r) for r in rows],
        )
```

In `_collect_crypto`, after the `rows = await self._crypto_repo.list_latest(...)` fetch:

```python
        rows = await self._crypto_repo.list_latest(
            preset_id="crypto_momentum", limit=limit
        )
        rows = _dedupe_rows(rows, key=lambda r: r.symbol)
        evidence = build_candidate_evidence(
            market="crypto",
            preset="crypto_momentum",
            rows=[_crypto_row_to_input(r) for r in rows],
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py -k dedupes -v`
Expected: PASS.

- [ ] **Step 6: Run the whole collector test file (no regression)**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py -q`
Expected: all PASS (dedupe is a no-op on the already-unique fakes).

- [ ] **Step 7: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/candidate_universe.py tests/services/action_report/test_candidate_universe_collector_evidence.py
git commit -m "feat(ROB-352): dedupe candidate_universe by normalized symbol (Slice C)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task C2: Surface the cap (universe_count + capped)

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/candidate_universe.py` (`_build_candidate_result`)
- Test: `tests/services/action_report/test_candidate_universe_collector_evidence.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/action_report/test_candidate_universe_collector_evidence.py`:

```python
@pytest.mark.asyncio
async def test_cap_surfaced_when_universe_exceeds_limit(db_session):
    """ROB-352 Slice C — universe larger than the limit is flagged, not silent."""
    repo = _FakeEquityRepository()  # 3 rows, fresh_count=3
    collector = CandidateUniverseSnapshotCollector(db_session, equity_repository=repo)
    results = await collector.collect(
        CollectorRequest(
            market="kr",
            account_scope=None,
            symbols=[],
            candidate_limit=2,
            policy_snapshot={},
        )
    )
    payload = results[0].payload_json
    assert payload["universe_count"] == 3
    assert payload["capped"] is True
    assert results[0].coverage_json["universe_count"] == 3
    assert results[0].coverage_json["capped"] is True


@pytest.mark.asyncio
async def test_cap_not_flagged_when_universe_within_limit(db_session):
    """ROB-352 Slice C — universe <= limit → capped is False."""
    repo = _FakeEquityRepository()  # 3 rows
    collector = CandidateUniverseSnapshotCollector(db_session, equity_repository=repo)
    results = await collector.collect(
        CollectorRequest(
            market="kr",
            account_scope=None,
            symbols=[],
            candidate_limit=10,
            policy_snapshot={},
        )
    )
    payload = results[0].payload_json
    assert payload["universe_count"] == 3
    assert payload["capped"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py -k "cap_surfaced or cap_not_flagged" -v`
Expected: FAIL — `KeyError: 'universe_count'`.

- [ ] **Step 3: Add the fields in `_build_candidate_result`**

In `_build_candidate_result`, compute the cap signal before building `payload` (right after the `freshness_status = ...` / `candidates = [...]` lines):

```python
        universe_count = fresh_count + stale_count
        capped = universe_count > candidate_limit
```

Add to the `payload` dict (after the existing `"candidate_limit": candidate_limit,` line):

```python
            "universe_count": universe_count,
            "capped": capped,
```

Add to the `coverage=` dict in the `build_result(...)` call (after the existing `"candidate_limit": candidate_limit,` line):

```python
                "universe_count": universe_count,
                "capped": capped,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py -k "cap_surfaced or cap_not_flagged" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the whole collector test file**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/candidate_universe.py tests/services/action_report/test_candidate_universe_collector_evidence.py
git commit -m "feat(ROB-352): surface candidate_universe cap (universe_count + capped) (Slice C)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task C3: Verification + lint + push PR

**Files:** none (verification only)

- [ ] **Step 1: Run the collector suite + adjacent generator/bundle suites**

Run:
```bash
uv run pytest \
  tests/services/action_report/test_candidate_universe_collector_evidence.py \
  tests/services/action_report/ tests/ -k "candidate_universe or snapshot_backed or bundle_ensure or investment_report" -q
```
Expected: all PASS.

- [ ] **Step 2: Lint + format**

Run: `uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/`
Expected: clean. (`uv run ruff format app/ tests/` if drift.)

- [ ] **Step 3: Import/host guards**

Run: `uv run pytest tests/ -k "guard or import_guard" -q`
Expected: PASS.

- [ ] **Step 4: Typecheck (best-effort)**

Run: `uv run ty check app/services/action_report/snapshot_backed/collectors/candidate_universe.py`
Expected: no new errors.

- [ ] **Step 5: Push + open PR (base main)**

```bash
git push -u origin rob-352-slice-c
gh pr create --base main --title "feat(ROB-352): candidate_universe collector hygiene (Slice C)" --body "<summary: dedupe + cap surfacing; scope note delegating ranking to ROB-346; test plan; side-effect boundary; link spec + Slice A/B>"
```
Confirm the CI Test workflow + lint are green before merge (branch protection does not gate them).

---

## Self-Review

**Spec coverage:**
- Change 1 (dedupe by normalized symbol, equity via `to_db_symbol`, crypto raw, contiguous ranks) → C1. ✓
- Change 2 (`universe_count` + `capped` in payload and coverage) → C2. ✓
- Delegated/out-of-scope (ranking, filters, held-tagging) → not implemented by design; noted in spec + PR body. ✓
- Testing (dedup collapse + contiguous ranks; cap surfaced both directions; existing green) → C1S1, C2S1, C3. ✓

**Placeholder scan:** The PR body in C3S5 is the only `<...>` — intentional, filled at push time. All code steps contain complete code.

**Type consistency:** `_dedupe_rows(rows, *, key)` used in both branches with the documented key lambdas. `universe_count`/`capped` names identical across the payload and coverage dicts and the two tests. `to_db_symbol` import path `app.core.symbol`. Field additions are purely additive — existing `candidate_limit`/`candidate_count`/`source_coverage` untouched.

**Note:** `_dedupe_rows` is typed with `Any` for rows/key to stay agnostic across the two ORM row types (`InvestScreenerSnapshot` / `InvestCryptoScreenerSnapshot`) and the test fakes — consistent with the collector already using `dict[str, Any]` row-input shapes.
