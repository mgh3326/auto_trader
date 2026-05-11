# ROB-201 /invest Coverage — Naver source-candidate / readiness / reference contract

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the read-only `/invest/api/coverage` contract so that Naver is represented as a *source-candidate* with an explicit readiness state on the surfaces where it applies (investor_flow, news_feed, valuation_fundamentals, research_reports) and as a plain *reference* benchmark alongside Toss for the rest — without ever being labelled the source-of-truth.

**Architecture:** Coverage today returns a flat `surfaces[]` array where each `InvestCoverageSurface` has a single `sourceOfTruth` string, a `state` (fresh/stale/partial/missing/unsupported/error/provider_unwired), and a `reference: str = "toss"`. ROB-201 adds (1) a new `CoverageSourceCandidate` value-object with its own `readiness` vocabulary distinct from `CoverageState`, (2) an optional `sourceCandidates: list[CoverageSourceCandidate]` per surface, and (3) replaces `reference: str` with `references: list[str]` (default `["toss"]`). The service computes Naver readiness from existing tables only (no scraping in request path); Naver `naver_finance` request-time calls and the discussion-signal / stock-detail PoCs are reported as candidates with declarative readiness ("live" / "request_time_only" / "fixture_backed_poc" / "aggregate_only_blocked" / "not_wired"). The frontend coverage page renders candidates as subordinate chips under each row.

**Tech Stack:** FastAPI + Pydantic v2 (`extra="forbid"` strict), SQLAlchemy async, pytest-asyncio, React/TypeScript (Vite) for `frontend/invest`. No DB migration, no new tables, no new ingestion job — read-only over existing read-models.

---

## Design Summary (the contract)

### Readiness vocabulary (NEW, distinct from CoverageState)

```python
CoverageCandidateReadiness = Literal[
    "live",                    # durable read-model rows actively written under this source (e.g. investor_flow_snapshots.source='naver_finance')
    "request_time_only",       # naver_finance live calls happen elsewhere but no durable /invest read-model is wired
    "fixture_backed_poc",      # PoC code with fixtures only; not in production /invest path
    "aggregate_only_blocked",  # gated by aggregate-only contract (e.g. discussion signal); no-go pending review
    "not_wired",               # no Naver path exists for this surface today
]
```

Rationale: keep `CoverageState` semantics ("how fresh is the durable read-model?") clean; readiness answers a different question ("how mature is this candidate source?"). Mixing them caused noise in earlier drafts (e.g. `state="provider_unwired"` already overloaded).

### Per-surface candidate map

| Surface                       | Naver candidate                       | Default readiness                                | Underlying signal                                                              |
| ----------------------------- | ------------------------------------- | ------------------------------------------------ | ------------------------------------------------------------------------------ |
| `symbol_universe`             | —                                     | —                                                | KR universe sourced from KRX, not Naver                                        |
| `screener_snapshots`          | `naver_finance`                       | `request_time_only`                              | naver_finance valuation calls are not persisted to `invest_screener_snapshots` |
| `news_feed`                   | `naver_finance_news`                  | derived from `news_articles.source ILIKE 'naver%'` | live freshness query if any naver-sourced articles exist; else `not_wired`     |
| `calendar_events`             | —                                     | —                                                | calendar uses Finnhub/DART/WiseFn/ForexFactory only                            |
| `research_reports`            | `naver_research`                      | `fixture_backed_poc`                             | `naver_stock_detail_poc` exposes research metadata via fixtures only           |
| `investor_flow` (KR)          | `naver_finance`                       | derived; `live` if `investor_flow_snapshots.source='naver_finance'` rows for trading_day | real query, fresh/stale/missing counts plus latest collected_at                |
| `holdings` / `pending_orders` | —                                     | —                                                | broker-side only                                                               |
| `orderbook_nxt_capability`    | —                                     | —                                                | KRX/NXT only                                                                   |
| `quotes`                      | `naver_finance` (KR)                  | `request_time_only`                              | naver_finance worldstock/domestic quote endpoints, request-time only           |
| `ohlcv`                       | —                                     | —                                                | not in Naver scope for ROB-201                                                 |
| `valuation_fundamentals`      | `naver_finance`                       | `request_time_only`                              | naver_finance financials/profile fetched at request-time, not persisted        |

Two surfaces that are NOT added in this ticket (Phase B): the discussion-signal PoC and the stock-detail enrichment PoC remain as a single response-level `notes` entry pointing to the existing aggregate-only contract docs. Adding them as their own surfaces is deferred until a durable read-model is wired (out of ROB-201 scope).

### Per-symbol delta

`InvestCoverageSymbol.surfaces[]` gains an optional `naver_investor_flow` entry for KR symbols, mirroring the existing `investor_flow` row but filtered to `source='naver_finance'`. The state is computed from the latest snapshot_date for that source. If there is no naver-sourced row for the symbol, the entry is `missing` (so the user sees the gap explicitly).

### Response-level

`InvestCoverageResponse.notes` gains a new entry:
> "Naver appears only as source-candidate or reference. Discussion signal and stock-detail enrichment PoCs remain fixture-backed under aggregate-only contract; see app/services/invest_view_model/naver_*_poc.py."

### Safety properties (carried forward, codified by tests)

- Read-only: no DB writes added; `test_invest_coverage_endpoint_is_read_only_and_exposes_gaps` extended to assert candidate fields do not introduce mutations.
- No external scraping in request path: Naver readiness queries hit only local tables (`investor_flow_snapshots`, `news_articles`).
- Discussion signal stays aggregate-only: nothing in the candidate readiness reads or returns post text; only its presence/readiness is reported.
- No broker/order/watch/order-intent mutation, no scheduler activation, no production DB backfill.

