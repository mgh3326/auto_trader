# ROB-310 News Dimension Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic market-wide News evidence bundle (`news_evidence.py`) and wire it into the Hermes context export, so Hermes can write a News dimension report — reusing the generic ROB-306/308 dimension contract (no new table/endpoint/migration).

**Architecture:** Mirror `market_evidence.build_market_evidence`. `build_news_evidence` queries `ResearchReportsQueryService` for recent research-report citations and returns a JSON-able bundle; the context exporter attaches it under `dimension_evidence["news"]` (best-effort, kr/us). Deterministic, read-only, no in-process LLM. research_reports is empty until ingestion is enabled (operator gate) — the assembler degrades to zero citations.

**Tech Stack:** Python 3.13, SQLAlchemy async, Pydantic v2, pytest (`db_session`), `uv`.

**Spec:** `docs/superpowers/specs/2026-05-24-invest-reports-news-dimension-design.md` · **Linear:** ROB-310 · **Branch:** `rob-310`

**Conventions:** `uv run pytest ... -v`; commit trailer `Co-Authored-By: Paperclip <noreply@paperclip.ing>`. Mirror target: `app/services/investment_dimensions/market_evidence.py`; context block: `app/services/investment_stages/hermes_context.py` (the `dimension_evidence["market"]` block ~line 104-123); query: `app/services/research_reports/query_service.py` (`ResearchReportsQueryService(db).find_relevant(*, since, limit) -> ResearchReportCitationListResponse{count, citations:[ResearchReportCitation{source,title,analyst,published_at,excerpt,symbol_candidates,...}]}`); model: `app/models/research_reports.py` (`ResearchReport`; NOT NULL: `dedup_key`, `report_type`, `source`).

---

## File Structure
- Create: `app/services/investment_dimensions/news_evidence.py`
- Create: `tests/services/investment_dimensions/test_news_evidence.py`
- Modify: `app/services/investment_stages/hermes_context.py` (add `dimension_evidence["news"]`)
- Test (modify/create): `tests/services/investment_stages/test_hermes_context_news_dimension.py`

---

## Task 1: News evidence assembler

**Files:**
- Create: `app/services/investment_dimensions/news_evidence.py`
- Test: `tests/services/investment_dimensions/test_news_evidence.py`

- [ ] **Step 1: Write the failing test**

```python
import datetime as dt

import pytest

from app.models.research_reports import ResearchReport
from app.services.research_reports.query_service import ResearchReportsQueryService
from app.services.investment_dimensions.news_evidence import build_news_evidence


async def _clear(db_session):
    from sqlalchemy import text
    await db_session.execute(text("DELETE FROM research_reports"))
    await db_session.commit()


def _report(dedup_key, *, published_at, title, symbols):
    return ResearchReport(
        dedup_key=dedup_key, report_type="research-reports.v1", source="naver_research",
        title=title, analyst="홍길동", summary_text="요약", detail_excerpt="발췌",
        published_at=published_at, published_at_text=published_at.isoformat(),
        symbol_candidates=[{"symbol": s, "market": "kr", "source": "naver_research"} for s in symbols],
    )


@pytest.mark.asyncio
async def test_build_news_evidence_fresh(db_session):
    await _clear(db_session)
    now = dt.datetime(2026, 5, 24, 12, 0, tzinfo=dt.UTC)
    db_session.add(_report("k1", published_at=now - dt.timedelta(hours=2),
                           title="삼성전자 목표가 상향", symbols=["005930"]))
    await db_session.commit()

    bundle = await build_news_evidence(
        ResearchReportsQueryService(db_session), market="kr", now=now
    )
    assert bundle["market"] == "kr"
    assert bundle["count"] == 1
    assert bundle["citations"][0]["title"] == "삼성전자 목표가 상향"
    assert bundle["citations"][0]["symbol_candidates"][0]["symbol"] == "005930"
    assert bundle["freshness"]["status"] == "fresh"


@pytest.mark.asyncio
async def test_build_news_evidence_stale_when_old(db_session):
    await _clear(db_session)
    now = dt.datetime(2026, 5, 24, 12, 0, tzinfo=dt.UTC)
    db_session.add(_report("k_old", published_at=now - dt.timedelta(days=5),
                           title="오래된 리포트", symbols=["000660"]))
    await db_session.commit()
    bundle = await build_news_evidence(
        ResearchReportsQueryService(db_session), market="kr", lookback_hours=24, now=now
    )
    assert bundle["count"] == 1
    assert bundle["freshness"]["status"] == "stale"


@pytest.mark.asyncio
async def test_build_news_evidence_empty_is_unavailable(db_session):
    await _clear(db_session)
    bundle = await build_news_evidence(
        ResearchReportsQueryService(db_session), market="us",
        now=dt.datetime(2026, 5, 24, tzinfo=dt.UTC),
    )
    assert bundle["count"] == 0
    assert bundle["citations"] == []
    assert bundle["freshness"]["status"] == "unavailable"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/investment_dimensions/test_news_evidence.py -v`
