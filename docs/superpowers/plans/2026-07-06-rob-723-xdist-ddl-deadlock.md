# ROB-723: xdist DDL-vs-SELECT Deadlock Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the recurring `asyncpg.exceptions.DeadlockDetectedError` CI flake by making all test-schema DDL run exactly once, before any test body, as an xdist-safe barrier — plus a deadlock-retry buffer for the residual TRUNCATE paths.

**Architecture:** Two fixtures (`db_session` in `tests/conftest.py`, `session` in `tests/_investment_reports_helpers.py`) each run a large first-use schema-patch DDL block (`create_all` + dozens of `ALTER`/`ADD CONSTRAINT`) against a single shared `test_db`. The current advisory lock releases *before* yield, so one worker's DDL (`AccessExclusiveLock`) overlaps another worker's test-body SELECT/DDL (multi-table lock-order cycle → deadlock). We (1) extract the union of both DDL blocks into one `apply_test_schema()`, (2) run it exactly once across all xdist workers via a session-scoped autouse fixture gated by a Postgres advisory lock + a durable content-hash sentinel — every other worker blocks on the lock until the first finishes, then skips all DDL entirely (true barrier: no DDL ever runs concurrently with a test body), and (3) strip DDL out of the per-test fixtures, leaving only lightweight `TRUNCATE` wrapped in a deadlock-retry helper.

**Tech Stack:** Python 3.13, pytest, pytest-asyncio, pytest-xdist (`--dist=loadfile`), pytest-split (4 shards), SQLAlchemy async, asyncpg, PostgreSQL 15.

## Global Constraints

- **migration 0** — this is pure CI/test-infra. Touch only `tests/**`. Do **not** add/modify `alembic/versions/**`, `app/**`, or `.github/workflows/test.yml`.
- **Single shared `test_db` retained** — do NOT switch to per-worker databases and do NOT change the CI matrix or `--dist=loadfile`. The barrier must work on one shared DB.
- **Schema parity preserved** — the unified `apply_test_schema()` must be the exact UNION of the two existing DDL blocks. No column/constraint/index may be dropped from what the suite currently guarantees. The `session`/`db_session` fixtures currently make schema drift between `create_all` and migrations idempotent; that guarantee must survive.
- **DDL statements stay idempotent** — every statement in `apply_test_schema()` must be safe to run against both a fresh DB and a persistent local DB (the established `ADD COLUMN IF NOT EXISTS` / `DROP CONSTRAINT IF EXISTS` + recreate pattern).
- **Deadlock detection convention** — match the existing repo idiom: catch `sqlalchemy.exc.DBAPIError` and treat as a deadlock when `"deadlock" in str(exc).lower()` (see `tests/services/test_paper_retrospective_pending.py::_pending_with_retry`). Re-raise anything else immediately.
- **Advisory lock id** — reuse the existing `INVESTMENT_REPORTS_TEST_LOCK_ID = 265_202_605` for the schema barrier so it interlocks with the helper `session` fixture's per-test TRUNCATE (which also uses it). Do not invent a second lock id.
- **Completion criterion (from the issue):** representative repro set (report / decision_history / valuation fixtures in parallel) runs repeatedly under `-n auto --dist=loadfile` with **zero** deadlocks; the 4-shard loadfile CI stays green.

---

## File Structure

- **Create `tests/_db_retry.py`** — one responsibility: a reusable async deadlock-retry helper (`run_with_deadlock_retry`). Consumed by the bootstrap fixture and the stripped per-test fixtures. Generalizes the existing ad-hoc `_pending_with_retry`.
- **Create `tests/_schema_bootstrap.py`** — one responsibility: the single source of truth for test-schema DDL. Holds `apply_test_schema(conn)` (the UNION of the two current DDL blocks), the `SCHEMA_BOOTSTRAP_VERSION` constant, and `schema_content_hash()`.
- **Modify `tests/conftest.py`** — (a) add the session-scoped autouse `_bootstrap_test_schema` barrier fixture; (b) gut the DDL block out of `db_session` (it becomes a thin session provider); (c) wrap the `investment_reports_cleanup_lock` TRUNCATE in the retry helper.
- **Modify `tests/_investment_reports_helpers.py`** — gut `create_all` + the ALTER list out of the `session` fixture; keep advisory-lock + start/cleanup TRUNCATE, wrapped in the retry helper. Keep `INVESTMENT_REPORTS_TABLES`, `INVESTMENT_REPORTS_TEST_LOCK_ID`, and the small helpers (`future_datetime`, `publish_report`, etc.) unchanged.
- **Create `tests/infra/test_schema_barrier.py`** — regression tests for the barrier: idempotency of `apply_test_schema`, sentinel presence/skip, retry-helper unit behavior, and a concurrency stress test that reproduces the old contention and asserts no deadlock escapes.

