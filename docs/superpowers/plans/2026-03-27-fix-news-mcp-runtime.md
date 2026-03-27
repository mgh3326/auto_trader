# Fix News MCP Runtime Errors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two runtime errors in `get_market_news` and `search_news` MCP tools so they return valid JSON results instead of crashing.

**Architecture:** Both bugs are narrow fixes in `news_handlers.py` and `llm_news_service.py`. Bug 1 is a `None` value entering arithmetic (likely `hours` default not applied by FastMCP). Bug 2 is a missing JSONB cast on the RHS of a `@>` containment operator.

**Tech Stack:** Python 3.13, SQLAlchemy (async, PostgreSQL dialect), FastMCP, pytest

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `app/mcp_server/tooling/news_handlers.py` | Modify | Guard `hours`/`limit` defaults, cast JSONB in `_search_news_db` |
| `app/services/llm_news_service.py` | Modify | Guard `hours` default in `get_news_articles`, cast JSONB for `keyword` filter |
| `tests/test_news_rss.py` | Modify | Add regression tests for both bugs |

---

### Task 1: Fix `get_market_news` NoneType arithmetic error

**Files:**
- Modify: `app/mcp_server/tooling/news_handlers.py:35-44`
- Modify: `app/services/llm_news_service.py:225-256`
- Test: `tests/test_news_rss.py`

**Root Cause:** FastMCP may pass `None` for parameters even when Python defaults are declared (e.g., `hours: int = 24`). When `hours=None` flows into `get_news_articles`, the guard `if hours is not None` protects the cutoff. But if `limit=None` flows through, `query.offset(offset).limit(None)` is fine in SQLAlchemy. The `NoneType + int` error likely comes from `timedelta(hours=None)` being reached through a code path where the guard is bypassed, or from FastMCP internal parameter handling. The safest fix is to enforce defaults at the entry point.

- [ ] **Step 1: Write failing test — `hours=None` passed to `_get_market_news_impl`**

In `tests/test_news_rss.py`, add to `TestMCPNewsTools`:

```python
@pytest.mark.asyncio
async def test_get_market_news_impl_hours_none_uses_default(self):
    """FastMCP may pass None for hours — should fall back to 24."""
    from app.mcp_server.tooling.news_handlers import _get_market_news_impl

    with patch(
        "app.mcp_server.tooling.news_handlers.get_news_articles",
        new_callable=AsyncMock,
        return_value=([], 0),
    ) as mock_get:
        result = await _get_market_news_impl(hours=None, feed_source=None, limit=None)

    # Should use default hours=24, limit=20
    call_kwargs = mock_get.call_args.kwargs
    assert call_kwargs["hours"] == 24
    assert call_kwargs["limit"] == 20
    assert result["count"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_news_rss.py::TestMCPNewsTools::test_get_market_news_impl_hours_none_uses_default -v
```

Expected: FAIL — `hours=None` is passed through without default enforcement.

- [ ] **Step 3: Fix `_get_market_news_impl` to enforce defaults**

In `app/mcp_server/tooling/news_handlers.py`, change `_get_market_news_impl`:

```python
async def _get_market_news_impl(
    hours: int | None = 24,
    feed_source: str | None = None,
    limit: int | None = 20,
) -> dict[str, Any]:
    hours = hours or 24
    limit = limit or 20

    articles, total = await get_news_articles(
        hours=hours,
        feed_source=feed_source,
        limit=limit,
    )

    news_list = [_article_to_dict(a) for a in articles]
    sources = list({a.get("feed_source") for a in news_list if a.get("feed_source")})

    return {
        "count": len(news_list),
        "total": total,
        "news": news_list,
        "sources": sorted(sources),
    }
```

Key changes:
- Type hints widen to `int | None` to reflect FastMCP reality
- `hours = hours or 24` and `limit = limit or 20` enforce defaults

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_news_rss.py::TestMCPNewsTools::test_get_market_news_impl_hours_none_uses_default -v
```

Expected: PASS

- [ ] **Step 5: Also guard `get_news_articles` in `llm_news_service.py`**

The `keyword` filter in `get_news_articles` has the same JSONB cast bug as `_search_news_db` (line 258). Fix it here too while we're at it — but the JSONB cast fix is Task 2. For now, just ensure `hours` is safe:

In `app/services/llm_news_service.py`, line 254-256, the existing code is already safe:

```python
if hours is not None:
    cutoff = now_kst_naive() - timedelta(hours=hours)
    conditions.append(NewsArticle.article_published_at >= cutoff)
