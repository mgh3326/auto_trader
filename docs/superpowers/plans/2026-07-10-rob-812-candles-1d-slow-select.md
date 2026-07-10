# ROB-812 `*_candles_1d` Slow SELECT — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the `analyze_stock_batch` daily-candle read from avg 945 ms to single-digit ms across `kr/us/crypto_candles_1d`, grounded in a real prod query plan rather than the (already-disproven) missing-index premise.

**Architecture:** Diagnosis-first. Task 1 runs read-only `EXPLAIN (ANALYZE, BUFFERS)` on prod and records which root cause fired. The fix is then whichever branch the plan justifies — primary path (most likely) is a query-layer **bounded time predicate** that lets TimescaleDB exclude old chunks; alternates (VACUUM/REINDEX, covering index) are ready if the plan points elsewhere.

**Tech Stack:** Python 3.13, SQLAlchemy async (`text()`), TimescaleDB hypertables, Alembic, pytest (`pytest-asyncio`).

## Global Constraints

- Read-only investigation first; live prod `EXPLAIN` is read-only and **authorization-gated** (auto-mode classifier blocks direct `.env.prod` `psql`; operator/user runs it).
- No broker / order / watch / order-intent mutation on any path.
- Any index creation uses `CREATE INDEX CONCURRENTLY`; any schema change is a **single additive alembic migration**, operator applies `alembic upgrade head` separately.
- Branch (d) (chunk-interval / retention/compression rework) is **out of scope** → file a follow-up issue, do not force into this PR.
- The bounded-time-predicate window MUST be sized so it never returns fewer rows than the unbounded `LIMIT :count` would for realistic history — correctness over speed.
- Prod DB: `auto_trader` @ localhost:5432 (from `.env.prod`). Dev/integration Timescale: `auto_trader` @ localhost:5434 (from `.env`); pytest conftest force-overrides `DATABASE_URL` to `test_db` @ 5432 which has **no** hypertable schema — Timescale integration tests read the real URL from `.env` directly (see `tests/integration/services/daily_candles/test_full_cycle.py`).
- Query source of truth: `app/services/daily_candles/repository.py::DailyCandlesRepository.fetch_recent` — kr/us branch at lines 445-458, crypto branch at 394-414.

---

## File Structure

- `app/services/daily_candles/repository.py` — MODIFY: extract SQL builders + add bounded-time predicate to `fetch_recent` (kr/us and crypto branches).
- `tests/unit/services/daily_candles/test_fetch_recent_query.py` — CREATE: DB-free unit tests for the SQL builder + time-floor helper.
- `tests/integration/services/daily_candles/test_full_cycle.py` — MODIFY: add multi-chunk row-equivalence test proving the predicate does not drop rows.
- `docs/runbooks/candles-1d-slow-select.md` — CREATE: Phase-1 diagnostics SQL bundle + before/after evidence + operator alternate fixes (branches b/c).
- `docs/superpowers/specs/2026-07-10-rob-812-candles-1d-slow-select-design.md` — reference (already committed).
- (CONDITIONAL, branch c only) `alembic/versions/<rev>_rob812_candles_1d_covering_index.py` — CREATE: additive covering index migration.

---

## Task 1: Phase-1 read-only diagnostics + root-cause decision (GATE)

This task produces a **decision**, not code. It is the gate that routes every later task. It is not TDD because there is no code to test — the deliverable is captured `EXPLAIN` output and a recorded branch choice.

**Files:**
- Create: `docs/runbooks/candles-1d-slow-select.md`

**Interfaces:**
- Produces: a recorded root-cause branch ∈ {a, b, c, d} that selects the fix task, plus captured `EXPLAIN` output pasted into the runbook as before-evidence.

- [ ] **Step 1: Create the runbook with the read-only diagnostics bundle**

Create `docs/runbooks/candles-1d-slow-select.md` containing this SQL (run per table; substitute a real key that exists — find one first with the `pick-a-key` query). All read-only.