---

### Task 1: Deadlock-retry helper (`#3` buffer)

**Files:**
- Create: `tests/_db_retry.py`
- Test: `tests/infra/test_schema_barrier.py` (retry-unit tests only in this task)

**Interfaces:**
- Produces:
  - `async def run_with_deadlock_retry(op, *, rollback=None, attempts=6, base_delay=0.05) -> Any` — calls `await op()`; on a `DBAPIError` whose `str(exc).lower()` contains `"deadlock"`, awaits `rollback()` (if given), sleeps `base_delay * 2**n`, and retries up to `attempts` times; re-raises non-deadlock `DBAPIError` immediately; raises the last deadlock error if all attempts fail.

- [ ] **Step 1: Write the failing test**

Add to `tests/infra/test_schema_barrier.py` (create the file + `tests/infra/__init__.py` if missing):

```python
import pytest
from sqlalchemy.exc import DBAPIError

from tests._db_retry import run_with_deadlock_retry


class _FakeDeadlock(DBAPIError):
    def __init__(self):
        super().__init__("stmt", {}, Exception("deadlock detected"))


class _FakeOtherDBAPIError(DBAPIError):
    def __init__(self):
        super().__init__("stmt", {}, Exception("unique constraint violated"))


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient_deadlock():
    calls = {"n": 0}
    rolled_back = {"n": 0}

    async def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FakeDeadlock()
        return "ok"

    async def rollback():
        rolled_back["n"] += 1

    result = await run_with_deadlock_retry(op, rollback=rollback, base_delay=0.0)
    assert result == "ok"
    assert calls["n"] == 3
    assert rolled_back["n"] == 2  # rolled back before each retry


@pytest.mark.asyncio
async def test_retry_reraises_non_deadlock_immediately():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise _FakeOtherDBAPIError()

    with pytest.raises(DBAPIError):
        await run_with_deadlock_retry(op, base_delay=0.0)
    assert calls["n"] == 1  # no retry on non-deadlock


@pytest.mark.asyncio
async def test_retry_gives_up_after_attempts():
    async def op():
        raise _FakeDeadlock()

    with pytest.raises(DBAPIError):
        await run_with_deadlock_retry(op, attempts=3, base_delay=0.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/infra/test_schema_barrier.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests._db_retry'`

- [ ] **Step 3: Write minimal implementation**

Create `tests/_db_retry.py`:

```python
"""Shared deadlock-retry helper for the xdist-shared test DB (ROB-723).

Under ``--dist=loadfile`` multiple workers share one PostgreSQL ``test_db``.
TRUNCATE/DDL (AccessExclusive) can still lose a lock-order race to another
worker's activity and be chosen as the deadlock victim. Those operations are
idempotent, so rollback + retry is safe. Generalizes the ad-hoc retry in
``tests/services/test_paper_retrospective_pending.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.exc import DBAPIError


def _is_deadlock(exc: DBAPIError) -> bool:
    return "deadlock" in str(exc).lower()


async def run_with_deadlock_retry(
    op: Callable[[], Awaitable[Any]],
    *,
    rollback: Callable[[], Awaitable[Any]] | None = None,
    attempts: int = 6,
    base_delay: float = 0.05,
) -> Any:
    """Run ``op`` retrying only on Postgres deadlock (SQLSTATE 40P01).

    Re-raises any non-deadlock ``DBAPIError`` immediately. Between attempts it
    awaits ``rollback`` (if provided) and backs off ``base_delay * 2**n``.
    """
    last: DBAPIError | None = None
    for n in range(attempts):
        try:
            return await op()
        except DBAPIError as exc:
            if not _is_deadlock(exc):
                raise
            last = exc
            if rollback is not None:
                await rollback()
            if n < attempts - 1 and base_delay:
                await asyncio.sleep(base_delay * (2**n))
    assert last is not None
    raise last
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/infra/test_schema_barrier.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/_db_retry.py tests/infra/__init__.py tests/infra/test_schema_barrier.py
git commit -m "test(ROB-723): add shared deadlock-retry helper for xdist test DB"
```

---

### Task 2: Unify test-schema DDL into `apply_test_schema()`

**Files:**
- Create: `tests/_schema_bootstrap.py`
- Modify (read-only reference for extraction): `tests/conftest.py:636-1465`, `tests/_investment_reports_helpers.py:72-305`
- Test: `tests/infra/test_schema_barrier.py`