---

## File Structure

**Modify (backend):**
- `app/schemas/invest_coverage.py` — add `CoverageCandidateReadiness`, `CoverageSourceCandidate`, `sourceCandidates` field; replace `reference: str = "toss"` with `references: list[str] = ["toss"]`.
- `app/services/invest_coverage_service.py` — add candidate-builder helpers, wire them into each `_*_surfaces` function, extend `_symbol_rows`.
- `app/routers/invest_api.py` — no change (router already returns the schema; new fields propagate automatically).

**Modify (frontend):**
- `frontend/invest/src/types/coverage.ts` — mirror new types.
- `frontend/invest/src/pages/desktop/DesktopCoveragePage.tsx` — render candidate chips under each surface row.

**Modify (tests):**
- `tests/test_invest_coverage.py` — three new test cases; extend the seeded fixtures with a naver_finance investor flow row and a naver-sourced news article.

**New (docs):**
- This plan file.
- One acceptance-checklist comment in the eventual PR description (template at the end of this plan).

**Do not touch:** any model under `app/models/` (no schema changes); `app/services/naver_finance/` (request-time fetcher, untouched); the discussion-signal and stock-detail PoC modules.

---

## Task 1: Schema additions and `reference` → `references` rename

**Files:**
- Modify: `app/schemas/invest_coverage.py:1-71`
- Test: `tests/test_invest_coverage.py` (new test function `test_coverage_surface_accepts_source_candidates`)

- [ ] **Step 1: Write the failing schema test**

Add to `tests/test_invest_coverage.py`:

```python
import datetime as dt

from app.schemas.invest_coverage import (
    CoverageSourceCandidate,
    InvestCoverageSurface,
)


def test_coverage_surface_accepts_source_candidates_and_references_list():
    surface = InvestCoverageSurface(
        surface="investor_flow",
        label="Investor flow",
        state="fresh",
        sourceOfTruth="investor_flow_snapshots",
        references=["toss", "naver"],
        sourceCandidates=[
            CoverageSourceCandidate(
                name="naver_finance",
                surface="investor_flow",
                kind="secondary_source",
                readiness="live",
                latestAt=dt.datetime(2026, 5, 11, 8, 0, tzinfo=dt.UTC),
                notes=["naver_finance is one of several wired investor-flow sources"],
            ),
        ],
    )
    assert surface.references == ["toss", "naver"]
    assert surface.sourceCandidates[0].readiness == "live"
    assert surface.sourceCandidates[0].kind == "secondary_source"
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/test_invest_coverage.py::test_coverage_surface_accepts_source_candidates_and_references_list -v
```
Expected: FAIL — `ImportError: cannot import name 'CoverageSourceCandidate'`.

- [ ] **Step 3: Implement schema additions**

Replace the contents of `app/schemas/invest_coverage.py` with:

```python
"""ROB-192 + ROB-201 — read-only /invest data coverage schemas.

ROB-201 adds source-candidate readiness so Naver can be reported as a
*candidate* / reference signal without ever being presented as the
source-of-truth.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CoverageState = Literal[
    "fresh",
    "stale",
    "partial",
    "missing",
    "unsupported",
    "error",
    "provider_unwired",
]
CoverageMarket = Literal["kr", "us", "crypto", "all"]

CoverageCandidateReadiness = Literal[
    "live",
    "request_time_only",
    "fixture_backed_poc",
    "aggregate_only_blocked",
    "not_wired",
]
CoverageCandidateKind = Literal["secondary_source", "reference", "candidate"]


class InvestCoverageCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected: int | None = None
    fresh: int = 0
    stale: int = 0
    missing: int = 0
    partial: int = 0
    total: int = 0


class CoverageSourceCandidate(BaseModel):
    """A non-source-of-truth signal attached to a coverage surface.

    Used to report Naver (and future candidate sources) as readiness/reference
    only. Never reuse `CoverageState` here — readiness answers a different
    question (how mature is the candidate source) than state (how fresh is the
    durable read-model).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    surface: str
    kind: CoverageCandidateKind
    readiness: CoverageCandidateReadiness
    latestAt: datetime | None = None
    latestDate: date | None = None
    counts: InvestCoverageCounts | None = None
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class InvestCoverageSurface(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface: str
    label: str
    state: CoverageState
    market: str | None = None
    sourceOfTruth: str
    references: list[str] = Field(default_factory=lambda: ["toss"])
    latestAt: datetime | None = None
    latestDate: date | None = None
    counts: InvestCoverageCounts = Field(default_factory=InvestCoverageCounts)
    staleAfterHours: int | None = None
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    sourceCandidates: list[CoverageSourceCandidate] = Field(default_factory=list)


class InvestCoverageSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    market: str
    surfaces: dict[str, CoverageState] = Field(default_factory=dict)
    latestDates: dict[str, date | None] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class InvestCoverageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: CoverageMarket
    asOf: datetime
    tradingDate: date
    states: list[CoverageState]
    surfaces: list[InvestCoverageSurface]
    symbols: list[InvestCoverageSymbol] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
uv run pytest tests/test_invest_coverage.py::test_coverage_surface_accepts_source_candidates_and_references_list -v
```
Expected: PASS.

- [ ] **Step 5: Run the existing coverage tests to confirm no regression**