```

This guard is correct. No change needed here for the `hours` bug — the fix in `_get_market_news_impl` is sufficient.

- [ ] **Step 6: Run all existing tests to verify no regression**

```bash
uv run pytest tests/test_news_rss.py -v
```

Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/tooling/news_handlers.py tests/test_news_rss.py
git commit -m "fix: guard None defaults in get_market_news MCP handler"
```

---

### Task 2: Fix `search_news` JSONB cast error

**Files:**
- Modify: `app/mcp_server/tooling/news_handlers.py:57-87`
- Modify: `app/services/llm_news_service.py:257-258`
- Test: `tests/test_news_rss.py`

**Root Cause:** `NewsArticle.keywords.op("@>")(json.dumps([query]))` passes a Python string to the `@>` operator. PostgreSQL expects both sides to be `jsonb`. The RHS needs an explicit `cast(..., JSONB)`.

- [ ] **Step 1: Write failing test — `_search_news_db` JSONB cast**

In `tests/test_news_rss.py`, add a new test class:

```python
class TestSearchNewsJsonbCast:
    """Verify search_news builds valid JSONB containment queries."""

    @pytest.mark.asyncio
    async def test_search_news_db_builds_valid_jsonb_query(self):
        """The @> operator RHS must be cast to JSONB, not passed as varchar."""
        from app.mcp_server.tooling.news_handlers import _search_news_db

        captured_stmts = []
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        async def capture_execute(stmt, *args, **kwargs):
            captured_stmts.append(stmt)
            mock_result = MagicMock()
            mock_result.scalars.return_value.all.return_value = []
            mock_result.scalar_one.return_value = 0
            return mock_result

        mock_db.execute = capture_execute

        with patch(
            "app.mcp_server.tooling.news_handlers.AsyncSessionLocal",
            return_value=mock_db,
        ):
            articles, total = await _search_news_db(query="반도체", days=7)

        assert total == 0
        assert articles == []
        # Verify query was built (2 statements: main + count)
        assert len(captured_stmts) == 2

        # Compile the main query and check that CAST appears for JSONB
        from sqlalchemy.dialects.postgresql import dialect as pg_dialect

        compiled = captured_stmts[0].compile(dialect=pg_dialect())
        sql_text = str(compiled)
        # The RHS of @> must be cast to JSONB
        assert "CAST" in sql_text.upper() or "::jsonb" in sql_text.lower(), (
            f"JSONB cast missing in query: {sql_text}"
        )

    @pytest.mark.asyncio
    async def test_search_news_impl_with_keyword_returns_result(self):
        """Full _search_news_impl should not raise JSONB operator error."""
        from app.mcp_server.tooling.news_handlers import _search_news_impl

        with patch(
            "app.mcp_server.tooling.news_handlers._search_news_db",
            new_callable=AsyncMock,
            return_value=([], 0),
        ):
            result = await _search_news_impl(query="반도체", days=7, limit=3)

        assert result["query"] == "반도체"
        assert result["count"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_news_rss.py::TestSearchNewsJsonbCast::test_search_news_db_builds_valid_jsonb_query -v
```

Expected: FAIL — no CAST in compiled SQL.

- [ ] **Step 3: Fix `_search_news_db` JSONB cast in `news_handlers.py`**

In `app/mcp_server/tooling/news_handlers.py`, add the import and fix the query:

Add to imports (line 7):
```python
from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
```

Change `_search_news_db` (lines 57-87):