**Interfaces:**
- Produces:
  - `async def apply_test_schema(conn) -> None` — runs `CREATE SCHEMA IF NOT EXISTS {paper,research,review}`, `Base.metadata.create_all` (all tables, sync-run), then the full idempotent ALTER/CONSTRAINT/INDEX/DROP-TABLE union. `conn` is an `AsyncConnection` already inside a transaction (the caller wraps it in `engine.begin()`).
  - `SCHEMA_BOOTSTRAP_VERSION: int` — manual escape-hatch version; bump when adding an ORM table with no mirrored ALTER here.
  - `def schema_content_hash() -> str` — sha256 hex of `SCHEMA_BOOTSTRAP_VERSION` + the concatenated DDL statement strings. Used by the barrier sentinel so a DDL edit re-triggers bootstrap exactly once on a persistent local DB.

**Extraction rules (do this mechanically, verify against the two sources):**
- The body of `apply_test_schema` = the UNION of:
  - `tests/conftest.py` `db_session` DDL block (currently lines ~636–1465): `CREATE SCHEMA` loop, `Base.metadata.create_all`, and every `ALTER/ADD CONSTRAINT/CREATE INDEX/DROP TABLE/DO $$...$$` plus the two helper calls `_ensure_market_valuation_source_constraint` and `_ensure_investment_snapshot_kind_constraint`.
  - `tests/_investment_reports_helpers.py` `session` DDL list (currently lines ~101–305). This is **mostly a subset** of the conftest block (investment_reports family), BUT it contains the **ROB-455 decision-verb CHECK on `review.investment_report_item_decisions`** (currently helper lines ~289–303) which is **absent from conftest** — this MUST be carried into `apply_test_schema`. Verified: `grep investment_report_item_decisions tests/conftest.py` → none.
- Move the two module-level helpers `_ensure_market_valuation_source_constraint` and `_ensure_investment_snapshot_kind_constraint` (and the constants they need: `MARKET_VALUATION_SOURCE_*`, `SNAPSHOT_KIND_*`, `_quote_ident`, `_check_constraint_sql`, `_constraint_definitions_need_refresh`) from `conftest.py` into `tests/_schema_bootstrap.py`, then re-import them into `conftest.py` (`from tests._schema_bootstrap import ...`) so any other conftest reference still resolves. Keep them callable with `(conn, text)`.
- Represent the ALTER/constraint statements as a module-level `_DDL_STATEMENTS: tuple[str, ...]` (the plain SQL strings). `apply_test_schema` runs `CREATE SCHEMA` + `create_all` + the two `_ensure_*` helpers (which are conditional catalog probes, kept as code) + `for stmt in _DDL_STATEMENTS: await conn.execute(text(stmt))`. Keep the conditional `information_schema.columns` "ALTER only when genuinely missing" guards (e.g. `high_52w_date`, `funding_rate`, crypto OI cols, `crypto_candles_1d` legacy drop) as code, not in `_DDL_STATEMENTS` — they must stay conditional to avoid needless AccessExclusive locks. `schema_content_hash()` hashes `SCHEMA_BOOTSTRAP_VERSION` + `"\n".join(_DDL_STATEMENTS)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/infra/test_schema_barrier.py`:

```python
from tests._schema_bootstrap import apply_test_schema, schema_content_hash


@pytest.mark.asyncio
async def test_apply_test_schema_is_idempotent():
    """Applying the unified DDL twice must not raise, and key drift columns
    guaranteed by the old fixtures must exist afterward."""
    from sqlalchemy import text

    from app.core.db import engine

    async with engine.begin() as conn:
        await apply_test_schema(conn)
    async with engine.begin() as conn:
        await apply_test_schema(conn)  # second run: no-op, must not error

    async with engine.connect() as conn:
        # A representative drift column from the conftest block ...
        got = (
            await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema='review' "
                    "AND table_name='investment_reports' "
                    "AND column_name='snapshot_bundle_uuid'"
                )
            )
        ).first()
        assert got is not None
        # ... and the ROB-455 decision CHECK unique to the helper block.
        got2 = (
            await conn.execute(
                text(
                    "SELECT 1 FROM pg_constraint "
                    "WHERE conname='ck_investment_report_item_decisions_decision'"
                )
            )
        ).first()
        assert got2 is not None


def test_schema_content_hash_is_stable_and_hex():
    h1 = schema_content_hash()
    h2 = schema_content_hash()
    assert h1 == h2
    assert len(h1) == 64 and all(c in "0123456789abcdef" for c in h1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/infra/test_schema_barrier.py -k "apply_test_schema or content_hash" -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests._schema_bootstrap'`