````markdown
# Runbook: candles_1d slow SELECT (ROB-812)

## Phase 1 — read-only diagnostics (authorization-gated; operator runs)

Connect (password not echoed):
```bash
PGURL=$(rg -oN "DATABASE_URL=postgresql[^ ]*" /Users/mgh3326/work/auto_trader/.env.prod | sed -E 's/^DATABASE_URL=//; s#\+asyncpg##')
psql "$PGURL" -f docs/runbooks/_rob812_diag.sql   # or paste blocks interactively
```

### pick-a-key (one per table)
```sql
SELECT symbol, venue    FROM public.kr_candles_1d     ORDER BY time DESC LIMIT 1;
SELECT symbol, exchange FROM public.us_candles_1d     ORDER BY time DESC LIMIT 1;
SELECT instrument_id    FROM public.crypto_candles_1d ORDER BY time DESC LIMIT 1;
```

### 1. Plan of the exact production query (repeat for us/crypto)
```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT time, symbol, venue AS partition, open, high, low, close,
       NULL AS adj_close, volume, value, source
FROM public.kr_candles_1d
WHERE symbol = :'sym' AND venue = :'ven'
ORDER BY time DESC
LIMIT 200;
```
Look for: `Custom Scan (ChunkAppend)` w/ ordered append vs plain `Append`/`MergeAppend`;
**chunks scanned** (want 1-2); per-chunk `Index Scan` vs `Seq Scan`;
`Planning Time` vs `Execution Time`; `Buffers`.

### 2. Chunk sprawl
```sql
SELECT hypertable_name, count(*) AS chunks
FROM timescaledb_information.chunks
WHERE hypertable_name IN ('kr_candles_1d','us_candles_1d','crypto_candles_1d')
GROUP BY 1;
```

### 3. Sizes
```sql
SELECT relname, pg_size_pretty(pg_total_relation_size(oid))
FROM pg_class WHERE relname IN
 ('kr_candles_1d','us_candles_1d','crypto_candles_1d',
  'ix_kr_candles_1d_symbol_venue_time_desc',
  'ix_us_candles_1d_symbol_exchange_time_desc',
  'ix_crypto_candles_1d_instrument_id_time');
```

### 4. Bloat / vacuum
```sql
SELECT relname, n_live_tup, n_dead_tup, last_autovacuum, last_analyze
FROM pg_stat_user_tables
WHERE relname LIKE '%candles_1d%' ORDER BY n_dead_tup DESC;
```

### 5. Index present on chunks (not just parent)
```sql
SELECT indexrelid::regclass FROM pg_index
WHERE indrelid IN (
  SELECT format('%I.%I', chunk_schema, chunk_name)::regclass
  FROM timescaledb_information.chunks
  WHERE hypertable_name='kr_candles_1d' LIMIT 3);
```

## Decision
Record the branch and paste EXPLAIN output below.
- (a) ordered-append not firing / all-chunk scan  → Task 2 + Task 3 (query predicate)
- (b) stale stats / bloat                          → Task 4a (VACUUM/REINDEX)
- (c) index unused / not on chunks                 → Task 4b (covering index migration)
- (d) chunk sprawl / planning overhead             → follow-up issue (out of scope)
````

- [ ] **Step 2: Operator runs the bundle with authorization and pastes output**

Run the runbook against prod (`auto_trader`). Paste the raw `EXPLAIN (ANALYZE, BUFFERS)` for all three tables and the chunk/bloat counts into the runbook's evidence section.

- [ ] **Step 3: Record the root-cause branch**

Write the chosen branch (a/b/c/d) into the runbook `## Decision` section with one-line justification tied to the EXPLAIN evidence (e.g. "branch a: 41 chunks all Index-Scanned, MergeAppend, Execution 930ms, Planning 6ms").

- [ ] **Step 4: Commit the runbook + evidence**

