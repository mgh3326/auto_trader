# ROB-579 US Finnhub News Relevance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `get_news(market="us")` Finnhub rows reliably leave `relevance.status="pending"` by re-entering the existing TaskIQ judgment pipeline and hiding judged-unrelated items from the main list.

**Architecture:** Keep the ROB-491/506 boundary intact: `symbol_news_relevance` remains the source of truth, and only the external judgment ingest path transitions rows to `confirmed` or `excluded`. The code change is intentionally narrow: `fetch_symbol_news` should enqueue `news_relevance.judge_pending` when the DB-loaded response still contains visible pending rows, not only when the current provider fetch created brand-new links. US worker behavior is then locked with integration coverage and documented with a live smoke path.

**Tech Stack:** Python 3.13, SQLAlchemy async, TaskIQ, Finnhub service seam, pytest/pytest-asyncio, existing `db_session` integration fixture.

---

## Decision

ROB-579 contains an optional suggestion to split obvious noise into `excluded_news` before judgment. The recommended first patch does **not** add deterministic pre-judgment exclusion, because `app/services/symbol_news_relevance.py` currently states that auto_trader never excludes symbol news on hints alone. This plan makes separation happen after the existing external judgment marks rows `excluded`; `load_symbol_news` already removes those rows from the main list and reports `excluded_count`.

User decision: pre-judgment `excluded_news` is excluded from this PR and should be handled as a follow-up. This avoids dual exclusion paths and avoids false-excluding boundary catalysts before the authoritative judgment step.

## File Structure

- Modify `app/services/symbol_news_service.py`: compute visible pending rows after canonical DB load and enqueue the judgment task for either new or still-pending links.
- Modify `tests/services/test_symbol_news_service.py`: add a US regression for re-enqueueing existing pending rows when the async judgment flag is enabled.
- Modify `tests/jobs/test_news_relevance_judgment.py`: add US/Finnhub worker coverage proving `run_news_relevance_judgment(market="us")` applies confirmed/excluded statuses and `load_symbol_news` hides excluded rows.
- Modify `docs/runbooks/news-relevance-judgment.md`: update the US/Finnhub troubleshooting section with the new re-enqueue behavior and a dry-run smoke command.

---

### Task 1: Re-Enqueue Visible US Pending Rows From `get_news`

**Files:**
- Modify: `tests/services/test_symbol_news_service.py`
- Modify: `app/services/symbol_news_service.py`

- [ ] **Step 1: Write the failing unit test**

Add this test near the existing US/Finnhub tests in `tests/services/test_symbol_news_service.py`:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_existing_pending_reenqueues_judgment_when_flag_on(
    monkeypatch,
) -> None:
    from app.core.config import settings
    from app.tasks import news_relevance_judgment_tasks

    monkeypatch.setattr(settings, "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED", True)
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_news_finnhub",
        AsyncMock(return_value=_FINNHUB_RAW),
    )
    _patch_store_with_insert_count(
        monkeypatch,
        stored=[_stored_us(1, "https://r/apple-beats", "Apple beats")],
        new_links=0,
    )
    kiq = AsyncMock()
    monkeypatch.setattr(
        news_relevance_judgment_tasks.news_relevance_judge_pending, "kiq", kiq
    )

    result = await symbol_news_service.fetch_symbol_news("AAPL", "us", limit=10)

    assert result.status == "ok"
    assert result.articles[0].provider_metadata["relevance"]["status"] == "pending"
    kiq.assert_awaited_once_with(market="us", symbol="AAPL", dry_run=False)
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run pytest tests/services/test_symbol_news_service.py::test_us_existing_pending_reenqueues_judgment_when_flag_on -v
```

Expected: FAIL because `_persist_and_load` currently calls `_maybe_enqueue_judgment()` with `new_pending=0`, so `kiq` is never awaited.

- [ ] **Step 3: Add a pending-count helper**

In `app/services/symbol_news_service.py`, add this helper below `_maybe_enqueue_judgment`:

```python
def _visible_pending_count(stored: list[StoredSymbolNews]) -> int:
    return sum(
        1
        for row in stored
        if isinstance(row.relevance, dict)
        and row.relevance.get("status") == "pending"
    )
```

- [ ] **Step 4: Use visible pending rows as an enqueue trigger**

In `app/services/symbol_news_service.py`, replace the current `new_pending` block inside `_persist_and_load`:

```python
    new_pending = inserted if isinstance(inserted, int) else 0
    await _maybe_enqueue_judgment(market, symbol, new_pending)