- [ ] **Step 3: Write minimal implementation**

Create `tests/_schema_bootstrap.py` with this skeleton, then paste the extracted DDL into the marked regions (verbatim from the two sources per the extraction rules above):

```python
"""Single source of truth for the pytest test-schema DDL (ROB-723).

Unifies the DDL that previously lived (duplicated) inside the ``db_session``
fixture (tests/conftest.py) and the ``session`` fixture
(tests/_investment_reports_helpers.py). Run exactly once per test DB by the
``_bootstrap_test_schema`` barrier in conftest, so no schema DDL ever overlaps
a concurrent xdist worker's test body.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from sqlalchemy import text

# Bump when adding an ORM table that has NO mirrored ALTER string below, so the
# content hash changes and a persistent local DB re-bootstraps once. Adding a
# mirrored ALTER already changes the hash automatically.
SCHEMA_BOOTSTRAP_VERSION = 1

# ---- constants moved verbatim from conftest.py (lines ~117-148) ----
MARKET_VALUATION_SOURCE_CHECK_NAME = "ck_market_valuation_snapshots_source"
MARKET_VALUATION_SOURCE_MODEL_CHECK_NAME = (
    "ck_market_valuation_snapshots_ck_market_valuation_snapshots_source"
)
MARKET_VALUATION_SOURCE_VALUES = ("naver_finance", "yahoo", "toss_openapi")
SNAPSHOT_KIND_CHECK_NAME = "ck_investment_snapshots_snapshot_kind"
SNAPSHOT_KIND_MODEL_CHECK_NAME = (
    "ck_investment_snapshots_ck_investment_snapshots_snapshot_kind"
)
SNAPSHOT_KIND_CHECK_NAMES = (SNAPSHOT_KIND_MODEL_CHECK_NAME, SNAPSHOT_KIND_CHECK_NAME)
SNAPSHOT_KIND_VALUES = (
    "portfolio", "market", "news", "symbol", "candidate_universe", "browser_probe",
    "invest_page", "journal", "watch_context", "naver_remote_debug",
    "toss_remote_debug", "llm_input_frozen", "pending_orders", "validated_run_card",
    "kr_market_ranking", "investor_flow",
)


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _check_constraint_sql(column_name: str, values: tuple[str, ...]) -> str:
    values_sql = ",".join(f"'{value}'" for value in values)
    return f"CHECK ({column_name} IN ({values_sql}))"


def _constraint_definitions_need_refresh(
    definitions: Iterable[str | None], required_values: tuple[str, ...]
) -> bool:
    definitions = list(definitions)
    if not definitions:
        return True
    return any(
        not all(value in (definition or "") for value in required_values)
        for definition in definitions
    )


async def _ensure_market_valuation_source_constraint(conn) -> None:
    ...  # MOVE verbatim from conftest.py:173-203 (replace `sql_text` param with module `text`)


async def _ensure_investment_snapshot_kind_constraint(conn) -> None:
    ...  # MOVE verbatim from conftest.py:206-237


# Idempotent, unconditional DDL statements (union of both fixtures). The
# conditional "ALTER only when genuinely missing" probes stay as code in
# apply_test_schema(), NOT here.
_DDL_STATEMENTS: tuple[str, ...] = (
    # MOVE every unconditional ALTER/ADD CONSTRAINT/CREATE INDEX/DROP TABLE/DO$$
    # string from conftest.py:643-1465 AND the ROB-455 decision CHECK from
    # _investment_reports_helpers.py:294-303. De-duplicate identical strings.
)


def schema_content_hash() -> str:
    payload = f"{SCHEMA_BOOTSTRAP_VERSION}\n" + "\n".join(_DDL_STATEMENTS)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def apply_test_schema(conn) -> None:
    """Create schemas + all ORM tables + apply the idempotent DDL union.

    ``conn`` is an AsyncConnection already inside a transaction.
    """
    import app.models  # noqa: F401  (register all ORM tables)
    import app.models.market_events  # noqa: F401
    from app.models.base import Base

    for schema in ("paper", "research", "review"):
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    await conn.run_sync(Base.metadata.create_all)

    # --- conditional "only when genuinely missing" probes (keep as code) ---
    # MOVE the mv_has_high_52w_date / crypto funding_rate / OI cols /
    # crypto_candles_1d legacy DROP / ROB-534 symbol-master column loops here,
    # verbatim from conftest.py (they must stay conditional).
    ...

    await _ensure_market_valuation_source_constraint(conn)
    await _ensure_investment_snapshot_kind_constraint(conn)

    for stmt in _DDL_STATEMENTS:
        await conn.execute(text(stmt))
```