```bash
git add docs/runbooks/candles-1d-slow-select.md
git commit -m "docs(ROB-812): Phase-1 diagnostics runbook + prod EXPLAIN evidence"
```

**Routing:** If branch = **a**, do Task 2 → Task 3 → Task 5. If **b**, do Task 4a → Task 5. If **c**, do Task 4b → Task 5. If **d**, stop and file a follow-up; do only Task 5's evidence capture.

---

## Task 2: Branch (a) — bounded time predicate for kr/us `fetch_recent` (TDD)

> **CONDITIONAL:** only if Task 1 recorded branch (a).

**Files:**
- Modify: `app/services/daily_candles/repository.py` (kr/us branch, lines 445-458)
- Create: `tests/unit/services/daily_candles/test_fetch_recent_query.py`

**Interfaces:**
- Produces: module-level pure helpers in `repository.py`:
  - `def _recent_time_floor(count: int, *, now: datetime) -> datetime` — returns the lower time bound; window = `max(400, count * 3)` calendar days before `now` (generous vs ~5 trading days / 7 calendar days, so it never undershoots `count` daily rows for realistic history).
  - `def _build_kr_us_recent_sql(partition_col: str, adj_close_select: str) -> str` — returns the SQL string including `AND time >= :time_floor`.

- [ ] **Step 1: Write the failing unit tests**

Create `tests/unit/services/daily_candles/test_fetch_recent_query.py`:

```python
from datetime import UTC, datetime

import pytest

from app.services.daily_candles.repository import (
    _build_kr_us_recent_sql,
    _recent_time_floor,
)


@pytest.mark.unit
def test_recent_time_floor_uses_generous_window_floor():
    now = datetime(2026, 7, 10, tzinfo=UTC)
    # small count clamps to the 400-day floor
    floor = _recent_time_floor(200, now=now)
    assert (now - floor).days == 600  # 200 * 3


@pytest.mark.unit
def test_recent_time_floor_scales_with_large_count():
    now = datetime(2026, 7, 10, tzinfo=UTC)
    floor = _recent_time_floor(50, now=now)
    assert (now - floor).days == 400  # max(400, 150)


@pytest.mark.unit
def test_kr_us_recent_sql_carries_time_floor_predicate():
    sql = _build_kr_us_recent_sql("venue", "NULL AS adj_close, ")
    assert "time >= :time_floor" in sql
    assert "ORDER BY time DESC" in sql
    assert "LIMIT :count" in sql
    assert "venue = :partition" in sql
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/services/daily_candles/test_fetch_recent_query.py -v`
Expected: FAIL — `ImportError: cannot import name '_build_kr_us_recent_sql'`.

- [ ] **Step 3: Add the helpers and rewire the kr/us branch**

In `app/services/daily_candles/repository.py`, add near the top-level imports (ensure `from datetime import datetime, timedelta, UTC` is present) and module scope:

```python
def _recent_time_floor(count: int, *, now: datetime) -> datetime:
    """Lower time bound for chunk exclusion on daily-candle reads.

    Sized generously (>= 400 days, or count*3 calendar days) so the bounded
    window never returns fewer rows than the unbounded LIMIT would for
    realistic history — chunk exclusion is a speed hint, not a data filter.
    """
    window_days = max(400, int(count) * 3)
    return now - timedelta(days=window_days)


def _build_kr_us_recent_sql(partition_col: str, adj_close_select: str) -> str:
    return f"""
        SELECT time, symbol, {partition_col} AS partition,
               open, high, low, close, {adj_close_select}volume, value, source
        FROM public.{{table_name}}
        WHERE symbol = :symbol AND {partition_col} = :partition
          AND time >= :time_floor
        ORDER BY time DESC
        LIMIT :count
    """
```

Then replace the kr/us branch body (currently lines 445-461) so the SQL comes from the builder and passes `time_floor`:

```python
        cfg = self._config(market)
        adj_close_select = (
            "adj_close, " if self._supports_adj_close(market) else "NULL AS adj_close, "
        )
        sql = text(
            _build_kr_us_recent_sql(cfg.partition_col, adj_close_select).format(
                table_name=cfg.table_name
            )
        )
        result = await self._session.execute(
            sql,
            {
                "symbol": symbol,
                "partition": partition,
                "count": int(count),
                "time_floor": _recent_time_floor(int(count), now=datetime.now(UTC)),
            },
        )
```

(Leave the row-mapping loop that follows unchanged.)

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `uv run pytest tests/unit/services/daily_candles/test_fetch_recent_query.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/daily_candles/repository.py tests/unit/services/daily_candles/test_fetch_recent_query.py
git commit -m "perf(ROB-812): bounded time predicate for kr/us daily candle reads"
```

---

## Task 3: Branch (a) — extend predicate to crypto `fetch_recent` + row-equivalence proof (TDD)

> **CONDITIONAL:** only if Task 1 recorded branch (a). Depends on Task 2.

**Files:**
- Modify: `app/services/daily_candles/repository.py` (crypto branch, lines 403-423)
- Modify: `tests/unit/services/daily_candles/test_fetch_recent_query.py`
- Modify: `tests/integration/services/daily_candles/test_full_cycle.py`

**Interfaces:**
- Consumes: `_recent_time_floor` from Task 2.
- Produces: module-level constant `_CRYPTO_RECENT_SQL: str` in `repository.py` (includes `AND time >= :time_floor`).

- [ ] **Step 1: Write the failing unit test for the crypto SQL**

Append to `tests/unit/services/daily_candles/test_fetch_recent_query.py`:

```python
@pytest.mark.unit
def test_crypto_recent_sql_carries_time_floor_predicate():
    from app.services.daily_candles.repository import _CRYPTO_RECENT_SQL

    assert "time >= :time_floor" in _CRYPTO_RECENT_SQL
    assert "instrument_id = :iid" in _CRYPTO_RECENT_SQL
    assert "ORDER BY time DESC" in _CRYPTO_RECENT_SQL
    assert "LIMIT :count" in _CRYPTO_RECENT_SQL
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/services/daily_candles/test_fetch_recent_query.py::test_crypto_recent_sql_carries_time_floor_predicate -v`
Expected: FAIL — `ImportError: cannot import name '_CRYPTO_RECENT_SQL'`.

- [ ] **Step 3: Add the constant and rewire the crypto branch**

In `repository.py`, add at module scope:

```python
_CRYPTO_RECENT_SQL = """
    SELECT time, :symbol AS symbol, :partition AS partition,
           open, high, low, close,
           NULL::numeric AS adj_close,
           base_volume AS volume, quote_volume AS value, source
    FROM public.crypto_candles_1d
    WHERE instrument_id = :iid
      AND time >= :time_floor
    ORDER BY time DESC
    LIMIT :count
"""
```

Replace the crypto branch `sql = text(...)` (lines 403-414) with `sql = text(_CRYPTO_RECENT_SQL)` and add `time_floor` to its param dict (currently lines 415-423):

```python
            result = await self._session.execute(
                sql,
                {
                    "iid": iid,
                    "symbol": symbol,
                    "partition": partition,
                    "count": int(count),
                    "time_floor": _recent_time_floor(int(count), now=datetime.now(UTC)),
                },
            )
```

- [ ] **Step 4: Run the unit test to verify it passes**