Run:
```bash
uv run pytest tests/test_invest_coverage.py -v
```
Expected: existing two tests still PASS (they don't touch `reference`/`references`).

- [ ] **Step 6: Commit**

```bash
git add app/schemas/invest_coverage.py tests/test_invest_coverage.py
git commit -m "feat(ROB-201): add CoverageSourceCandidate schema and references list"
```

---

## Task 2: Service helper — investor_flow Naver candidate (the live case)

**Files:**
- Modify: `app/services/invest_coverage_service.py:441-500` (add helper + wire into `_investor_flow_surfaces`)
- Test: `tests/test_invest_coverage.py` (new test function `test_investor_flow_surface_reports_naver_finance_as_live_candidate`)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_invest_coverage.py`:

```python
@pytest.mark.asyncio
async def test_investor_flow_surface_reports_naver_finance_as_live_candidate(
    db_session,
):
    trading_day = dt.date(2026, 5, 11)
    now = dt.datetime(2026, 5, 11, 8, 0, tzinfo=dt.UTC)

    # Clean & seed: one naver_finance + one kis row for the same trading day.
    await db_session.execute(
        sa.delete(InvestorFlowSnapshot).where(
            InvestorFlowSnapshot.id.in_([9310, 9311])
        )
    )
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(
            KRSymbolUniverse.symbol.in_(["900210", "900211"])
        )
    )
    await db_session.commit()
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol="900210", name="ROB201 NF", exchange="KOSPI", is_active=True
            ),
            KRSymbolUniverse(
                symbol="900211", name="ROB201 KIS", exchange="KOSPI", is_active=True
            ),
            InvestorFlowSnapshot(
                id=9310, market="kr", symbol="900210", snapshot_date=trading_day,
                source="naver_finance", foreign_net=10, institution_net=5,
                individual_net=-15, collected_at=now,
            ),
            InvestorFlowSnapshot(
                id=9311, market="kr", symbol="900211", snapshot_date=trading_day,
                source="kis", foreign_net=20, institution_net=10,
                individual_net=-30, collected_at=now,
            ),
        ]
    )
    await db_session.commit()

    response = await build_invest_coverage(
        db_session, market="kr", as_of=trading_day,
    )
    flow = next(s for s in response.surfaces if s.surface == "investor_flow")

    naver = next(
        (c for c in flow.sourceCandidates if c.name == "naver_finance"), None
    )
    assert naver is not None, "naver_finance candidate must be present on investor_flow"
    assert naver.kind == "secondary_source"
    assert naver.readiness == "live"
    assert naver.counts is not None
    assert naver.counts.fresh >= 1
    # sourceOfTruth must remain durable read-model, NOT naver.
    assert flow.sourceOfTruth == "investor_flow_snapshots"
    assert "toss" in flow.references
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/test_invest_coverage.py::test_investor_flow_surface_reports_naver_finance_as_live_candidate -v
```
Expected: FAIL — `naver_finance candidate must be present on investor_flow` (sourceCandidates is empty).

- [ ] **Step 3: Implement the candidate helper and wire it in**

Edit `app/services/invest_coverage_service.py`. Add a new helper near the other private helpers (after `_state_from_counts`, before `_universe_count`):

```python
async def _naver_finance_investor_flow_candidate(
    db: AsyncSession, trading_day: dt.date
) -> CoverageSourceCandidate:
    """Build the live Naver candidate row for the KR investor_flow surface.

    Reads only investor_flow_snapshots where source='naver_finance' — no
    external scraping, no broker calls.
    """
    row = (
        await db.execute(
            sa.select(
                sa.func.count()
                .filter(InvestorFlowSnapshot.snapshot_date >= trading_day)
                .label("fresh"),
                sa.func.count()
                .filter(InvestorFlowSnapshot.snapshot_date < trading_day)
                .label("stale"),
                sa.func.max(InvestorFlowSnapshot.collected_at).label("latest_at"),
                sa.func.max(InvestorFlowSnapshot.snapshot_date).label("latest_date"),
            ).where(
                InvestorFlowSnapshot.market == "kr",
                InvestorFlowSnapshot.source == "naver_finance",
            )
        )
    ).one()
    fresh = int(row.fresh or 0)
    stale = int(row.stale or 0)
    readiness: CoverageCandidateReadiness = "live" if fresh + stale > 0 else "not_wired"
    return CoverageSourceCandidate(
        name="naver_finance",
        surface="investor_flow",
        kind="secondary_source",
        readiness=readiness,
        latestAt=row.latest_at,
        latestDate=row.latest_date,
        counts=InvestCoverageCounts(fresh=fresh, stale=stale, total=fresh + stale),
        notes=[
            "naver_finance is one of several wired investor-flow sources; investor_flow_snapshots remains the source of truth.",
        ],
    )