> **Extraction note:** the two `_ensure_*` helpers currently take `(conn, sql_text)`; change their signature to `(conn)` and use the module-level `text`. Update their bodies' `sql_text(...)` → `text(...)`.

- [ ] **Step 4: Run test to verify it passes**

Requires a live Postgres `test_db` (same as CI). Run:
```bash
uv run pytest tests/infra/test_schema_barrier.py -k "apply_test_schema or content_hash" -v
```
Expected: PASS (2 passed). If a constraint/column assertion fails, a DDL string was missed in extraction — diff `_DDL_STATEMENTS` against the two sources.

- [ ] **Step 5: Commit**

```bash
git add tests/_schema_bootstrap.py tests/infra/test_schema_barrier.py
git commit -m "test(ROB-723): unify test-schema DDL into apply_test_schema()"
```

---

### Task 3: xdist-safe one-time bootstrap barrier (`#2` root fix)

**Files:**
- Modify: `tests/conftest.py` (add fixture near the top of the DB-fixtures section, after `_ensure_test_env()` and imports)
- Test: `tests/infra/test_schema_barrier.py`

**Interfaces:**
- Consumes: `apply_test_schema`, `schema_content_hash` (Task 2); `run_with_deadlock_retry` (Task 1); `INVESTMENT_REPORTS_TEST_LOCK_ID` (from `tests._investment_reports_helpers`).
- Produces: session-scoped autouse fixture `_bootstrap_test_schema` that guarantees the schema exists before any test body in every worker, running the DDL at most once per (DB, content-hash).

**Sentinel design:** a durable marker table `public._pytest_schema_ready(content_hash text primary key, applied_at timestamptz default now())`. Under the advisory lock: create the marker table (`CREATE TABLE IF NOT EXISTS`), check for a row matching `schema_content_hash()`; if present → skip all DDL; else run `apply_test_schema`, then `DELETE` stale rows + `INSERT` the current hash. Because the fixture is session-scoped autouse, every worker passes through this gate *before its first test*; the first worker to win the lock runs DDL while all others block on the lock (barrier) and then skip. No DDL ever runs while another worker executes a test body.

- [ ] **Step 1: Write the failing test**

Add to `tests/infra/test_schema_barrier.py`:

```python
@pytest.mark.asyncio
async def test_bootstrap_sentinel_present_after_session():
    """The barrier must have recorded the current schema hash exactly once."""
    from sqlalchemy import text

    from app.core.db import engine
    from tests._schema_bootstrap import schema_content_hash

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT content_hash FROM public._pytest_schema_ready")
            )
        ).fetchall()
    hashes = {r[0] for r in rows}
    assert schema_content_hash() in hashes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/infra/test_schema_barrier.py::test_bootstrap_sentinel_present_after_session -v`
Expected: FAIL — either `relation "public._pytest_schema_ready" does not exist` (barrier not wired yet) or the hash is absent.

- [ ] **Step 3: Write minimal implementation**

Add to `tests/conftest.py` (after the imports, before `db_session`):

```python
@pytest_asyncio.fixture(scope="session", autouse=True)
async def _bootstrap_test_schema():
    """ROB-723: apply the test schema exactly once, before any test body.

    Under xdist ``--dist=loadfile`` every worker enters this session-scoped
    autouse fixture before running its first test. The first worker to win the
    advisory lock runs the full DDL while all other workers block on the lock
    (barrier); subsequent workers see the content-hash sentinel and skip all
    DDL. Result: schema DDL (AccessExclusive) never overlaps another worker's
    test-body SELECT, closing the deadlock window.
    """
    from sqlalchemy import text

    from app.core.db import engine
    from tests._db_retry import run_with_deadlock_retry
    from tests._investment_reports_helpers import INVESTMENT_REPORTS_TEST_LOCK_ID
    from tests._schema_bootstrap import apply_test_schema, schema_content_hash

    wanted = schema_content_hash()

    async def _bootstrap_once() -> None:
        async with engine.connect() as guard:
            await guard.execute(
                text("SELECT pg_advisory_lock(CAST(:lock_id AS bigint))"),
                {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
            )
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "CREATE TABLE IF NOT EXISTS public._pytest_schema_ready ("
                            "content_hash TEXT PRIMARY KEY, "
                            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                        )
                    )
                    already = (
                        await conn.execute(
                            text(
                                "SELECT 1 FROM public._pytest_schema_ready "
                                "WHERE content_hash = :h"
                            ),
                            {"h": wanted},
                        )
                    ).first()
                    if already:
                        return
                    await apply_test_schema(conn)
                    await conn.execute(
                        text("DELETE FROM public._pytest_schema_ready")
                    )
                    await conn.execute(
                        text(
                            "INSERT INTO public._pytest_schema_ready (content_hash) "
                            "VALUES (:h)"
                        ),
                        {"h": wanted},
                    )
            finally:
                await guard.execute(
                    text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"),
                    {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
                )

    await run_with_deadlock_retry(_bootstrap_once)
    yield
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/infra/test_schema_barrier.py::test_bootstrap_sentinel_present_after_session -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/infra/test_schema_barrier.py
git commit -m "test(ROB-723): add xdist-safe one-time schema bootstrap barrier"
```