```python
async def _search_news_db(
    query: str,
    days: int = 7,
    limit: int = 20,
) -> tuple[list[NewsArticle], int]:
    cutoff = now_kst_naive() - timedelta(days=days)
    like_pattern = f"%{query}%"

    async with AsyncSessionLocal() as db:
        base_filter = [
            NewsArticle.article_published_at >= cutoff,
            or_(
                NewsArticle.title.ilike(like_pattern),
                NewsArticle.keywords.op("@>")(cast(json.dumps([query]), JSONB)),
            ),
        ]

        q = (
            select(NewsArticle)
            .where(*base_filter)
            .order_by(NewsArticle.article_published_at.desc().nulls_last())
            .limit(limit)
        )
        result = await db.execute(q)
        articles = list(result.scalars().all())

        count_q = select(func.count(NewsArticle.id)).where(*base_filter)
        count_result = await db.execute(count_q)
        total = count_result.scalar_one()

    return articles, total
```

Key change: `cast(json.dumps([query]), JSONB)` wraps the RHS so PostgreSQL sees `jsonb @> jsonb` instead of `jsonb @> varchar`.

- [ ] **Step 4: Also fix same bug in `get_news_articles` in `llm_news_service.py`**

In `app/services/llm_news_service.py`, line 258 has the same pattern:

```python
conditions.append(NewsArticle.keywords.op("@>")(json.dumps([keyword])))
```

Change to:

```python
from sqlalchemy import cast
from sqlalchemy.dialects.postgresql import JSONB as JSONB_TYPE
# (add to existing imports at top of file)

# Line 258:
conditions.append(NewsArticle.keywords.op("@>")(cast(json.dumps([keyword]), JSONB_TYPE)))
```

Note: The model file already imports `JSONB` from `sqlalchemy.dialects.postgresql`, but `llm_news_service.py` doesn't. Import as `JSONB_TYPE` to avoid confusion if `JSONB` is already imported from the model. Or just import directly:

```python
from sqlalchemy import cast
from sqlalchemy.dialects.postgresql import JSONB
```

Then:
```python
conditions.append(NewsArticle.keywords.op("@>")(cast(json.dumps([keyword]), JSONB)))
```

- [ ] **Step 5: Write test for `get_news_articles` keyword JSONB cast**

In `tests/test_news_rss.py`, add to `TestGetNewsArticlesFilters`:

```python
@pytest.mark.asyncio
async def test_keyword_filter_uses_jsonb_cast(self):
    """get_news_articles keyword filter must cast RHS to JSONB."""
    from app.services.llm_news_service import get_news_articles

    captured_stmts = []
    mock_db = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)

    async def capture_execute(stmt, *args, **kwargs):
        captured_stmts.append(stmt)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_result.scalar_one.return_value = 0
        return mock_result

    mock_db.execute = capture_execute

    with patch(
        "app.services.llm_news_service.AsyncSessionLocal",
        return_value=mock_db,
    ):
        await get_news_articles(keyword="반도체")

    assert len(captured_stmts) >= 2

    from sqlalchemy.dialects.postgresql import dialect as pg_dialect

    compiled = captured_stmts[0].compile(dialect=pg_dialect())
    sql_text = str(compiled)
    assert "CAST" in sql_text.upper() or "::jsonb" in sql_text.lower(), (
        f"JSONB cast missing in query: {sql_text}"
    )
```

- [ ] **Step 6: Run all tests**

