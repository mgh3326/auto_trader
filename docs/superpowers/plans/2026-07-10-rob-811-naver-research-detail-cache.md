# ROB-811 Naver Research Detail-Page DB Cache — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fetch each immutable Naver research **detail** page (`company_read.naver?nid=X`) at most once ever by adding a durable, per-`nid` DB cache beneath the existing Redis caches, cutting ~590s/day of scraping shared by `analyze_stock_batch` and `screen_stocks_snapshot`.

**Architecture:** A new dedicated table `naver_research_detail_cache` (PK `nid`) stores the two fields the detail page yields (`target_price`, `rating`). A sole-writer repository plus a session-owning, error-swallowing store implement a `DetailCachePort` Protocol. The shared assembly function in `investor.py` (`_build_investment_opinions_from_company_list_soup`) batch-reads the cache, fans out detail GETs for misses only, and batch-writes successes. `investor.py` stays DB-agnostic (depends only on the Protocol); the concrete store is injected at the `fundamentals_sources_naver` call sites.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 async, Alembic, PostgreSQL, httpx, BeautifulSoup, pytest (`uv run pytest`).

## Global Constraints

- Analysis output must be **byte-identical** to today: list-row fields (title/firm/date) still come fresh from the live `company_list.naver` page; only `target_price`/`rating` may come from cache. A cache hit must feed the exact same dict shape `{"target_price": ..., "rating": ...}` into the existing merge/`build_consensus` code.
- **Cache only on a successful GET+parse.** The detail fetchers return `dict` on success and `None` on any exception. Write a row **iff the fetcher returned a `dict`**. A `None` result (fetch/HTTP failure) must **never** be written.
- **Success-with-no-target is cache-worthy.** A 200 page with no target element yields `{"target_price": None, "rating": None}`; store it (null columns) so the report is not re-fetched.
- `target_price` comes from `parse_korean_number` (`int | float | None`). The `Numeric` column round-trips to `Decimal`; reads MUST coerce back to `int` (integral) or `float` so downstream arithmetic never mixes `Decimal` with `float`.
- All writes to `naver_research_detail_cache` go through `NaverResearchDetailCacheRepository` (sole writer), mirroring the repo's ledger/research-report convention.
- `rating` stored is the **raw** string from `_parse_report_detail_soup` (pre-normalization); the assembly still normalizes via `normalize_rating_label` on read.
- Cache failure must degrade gracefully to uncached scraping — never raise into the analysis path.
- No change to the existing Redis caches (ROB-638 / ROB-686), consensus math, or the `/research-reports/recent` feed. Do **not** reuse the `research_reports` table.
- `investor.py` must not import DB/session code at runtime; it may import the pure-typing `DetailCachePort` Protocol only.
- Migration-0 to existing schema: exactly one additive new table.
- Alembic head at plan time: `20260707_rob757_toss_fill_poller` (use as `down_revision`).

---

## File Structure

- `app/models/naver_research_detail_cache.py` — **Create.** ORM model `NaverResearchDetailCache` (table `naver_research_detail_cache`).
- `alembic/versions/rob811_naver_research_detail_cache.py` — **Create.** New-table migration.
- `app/services/naver_finance/detail_cache_port.py` — **Create.** `DetailCachePort` Protocol (typing only, no DB imports).
- `app/services/naver_finance/detail_cache.py` — **Create.** `NaverResearchDetailCacheRepository` (sole writer, takes a session), `NaverResearchDetailCacheStore` (owns sessions, swallows errors), `get_detail_cache()` factory (env gate), `_coerce_target_price` helper.
- `app/services/naver_finance/investor.py` — **Modify.** Thread `detail_cache` param through `_build_investment_opinions_from_company_list_soup` (rewrite the fan-out), `fetch_investment_opinions`, `_fetch_kr_snapshot`.
- `app/mcp_server/tooling/fundamentals_sources_naver.py` — **Modify.** Construct + inject the cache in `_fetch_investment_opinions_naver` and `_fetch_analysis_snapshot_naver`.
- Tests: `tests/services/naver_finance/test_detail_cache_repository.py`, `tests/services/naver_finance/test_detail_cache_store.py`, `tests/services/naver_finance/test_investor_detail_cache_wiring.py`, and edits to any existing double that monkeypatches the two wrappers.

---

## Task 1: ORM model + migration for `naver_research_detail_cache`