```

with:

```python
    new_pending = inserted if isinstance(inserted, int) else 0
    visible_pending = _visible_pending_count(stored)
    await _maybe_enqueue_judgment(market, symbol, max(new_pending, visible_pending))
```

This preserves the original behavior for newly created links and adds recovery for rows that were created while the worker flag, queue, or webhook was unavailable.

- [ ] **Step 5: Run the focused service tests**

Run:

```bash
uv run pytest tests/services/test_symbol_news_service.py::test_us_existing_pending_reenqueues_judgment_when_flag_on tests/services/test_symbol_news_service.py::test_kr_no_enqueue_when_no_new_pending -v
```

Expected: the new US test passes. `test_kr_no_enqueue_when_no_new_pending` will now fail because its fake stored row is pending, and the desired behavior has changed from "new links only" to "visible pending rows". Update that test in Step 6.

- [ ] **Step 6: Update the KR no-pending test to use a confirmed row**

In `tests/services/test_symbol_news_service.py`, change the stored row in `test_kr_no_enqueue_when_no_new_pending` from:

```python
        stored=[_stored(1, _RAW_ITEM["url"], _RAW_ITEM["title"])],
```

to:

```python
        stored=[_stored(1, _RAW_ITEM["url"], _RAW_ITEM["title"], status="confirmed")],
```

Keep `new_links=0`. The test now verifies that no enqueue happens when there are no new links and no visible pending rows.

- [ ] **Step 7: Run the service test file**

Run:

```bash
uv run pytest tests/services/test_symbol_news_service.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/services/symbol_news_service.py tests/services/test_symbol_news_service.py
git commit -m "fix(ROB-579): re-enqueue visible US news relevance pending rows"
```

---

### Task 2: Lock US/Finnhub Judgment Worker Behavior

**Files:**
- Modify: `tests/jobs/test_news_relevance_judgment.py`

- [ ] **Step 1: Make the test helpers market-aware**

In `tests/jobs/test_news_relevance_judgment.py`, replace `_judgment` with:

```python
def _judgment(
    article_id: int,
    symbol: str,
    *,
    market: str = "kr",
    relevance: str = "high",
    relationship: str = "direct",
):
    return NewsRelevanceJudgment(
        article_id=article_id,
        market=market,
        symbol=symbol,
        relationship=relationship,
        relevance=relevance,
        price_relevance="catalyst" if relevance == "high" else "none",
        score=0.9,
        reason="테스트 판정",
        judged_by="hermes",
    )
```

Add this helper below `_seed_pending`:

```python
async def _seed_pending_for_market(
    db,
    *,
    market: str,
    symbol: str,
    n: int = 1,
) -> list[int]:
    feed_source = (
        symbol_news_store.FINNHUB_COMPANY_FEED_SOURCE
        if market == "us"
        else symbol_news_store.FINNHUB_GENERAL_FEED_SOURCE
    )
    items = [
        FeedArticleInput(
            url=f"https://x/rob579-{market}-{symbol}-{i}-{uuid.uuid4()}",
            title=f"{symbol} Finnhub article {i}",
            source="Reuters",
            published_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
            summary=f"{symbol} summary {i}",
        )
        for i in range(n)
    ]
    await symbol_news_store.upsert_feed_articles(
        db,
        market,
        symbol,
        items,
        feed_source=feed_source,
    )
    rows = await symbol_news_store.list_pending(db, market, 50, symbol=symbol)
    return [row["article_id"] for row in rows]
```

- [ ] **Step 2: Add the US worker regression**

Add this test near `test_happy_path_applies_judgments_with_server_derived_status`:

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_us_happy_path_applies_judgments_and_hides_excluded(
    db_session,
) -> None:
    symbol = f"A{uuid.uuid4().hex[:8].upper()}"
    ids = await _seed_pending_for_market(
        db_session, market="us", symbol=symbol, n=2
    )
    client = _FakeClient(
        JudgmentClientResult(
            status="judged",
            judgments=[
                _judgment(ids[0], symbol, market="us", relevance="high"),
                _judgment(
                    ids[1],
                    symbol,
                    market="us",
                    relevance="low",
                    relationship="unrelated",
                ),
            ],
        )
    )

    summary = await run_news_relevance_judgment(
        market="us",
        symbol=symbol,
        dry_run=False,
        client=client,
        session_factory=_SessionFactory(db_session),
    )

    assert summary["status"] == "judged"
    assert summary["applied_confirmed"] == 1
    assert summary["applied_excluded"] == 1
    stored, excluded_count = await symbol_news_store.load_symbol_news(
        db_session, symbol, "us", limit=10
    )
    assert excluded_count == 1
    assert [row.relevance["status"] for row in stored] == ["confirmed"]
    assert client.calls[0]["market"] == "us"
    assert all(row["market"] == "us" for row in client.calls[0]["pending"])
```