```

Add the new imports to the top of the file (alongside existing schema imports):

```python
from app.schemas.invest_coverage import (
    CoverageCandidateReadiness,
    CoverageMarket,
    CoverageSourceCandidate,
    CoverageState,
    InvestCoverageCounts,
    InvestCoverageResponse,
    InvestCoverageSurface,
    InvestCoverageSymbol,
)
```

Modify `_investor_flow_surfaces` (around line 441-500) to attach the candidate on KR rows. Replace the inside of the `for m in markets` loop so that after constructing the surface, the candidate is appended:

```python
        surface = InvestCoverageSurface(
            surface="investor_flow",
            label="Investor flow",
            market=m,
            state=_state_from_counts(fresh=fresh, stale=stale, expected=expected),
            sourceOfTruth="investor_flow_snapshots",
            latestAt=row.latest_at,
            latestDate=row.latest_date,
            counts=InvestCoverageCounts(
                expected=expected,
                fresh=fresh,
                stale=stale,
                missing=missing,
                total=fresh + stale,
            ),
            staleAfterHours=36,
            warnings=[]
            if fresh
            else ["No investor-flow snapshots cover the selected trading date."],
        )
        if m == "kr":
            surface.sourceCandidates.append(
                await _naver_finance_investor_flow_candidate(db, trading_day)
            )
        rows.append(surface)
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
uv run pytest tests/test_invest_coverage.py::test_investor_flow_surface_reports_naver_finance_as_live_candidate -v
```
Expected: PASS.

- [ ] **Step 5: Run all coverage tests**

Run:
```bash
uv run pytest tests/test_invest_coverage.py -v
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/invest_coverage_service.py tests/test_invest_coverage.py
git commit -m "feat(ROB-201): expose naver_finance as live candidate on investor_flow"
```

---

## Task 3: Static Naver candidates — fundamentals, research_reports, quotes, screener_snapshots

**Files:**
- Modify: `app/services/invest_coverage_service.py` (extend `_provider_unwired_surfaces`, `_research_report_surfaces`, `_screener_surfaces`)
- Test: `tests/test_invest_coverage.py` (new test function `test_static_naver_candidates_are_attached_to_request_time_surfaces`)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_invest_coverage.py`:

```python
@pytest.mark.asyncio
async def test_static_naver_candidates_are_attached_to_request_time_surfaces(
    db_session,
):
    response = await build_invest_coverage(
        db_session, market="kr", as_of=dt.date(2026, 5, 11),
    )
    by_surface = {s.surface: s for s in response.surfaces if s.market == "kr"}

    # request_time_only candidates on KR
    for name in ("valuation_fundamentals", "quotes", "screener_snapshots"):
        candidates = by_surface[name].sourceCandidates
        nf = next((c for c in candidates if c.name == "naver_finance"), None)
        assert nf is not None, f"naver_finance candidate missing on {name}"
        assert nf.readiness == "request_time_only"
        assert nf.kind == "candidate"

    # research_reports gets a fixture_backed_poc candidate.
    research = by_surface["research_reports"]
    nv = next((c for c in research.sourceCandidates if c.name == "naver_research"), None)
    assert nv is not None
    assert nv.readiness == "fixture_backed_poc"

    # Response-level note about naver PoCs.
    assert any("Naver" in note for note in response.notes)
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/test_invest_coverage.py::test_static_naver_candidates_are_attached_to_request_time_surfaces -v
```
Expected: FAIL — KeyError or missing candidate.

- [ ] **Step 3: Add static-candidate builders**

In `app/services/invest_coverage_service.py`, add a helper after `_naver_finance_investor_flow_candidate`:

```python
def _naver_static_candidate(
    *,
    name: str,
    surface: str,
    kind: CoverageCandidateKind,
    readiness: CoverageCandidateReadiness,
    note: str,
) -> CoverageSourceCandidate:
    return CoverageSourceCandidate(
        name=name,
        surface=surface,
        kind=kind,
        readiness=readiness,
        notes=[note],
    )
```

Import `CoverageCandidateKind` too:

```python
from app.schemas.invest_coverage import (
    CoverageCandidateKind,
    CoverageCandidateReadiness,
    CoverageMarket,
    CoverageSourceCandidate,
    ...
)
```

Wire candidates into the relevant `_*_surfaces` helpers:

In `_screener_surfaces`, after building each KR/US `InvestCoverageSurface`, before appending to `rows`, attach:

```python
        if m == "kr":
            row_surface.sourceCandidates.append(
                _naver_static_candidate(
                    name="naver_finance",
                    surface="screener_snapshots",
                    kind="candidate",
                    readiness="request_time_only",
                    note="naver_finance valuation calls are request-time only; not persisted to invest_screener_snapshots.",
                )
            )
```

(Where `row_surface` is the local variable holding the surface before append; refactor the existing `rows.append(InvestCoverageSurface(...))` to a temp variable.)

In `_research_report_surfaces`, on the equity branch only (skip on `market='crypto'`), attach:

```python
    surface.sourceCandidates.append(
        _naver_static_candidate(
            name="naver_research",
            surface="research_reports",
            kind="candidate",
            readiness="fixture_backed_poc",
            note="naver_stock_detail_poc exposes Naver research metadata via fixtures; not ingested.",
        )
    )
```

In `_provider_unwired_surfaces`, for surfaces `quotes` and `valuation_fundamentals` (skip `ohlcv`) on KR market, attach:

```python
        if surface == "quotes" and m == "kr":
            surface_row.sourceCandidates.append(
                _naver_static_candidate(
                    name="naver_finance",
                    surface="quotes",
                    kind="candidate",
                    readiness="request_time_only",
                    note="naver_finance worldstock/domestic quote endpoints are available request-time only.",
                )
            )
        elif surface == "valuation_fundamentals" and m == "kr":
            surface_row.sourceCandidates.append(
                _naver_static_candidate(
                    name="naver_finance",
                    surface="valuation_fundamentals",
                    kind="candidate",
                    readiness="request_time_only",
                    note="naver_finance financials/profile endpoints are request-time only; no durable read-model.",
                )
            )
```