Expected: FAIL — `ModuleNotFoundError: ...news_evidence`.

- [ ] **Step 3: Implement** `app/services/investment_dimensions/news_evidence.py`:

```python
"""Deterministic News dimension evidence bundle (ROB-310).

Assembles recent research-report citations into a market-wide News evidence
bundle, mirroring ``market_evidence``. No prose, no LLM — raw material for the
Hermes News dimension report. ``research_reports`` is empty until ingestion is
enabled (operator gate); this degrades gracefully to zero citations.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.services.research_reports.query_service import ResearchReportsQueryService

CITATION_LIMIT = 20


async def build_news_evidence(
    query_service: ResearchReportsQueryService,
    *,
    market: str,
    lookback_hours: int = 24,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now_dt = now or dt.datetime.now(tz=dt.UTC)
    # research_reports rows are multi-symbol mentions, not market-scoped, and the
    # query surface has no market filter — take the most recent reports and pass
    # symbol_candidates through so Hermes can scope per market. No ``since`` so
    # freshness (fresh vs stale) is meaningful rather than always-fresh.
    response = await query_service.find_relevant(limit=CITATION_LIMIT)

    citations: list[dict[str, Any]] = []
    latest_published: dt.datetime | None = None
    for c in response.citations:
        citations.append(
            {
                "title": c.title,
                "source": c.source,
                "analyst": c.analyst,
                "published_at": c.published_at.isoformat() if c.published_at else None,
                "excerpt": c.excerpt,
                "symbol_candidates": [sc.model_dump() for sc in c.symbol_candidates],
            }
        )
        if c.published_at is not None and (
            latest_published is None or c.published_at > latest_published
        ):
            latest_published = c.published_at

    if not citations:
        status = "unavailable"
    elif (
        latest_published is not None
        and latest_published >= now_dt - dt.timedelta(hours=lookback_hours)
    ):
        status = "fresh"
    else:
        status = "stale"

    return {
        "market": market,
        "citations": citations,
        "count": len(citations),
        "freshness": {
            "status": status,
            "latest_published_at": latest_published.isoformat()
            if latest_published
            else None,
        },
        "data_health": {"available_count": len(citations)},
    }
```