---

### Task 4: Strip DDL from the `db_session` fixture

**Files:**
- Modify: `tests/conftest.py` — `db_session` (currently ~607-1478) and `investment_reports_cleanup_lock` (~1481-1536)
- Test: existing DB suite + `tests/infra/test_schema_barrier.py`

**Interfaces:**
- Consumes: `_bootstrap_test_schema` (Task 3 guarantees schema exists), `run_with_deadlock_retry` (Task 1).
- Produces: `db_session` unchanged as a public fixture name/return type (`AsyncSession`), now DDL-free.

- [ ] **Step 1: Write the failing test**

Add a guard test asserting `db_session` no longer performs DDL (schema is present *because the barrier ran*, and the fixture itself does no `create_all`). Add to `tests/infra/test_schema_barrier.py`:

```python
@pytest.mark.asyncio
async def test_db_session_fixture_is_ddl_free(db_session):
    """db_session must be a thin session provider now (schema owned by barrier).

    Sentinel: the review schema + a representative table exist, but the fixture
    source must not reference create_all / metadata anymore.
    """
    import inspect

    import tests.conftest as conftest_mod
    from sqlalchemy import text

    src = inspect.getsource(conftest_mod.db_session.__wrapped__)
    assert "create_all" not in src
    assert "ALTER TABLE" not in src

    got = (
        await db_session.execute(
            text("SELECT 1 FROM information_schema.tables "
                 "WHERE table_schema='review' AND table_name='investment_reports'")
        )
    ).first()
    assert got is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/infra/test_schema_barrier.py::test_db_session_fixture_is_ddl_free -v`
Expected: FAIL — `assert "create_all" not in src` fails (the DDL block is still inline).

- [ ] **Step 3: Write minimal implementation**

Replace the entire `db_session` fixture body (drop the `engine.connect()` guard, the advisory lock, and the whole DDL block ~630-1475) with the thin provider:

```python
@pytest_asyncio.fixture
async def db_session():
    """Async session against the shared test_db.

    Schema is owned by the session-scoped ``_bootstrap_test_schema`` barrier
    (ROB-723); this fixture performs no DDL — that is what previously overlapped
    other xdist workers' test bodies and deadlocked.
    """
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        yield session
```

Then update `investment_reports_cleanup_lock`: keep the advisory lock + TRUNCATE, but wrap the truncate call in the retry helper. Change the body inside the `try:` to:

```python
        try:
            await db_session.rollback()
            await run_with_deadlock_retry(
                _truncate_investment_report_tables, rollback=db_session.rollback
            )
            yield db_session
            await db_session.rollback()
            await run_with_deadlock_retry(
                _truncate_investment_report_tables, rollback=db_session.rollback
            )
        finally:
```