- [ ] **Step 3: Run the new worker test**

Run:

```bash
uv run pytest tests/jobs/test_news_relevance_judgment.py::test_us_happy_path_applies_judgments_and_hides_excluded -v
```

Expected: PASS. If it fails, fix only the generic market handling that the failure identifies; do not add US-specific branches unless the generic code path is wrong.

- [ ] **Step 4: Run the full worker test file**

Run:

```bash
uv run pytest tests/jobs/test_news_relevance_judgment.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/jobs/test_news_relevance_judgment.py
git commit -m "test(ROB-579): cover US Finnhub news relevance judgment"
```

---

### Task 3: Document US Pending Recovery and Smoke Checks

**Files:**
- Modify: `docs/runbooks/news-relevance-judgment.md`

- [ ] **Step 1: Update the US/Finnhub section**

In `docs/runbooks/news-relevance-judgment.md`, extend the `## US / crypto (Finnhub) — ROB-510` section after the feed-source bullets with:

```markdown
- ROB-579: when `NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED=true`, a `get_news`
  call enqueues `news_relevance.judge_pending` if the canonical DB response
  still contains visible `pending` rows. This covers both newly inserted links
  and links created during an earlier worker/webhook outage. Duplicate enqueue
  is acceptable: the job re-queries pending rows and exits `no_pending` after
  another worker has applied judgments.
```

- [ ] **Step 2: Add a dry-run smoke command**

In the same troubleshooting area, add:

````markdown
US dry-run smoke:

```bash
uv run python - <<'PY'
import asyncio
from app.jobs.news_relevance_judgment import run_news_relevance_judgment

print(asyncio.run(run_news_relevance_judgment(
    market="us",
    symbol="AMZN",
    dry_run=True,
)))
PY
```

Expected: `status` is `dry_run` with `fetched_pending > 0` when AMZN has pending
rows; otherwise `no_pending`. No webhook call or DB write happens in dry-run.
````

- [ ] **Step 3: Run a docs sanity check**

Run:

```bash
rg -n 'ROB-579|US dry-run smoke|visible `pending`' docs/runbooks/news-relevance-judgment.md
```

Expected: all three strings are present.

- [ ] **Step 4: Commit**

```bash
git add docs/runbooks/news-relevance-judgment.md
git commit -m "docs(ROB-579): document US news relevance pending recovery"
```

---

### Task 4: Final Verification

**Files:**
- Verify only.

- [ ] **Step 1: Run focused regression tests**

Run:

```bash
uv run pytest tests/services/test_symbol_news_service.py tests/jobs/test_news_relevance_judgment.py tests/tasks/test_news_relevance_judgment_tasks.py tests/mcp_server/tooling/test_get_news_envelope.py -v
```

Expected: PASS.

- [ ] **Step 2: Run lint for touched files**

Run:

```bash
uv run ruff check app/services/symbol_news_service.py tests/services/test_symbol_news_service.py tests/jobs/test_news_relevance_judgment.py
```

Expected: PASS.

- [ ] **Step 3: Optional live smoke when env is configured**

Only run this with DB, Redis, Finnhub key, and judgment webhook configured:

```bash
uv run pytest tests/live/test_rob491_news_relevance_roundtrip.py -m live --run-live -v
```

Expected: existing KR live roundtrip remains PASS. For ROB-579 US validation, call `get_news(symbol="AMZN", market="us")`, then run the dry-run command from the runbook. If `fetched_pending > 0`, the TaskIQ worker should log `news_relevance judgment run: market=us ... status=judged|dispatched` after the next enqueue.

- [ ] **Step 4: Summarize Linear update**

Post or prepare this Linear comment for ROB-579:

```markdown
Implemented ROB-579 core recovery path: `get_news(market="us")` now re-enqueues visible pending Finnhub relevance rows when async judgment is enabled, even if the current fetch inserted no new links. Added US worker coverage proving judgments apply to `confirmed`/`excluded` and excluded rows stay out of the main list. Verification: focused service/job/task/MCP tests and Ruff on touched files.

Note: this patch keeps external judgment authoritative and does not add pre-judgment title-noise exclusion. If immediate `excluded_news` for unjudged Finnhub noise is desired, that should be a follow-up because it relaxes the current `symbol_news_relevance` invariant.
```

- [ ] **Step 5: Commit verification changes if any**

If Step 4 adds a local documentation change, commit it. If it only posts a Linear comment, no commit is needed.