> If `ResearchReport.symbol_candidates` or `detail_excerpt` column names differ from the test fixture, reconcile against `app/models/research_reports.py` (the citation maps `excerpt` from `detail_excerpt` via `_row_to_citation`). The fixture must set the NOT NULL columns `dedup_key`/`report_type`/`source`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/investment_dimensions/test_news_evidence.py -v`
Expected: PASS (3 cases).

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_dimensions/news_evidence.py tests/services/investment_dimensions/test_news_evidence.py
git commit -m "feat(rob-310): deterministic News dimension evidence bundle

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: Wire News evidence into the Hermes context export

**Files:**
- Modify: `app/services/investment_stages/hermes_context.py`
- Test: `tests/services/investment_stages/test_hermes_context_news_dimension.py`

- [ ] **Step 1: Write the failing test** — mirror the market-dimension context test (`grep -rl "dimension_evidence" tests/` to find it). Seed a research_reports row, build the context for a `kr` bundle, and assert:

```python
# (use the same context-build harness as the market dimension test)
payload = await exporter.export(bundle_uuid)   # however the existing test invokes it
assert "news" in payload.dimension_evidence
assert payload.dimension_evidence["news"]["count"] >= 1
assert payload.dimension_evidence["news"]["market"] == "kr"
```

Copy the bundle/run/session setup verbatim from the existing market-dimension context test; add one `ResearchReport` row (recent `published_at`) before `export`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/investment_stages/test_hermes_context_news_dimension.py -v`
Expected: FAIL — `KeyError: 'news'` / `"news" not in dimension_evidence`.

- [ ] **Step 3: Implement** — in `hermes_context.py`, add the import near the market evidence import:

```python
from app.services.investment_dimensions.news_evidence import build_news_evidence
from app.services.research_reports.query_service import ResearchReportsQueryService
```

Inside the `if bundle.market in ("kr", "us"):` block, immediately after `dimension_evidence["market"] = ...` (and its `except`), add a sibling try/except:

```python
            try:
                news_evidence = await build_news_evidence(
                    ResearchReportsQueryService(self._session), market=bundle.market
                )
                dimension_evidence["news"] = news_evidence
            except Exception as exc:  # noqa: BLE001 — best-effort, like market
                _logger.exception("Failed to build news evidence for context export")
                dimension_evidence["news"] = {"unavailable": str(exc)}
```

(Place it within the same `kr/us` guard so it shares the gating; it is independent of the market block's success.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/investment_stages/test_hermes_context_news_dimension.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/hermes_context.py tests/services/investment_stages/test_hermes_context_news_dimension.py
git commit -m "feat(rob-310): attach News evidence bundle to Hermes context export

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 3: Verification

- [ ] **Step 1:** `uv run pytest tests/services/investment_dimensions/ tests/services/investment_stages/ -q` → all pass (News + existing Market/dimension tests).
- [ ] **Step 2:** ROB-287 guard: `uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -q` → pass (news_evidence imports no LLM provider).
- [ ] **Step 3:** `make lint` → clean.
- [ ] **Step 4:** broad regression: `uv run pytest tests/ -k "hermes or dimension or research or news" -q` → green.
- [ ] **Step 5:** Open PR. Handoff: branch, PR URL, tests; note research_reports ingestion enablement remains an operator gate (data deferred), and the News report prose is produced by Hermes via the existing `/hermes/dimension-reports` (dimension="news").

---

## Self-Review (against spec)

**Spec coverage:**
- N1 `news_evidence.build_news_evidence` (market-wide, recent citations, freshness, soft-fail empty) → Task 1. ✓
- N2 context wiring `dimension_evidence["news"]` (kr/us, soft-fail, session-constructed query service) → Task 2. ✓
- N3 tests (assembler fresh/stale/empty + context export) → Tasks 1–2. ✓
- Boundaries: no LLM (Task 3 guard); no table/endpoint/migration (none added); read-only; ingestion deferred (no enablement here). ✓

**Placeholder scan:** Task 2 Step 1 says "copy the existing market-dimension context test harness" + "grep to find it" — explicit instruction against a named pattern, not deferred work. The assembler (Task 1) is complete code. The model-column reconcile note in Task 1 is a verification instruction.

**Type consistency:** `build_news_evidence(query_service, *, market, lookback_hours, now)` identical in Task 1 (def + tests) and Task 2 (call, omitting optional kwargs). Returns `{market, citations, count, freshness:{status, latest_published_at}, data_health}` — asserted consistently. `ResearchReportsQueryService(session).find_relevant(limit=...)` matches the real signature.