Run: `uv run pytest tests/unit/services/daily_candles/test_fetch_recent_query.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Write the failing multi-chunk row-equivalence integration test**

This proves the window does not drop rows: insert daily rows spanning **two 90-day chunks**, then assert `fetch_recent(count=200)` returns exactly the newest 200 rows, identical to an unbounded reference query. Append to `tests/integration/services/daily_candles/test_full_cycle.py` (reuse its `dev_session` fixture and `_SYMBOL_KR` pattern):

```python
    @pytest.mark.integration
    async def test_fetch_recent_bounded_window_matches_unbounded(self, dev_session):
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import text as _text

        repo = DailyCandlesRepository(session=dev_session)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        rows = [
            DailyCandleRow(
                time_utc=base + timedelta(days=i),
                symbol=_SYMBOL_KR,
                partition="KRX",
                open=1.0, high=2.0, low=0.5, close=1.5,
                adj_close=None, volume=10.0, value=15.0, source="test",
            )
            for i in range(250)  # 250 days => spans >2 chunks (90d each)
        ]
        await repo.upsert_rows(market=MarketKey.KR, rows=rows)

        fetched = await repo.fetch_recent(
            market=MarketKey.KR, symbol=_SYMBOL_KR, partition="KRX", count=200
        )
        ref = (await dev_session.execute(
            _text(
                "SELECT time FROM public.kr_candles_1d "
                "WHERE symbol=:s AND venue='KRX' ORDER BY time DESC LIMIT 200"
            ),
            {"s": _SYMBOL_KR},
        )).scalars().all()

        assert len(fetched) == 200
        # fetch_recent returns ascending (reversed); compare newest set
        fetched_times = {r.time_utc for r in fetched}
        assert fetched_times == set(ref)
```

- [ ] **Step 6: Run the integration test**

Run: `uv run pytest tests/integration/services/daily_candles/test_full_cycle.py -v -k bounded_window`
Expected: PASS. (Requires the dev Timescale at port 5434 per the file's header; if unavailable locally, run in the environment that has it and record the result.)

- [ ] **Step 7: Commit**

```bash
git add app/services/daily_candles/repository.py tests/unit/services/daily_candles/test_fetch_recent_query.py tests/integration/services/daily_candles/test_full_cycle.py
git commit -m "perf(ROB-812): bounded time predicate for crypto daily reads + multi-chunk row-equivalence test"
```

---

## Task 4a: Branch (b) — operator VACUUM / REINDEX (no code)

> **CONDITIONAL:** only if Task 1 recorded branch (b). Skip Tasks 2/3.

**Files:**
- Modify: `docs/runbooks/candles-1d-slow-select.md` (append an "Alternate fix: stats/bloat" section)

- [ ] **Step 1: Append the operator commands to the runbook**

```markdown
## Alternate fix (branch b): stale stats / bloat
Read/maintenance only; operator runs, off-hours preferred:
```sql
VACUUM (ANALYZE) public.kr_candles_1d;
VACUUM (ANALYZE) public.us_candles_1d;
VACUUM (ANALYZE) public.crypto_candles_1d;
-- if index bloat is the culprit (per Phase-1 sizes):
REINDEX INDEX CONCURRENTLY public.ix_kr_candles_1d_symbol_venue_time_desc;
REINDEX INDEX CONCURRENTLY public.ix_us_candles_1d_symbol_exchange_time_desc;
REINDEX INDEX CONCURRENTLY public.ix_crypto_candles_1d_instrument_id_time;
```
Consider per-table autovacuum tuning if `n_dead_tup` was high.
```

- [ ] **Step 2: Operator runs the maintenance, re-captures EXPLAIN** (proceed to Task 5 for before/after).

- [ ] **Step 3: Commit runbook update**

```bash
git add docs/runbooks/candles-1d-slow-select.md
git commit -m "docs(ROB-812): branch-b maintenance fix (vacuum/reindex) + evidence"
```

---

## Task 4b: Branch (c) — additive covering-index migration (CONCURRENTLY)

> **CONDITIONAL:** only if Task 1 recorded branch (c) (existing index genuinely unused/absent-on-chunks). Skip Tasks 2/3.

**Files:**
- Create: `alembic/versions/<rev>_rob812_candles_1d_covering_index.py`

- [ ] **Step 1: Generate a revision stub**

Run: `uv run alembic revision -m "rob812 candles_1d covering index"`
Note the generated `<rev>` filename.

- [ ] **Step 2: Write the additive migration**

`CONCURRENTLY` cannot run inside a transaction, so disable the per-migration transaction. Fill the generated file:

```python
from collections.abc import Sequence
from alembic import op