(Again, refactor to use a temp `surface_row` variable per loop iteration.)

In `build_invest_coverage`, append a Naver note to the response:

```python
    return InvestCoverageResponse(
        ...
        notes=[
            "Read-only coverage report; auto_trader DB/read models are source of truth.",
            "Toss is used only as a parity benchmark/reference, not as a data source.",
            "Naver appears only as source-candidate or reference; discussion-signal and stock-detail PoCs remain fixture-backed under the aggregate-only contract.",
        ],
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
uv run pytest tests/test_invest_coverage.py::test_static_naver_candidates_are_attached_to_request_time_surfaces -v
```
Expected: PASS.

- [ ] **Step 5: Re-run full coverage suite**

Run:
```bash
uv run pytest tests/test_invest_coverage.py -v
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/invest_coverage_service.py tests/test_invest_coverage.py
git commit -m "feat(ROB-201): attach naver_finance candidates to request-time and PoC surfaces"
```

---

## Task 4: Naver news candidate — derived from `news_articles.source`

**Files:**
- Modify: `app/services/invest_coverage_service.py:246-304` (`_news_surfaces`)
- Test: `tests/test_invest_coverage.py` (new test function `test_news_feed_surface_reports_naver_news_candidate_when_articles_exist`)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_invest_coverage.py`:

```python
@pytest.mark.asyncio
async def test_news_feed_surface_reports_naver_news_candidate_when_articles_exist(
    db_session,
):
    now = dt.datetime(2026, 5, 11, 8, 0, tzinfo=dt.UTC)
    now_naive = now.replace(tzinfo=None)

    await db_session.execute(sa.delete(NewsArticle).where(NewsArticle.id == 9610))
    await db_session.commit()
    db_session.add(
        NewsArticle(
            id=9610,
            url="https://finance.naver.com/item/news?code=900210",
            title="ROB201 naver news",
            source="naver_finance",
            feed_source="naver_finance",
            market="kr",
            keywords=[],
            article_published_at=now_naive,
            scraped_at=now_naive,
            created_at=now_naive,
        )
    )
    await db_session.commit()

    response = await build_invest_coverage(
        db_session, market="kr", as_of=dt.date(2026, 5, 11),
    )
    news = next(s for s in response.surfaces if s.surface == "news_feed")
    nv = next(
        (c for c in news.sourceCandidates if c.name == "naver_finance_news"), None
    )
    assert nv is not None
    assert nv.readiness == "live"
    assert nv.counts is not None
    assert nv.counts.fresh >= 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/test_invest_coverage.py::test_news_feed_surface_reports_naver_news_candidate_when_articles_exist -v
```
Expected: FAIL — no `naver_finance_news` candidate.

- [ ] **Step 3: Implement the helper**

Add helper near `_naver_finance_investor_flow_candidate`:

```python
async def _naver_news_candidate(
    db: AsyncSession, market: str, now: dt.datetime
) -> CoverageSourceCandidate:
    """Build the Naver-news candidate row for the news_feed surface.

    Uses news_articles.source ILIKE 'naver%'. No external scraping at request
    time.
    """
    stale_cutoff = now.replace(tzinfo=None) - dt.timedelta(hours=24)
    row = (
        await db.execute(
            sa.select(
                sa.func.count()
                .filter(NewsArticle.article_published_at >= stale_cutoff)
                .label("fresh"),
                sa.func.count()
                .filter(NewsArticle.article_published_at < stale_cutoff)
                .label("stale"),
                sa.func.max(NewsArticle.article_published_at).label("latest_at"),
            ).where(
                NewsArticle.market == market,
                NewsArticle.source.ilike("naver%"),
            )
        )
    ).one()
    fresh = int(row.fresh or 0)
    stale = int(row.stale or 0)
    readiness: CoverageCandidateReadiness
    if fresh > 0:
        readiness = "live"
    elif stale > 0:
        readiness = "live"  # data exists, just not fresh; the candidate is still wired
    else:
        readiness = "not_wired"
    notes = (
        ["naver_finance is one of several news sources writing to news_articles."]
        if fresh + stale > 0
        else ["No Naver-sourced news articles in the local read-model."]
    )
    return CoverageSourceCandidate(
        name="naver_finance_news",
        surface="news_feed",
        kind="secondary_source" if fresh + stale > 0 else "candidate",
        readiness=readiness,
        latestAt=row.latest_at,
        counts=InvestCoverageCounts(fresh=fresh, stale=stale, total=fresh + stale),
        notes=notes,
    )
```

Wire it into `_news_surfaces`. Refactor `rows.append(InvestCoverageSurface(...))` to use a temp variable, then for KR/US/crypto markets append the candidate:

```python
        surface_row = InvestCoverageSurface(...)
        # Naver news candidate only meaningful for KR
        if m == "kr":
            surface_row.sourceCandidates.append(
                await _naver_news_candidate(db, m, now)
            )
        rows.append(surface_row)
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
uv run pytest tests/test_invest_coverage.py::test_news_feed_surface_reports_naver_news_candidate_when_articles_exist -v
```
Expected: PASS.

- [ ] **Step 5: Re-run full coverage suite**

Run:
```bash
uv run pytest tests/test_invest_coverage.py -v
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/invest_coverage_service.py tests/test_invest_coverage.py
git commit -m "feat(ROB-201): expose naver_finance_news as candidate on news_feed surface"
```

---

## Task 5: Per-symbol `naver_investor_flow` state

**Files:**
- Modify: `app/services/invest_coverage_service.py:722-815` (`_symbol_rows`)
- Test: `tests/test_invest_coverage.py` (new test function `test_symbol_rows_expose_naver_investor_flow_state_for_kr`)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_invest_coverage.py`:

```python
@pytest.mark.asyncio
async def test_symbol_rows_expose_naver_investor_flow_state_for_kr(db_session):
    trading_day = dt.date(2026, 5, 11)
    now = dt.datetime(2026, 5, 11, 8, 0, tzinfo=dt.UTC)

    await db_session.execute(
        sa.delete(InvestorFlowSnapshot).where(
            InvestorFlowSnapshot.id.in_([9320, 9321])
        )
    )
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(
            KRSymbolUniverse.symbol.in_(["900220", "900221"])
        )
    )
    await db_session.commit()
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol="900220", name="ROB201 NF sym", exchange="KOSPI", is_active=True
            ),
            KRSymbolUniverse(
                symbol="900221", name="ROB201 no-NF", exchange="KOSPI", is_active=True
            ),
            InvestorFlowSnapshot(
                id=9320, market="kr", symbol="900220", snapshot_date=trading_day,
                source="naver_finance", foreign_net=1, institution_net=1,
                individual_net=-2, collected_at=now,
            ),
            InvestorFlowSnapshot(
                id=9321, market="kr", symbol="900221", snapshot_date=trading_day,
                source="kis", foreign_net=1, institution_net=1,
                individual_net=-2, collected_at=now,
            ),
        ]
    )
    await db_session.commit()

    response = await build_invest_coverage(
        db_session,
        market="kr",
        symbols=["900220", "900221"],
        as_of=trading_day,
    )
    by_symbol = {row.symbol: row for row in response.symbols}
    assert by_symbol["900220"].surfaces["naver_investor_flow"] == "fresh"
    assert by_symbol["900221"].surfaces["naver_investor_flow"] == "missing"
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/test_invest_coverage.py::test_symbol_rows_expose_naver_investor_flow_state_for_kr -v
```
Expected: FAIL — `KeyError: 'naver_investor_flow'`.

- [ ] **Step 3: Implement the per-symbol naver flow query**

Modify `_symbol_rows` in `app/services/invest_coverage_service.py`. After the existing `investor_dates` block, add a Naver-specific query:

```python
    naver_flow_dates: dict[str, dt.date | None] = {}
    if market == "kr":
        naver_rows = (
            await db.execute(
                sa.select(
                    InvestorFlowSnapshot.symbol,
                    sa.func.max(InvestorFlowSnapshot.snapshot_date),
                )
                .where(
                    InvestorFlowSnapshot.market == "kr",
                    InvestorFlowSnapshot.source == "naver_finance",
                    InvestorFlowSnapshot.symbol.in_(symbols),
                )
                .group_by(InvestorFlowSnapshot.symbol)
            )
        ).all()
        naver_flow_dates = dict(naver_rows)
```

Then inside the `for symbol in symbols:` loop, populate the new surface key:

```python
        if market == "kr":
            surfaces["investor_flow"] = _date_state(latest_flow, trading_day)
            latest_dates["investor_flow"] = latest_flow
            latest_naver_flow = naver_flow_dates.get(symbol)
            surfaces["naver_investor_flow"] = _date_state(
                latest_naver_flow, trading_day
            )
            latest_dates["naver_investor_flow"] = latest_naver_flow
        else:
            surfaces["investor_flow"] = "unsupported"
            latest_dates["investor_flow"] = None
            surfaces["naver_investor_flow"] = "unsupported"
            latest_dates["naver_investor_flow"] = None
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
uv run pytest tests/test_invest_coverage.py::test_symbol_rows_expose_naver_investor_flow_state_for_kr -v
```
Expected: PASS.

- [ ] **Step 5: Re-run full coverage suite**