**Files:**
- Create: `app/models/naver_research_detail_cache.py`
- Create: `alembic/versions/rob811_naver_research_detail_cache.py`
- Test: `tests/services/naver_finance/test_detail_cache_repository.py` (created here, populated in Task 2)

**Interfaces:**
- Produces: ORM class `NaverResearchDetailCache` with columns `nid: str` (PK), `target_price: Decimal | None`, `rating: str | None`, `fetched_at: datetime`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/naver_finance/test_detail_cache_repository.py`:

```python
"""ROB-811 naver_research_detail_cache model + repository tests."""

from __future__ import annotations

import pytest

from app.models.naver_research_detail_cache import NaverResearchDetailCache


@pytest.mark.unit
def test_model_table_and_columns() -> None:
    assert NaverResearchDetailCache.__tablename__ == "naver_research_detail_cache"
    cols = set(NaverResearchDetailCache.__table__.columns.keys())
    assert cols == {"nid", "target_price", "rating", "fetched_at"}
    assert NaverResearchDetailCache.__table__.c.nid.primary_key is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/naver_finance/test_detail_cache_repository.py::test_model_table_and_columns -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.naver_research_detail_cache'`.

- [ ] **Step 3: Write the ORM model**

Create `app/models/naver_research_detail_cache.py`:

```python
"""Naver research detail-page cache (ROB-811).

Immutable per-report cache for `company_read.naver?nid=X` detail pages. Stores
only the two fields the detail page yields (target price, rating). All writes go
through NaverResearchDetailCacheRepository.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class NaverResearchDetailCache(Base):
    __tablename__ = "naver_research_detail_cache"

    nid: Mapped[str] = mapped_column(Text, primary_key=True)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    rating: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 4: Register the model for Alembic autogenerate/metadata**

Confirm the model is imported wherever models are aggregated (grep for how `app/models/research_reports.py` is imported):

Run: `grep -rn "research_reports" app/models/__init__.py alembic/env.py`

If `app/models/__init__.py` explicitly imports models, add `from app.models.naver_research_detail_cache import NaverResearchDetailCache  # noqa: F401` alongside the others. If models are auto-discovered (no explicit list), no edit is needed.

- [ ] **Step 5: Write the migration**

Create `alembic/versions/rob811_naver_research_detail_cache.py`:

```python
"""ROB-811 add naver_research_detail_cache

Revision ID: rob811_naver_research_detail_cache
Revises: 20260707_rob757_toss_fill_poller
Create Date: 2026-07-10 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "rob811_naver_research_detail_cache"
down_revision = "20260707_rob757_toss_fill_poller"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "naver_research_detail_cache",
        sa.Column("nid", sa.Text(), nullable=False),
        sa.Column("target_price", sa.Numeric(), nullable=True),
        sa.Column("rating", sa.Text(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("nid", name="pk_naver_research_detail_cache"),
    )


def downgrade() -> None:
    op.drop_table("naver_research_detail_cache")
```

- [ ] **Step 6: Apply the migration to the local dev DB and verify head**

Run: `uv run alembic upgrade head && uv run alembic current`
Expected: current revision is `rob811_naver_research_detail_cache (head)`.

- [ ] **Step 7: Run the model test**

Run: `uv run pytest tests/services/naver_finance/test_detail_cache_repository.py::test_model_table_and_columns -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/models/naver_research_detail_cache.py alembic/versions/rob811_naver_research_detail_cache.py tests/services/naver_finance/test_detail_cache_repository.py app/models/__init__.py
git commit -m "feat(ROB-811): naver_research_detail_cache table + model"
```

---

## Task 2: `NaverResearchDetailCacheRepository` (sole writer)

**Files:**
- Create: `app/services/naver_finance/detail_cache.py` (repository + coercion helper only in this task)
- Test: `tests/services/naver_finance/test_detail_cache_repository.py` (append)

**Interfaces:**
- Consumes: `NaverResearchDetailCache` (Task 1); `AsyncSession`.
- Produces:
  - `_coerce_target_price(v) -> int | float | None`
  - `class NaverResearchDetailCacheRepository(db: AsyncSession)` with:
    - `async def get_many(self, nids: list[str]) -> dict[str, dict[str, Any]]` — returns `{nid: {"target_price": int|float|None, "rating": str|None}}` for existing rows only.
    - `async def put_many(self, entries: dict[str, dict[str, Any]]) -> None` — one insert with `ON CONFLICT (nid) DO NOTHING`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/naver_finance/test_detail_cache_repository.py`:

```python
from app.core.db import AsyncSessionLocal
from app.services.naver_finance.detail_cache import (
    NaverResearchDetailCacheRepository,
    _coerce_target_price,
)