revision: str = "<rev>"
down_revision: str | Sequence[str] | None = "<current head>"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CONCURRENTLY must run outside a transaction block.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_kr_candles_1d_symbol_venue_time_desc_covering "
            "ON public.kr_candles_1d (symbol, venue, time DESC) "
            "INCLUDE (open, high, low, close, volume, value, source)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "public.ix_kr_candles_1d_symbol_venue_time_desc_covering"
        )
```

(Extend to us/crypto only if Phase-1 showed the same on those tables. Set `down_revision` to the actual current head from `uv run alembic heads`.)

- [ ] **Step 3: Verify the migration is well-formed (no DB write here)**

Run: `uv run alembic history | head` and confirm the new revision chains off the current head. Operator applies `alembic upgrade head` separately after review.

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/
git commit -m "perf(ROB-812): additive covering index for candles_1d daily reads (CONCURRENTLY)"
```

---

## Task 5: Verify + finalize (all branches)

**Files:**
- Modify: `docs/runbooks/candles-1d-slow-select.md` (before/after evidence)
- Modify: `docs/superpowers/specs/2026-07-10-rob-812-candles-1d-slow-select-design.md` (record outcome)

- [ ] **Step 1: Re-run Phase-1 EXPLAIN after the fix (authorization-gated)**

Operator re-runs the Task-1 `EXPLAIN (ANALYZE, BUFFERS)` for the affected tables. Confirm: chunks scanned drops (branch a), and Execution Time falls from ~945 ms to single-digit ms. Paste before/after into the runbook.

- [ ] **Step 2: Run the full daily-candles test suite**

Run: `uv run pytest tests/unit/services/daily_candles/ tests/integration/services/daily_candles/ -v`
Expected: PASS (integration requires the 5434 Timescale; record results from the environment that has it).

- [ ] **Step 3: Lint + typecheck the touched module**

Run: `make lint` (or `uv run ruff check app/services/daily_candles/repository.py && uv run ty ...`)
Expected: clean.

- [ ] **Step 4: Record outcome in the spec and open the PR**

Update the spec's "Expected impact" with the measured numbers. Push the branch and open a PR (base `main`) summarizing: root cause (branch), fix, before/after ms, and the CONCURRENTLY/operator-apply note for any migration.

```bash
git push -u origin rob-812
gh pr create --base main --title "perf(ROB-812): candles_1d daily read 945ms -> ~Nms" --body "<summary + before/after EXPLAIN>"
```

- [ ] **Step 5: File the branch-(d) follow-up if Phase-1 flagged chunk sprawl**

If Phase-1 showed excessive chunk count / high planning time, create a follow-up Linear issue for daily-table `chunk_time_interval` / retention/compression rework (explicitly out of this PR's scope).

---

## Self-Review notes

- **Spec coverage:** Phase 1 → Task 1; Phase 2 branch mapping → Tasks 2/3 (a), 4a (b), 4b (c), Task 5 step 5 (d); Phase 3 verify → Task 5; safety boundaries → Global Constraints. All covered.
- **Contingency honesty:** the fix is gated on Task 1's EXPLAIN because the missing-index premise is disproven; every branch has concrete commands/code so there are no placeholders regardless of which fires.
- **Type/name consistency:** `_recent_time_floor(count, *, now)`, `_build_kr_us_recent_sql(partition_col, adj_close_select)`, `_CRYPTO_RECENT_SQL`, param key `:time_floor` used identically across Tasks 2, 3, and their tests.
- **Correctness guard:** the multi-chunk row-equivalence integration test (Task 3 Step 5) is the safeguard against the one real risk of the predicate approach — dropping rows.