Run:
```bash
uv run pytest tests/test_invest_coverage.py -v
```
Expected: all PASS (including the original `test_build_invest_coverage_reports_fresh_partial_and_provider_unwired`, which seeds a naver_finance flow row for 900201 — that test's symbol assertions still hold because we only ADD a key, never remove).

- [ ] **Step 6: Commit**

```bash
git add app/services/invest_coverage_service.py tests/test_invest_coverage.py
git commit -m "feat(ROB-201): expose per-symbol naver_investor_flow state in coverage"
```

---

## Task 6: Router-level integration test — endpoint surfaces new fields

**Files:**
- Test: `tests/test_invest_coverage.py` (extend `test_invest_coverage_endpoint_is_read_only_and_exposes_gaps` OR add new test `test_coverage_endpoint_exposes_naver_candidates_field`)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_invest_coverage.py`:

```python
@pytest.mark.asyncio
async def test_coverage_endpoint_exposes_naver_candidates_field_on_kr(
    app: FastAPI, db_session
):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/invest/api/coverage?market=kr")
    assert r.status_code == 200
    payload = r.json()

    # At least one KR surface lists naver_finance as a candidate.
    flat_candidates = [
        c for surface in payload["surfaces"]
        for c in surface.get("sourceCandidates", [])
    ]
    naver_names = {c["name"] for c in flat_candidates}
    assert "naver_finance" in naver_names

    # `references` is the plural list now.
    for surface in payload["surfaces"]:
        assert isinstance(surface.get("references"), list)
        assert "toss" in surface["references"]

    # Response-level note flags Naver-as-candidate posture.
    assert any("Naver" in note for note in payload["notes"])
```

- [ ] **Step 2: Run the test to verify it fails or passes**

Run:
```bash
uv run pytest tests/test_invest_coverage.py::test_coverage_endpoint_exposes_naver_candidates_field_on_kr -v
```
Expected: PASS (since Tasks 1–5 already implement the fields). If it FAILs, fix the gap before continuing.

- [ ] **Step 3: Commit**

```bash
git add tests/test_invest_coverage.py
git commit -m "test(ROB-201): assert /invest/api/coverage exposes naver candidates"
```

---

## Task 7: Frontend types and rendering

**Files:**
- Modify: `frontend/invest/src/types/coverage.ts`
- Modify: `frontend/invest/src/pages/desktop/DesktopCoveragePage.tsx`

- [ ] **Step 1: Read the current frontend types**

Run:
```bash
cat frontend/invest/src/types/coverage.ts
```

- [ ] **Step 2: Add `CoverageSourceCandidate` and update `InvestCoverageSurface`**

Modify `frontend/invest/src/types/coverage.ts` so it mirrors the backend:

```typescript
export type CoverageState =
  | "fresh"
  | "stale"
  | "partial"
  | "missing"
  | "unsupported"
  | "error"
  | "provider_unwired";

export type CoverageCandidateReadiness =
  | "live"
  | "request_time_only"
  | "fixture_backed_poc"
  | "aggregate_only_blocked"
  | "not_wired";

export type CoverageCandidateKind = "secondary_source" | "reference" | "candidate";

export type InvestCoverageCounts = {
  expected?: number | null;
  fresh: number;
  stale: number;
  missing: number;
  partial: number;
  total: number;
};

export type CoverageSourceCandidate = {
  name: string;
  surface: string;
  kind: CoverageCandidateKind;
  readiness: CoverageCandidateReadiness;
  latestAt?: string | null;
  latestDate?: string | null;
  counts?: InvestCoverageCounts | null;
  warnings: string[];
  notes: string[];
};

export type InvestCoverageSurface = {
  surface: string;
  label: string;
  state: CoverageState;
  market?: string | null;
  sourceOfTruth: string;
  references: string[];
  latestAt?: string | null;
  latestDate?: string | null;
  counts: InvestCoverageCounts;
  staleAfterHours?: number | null;
  warnings: string[];
  notes: string[];
  sourceCandidates: CoverageSourceCandidate[];
};

export type InvestCoverageSymbol = {
  symbol: string;
  market: string;
  surfaces: Record<string, CoverageState>;
  latestDates: Record<string, string | null>;
  warnings: string[];
};

export type InvestCoverageResponse = {
  market: "kr" | "us" | "crypto" | "all";
  asOf: string;
  tradingDate: string;
  states: CoverageState[];
  surfaces: InvestCoverageSurface[];
  symbols: InvestCoverageSymbol[];
  gaps: string[];
  notes: string[];
};
```

- [ ] **Step 3: Render candidates as subordinate chips under each row**

In `frontend/invest/src/pages/desktop/DesktopCoveragePage.tsx`, add a `READINESS_LABEL` and `READINESS_COLOR` near `STATE_LABEL`:

```typescript
const READINESS_LABEL: Record<CoverageCandidateReadiness, string> = {
  live: "live",
  request_time_only: "request-time",
  fixture_backed_poc: "PoC",
  aggregate_only_blocked: "blocked",
  not_wired: "미연결",
};

const READINESS_COLOR: Record<CoverageCandidateReadiness, string> = {
  live: "#0ea5e9",
  request_time_only: "#6366f1",
  fixture_backed_poc: "#a16207",
  aggregate_only_blocked: "#9333ea",
  not_wired: "#64748b",
};
```

Add the import:

```typescript
import type {
  CoverageState,
  CoverageCandidateReadiness,
  InvestCoverageResponse,
  InvestCoverageSurface,
} from "../../types/coverage";
```

Modify `SurfaceRow` so the candidate chips render under the Surface column (not source-of-truth — that stays as the authoritative cell):

```typescript
function SurfaceRow({ surface }: { surface: InvestCoverageSurface }) {
  const latest = surface.latestDate ?? surface.latestAt ?? "-";
  const counts = surface.counts;
  return (
    <tr>
      <td style={{ padding: "12px 10px", borderBottom: "1px solid var(--divider)" }}>
        <div style={{ fontWeight: 800 }}>{surface.label}</div>
        <div style={{ color: "var(--fg-3)", fontSize: 12 }}>{surface.surface}</div>
        {surface.sourceCandidates.length > 0 && (
          <div style={{ marginTop: 6, display: "flex", gap: 6, flexWrap: "wrap" }}>
            {surface.sourceCandidates.map((c) => (
              <span
                key={c.name}
                title={c.notes[0] ?? c.warnings[0] ?? ""}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 4,
                  borderRadius: 8,
                  padding: "2px 6px",
                  fontSize: 11,
                  fontWeight: 700,
                  color: "white",
                  background: READINESS_COLOR[c.readiness],
                }}
              >
                {c.name} · {READINESS_LABEL[c.readiness]}
              </span>
            ))}
          </div>
        )}
      </td>
      {/* …rest of the cells unchanged… */}
    </tr>
  );
}
```

(Keep the rest of the row untouched.)

- [ ] **Step 4: Verify the bundle builds**

Run:
```bash
cd frontend/invest && npm run build
```
Expected: PASS, no TypeScript errors. Bundle size delta is small.

- [ ] **Step 5: Smoke-check the dev server**

Run (in a separate shell):
```bash
make dev
```
Open `http://localhost:8000/invest/coverage/` in a browser (or wherever the route resolves). Confirm:
- Investor flow row shows a `naver_finance · live` chip.
- News feed row shows a `naver_finance_news · live` (or `missing`) chip.
- Source-of-truth column still reads `investor_flow_snapshots` (Naver is NOT presented as source-of-truth).