```bash
uv run pytest tests/test_news_rss.py -v
```

Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/tooling/news_handlers.py app/services/llm_news_service.py tests/test_news_rss.py
git commit -m "fix: cast JSONB in keyword containment queries for search_news"
```

---

### Task 3: Guard `_search_news_impl` defaults and add integration-style regression tests

**Files:**
- Modify: `app/mcp_server/tooling/news_handlers.py:90-103`
- Test: `tests/test_news_rss.py`

- [ ] **Step 1: Write regression test — `_search_news_impl` with `None` defaults**

```python
class TestMCPNewsRuntimeRegression:
    """Regression tests reproducing actual MCP runtime errors."""

    @pytest.mark.asyncio
    async def test_get_market_news_default_call(self):
        """Reproduce: mcporter call auto_trader.get_market_news hours=24 limit=3"""
        from app.mcp_server.tooling.news_handlers import _get_market_news_impl

        mock_article = MagicMock(
            id=1,
            url="https://example.com/1",
            title="Test",
            source="매일경제",
            feed_source="mk_stock",
            summary="요약",
            article_published_at=datetime(2026, 3, 27, 9, 0, 0),
            keywords=["반도체"],
        )

        with patch(
            "app.mcp_server.tooling.news_handlers.get_news_articles",
            new_callable=AsyncMock,
            return_value=([mock_article], 1),
        ):
            result = await _get_market_news_impl(hours=24, limit=3)

        assert result["count"] == 1
        assert result["news"][0]["title"] == "Test"

    @pytest.mark.asyncio
    async def test_search_news_default_call(self):
        """Reproduce: mcporter call auto_trader.search_news query="반도체" days=7 limit=3"""
        from app.mcp_server.tooling.news_handlers import _search_news_impl

        with patch(
            "app.mcp_server.tooling.news_handlers._search_news_db",
            new_callable=AsyncMock,
            return_value=([], 0),
        ):
            result = await _search_news_impl(query="반도체", days=7, limit=3)

        assert result["query"] == "반도체"
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_search_news_impl_days_none_uses_default(self):
        """FastMCP may pass None for days — should fall back to 7."""
        from app.mcp_server.tooling.news_handlers import _search_news_impl

        with patch(
            "app.mcp_server.tooling.news_handlers._search_news_db",
            new_callable=AsyncMock,
            return_value=([], 0),
        ) as mock_search:
            result = await _search_news_impl(query="반도체", days=None, limit=None)

        call_kwargs = mock_search.call_args.kwargs
        assert call_kwargs["days"] == 7
        assert call_kwargs["limit"] == 20
```

- [ ] **Step 2: Run test to verify `_search_news_impl` None defaults fail**

```bash
uv run pytest tests/test_news_rss.py::TestMCPNewsRuntimeRegression::test_search_news_impl_days_none_uses_default -v
```

Expected: FAIL — `days=None` passed through to `_search_news_db`.

- [ ] **Step 3: Fix `_search_news_impl` to enforce defaults**

In `app/mcp_server/tooling/news_handlers.py`:

```python
async def _search_news_impl(
    query: str,
    days: int | None = 7,
    limit: int | None = 20,
) -> dict[str, Any]:
    days = days or 7
    limit = limit or 20

    articles, total = await _search_news_db(query=query, days=days, limit=limit)
    news_list = [_article_to_dict(a) for a in articles]

    return {
        "query": query,
        "count": len(news_list),
        "total": total,
        "news": news_list,
    }
```

- [ ] **Step 4: Run all tests**

```bash
uv run pytest tests/test_news_rss.py -v
```

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/news_handlers.py tests/test_news_rss.py
git commit -m "fix: guard None defaults in search_news and add regression tests"
```

---

### Task 4: Final verification and squash commit

**Files:**
- All modified files from Tasks 1-3

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest tests/test_news_rss.py -v
```

Expected: All tests PASS (including new regression tests)

- [ ] **Step 2: Run linter**

```bash
make lint
```

Expected: No errors

- [ ] **Step 3: Run formatter**

```bash
make format
```

- [ ] **Step 4: Verify final state of modified files**

Read all three modified files to ensure:
- `news_handlers.py` has: `cast` + `JSONB` imports, default guards in both `_impl` functions, JSONB cast in `_search_news_db`
- `llm_news_service.py` has: `cast` + `JSONB` imports, JSONB cast in `get_news_articles` keyword filter
- `tests/test_news_rss.py` has: new tests for None defaults, JSONB cast verification, runtime regression tests

- [ ] **Step 5: Squash into single fix commit**

```bash
git reset --soft HEAD~3
git commit -m "fix: resolve RSS news MCP runtime errors for market and search tools"
```

This squashes the 3 task commits into one clean hotfix commit.

---

## Summary of Changes

| Bug | Error | Root Cause | Fix |
|-----|-------|-----------|-----|
| `get_market_news` | `NoneType + int` | FastMCP passes `None` for defaulted params | `hours = hours or 24` in `_get_market_news_impl` |
| `search_news` | `jsonb @> varchar` | `json.dumps()` returns `str`, not JSONB | `cast(json.dumps([query]), JSONB)` |
| `get_news_articles` keyword | Same JSONB issue | Same pattern in `llm_news_service.py:258` | Same `cast()` fix |
| Both `_impl` functions | Defensive | FastMCP may pass `None` for any param | Default guards on all numeric params |