@pytest.mark.unit
def test_coerce_target_price_types() -> None:
    from decimal import Decimal

    assert _coerce_target_price(None) is None
    assert _coerce_target_price(Decimal("150000")) == 150000
    assert isinstance(_coerce_target_price(Decimal("150000")), int)
    assert _coerce_target_price(Decimal("12.5")) == 12.5
    assert isinstance(_coerce_target_price(Decimal("12.5")), float)


@pytest.mark.integration
async def test_get_many_empty_returns_empty() -> None:
    async with AsyncSessionLocal() as session:
        repo = NaverResearchDetailCacheRepository(session)
        assert await repo.get_many([]) == {}
        assert await repo.get_many(["does-not-exist"]) == {}


@pytest.mark.integration
async def test_put_then_get_roundtrip_and_idempotent() -> None:
    async with AsyncSessionLocal() as session:
        repo = NaverResearchDetailCacheRepository(session)
        await repo.put_many(
            {
                "111": {"target_price": 150000, "rating": "매수"},
                "222": {"target_price": None, "rating": None},
            }
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        repo = NaverResearchDetailCacheRepository(session)
        got = await repo.get_many(["111", "222", "333"])
        assert got == {
            "111": {"target_price": 150000, "rating": "매수"},
            "222": {"target_price": None, "rating": None},
        }
        assert isinstance(got["111"]["target_price"], int)

    # ON CONFLICT DO NOTHING: re-put with different values must not raise or overwrite
    async with AsyncSessionLocal() as session:
        repo = NaverResearchDetailCacheRepository(session)
        await repo.put_many({"111": {"target_price": 999, "rating": "매도"}})
        await session.commit()

    async with AsyncSessionLocal() as session:
        repo = NaverResearchDetailCacheRepository(session)
        got = await repo.get_many(["111"])
        assert got["111"] == {"target_price": 150000, "rating": "매수"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/naver_finance/test_detail_cache_repository.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.naver_finance.detail_cache'`.

- [ ] **Step 3: Write the repository + coercion helper**

Create `app/services/naver_finance/detail_cache.py`:

```python
"""Naver research detail-page cache repository/store (ROB-811).

Sole writer for `naver_research_detail_cache`. The store owns short-lived
sessions and swallows DB errors so a cache fault degrades to uncached scraping.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.naver_research_detail_cache import NaverResearchDetailCache


def _coerce_target_price(v: Decimal | int | float | None) -> int | float | None:
    """Reconstruct the int|float that parse_korean_number produced.

    The Numeric column round-trips to Decimal; integral values become int and
    fractional values become float so downstream arithmetic never mixes Decimal
    with float.
    """
    if v is None:
        return None
    d = Decimal(v)
    if d == d.to_integral_value():
        return int(d)
    return float(d)


class NaverResearchDetailCacheRepository:
    """Sole writer for naver_research_detail_cache."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_many(self, nids: list[str]) -> dict[str, dict[str, Any]]:
        if not nids:
            return {}
        rows = (
            await self.db.execute(
                select(NaverResearchDetailCache).where(
                    NaverResearchDetailCache.nid.in_(nids)
                )
            )
        ).scalars().all()
        return {
            row.nid: {
                "target_price": _coerce_target_price(row.target_price),
                "rating": row.rating,
            }
            for row in rows
        }

    async def put_many(self, entries: dict[str, dict[str, Any]]) -> None:
        if not entries:
            return
        values = [
            {
                "nid": nid,
                "target_price": detail.get("target_price"),
                "rating": detail.get("rating"),
            }
            for nid, detail in entries.items()
        ]
        stmt = (
            pg_insert(NaverResearchDetailCache)
            .values(values)
            .on_conflict_do_nothing(index_elements=["nid"])
        )
        await self.db.execute(stmt)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/naver_finance/test_detail_cache_repository.py -v`
Expected: PASS (integration tests require the test DB with the Task 1 migration applied).

- [ ] **Step 5: Commit**

```bash
git add app/services/naver_finance/detail_cache.py tests/services/naver_finance/test_detail_cache_repository.py
git commit -m "feat(ROB-811): NaverResearchDetailCacheRepository get_many/put_many"
```

---

## Task 3: `DetailCachePort` Protocol + session-owning store + factory

**Files:**
- Create: `app/services/naver_finance/detail_cache_port.py`
- Modify: `app/services/naver_finance/detail_cache.py` (add `NaverResearchDetailCacheStore`, `get_detail_cache`)
- Test: `tests/services/naver_finance/test_detail_cache_store.py`

**Interfaces:**
- Consumes: `NaverResearchDetailCacheRepository` (Task 2); `AsyncSessionLocal` from `app.core.db`.
- Produces:
  - `class DetailCachePort(Protocol)` with `async get_many(nids) -> dict[str, dict[str, Any]]` and `async put_many(entries) -> None`.
  - `class NaverResearchDetailCacheStore` implementing `DetailCachePort`, opening its own session per call and swallowing exceptions (`get_many` returns `{}`, `put_many` no-ops on error).
  - `def get_detail_cache() -> DetailCachePort | None` — returns `None` when `NAVER_RESEARCH_DETAIL_CACHE_ENABLED` is not truthy (default enabled), else a store.

- [ ] **Step 1: Write the failing tests**

Create `tests/services/naver_finance/test_detail_cache_store.py`:

```python
"""ROB-811 detail-cache store + factory tests."""

from __future__ import annotations

import pytest

from app.services.naver_finance import detail_cache
from app.services.naver_finance.detail_cache import (
    NaverResearchDetailCacheStore,
    get_detail_cache,
)


@pytest.mark.unit
def test_factory_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAVER_RESEARCH_DETAIL_CACHE_ENABLED", "false")
    assert get_detail_cache() is None


@pytest.mark.unit
def test_factory_default_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NAVER_RESEARCH_DETAIL_CACHE_ENABLED", raising=False)
    assert isinstance(get_detail_cache(), NaverResearchDetailCacheStore)


@pytest.mark.unit
async def test_store_swallows_db_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def __call__(self) -> "_Boom":
            return self

        async def __aenter__(self) -> None:
            raise RuntimeError("db down")

        async def __aexit__(self, *a: object) -> None:
            return None

    monkeypatch.setattr(detail_cache, "AsyncSessionLocal", _Boom())
    store = NaverResearchDetailCacheStore()
    assert await store.get_many(["1"]) == {}
    # must not raise
    await store.put_many({"1": {"target_price": 1, "rating": "x"}})


@pytest.mark.integration
async def test_store_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NAVER_RESEARCH_DETAIL_CACHE_ENABLED", raising=False)
    store = get_detail_cache()
    assert store is not None
    await store.put_many({"901": {"target_price": 42000, "rating": "매수"}})
    got = await store.get_many(["901"])
    assert got == {"901": {"target_price": 42000, "rating": "매수"}}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/naver_finance/test_detail_cache_store.py -v`
Expected: FAIL with `ImportError: cannot import name 'NaverResearchDetailCacheStore'`.

- [ ] **Step 3: Write the Protocol**

Create `app/services/naver_finance/detail_cache_port.py`:

```python
"""DetailCachePort — the DB-agnostic contract investor.py depends on (ROB-811).

Typing only; imports no DB/session code so the pure scrape module stays free of
runtime DB coupling.
"""

from __future__ import annotations

from typing import Any, Protocol


class DetailCachePort(Protocol):
    async def get_many(self, nids: list[str]) -> dict[str, dict[str, Any]]: ...

    async def put_many(self, entries: dict[str, dict[str, Any]]) -> None: ...
```

- [ ] **Step 4: Add the store + factory**

Append to `app/services/naver_finance/detail_cache.py`. Add imports at the top of the file (`import logging`, `import os`, `from app.core.db import AsyncSessionLocal`, `from app.services.naver_finance.detail_cache_port import DetailCachePort`) and this body:

```python
logger = logging.getLogger(__name__)


class NaverResearchDetailCacheStore:
    """DetailCachePort backed by naver_research_detail_cache.

    Owns a short-lived session per call. Any DB error is swallowed so analysis
    degrades to uncached scraping rather than failing.
    """

    async def get_many(self, nids: list[str]) -> dict[str, Any]:
        if not nids:
            return {}
        try:
            async with AsyncSessionLocal() as session:
                repo = NaverResearchDetailCacheRepository(session)
                return await repo.get_many(nids)
        except Exception:  # pragma: no cover - defensive
            logger.warning("naver detail cache get_many failed", exc_info=True)
            return {}

    async def put_many(self, entries: dict[str, Any]) -> None:
        if not entries:
            return
        try:
            async with AsyncSessionLocal() as session:
                repo = NaverResearchDetailCacheRepository(session)
                await repo.put_many(entries)
                await session.commit()
        except Exception:  # pragma: no cover - defensive
            logger.warning("naver detail cache put_many failed", exc_info=True)


def get_detail_cache() -> DetailCachePort | None:
    """Return a store, or None when disabled via env (default enabled)."""
    if os.getenv("NAVER_RESEARCH_DETAIL_CACHE_ENABLED", "true").strip().lower() != "true":
        return None
    return NaverResearchDetailCacheStore()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/services/naver_finance/test_detail_cache_store.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/naver_finance/detail_cache_port.py app/services/naver_finance/detail_cache.py tests/services/naver_finance/test_detail_cache_store.py
git commit -m "feat(ROB-811): DetailCachePort + session-owning store + factory"
```

---

## Task 4: Wire the cache into the shared assembly in `investor.py`

**Files:**
- Modify: `app/services/naver_finance/investor.py` (`_build_investment_opinions_from_company_list_soup` ~line 106; `fetch_investment_opinions` ~line 304; `_fetch_kr_snapshot` ~line 344 and its assembly call ~line 405)
- Test: `tests/services/naver_finance/test_investor_detail_cache_wiring.py`

**Interfaces:**
- Consumes: `DetailCachePort` (Task 3).
- Produces: `_build_investment_opinions_from_company_list_soup(..., detail_cache: DetailCachePort | None = None)`, `fetch_investment_opinions(code, limit=10, *, window_months=12, detail_cache=None)`, `_fetch_kr_snapshot(code, *, news_limit=5, opinion_limit=10, detail_cache=None)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/services/naver_finance/test_investor_detail_cache_wiring.py`:

```python
"""ROB-811 cache wiring in the opinion assembly."""

from __future__ import annotations

from typing import Any

import pytest
from bs4 import BeautifulSoup

from app.services.naver_finance import investor


class FakeCache:
    def __init__(self, seeded: dict[str, dict[str, Any]] | None = None) -> None:
        self.store: dict[str, dict[str, Any]] = dict(seeded or {})
        self.get_calls: list[list[str]] = []
        self.put_calls: list[dict[str, dict[str, Any]]] = []

    async def get_many(self, nids: list[str]) -> dict[str, dict[str, Any]]:
        self.get_calls.append(list(nids))
        return {n: self.store[n] for n in nids if n in self.store}

    async def put_many(self, entries: dict[str, dict[str, Any]]) -> None:
        self.put_calls.append(dict(entries))
        self.store.update(entries)


def _list_soup() -> BeautifulSoup:
    html = """
    <table class="type_1"><tbody>
      <tr>
        <td>삼성전자</td>
        <td><a href="company_read.naver?nid=111">목표가 상향</a></td>
        <td>미래에셋</td><td>x</td><td>26.07.09</td>
      </tr>
      <tr>
        <td>삼성전자</td>
        <td><a href="company_read.naver?nid=222">유지</a></td>
        <td>KB증권</td><td>x</td><td>26.07.08</td>
      </tr>
    </tbody></table>
    """
    return BeautifulSoup(html, "lxml")


async def _build(detail_fetcher, detail_cache):
    return await investor._build_investment_opinions_from_company_list_soup(
        "005930",
        _list_soup(),
        limit=10,
        current_price=100000,
        detail_fetcher=detail_fetcher,
        detail_cache=detail_cache,
    )


@pytest.mark.unit
async def test_all_hits_makes_zero_fetches() -> None:
    calls: list[str] = []

    async def fetcher(nid: str) -> dict[str, Any]:
        calls.append(nid)
        return {"target_price": 1, "rating": "x"}

    cache = FakeCache(
        {
            "111": {"target_price": 160000, "rating": "매수"},
            "222": {"target_price": None, "rating": None},
        }
    )
    result = await _build(fetcher, cache)
    assert calls == []  # no HTTP detail calls
    assert cache.put_calls == []  # nothing new to write
    tp = {o["title"]: o["target_price"] for o in result["opinions"]}
    assert tp == {"목표가 상향": 160000, "유지": None}


@pytest.mark.unit
async def test_miss_fetches_and_writes() -> None:
    async def fetcher(nid: str) -> dict[str, Any]:
        return {"target_price": 170000 if nid == "111" else None, "rating": "매수"}

    cache = FakeCache()
    await _build(fetcher, cache)
    assert cache.get_calls == [["111", "222"]]
    assert cache.put_calls == [
        {
            "111": {"target_price": 170000, "rating": "매수"},
            "222": {"target_price": None, "rating": "매수"},
        }
    ]


@pytest.mark.unit
async def test_fetch_failure_not_written() -> None:
    async def fetcher(nid: str) -> dict[str, Any] | None:
        return None if nid == "111" else {"target_price": 180000, "rating": "매수"}

    cache = FakeCache()
    result = await _build(fetcher, cache)
    assert list(cache.put_calls[0].keys()) == ["222"]  # 111 (None) not written
    tp = {o["title"]: o["target_price"] for o in result["opinions"]}
    assert tp["목표가 상향"] is None


@pytest.mark.unit
async def test_none_cache_matches_legacy_behavior() -> None:
    async def fetcher(nid: str) -> dict[str, Any]:
        return {"target_price": 190000, "rating": "매수"}

    result = await _build(fetcher, None)  # detail_cache=None → legacy path
    assert result["count"] == 2
    assert all(o["target_price"] == 190000 for o in result["opinions"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/naver_finance/test_investor_detail_cache_wiring.py -v`
Expected: FAIL with `TypeError: _build_investment_opinions_from_company_list_soup() got an unexpected keyword argument 'detail_cache'`.

- [ ] **Step 3: Add the Protocol import and rewrite the assembly**

In `app/services/naver_finance/investor.py`, add this import near the other `app.services.naver_finance` imports (after line 28):

```python
from app.services.naver_finance.detail_cache_port import DetailCachePort
```

Replace the body of `_build_investment_opinions_from_company_list_soup` (currently lines 106-151). Change the signature to add `detail_cache: DetailCachePort | None = None` (keyword, after `detail_fetcher`), and replace the `if report_infos:` fan-out block with cache-aware batching:

```python
async def _build_investment_opinions_from_company_list_soup(
    code: str,
    company_list_soup: BeautifulSoup,
    limit: int,
    *,
    current_price: int | None,
    detail_fetcher: Callable[[str], Awaitable[dict[str, Any] | None]],
    window_months: int = 12,
    detail_cache: DetailCachePort | None = None,
) -> dict[str, Any]:
    opinions: dict[str, Any] = {
        "symbol": code,
        "count": 0,
        "opinions": [],
        "consensus": None,
    }
    report_infos = _collect_opinion_report_infos(company_list_soup, limit)
    if report_infos:
        nids = [info["nid"] for info in report_infos]
        cached: dict[str, Any] = {}
        if detail_cache is not None:
            cached = await detail_cache.get_many(nids)

        # Fetch only the misses; hits skip the HTTP call.
        miss_indexes = [i for i, nid in enumerate(nids) if nid not in cached]
        miss_results = await asyncio.gather(
            *(detail_fetcher(nids[i]) for i in miss_indexes),
            return_exceptions=True,
        )

        details: list[Any] = [cached.get(nid) for nid in nids]
        to_write: dict[str, Any] = {}
        for i, result in zip(miss_indexes, miss_results, strict=True):
            details[i] = result
            if isinstance(result, dict):
                to_write[nids[i]] = result

        if detail_cache is not None and to_write:
            await detail_cache.put_many(to_write)

        for info, detail in zip(report_infos, details, strict=True):
            raw_rating = None
            if isinstance(detail, dict):
                raw_rating = detail.get("rating")

            rating_label = normalize_rating_label(raw_rating)
            opinions["opinions"].append(
                {
                    "stock_name": info["stock_name"],
                    "title": info["title"],
                    "firm": info["firm"],
                    "date": info["date"],
                    "url": info["url"],
                    "target_price": detail.get("target_price")
                    if isinstance(detail, dict)
                    else None,
                    "rating": rating_label,
                    "rating_bucket": rating_to_bucket(rating_label),
                }
            )

    opinions["count"] = len(opinions["opinions"])
    opinions["consensus"] = build_consensus(
        opinions["opinions"], current_price, window_months=window_months
    )
    return opinions
```

- [ ] **Step 4: Thread the param through the two public callers**

Edit `fetch_investment_opinions` (lines 304-341): add `detail_cache: DetailCachePort | None = None` to the signature (keyword-only, after `window_months`) and pass `detail_cache=detail_cache` into the assembly call:

```python
async def fetch_investment_opinions(
    code: str,
    limit: int = 10,
    *,
    window_months: int = 12,
    detail_cache: DetailCachePort | None = None,
) -> dict[str, Any]:
    # ... docstring unchanged ...
    url = f"{NAVER_FINANCE_BASE}/research/company_list.naver"
    company_list_soup = await _fetch_html(
        url, params={"searchType": "itemCode", "itemCode": code}
    )
    current_price = await _fetch_current_price(code)
    return await _build_investment_opinions_from_company_list_soup(
        code,
        company_list_soup,
        limit,
        current_price=current_price,
        detail_fetcher=_fetch_report_detail,
        window_months=window_months,
        detail_cache=detail_cache,
    )
```

Edit `_fetch_kr_snapshot` (lines 344-415): add `detail_cache: DetailCachePort | None = None` to the signature (keyword-only, after `opinion_limit`) and pass it into the assembly call at line 405:

```python
async def _fetch_kr_snapshot(
    code: str,
    *,
    news_limit: int = 5,
    opinion_limit: int = 10,
    detail_cache: DetailCachePort | None = None,
) -> dict[str, Any]:
    # ... unchanged up to the opinions branch ...
            snapshot[
                "opinions"
            ] = await _build_investment_opinions_from_company_list_soup(
                code,
                company_list_soup,
                opinion_limit,
                current_price=current_price,
                detail_fetcher=lambda nid: _fetch_report_detail_with_client(
                    client, nid
                ),
                detail_cache=detail_cache,
            )
```

- [ ] **Step 5: Run the new tests + existing scrape tests**

Run: `uv run pytest tests/services/naver_finance/test_investor_detail_cache_wiring.py tests/test_naver_finance.py -v`
Expected: PASS — the new wiring tests pass and all existing `TestFetchInvestmentOpinions` / `TestFetchKrSnapshot` tests still pass (default `detail_cache=None` preserves behavior).

- [ ] **Step 6: Commit**

```bash
git add app/services/naver_finance/investor.py tests/services/naver_finance/test_investor_detail_cache_wiring.py
git commit -m "feat(ROB-811): cache-aware detail fan-out in opinion assembly"
```

---

## Task 5: Inject the cache at the `fundamentals_sources_naver` call sites

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals_sources_naver.py` (`_fetch_analysis_snapshot_naver` line 33; `_fetch_investment_opinions_naver` line 102)
- Test: `tests/services/naver_finance/test_investor_detail_cache_wiring.py` (append an injection test)

**Interfaces:**
- Consumes: `get_detail_cache` (Task 3); `naver_finance.fetch_investment_opinions` / `naver_finance._fetch_kr_snapshot` with `detail_cache` (Task 4).

- [ ] **Step 1: Guard existing doubles**

Some tests monkeypatch these two wrappers' downstream functions. Confirm they tolerate the new keyword:

Run: `grep -rn "fetch_investment_opinions\|_fetch_kr_snapshot" tests/ | grep -i "monkeypatch\|AsyncMock\|Mock\|def _fake\|lambda"`

For each hit, ensure the double accepts `**kwargs` (or `detail_cache=None`). `AsyncMock` already accepts any kwargs. Update any plain `async def`/`lambda` doubles that would raise on the extra keyword by adding `**kwargs`.

- [ ] **Step 2: Write the failing injection test**

Append to `tests/services/naver_finance/test_investor_detail_cache_wiring.py`:

```python
from app.mcp_server.tooling import fundamentals_sources_naver


@pytest.mark.unit
async def test_wrapper_passes_cache_to_fetch_investment_opinions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_fetch(symbol, limit=10, *, window_months=12, detail_cache=None):
        seen["detail_cache"] = detail_cache
        return {"symbol": symbol, "count": 0, "opinions": [], "consensus": None}

    monkeypatch.setattr(
        fundamentals_sources_naver.naver_finance,
        "fetch_investment_opinions",
        fake_fetch,
    )
    monkeypatch.delenv("NAVER_RESEARCH_DETAIL_CACHE_ENABLED", raising=False)
    await fundamentals_sources_naver._fetch_investment_opinions_naver("005930", 10)
    assert seen["detail_cache"] is not None  # injected store
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/services/naver_finance/test_investor_detail_cache_wiring.py::test_wrapper_passes_cache_to_fetch_investment_opinions -v`
Expected: FAIL — `seen["detail_cache"]` is `None` (wrapper does not yet inject).

- [ ] **Step 4: Inject the cache in both wrappers**

In `app/mcp_server/tooling/fundamentals_sources_naver.py`, add the import near the top (after line 19):

```python
from app.services.naver_finance.detail_cache import get_detail_cache
```

Edit `_fetch_analysis_snapshot_naver` (line 38 call) to pass the cache:

```python
    snapshot = await naver_finance._fetch_kr_snapshot(
        symbol,
        news_limit=news_limit,
        opinion_limit=opinions_limit,
        detail_cache=get_detail_cache(),
    )
```

Edit `_fetch_investment_opinions_naver` (line 105 call) to pass the cache:

```python
    opinions = await naver_finance.fetch_investment_opinions(
        symbol,
        limit=limit,
        window_months=window_months,
        detail_cache=get_detail_cache(),
    )
```

- [ ] **Step 5: Run the injection test + the wrapper's existing tests**

Run: `uv run pytest tests/services/naver_finance/test_investor_detail_cache_wiring.py -v && uv run pytest tests/ -k "fundamentals_sources_naver or investment_opinions or kr_snapshot" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/fundamentals_sources_naver.py tests/services/naver_finance/test_investor_detail_cache_wiring.py
git commit -m "feat(ROB-811): inject detail cache at naver fundamentals call sites"
```

---

## Task 6: Full-suite regression, lint, typecheck

**Files:** none (verification only)

- [ ] **Step 1: Run the targeted suites**

Run: `uv run pytest tests/test_naver_finance.py tests/services/naver_finance/ tests/test_analyze_stock_batch_cache.py -v`
Expected: PASS.

- [ ] **Step 2: Lint + typecheck**

Run: `make lint && make typecheck`
Expected: clean (no new Ruff/ty findings in the created/modified files).

- [ ] **Step 3: Run the broader analysis/screener suites**

Run: `uv run pytest tests/ -k "analyze_stock_batch or screen_stocks_snapshot or consensus" -q`
Expected: PASS.

- [ ] **Step 4: Commit any lint/format fixups**

```bash
git add -A
git commit -m "chore(ROB-811): lint/format fixups" || echo "nothing to commit"
```

---

## Post-implementation verification (manual, not a code task)

1. Apply the migration in the target env: `uv run alembic upgrade head`.
2. Run `analyze_stock_batch` / `screen_stocks_snapshot` twice for the same KR symbols; confirm the second run issues **no** `company_read.naver` GETs for already-seen `nid`s (observe via logs or a `SELECT count(*) FROM naver_research_detail_cache`).
3. Over 24h, confirm via Sentry that `company_read.naver` GET/day drops toward the new-reports-only floor and `analyze_stock_batch` / `screen_stocks_snapshot` avg latency roughly halves.
4. Kill switch check: set `NAVER_RESEARCH_DETAIL_CACHE_ENABLED=false`; confirm the tools revert to live scraping with no errors.

---

## Self-Review

**Spec coverage:**
- Dedicated table `naver_research_detail_cache` (nid/target_price/rating/fetched_at, no TTL) → Task 1. ✓
- Sole-writer repository, batch `get_many` (single `IN` query), idempotent `put_many` (`ON CONFLICT DO NOTHING`) → Task 2. ✓
- DB-agnostic `DetailCachePort`, session-owning error-swallowing store, env kill switch → Task 3. ✓
- One insertion in the shared assembly covering both tool paths; misses-only fetch; batch write; `detail_cache=None` = legacy → Task 4. ✓
- Injection at both `fundamentals_sources_naver` call sites → Task 5. ✓
- Correctness invariants: cache-only-on-success (Task 4 `test_fetch_failure_not_written`), null-target cache-worthy (Task 4 `test_all_hits`/`test_miss`), Numeric→int/float coercion (Task 2 `_coerce_target_price`), byte-identical legacy path (Task 4 `test_none_cache_matches_legacy_behavior`). ✓
- List page left live (never cached) — no task touches `company_list.naver` fetching. ✓
- No `research_reports` reuse, no Redis change, no feed change — none of the tasks touch those. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; every command has expected output. ✓

**Type consistency:** `get_many(nids: list[str]) -> dict[str, dict[str, Any]]` and `put_many(entries: dict[str, dict[str, Any]])` are identical across the Protocol (Task 3), repository (Task 2), store (Task 3), fake (Task 4), and assembly call (Task 4). `detail_cache` keyword name is consistent across `_build_investment_opinions_from_company_list_soup`, `fetch_investment_opinions`, `_fetch_kr_snapshot`, and both wrappers. `_coerce_target_price` name consistent (Task 2 def + use). ✓