If you cannot run the dev server, say so explicitly in the PR description; do not claim UI tested.

- [ ] **Step 6: Commit**

```bash
git add frontend/invest/src/types/coverage.ts frontend/invest/src/pages/desktop/DesktopCoveragePage.tsx
git commit -m "feat(ROB-201): render Naver source-candidate chips on coverage rows"
```

---

## Task 8: Sweep and final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run:
```bash
uv run pytest tests/test_invest_coverage.py -v
uv run pytest tests/test_invest_api_router_safety.py -v 2>/dev/null || true
```
Expected: all PASS.

- [ ] **Step 2: Lint and typecheck**

Run:
```bash
make lint
make typecheck
```
Expected: no new errors.

- [ ] **Step 3: Grep for stale `reference: "toss"` references in code or fixtures**

Run via Grep tool (pattern `\breference\b.*toss`, glob `app/**/*.py`). Confirm no place still writes the old singular `reference="toss"` style except where intentionally migrated to `references=["toss"]`.

- [ ] **Step 4: Confirm safety invariants**

Manually audit `app/services/invest_coverage_service.py`:
- No `httpx`, `requests`, `aiohttp`, or `naver_finance.fetch_*` calls in the new candidate helpers — only `db.execute(...)`.
- No DB INSERT/UPDATE/DELETE.
- No `asyncio.create_task` spawning collectors.
- Discussion-signal text fields are not read or returned anywhere.

- [ ] **Step 5: Commit the housekeeping if any was needed**

If steps 3–4 surfaced any stale references, fix them and commit with `chore(ROB-201): …`.

---

## Acceptance Checklist (paste into PR description)

Coverage contract:
- [ ] `CoverageSourceCandidate` type added with `name`, `surface`, `kind` (`secondary_source | reference | candidate`), `readiness` (`live | request_time_only | fixture_backed_poc | aggregate_only_blocked | not_wired`), optional `latestAt`, `latestDate`, `counts`, `warnings`, `notes`.
- [ ] `InvestCoverageSurface.sourceCandidates` defaults to empty list.
- [ ] `InvestCoverageSurface.reference: str = "toss"` replaced by `references: list[str] = ["toss"]`.
- [ ] `InvestCoverageResponse.notes` flags Naver candidate / PoC posture.
- [ ] `investor_flow` (KR) lists a `naver_finance` candidate with `kind="secondary_source"` and `readiness="live"` when rows exist.
- [ ] `news_feed` (KR) lists a `naver_finance_news` candidate with readiness derived from `news_articles.source ILIKE 'naver%'`.
- [ ] `screener_snapshots`, `quotes`, `valuation_fundamentals` (KR) each list a `naver_finance` candidate with `readiness="request_time_only"`.
- [ ] `research_reports` lists a `naver_research` candidate with `readiness="fixture_backed_poc"`.
- [ ] Per-symbol coverage exposes `naver_investor_flow` state on KR symbols.

Safety:
- [ ] No new DB writes; `test_invest_coverage_endpoint_is_read_only_and_exposes_gaps` still passes.
- [ ] No external Naver HTTP call in the coverage request path — all readiness derived from local tables.
- [ ] Discussion-signal aggregate-only contract preserved (no post text fields touched).
- [ ] No broker / order / watch / order-intent mutation introduced.
- [ ] No scheduler activation or production backfill.
- [ ] Worktree-only changes: all edits inside `/Users/mgh3326/worktrees/auto_trader/rob-201-naver-coverage` — confirm `git status` shows no edits in `/Users/mgh3326/services/auto_trader/current` or `/Users/mgh3326/work/auto_trader`.

Tests:
- [ ] `test_coverage_surface_accepts_source_candidates_and_references_list` PASS
- [ ] `test_investor_flow_surface_reports_naver_finance_as_live_candidate` PASS
- [ ] `test_static_naver_candidates_are_attached_to_request_time_surfaces` PASS
- [ ] `test_news_feed_surface_reports_naver_news_candidate_when_articles_exist` PASS
- [ ] `test_symbol_rows_expose_naver_investor_flow_state_for_kr` PASS
- [ ] `test_coverage_endpoint_exposes_naver_candidates_field_on_kr` PASS
- [ ] Existing two coverage tests still PASS.
- [ ] `make lint` and `make typecheck` pass.

Frontend:
- [ ] `frontend/invest/src/types/coverage.ts` mirrors the backend schema.
- [ ] `DesktopCoveragePage` renders readiness chips for each candidate under the Surface column without overriding the `sourceOfTruth` cell.
- [ ] `npm run build` succeeds; manual browser smoke confirms chips appear for KR investor_flow, news_feed, screener, quotes, valuation_fundamentals, research_reports.

Out of scope / Phase B (record explicitly, do not implement here):
- Adding `naver_discussion_signal` and `naver_stock_detail_enrichment` as standalone surfaces (still PoC-only).
- Persisting Naver request-time fetches to a durable read-model so they can graduate from `request_time_only` to `live`.
- Adding US-market Naver candidates (Naver worldstock parity for US tickers is not yet relevant to /invest coverage).