Add `from tests._db_retry import run_with_deadlock_retry` inside the fixture (local import, matching the file's style).

> Remove the now-unused module-level `_ensure_market_valuation_source_constraint`, `_ensure_investment_snapshot_kind_constraint`, and the `MARKET_VALUATION_SOURCE_*` / `SNAPSHOT_KIND_*` / `_quote_ident` / `_check_constraint_sql` / `_constraint_definitions_need_refresh` definitions from `conftest.py` (they now live in `_schema_bootstrap.py`). If ruff flags any remaining reference, re-import from `tests._schema_bootstrap`.

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/infra/test_schema_barrier.py::test_db_session_fixture_is_ddl_free -v
# regression: a representative db_session consumer suite
uv run pytest tests/services/test_decision_history.py tests/services/test_analysis_artifact_service.py -p no:randomly -q
```
Expected: PASS. `ruff check tests/conftest.py` clean (no unused defs).

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/infra/test_schema_barrier.py
git commit -m "test(ROB-723): make db_session DDL-free; schema owned by barrier"
```

---

### Task 5: Strip DDL from the helper `session` fixture

**Files:**
- Modify: `tests/_investment_reports_helpers.py` — `session` fixture (~50-326)
- Test: existing investment-report suite

**Interfaces:**
- Consumes: `_bootstrap_test_schema` (schema exists), `run_with_deadlock_retry` (Task 1).
- Produces: `session` unchanged as a public fixture name/return type (`AsyncSession`), now DDL-free (no `create_all`, no ALTER list); still advisory-locked start/cleanup TRUNCATE.

- [ ] **Step 1: Write the failing test**

Add to `tests/infra/test_schema_barrier.py`:

```python
def test_helper_session_fixture_is_ddl_free():
    import inspect

    import tests._investment_reports_helpers as helpers

    src = inspect.getsource(helpers.session.__wrapped__)
    assert "create_all" not in src
    assert "ADD CONSTRAINT" not in src
    assert "ALTER TABLE" not in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/infra/test_schema_barrier.py::test_helper_session_fixture_is_ddl_free -v`
Expected: FAIL — the ALTER list is still inline.

- [ ] **Step 3: Write minimal implementation**

Replace the `session` fixture body. Drop `create_all` (line ~80-84) and the whole `for stmt in (...)` ALTER list (~101-305). Keep: own engine, advisory lock, start TRUNCATE, yield, cleanup TRUNCATE — both TRUNCATEs via retry:

```python
@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """Per-test AsyncSession against the real PostgreSQL test_db.

    Schema is owned by the session-scoped ``_bootstrap_test_schema`` barrier
    (ROB-723) — this fixture performs no DDL. Between tests it TRUNCATEs the
    investment-report table family, serialized against the conftest cleanup by
    the shared advisory lock and made deadlock-resilient by run_with_deadlock_retry.
    """
    from tests._db_retry import run_with_deadlock_retry

    engine = create_async_engine(settings.DATABASE_URL, future=True)

    async def _truncate() -> None:
        async with engine.begin() as conn:
            for table in reversed(INVESTMENT_REPORTS_TABLES):
                await conn.execute(
                    sa.text(
                        f'TRUNCATE TABLE review."{table.name}" RESTART IDENTITY CASCADE'
                    )
                )

    try:
        async with engine.connect() as guard:
            await guard.execute(
                sa.text("SELECT pg_advisory_lock(CAST(:lock_id AS bigint))"),
                {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
            )
            try:
                await run_with_deadlock_retry(_truncate)
                factory = async_sessionmaker(engine, expire_on_commit=False)
                async with factory() as sess:
                    try:
                        yield sess
                    finally:
                        await sess.rollback()
                await run_with_deadlock_retry(_truncate)
            finally:
                await guard.execute(
                    sa.text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"),
                    {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
                )
    finally:
        await engine.dispose()
```

> Keep `INVESTMENT_REPORTS_TABLES`, `INVESTMENT_REPORTS_TEST_LOCK_ID`, `future_datetime`, `publish_report`, `assert_integrity_error` unchanged. Remove now-unused imports (`Base` if no longer referenced) — let `ruff check` guide you.

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/infra/test_schema_barrier.py::test_helper_session_fixture_is_ddl_free -v
# regression: the investment-report suite that uses the helper session
uv run pytest tests/ -k "investment_report" -p no:randomly -q
```
Expected: PASS. `ruff check tests/_investment_reports_helpers.py` clean.

- [ ] **Step 5: Commit**

```bash
git add tests/_investment_reports_helpers.py tests/infra/test_schema_barrier.py
git commit -m "test(ROB-723): make helper session DDL-free; retry-wrap TRUNCATE"
```

---

### Task 6: Concurrency regression test + full-suite verification

**Files:**
- Modify: `tests/infra/test_schema_barrier.py` (add stress test)
- Verify: whole suite under xdist loadfile

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the failing test**

Add a stress test that reproduces the old contention shape — many concurrent TRUNCATE-then-read cycles on the review tables against the shared engine — and asserts no `DeadlockDetectedError` escapes (retry + barrier absorb it). Add to `tests/infra/test_schema_barrier.py`:

```python
@pytest.mark.slow
@pytest.mark.asyncio
async def test_concurrent_truncate_and_read_no_deadlock_escape():
    """Hammer the review tables concurrently; retry+barrier must absorb any
    deadlock so none escapes to the caller."""
    import asyncio

    from sqlalchemy import text

    from app.core.db import engine
    from tests._db_retry import run_with_deadlock_retry
    from tests._investment_reports_helpers import INVESTMENT_REPORTS_TABLES

    async def truncate_cycle():
        async def _op():
            async with engine.begin() as conn:
                for table in reversed(INVESTMENT_REPORTS_TABLES):
                    await conn.execute(
                        text(
                            f'TRUNCATE TABLE review."{table.name}" '
                            "RESTART IDENTITY CASCADE"
                        )
                    )
        await run_with_deadlock_retry(_op)

    async def read_cycle():
        async def _op():
            async with engine.connect() as conn:
                for table in INVESTMENT_REPORTS_TABLES:
                    await conn.execute(
                        text(f'SELECT count(*) FROM review."{table.name}"')
                    )
        await run_with_deadlock_retry(_op)

    tasks = []
    for _ in range(8):
        tasks.append(truncate_cycle())
        tasks.append(read_cycle())
    # Must complete without a DeadlockDetectedError propagating.
    await asyncio.gather(*tasks)
```

- [ ] **Step 2: Run test to verify it fails (or is meaningful)**

Run: `uv run pytest tests/infra/test_schema_barrier.py::test_concurrent_truncate_and_read_no_deadlock_escape -v`
Expected: PASS with the retry helper (the test proves the buffer works). If you temporarily set `attempts=1` in a local edit it should be able to surface a deadlock — confirming the test exercises real contention — then restore.

- [ ] **Step 3: Full-suite xdist verification (the real acceptance)**

Run the representative repro set repeatedly under the exact CI parallelism, three times, expecting zero deadlocks:

```bash
for i in 1 2 3; do
  echo "=== run $i ===";
  uv run pytest \
    tests/services/test_decision_history.py \
    tests/test_investment_reports_snapshot_evidence_service.py \
    tests/services/test_analysis_artifact_service.py \
    tests/ -k "investment_report or valuation or decision_history" \
    -n auto --dist=loadfile -q -m "not live" || break
done
```
Expected: 3/3 green, no `DeadlockDetectedError` in output.

Then a full shard-parity smoke (one shard) to catch collection/import regressions:
```bash
uv run pytest tests/ -m "not live" --splits 4 --group 2 --durations-path .test_durations -n auto --dist=loadfile -q
```
Expected: green (or only pre-existing unrelated failures — compare against `origin/main` if unsure).

- [ ] **Step 4: Commit**

```bash
git add tests/infra/test_schema_barrier.py
git commit -m "test(ROB-723): concurrency regression for xdist deadlock barrier"
```

- [ ] **Step 5: Refresh durations (optional, if timings shifted materially)**

Only if the new `tests/infra/` tests noticeably unbalance shards:
```bash
uv run pytest tests/ -m "not live" --store-durations -n 4 --dist=loadfile
git add .test_durations && git commit -m "chore(ROB-723): refresh .test_durations"
```

---

## Self-Review

**Spec coverage:**
- Issue candidate `#2` (DDL barrier) → Tasks 2 (unify) + 3 (barrier) + 4/5 (strip per-test DDL). ✅
- Issue candidate `#3` (deadlock retry buffer) → Task 1 (helper) applied in Tasks 3/4/5/6. ✅
- Completion criterion "repeated parallel runs → 0 deadlocks; 4-shard loadfile green" → Task 6 Step 3. ✅
- "migration 0 / test-infra only" → Global Constraints; no `alembic/`, `app/`, or workflow edits. ✅
- Preserve schema parity incl. ROB-455 decision CHECK unique to the helper → Task 2 extraction rule + Task 2 Step 1 assertion. ✅

**Placeholder scan:** The only intentional `...` markers are in Task 2's skeleton, each paired with an exact "MOVE verbatim from `<file>:<lines>`" instruction — this is a mechanical extraction of existing, in-repo DDL, not invented logic. All new logic (retry helper, barrier fixture, sentinel, stripped fixtures, tests) is shown in full.

**Type consistency:** `run_with_deadlock_retry(op, *, rollback, attempts, base_delay)` — same signature used in Tasks 1/3/4/5/6. `apply_test_schema(conn)` and `schema_content_hash()` — same names/signatures in Tasks 2/3. Sentinel table `public._pytest_schema_ready(content_hash, applied_at)` — same in Task 3 impl and Task 3 test. Advisory lock id `INVESTMENT_REPORTS_TEST_LOCK_ID` reused consistently.
