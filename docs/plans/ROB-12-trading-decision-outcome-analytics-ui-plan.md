# ROB-12 — Trading Decision Outcome Analytics UI Implementation Plan

> **For agentic workers (Codex --yolo):** Implement task by task in order. Tasks are TDD-style; write the failing test first, then the implementation. Run the listed validation commands at every checkpoint. Use checkbox `- [ ]` syntax for tracking. **Do not** add new dependencies, schema changes, or live broker code paths.

**Goal:** Render and record outcome marks on the Trading Decision SPA, and add a compact server-side analytics view comparing all five tracks (`accepted_live`, `accepted_paper`, `rejected_counterfactual`, `analyst_alternative`, `user_alternative`) across all six horizons (`1h`, `4h`, `1d`, `3d`, `7d`, `final`) for a single session.

**Architecture:**
- Backend: a single new additive endpoint (`GET /trading/api/decisions/{session_uuid}/analytics`) plus a small service helper. Outcome write/read APIs already exist and are reused unchanged.
- Frontend: three new components (`OutcomesPanel`, `OutcomeMarkForm`, `AnalyticsMatrix`), a `useSessionAnalytics` hook, and three new API client functions. All wired into the existing `SessionDetailPage`. No new routes.
- No DB schema changes. No new packages. No live broker / KIS / Upbit imports anywhere.

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2.x async / Pydantic v2 (backend); React 19 / react-router-dom 7 / Vite 8 / Vitest 4 (frontend, no new deps).

---

## 1. Discovered State (anchor for the plan)

### Backend (already merged via ROB-1 / ROB-2)

| Concern | Location | Status |
|---|---|---|
| Outcome model | `app/models/trading_decision.py:289-340` (`TradingDecisionOutcome`) | ✅ ready |
| `TrackKind` enum | `app/models/trading_decision.py:60-65` | ✅ ready |
| `OutcomeHorizon` enum | `app/models/trading_decision.py:68-74` (`h1=1h, h4=4h, d1=1d, d3=3d, d7=7d, final=final`) | ✅ ready |
| Unique index `(proposal_id, counterfactual_id, track_kind, horizon)` | `app/models/trading_decision.py:304-312` (`postgresql_nulls_not_distinct=True`) | ✅ ready |
| `record_outcome_mark` service | `app/services/trading_decision_service.py:201-237` | ✅ ready, reuse |
| `OutcomeCreateRequest` / `OutcomeDetail` schemas | `app/schemas/trading_decisions.py:261-292` | ✅ ready |
| `POST /trading/api/proposals/{proposal_uuid}/outcomes` | `app/routers/trading_decisions.py:431-487` | ✅ ready, reuse unchanged |
| Outcomes nested in session detail | `app/routers/trading_decisions.py:109` (`outcomes=[_to_outcome_detail(o) ...]`) | ✅ ready, reuse |
| Router safety test (forbids KIS/Upbit imports) | `tests/test_trading_decisions_router_safety.py` | ⚠ keep green |
| Service tests for outcomes | `tests/models/test_trading_decision_service.py:440-495` | ✅ exist |
| Router tests for outcomes | `tests/test_trading_decisions_router.py:680-755, 825-928` | ✅ exist |
| Analytics endpoints | none | ❌ missing — add in this PR |

### Frontend (already merged via ROB-6 / ROB-7 / ROB-11)

| Concern | Location | Status |
|---|---|---|
| Vite SPA root | `frontend/trading-decision/` | ✅ |
| Router | `frontend/trading-decision/src/routes.tsx` (`basename: "/trading/decisions"`) | ✅ |
| API base | `frontend/trading-decision/src/api/client.ts:1` (`/trading/api`) | ✅ |
| Existing typed `OutcomeDetail`, `TrackKind`, `OutcomeHorizon` | `frontend/trading-decision/src/api/types.ts:30-36, 87-98, 128` | ✅ already imported into `ProposalDetail.outcomes`, but **not rendered anywhere** |
| `getDecisions` / `getSession` / `respondToProposal` | `frontend/trading-decision/src/api/decisions.ts` | ✅ |
| `LinkedActionsPanel` (renders only `actions` + `counterfactuals`) | `frontend/trading-decision/src/components/LinkedActionsPanel.tsx` | ✅ — outcome marks NOT rendered |
| `useDecisionSession` hook with `refetch()` | `frontend/trading-decision/src/hooks/useDecisionSession.ts` | ✅ — extend pattern |
| Test fixtures (`makeProposal({outcomes: []})`) | `frontend/trading-decision/src/test/fixtures.ts:76` | ⚠ extend |
| `mockFetch` test helper (no MSW) | `frontend/trading-decision/src/test/server.ts` | ✅ reuse |
| Decimal formatter `formatDecimal` | `frontend/trading-decision/src/format/decimal.ts` | ✅ reuse for pnl_pct / pnl_amount |
| Outcome rendering UI | none | ❌ missing — add in this PR |
| Outcome mark form | none | ❌ missing — add in this PR |
| Analytics view | none | ❌ missing — add in this PR |

### Roadmap source (Prompt 5)

`/Users/mgh3326/.hermes/workspace/prompts/auto_trader_trading_decision_workspace_roadmap.md` lines 263-293 — confirmed scope: outcome read/write API (already done) + UI sections for outcome marks + analytics comparing the five tracks across the six horizons. Future-note explicitly says **do not** implement Hermes profile routing in this PR.

---

## 2. Scope Decisions (minimum safe slice)

### In scope (this PR)

1. **Backend**: one new endpoint and one new service helper.
   - `GET /trading/api/decisions/{session_uuid}/analytics` — server-side aggregation of all marks for one session keyed by `(track_kind, horizon)`. Returns count, mean `pnl_pct`, sum `pnl_amount`, latest `marked_at`, distinct proposal count. Keeps the SPA dumb and lets us add tests cheaply.
2. **Frontend**: render outcomes (already fetched) and add a creation form on the session detail page. Add a compact analytics matrix.

### Out of scope (deferred)

- Hermes profile routing (`day-trader` etc.) — explicitly deferred by roadmap.
- Cross-session analytics (global dashboard).
- Live broker / KIS / Upbit calls of any kind (forbidden by safety test).
- Editing / deleting outcome marks (only create + read in this PR).
- Charting libraries — render as a plain HTML table + summary cards. No Recharts/Chart.js.
- New SPA routes — everything inlined into `SessionDetailPage`.
- DB schema changes / new migrations.

### Why a new endpoint instead of client-side aggregation

The data needed is already returned in `GET /trading/api/decisions/{session_uuid}` via `ProposalDetail.outcomes[]`. Either approach works for the small data sizes expected. We pick **server-side** because:
- Easier to unit-test aggregation logic in Python with existing fixtures.
- Keeps the SPA presentational; the matrix can be rendered as-is.
- Future-proofs for cross-session aggregation without reshaping the client.
- The router safety test continues to pass — pure SQL, no broker imports.

If implementer hits a blocker on the SQL aggregation, it is acceptable to fall back to in-Python aggregation inside the service (loop over eager-loaded proposals/outcomes) — the endpoint contract is what matters.

---

## 3. File Structure

### Backend — files to modify / create

| Path | Action | Responsibility |
|---|---|---|
| `app/services/trading_decision_service.py` | Modify | Add `aggregate_session_outcomes()` async helper |
| `app/schemas/trading_decisions.py` | Modify | Add `SessionAnalyticsCell` and `SessionAnalyticsResponse` Pydantic models |
| `app/routers/trading_decisions.py` | Modify | Add `GET /api/decisions/{session_uuid}/analytics` route |
| `tests/models/test_trading_decision_service.py` | Modify | Add aggregation tests using existing test DB fixtures |
| `tests/test_trading_decisions_router.py` | Modify | Add analytics router unit tests (mock service) |

### Frontend — files to modify / create

| Path | Action | Responsibility |
|---|---|---|
| `frontend/trading-decision/src/api/types.ts` | Modify | Add `SessionAnalyticsCell`, `SessionAnalyticsResponse`, `OutcomeCreateRequest` types |
| `frontend/trading-decision/src/api/decisions.ts` | Modify | Add `getSessionAnalytics`, `createOutcomeMark` |
| `frontend/trading-decision/src/hooks/useSessionAnalytics.ts` | **Create** | Fetch analytics for a session UUID |
| `frontend/trading-decision/src/components/OutcomesPanel.tsx` | **Create** | Render `proposal.outcomes[]` grouped by `track_kind`/`horizon` |
| `frontend/trading-decision/src/components/OutcomesPanel.module.css` | **Create** | Styling |
| `frontend/trading-decision/src/components/OutcomeMarkForm.tsx` | **Create** | Form to POST a new outcome mark |
| `frontend/trading-decision/src/components/OutcomeMarkForm.module.css` | **Create** | Styling |
| `frontend/trading-decision/src/components/AnalyticsMatrix.tsx` | **Create** | Track × horizon grid showing aggregate PnL per cell |
| `frontend/trading-decision/src/components/AnalyticsMatrix.module.css` | **Create** | Styling |
| `frontend/trading-decision/src/components/ProposalRow.tsx` | Modify | Render `<OutcomesPanel>` and `<OutcomeMarkForm>` inside collapsed details |
| `frontend/trading-decision/src/pages/SessionDetailPage.tsx` | Modify | Mount `<AnalyticsMatrix>` near top of page |
| `frontend/trading-decision/src/test/fixtures.ts` | Modify | Add `makeOutcome()`, `makeAnalyticsResponse()` |
| `frontend/trading-decision/src/__tests__/OutcomesPanel.test.tsx` | **Create** | Render tests |
| `frontend/trading-decision/src/__tests__/OutcomeMarkForm.test.tsx` | **Create** | Submit + validation tests |
| `frontend/trading-decision/src/__tests__/AnalyticsMatrix.test.tsx` | **Create** | Cell rendering tests |
| `frontend/trading-decision/src/__tests__/api.decisions.test.ts` | Modify | Cover new client functions |
| `frontend/trading-decision/src/__tests__/SessionDetailPage.test.tsx` | Modify | Cover analytics matrix integration |

---

## 4. API Contract (new — additive)

### `GET /trading/api/decisions/{session_uuid}/analytics`

**Auth:** cookie session via `get_authenticated_user`. Cross-user access returns 404 (matches existing pattern).

**Path params:** `session_uuid: UUID`

**Response 200** — `SessionAnalyticsResponse`:

```json
{
  "session_uuid": "…",
  "generated_at": "2026-04-28T06:00:00Z",
  "tracks": ["accepted_live", "accepted_paper", "rejected_counterfactual",
             "analyst_alternative", "user_alternative"],
  "horizons": ["1h", "4h", "1d", "3d", "7d", "final"],
  "cells": [
    {
      "track_kind": "accepted_live",
      "horizon": "1h",
      "outcome_count": 3,
      "proposal_count": 2,
      "mean_pnl_pct": "1.5320",
      "sum_pnl_amount": "120.4500",
      "latest_marked_at": "2026-04-28T07:00:00Z"
    }
    // …one entry per (track_kind, horizon) that has at least one outcome
  ]
}
```

- Cells are **sparse** — only emit a row when at least one outcome exists for that `(track_kind, horizon)`. The frontend fills missing cells with `—`.
- `mean_pnl_pct`, `sum_pnl_amount` are decimal strings (Pydantic Decimal default), or `null` if no outcome in that cell has a non-null pnl field.
- `outcome_count` is the number of marks; `proposal_count` is `count(distinct proposal_id)` so the UI can disambiguate "5 marks across 1 proposal" vs "5 marks across 5 proposals".

**Errors:**
- 404 if session not found OR session does not belong to current user (do not leak existence).
- 401 from existing auth dependency.

---

## 5. Implementation Tasks

> **TDD discipline:** for each task, write the test first, run it red, implement, run it green, commit. Each step ≤ 5 minutes.

### Task B1: Service helper `aggregate_session_outcomes`

**Files:**
- Modify: `app/services/trading_decision_service.py`
- Test: `tests/models/test_trading_decision_service.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/models/test_trading_decision_service.py` (use the same `pytest.mark.integration` async style used by `test_record_1h_and_1d_outcome_marks` near line 440).

```python
@pytest.mark.integration
async def test_aggregate_session_outcomes_groups_by_track_and_horizon() -> None:
    """Aggregate marks across two proposals of one session into (track, horizon) cells."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.models.trading_decision import OutcomeHorizon, TrackKind
    from app.services.trading_decision_service import (
        aggregate_session_outcomes,
        create_counterfactual_track,
        create_decision_session,
        add_decision_proposals,
        record_outcome_mark,
    )

    async with AsyncTestSession() as session:  # use the existing harness in this file
        sess = await create_decision_session(
            session,
            user_id=1,
            source_profile="roadmap",
            generated_at=datetime.now(UTC),
        )
        proposals = await add_decision_proposals(
            session,
            session_id=sess.id,
            items=[
                {"symbol": "BTC", "instrument_type": "crypto",
                 "proposal_kind": "trim", "side": "sell",
                 "original_payload": {}},
                {"symbol": "ETH", "instrument_type": "crypto",
                 "proposal_kind": "add", "side": "buy",
                 "original_payload": {}},
            ],
        )

        # accepted_live marks at 1h on both proposals
        for p in proposals:
            await record_outcome_mark(
                session,
                proposal_id=p.id,
                track_kind=TrackKind.accepted_live,
                horizon=OutcomeHorizon.h1,
                price_at_mark=Decimal("100"),
                pnl_pct=Decimal("2.0"),
                pnl_amount=Decimal("10"),
                marked_at=datetime.now(UTC),
            )

        # rejected_counterfactual at 1d on first proposal
        cf = await create_counterfactual_track(
            session,
            proposal_id=proposals[0].id,
            track_kind=TrackKind.rejected_counterfactual,
            baseline_price=Decimal("100"),
            baseline_at=datetime.now(UTC),
            payload={},
        )
        await record_outcome_mark(
            session,
            proposal_id=proposals[0].id,
            counterfactual_id=cf.id,
            track_kind=TrackKind.rejected_counterfactual,
            horizon=OutcomeHorizon.d1,
            price_at_mark=Decimal("110"),
            pnl_pct=Decimal("-1.0"),
            pnl_amount=Decimal("-5"),
            marked_at=datetime.now(UTC),
        )
        await session.flush()

        cells = await aggregate_session_outcomes(
            session, session_uuid=sess.session_uuid, user_id=1
        )

        # one cell per (track_kind, horizon) that has marks
        keyed = {(c.track_kind, c.horizon): c for c in cells}
        assert (TrackKind.accepted_live.value, OutcomeHorizon.h1.value) in keyed
        live_1h = keyed[(TrackKind.accepted_live.value, OutcomeHorizon.h1.value)]
        assert live_1h.outcome_count == 2
        assert live_1h.proposal_count == 2
        assert live_1h.mean_pnl_pct == Decimal("2.0000")
        assert live_1h.sum_pnl_amount == Decimal("20.0000")

        rej_1d = keyed[
            (TrackKind.rejected_counterfactual.value, OutcomeHorizon.d1.value)
        ]
        assert rej_1d.outcome_count == 1
        assert rej_1d.proposal_count == 1


@pytest.mark.integration
async def test_aggregate_session_outcomes_returns_none_for_other_user() -> None:
    """Cross-user access yields None (treated as 404 by router)."""
    from datetime import UTC, datetime

    from app.services.trading_decision_service import (
        aggregate_session_outcomes,
        create_decision_session,
    )

    async with AsyncTestSession() as session:
        sess = await create_decision_session(
            session, user_id=1, source_profile="x",
            generated_at=datetime.now(UTC),
        )
        await session.flush()
        result = await aggregate_session_outcomes(
            session, session_uuid=sess.session_uuid, user_id=999
        )
        assert result is None
```

> ℹ The exact `AsyncTestSession` / DB-session-factory name in the test file should be reused as-is from the existing `test_record_1h_and_1d_outcome_marks` test (look near line 440-495). Do not introduce a new harness.

- [ ] **Step 2: Run the test red**

```
uv run pytest tests/models/test_trading_decision_service.py::test_aggregate_session_outcomes_groups_by_track_and_horizon -v
```
Expected: `ImportError: cannot import name 'aggregate_session_outcomes'`.

- [ ] **Step 3: Implement the helper**

Append to `app/services/trading_decision_service.py` after `record_outcome_mark` (around line 238). Use a single SQL `GROUP BY (track_kind, horizon)` query that joins through proposal → session and filters by `user_id`. Return `None` if the session does not exist for that user.

```python
from dataclasses import dataclass
from sqlalchemy import select, func, distinct

from app.models.trading_decision import (
    TradingDecisionOutcome,
    TradingDecisionProposal,
    TradingDecisionSession,
)


@dataclass(frozen=True)
class AggregatedOutcomeCell:
    track_kind: str
    horizon: str
    outcome_count: int
    proposal_count: int
    mean_pnl_pct: Decimal | None
    sum_pnl_amount: Decimal | None
    latest_marked_at: datetime | None


async def aggregate_session_outcomes(
    session: AsyncSession,
    *,
    session_uuid: UUID,
    user_id: int,
) -> list[AggregatedOutcomeCell] | None:
    """Aggregate outcomes for one session, grouped by (track_kind, horizon).

    Returns None if the session does not exist or does not belong to user_id.
    """
    sess_row = await session.execute(
        select(TradingDecisionSession.id)
        .where(
            TradingDecisionSession.session_uuid == session_uuid,
            TradingDecisionSession.user_id == user_id,
        )
    )
    sess_id = sess_row.scalar_one_or_none()
    if sess_id is None:
        return None

    stmt = (
        select(
            TradingDecisionOutcome.track_kind,
            TradingDecisionOutcome.horizon,
            func.count(TradingDecisionOutcome.id).label("outcome_count"),
            func.count(distinct(TradingDecisionOutcome.proposal_id))
                .label("proposal_count"),
            func.avg(TradingDecisionOutcome.pnl_pct).label("mean_pnl_pct"),
            func.sum(TradingDecisionOutcome.pnl_amount).label("sum_pnl_amount"),
            func.max(TradingDecisionOutcome.marked_at).label("latest_marked_at"),
        )
        .join(
            TradingDecisionProposal,
            TradingDecisionProposal.id == TradingDecisionOutcome.proposal_id,
        )
        .where(TradingDecisionProposal.session_id == sess_id)
        .group_by(
            TradingDecisionOutcome.track_kind,
            TradingDecisionOutcome.horizon,
        )
        .order_by(
            TradingDecisionOutcome.track_kind,
            TradingDecisionOutcome.horizon,
        )
    )

    rows = (await session.execute(stmt)).all()
    return [
        AggregatedOutcomeCell(
            track_kind=row.track_kind,
            horizon=row.horizon,
            outcome_count=int(row.outcome_count),
            proposal_count=int(row.proposal_count),
            mean_pnl_pct=row.mean_pnl_pct,
            sum_pnl_amount=row.sum_pnl_amount,
            latest_marked_at=row.latest_marked_at,
        )
        for row in rows
    ]
```

- [ ] **Step 4: Run tests green**

```
uv run pytest tests/models/test_trading_decision_service.py::test_aggregate_session_outcomes_groups_by_track_and_horizon tests/models/test_trading_decision_service.py::test_aggregate_session_outcomes_returns_none_for_other_user -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add app/services/trading_decision_service.py tests/models/test_trading_decision_service.py
git commit -m "feat(rob-12): aggregate session outcomes by track/horizon"
```

---

### Task B2: Schemas `SessionAnalyticsCell` / `SessionAnalyticsResponse`

**Files:**
- Modify: `app/schemas/trading_decisions.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trading_decisions_router.py` (does not need DB — pure Pydantic validation):

```python
@pytest.mark.unit
def test_session_analytics_response_serializes_decimal_strings():
    from datetime import UTC, datetime
    from decimal import Decimal
    from app.schemas.trading_decisions import (
        SessionAnalyticsCell,
        SessionAnalyticsResponse,
    )
    payload = SessionAnalyticsResponse(
        session_uuid=uuid4(),
        generated_at=datetime.now(UTC),
        tracks=[
            "accepted_live", "accepted_paper", "rejected_counterfactual",
            "analyst_alternative", "user_alternative",
        ],
        horizons=["1h", "4h", "1d", "3d", "7d", "final"],
        cells=[
            SessionAnalyticsCell(
                track_kind="accepted_live",
                horizon="1h",
                outcome_count=2,
                proposal_count=2,
                mean_pnl_pct=Decimal("1.5"),
                sum_pnl_amount=Decimal("12.34"),
                latest_marked_at=datetime.now(UTC),
            )
        ],
    )
    body = payload.model_dump(mode="json")
    assert body["cells"][0]["mean_pnl_pct"] == "1.5"
    assert body["cells"][0]["sum_pnl_amount"] == "12.34"
    assert body["tracks"][0] == "accepted_live"
```

- [ ] **Step 2: Run red**

```
uv run pytest tests/test_trading_decisions_router.py::test_session_analytics_response_serializes_decimal_strings -v
```

- [ ] **Step 3: Implement schemas**

Append to `app/schemas/trading_decisions.py` after the existing `OutcomeDetail` (around line 292):

```python
# ========== Analytics Schemas ==========


class SessionAnalyticsCell(BaseModel):
    track_kind: TrackKindLiteral
    horizon: OutcomeHorizonLiteral
    outcome_count: int
    proposal_count: int
    mean_pnl_pct: Decimal | None = None
    sum_pnl_amount: Decimal | None = None
    latest_marked_at: datetime | None = None


class SessionAnalyticsResponse(BaseModel):
    session_uuid: UUID
    generated_at: datetime
    tracks: list[TrackKindLiteral]
    horizons: list[OutcomeHorizonLiteral]
    cells: list[SessionAnalyticsCell]
```

- [ ] **Step 4: Run green**

Same command as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```
git add app/schemas/trading_decisions.py tests/test_trading_decisions_router.py
git commit -m "feat(rob-12): session analytics response schema"
```

---

### Task B3: Router `GET /trading/api/decisions/{session_uuid}/analytics`

**Files:**
- Modify: `app/routers/trading_decisions.py`
- Test: `tests/test_trading_decisions_router.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_trading_decisions_router.py` (mirror the mocking style of `test_outcome_mark_duplicate_horizon_returns_409`):

```python
@pytest.mark.unit
def test_get_session_analytics_happy_path():
    from datetime import UTC, datetime
    from decimal import Decimal
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service
    from app.services.trading_decision_service import AggregatedOutcomeCell

    app = FastAPI()
    app.include_router(trading_decisions.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=7)

    session_uuid = uuid4()
    trading_decision_service.aggregate_session_outcomes = AsyncMock(
        return_value=[
            AggregatedOutcomeCell(
                track_kind="accepted_live",
                horizon="1h",
                outcome_count=3,
                proposal_count=2,
                mean_pnl_pct=Decimal("1.5"),
                sum_pnl_amount=Decimal("12.34"),
                latest_marked_at=datetime.now(UTC),
            )
        ]
    )

    client = TestClient(app)
    res = client.get(f"/trading/api/decisions/{session_uuid}/analytics")
    assert res.status_code == 200
    body = res.json()
    assert body["session_uuid"] == str(session_uuid)
    assert body["tracks"] == [
        "accepted_live", "accepted_paper", "rejected_counterfactual",
        "analyst_alternative", "user_alternative",
    ]
    assert body["horizons"] == ["1h", "4h", "1d", "3d", "7d", "final"]
    assert len(body["cells"]) == 1
    assert body["cells"][0]["mean_pnl_pct"] == "1.5"


@pytest.mark.unit
def test_get_session_analytics_returns_404_for_unknown_session():
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service

    app = FastAPI()
    app.include_router(trading_decisions.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=7)
    trading_decision_service.aggregate_session_outcomes = AsyncMock(return_value=None)

    client = TestClient(app)
    res = client.get(f"/trading/api/decisions/{uuid4()}/analytics")
    assert res.status_code == 404
```

- [ ] **Step 2: Run red**

```
uv run pytest tests/test_trading_decisions_router.py::test_get_session_analytics_happy_path tests/test_trading_decisions_router.py::test_get_session_analytics_returns_404_for_unknown_session -v
```

- [ ] **Step 3: Implement the route**

Append the new route to `app/routers/trading_decisions.py` (anywhere after line 487 — keep it grouped with other GET endpoints). Update the imports near line 14-30 to add `SessionAnalyticsResponse` and `SessionAnalyticsCell`.

```python
@router.get(
    "/api/decisions/{session_uuid}/analytics",
    response_model=SessionAnalyticsResponse,
)
async def get_session_analytics(
    session_uuid: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> SessionAnalyticsResponse:
    cells = await trading_decision_service.aggregate_session_outcomes(
        db, session_uuid=session_uuid, user_id=current_user.id
    )
    if cells is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    return SessionAnalyticsResponse(
        session_uuid=session_uuid,
        generated_at=datetime.now(UTC),
        tracks=[
            "accepted_live", "accepted_paper", "rejected_counterfactual",
            "analyst_alternative", "user_alternative",
        ],
        horizons=["1h", "4h", "1d", "3d", "7d", "final"],
        cells=[
            SessionAnalyticsCell(
                track_kind=c.track_kind,
                horizon=c.horizon,
                outcome_count=c.outcome_count,
                proposal_count=c.proposal_count,
                mean_pnl_pct=c.mean_pnl_pct,
                sum_pnl_amount=c.sum_pnl_amount,
                latest_marked_at=c.latest_marked_at,
            )
            for c in cells
        ],
    )
```

- [ ] **Step 4: Run green and confirm safety test still passes**

```
uv run pytest tests/test_trading_decisions_router.py -v
uv run pytest tests/test_trading_decisions_router_safety.py -v
```
Expected: all PASS. The safety test must still detect zero forbidden imports.

- [ ] **Step 5: Commit**

```
git add app/routers/trading_decisions.py tests/test_trading_decisions_router.py
git commit -m "feat(rob-12): GET session analytics endpoint"
```

---

### Task F1: Frontend types and API client

**Files:**
- Modify: `frontend/trading-decision/src/api/types.ts`
- Modify: `frontend/trading-decision/src/api/decisions.ts`
- Modify: `frontend/trading-decision/src/__tests__/api.decisions.test.ts`

- [ ] **Step 1: Write failing tests**

Append to `frontend/trading-decision/src/__tests__/api.decisions.test.ts` (study the existing tests to match style; use `mockFetch` from `../test/server`):

```ts
import { describe, it, expect } from "vitest";
import { mockFetch } from "../test/server";
import { createOutcomeMark, getSessionAnalytics } from "../api/decisions";

describe("getSessionAnalytics", () => {
  it("calls GET /trading/api/decisions/:uuid/analytics", async () => {
    const { calls } = mockFetch({
      "/trading/api/decisions/sess-1/analytics": () =>
        new Response(
          JSON.stringify({
            session_uuid: "sess-1",
            generated_at: "2026-04-28T06:00:00Z",
            tracks: [
              "accepted_live", "accepted_paper", "rejected_counterfactual",
              "analyst_alternative", "user_alternative",
            ],
            horizons: ["1h", "4h", "1d", "3d", "7d", "final"],
            cells: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    const res = await getSessionAnalytics("sess-1");
    expect(res.session_uuid).toBe("sess-1");
    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe("GET");
  });
});

describe("createOutcomeMark", () => {
  it("POSTs to /trading/api/proposals/:uuid/outcomes with the body", async () => {
    const { calls } = mockFetch({
      "/trading/api/proposals/p-1/outcomes": () =>
        new Response(
          JSON.stringify({
            id: 1,
            counterfactual_id: null,
            track_kind: "accepted_live",
            horizon: "1h",
            price_at_mark: "100",
            pnl_pct: null,
            pnl_amount: null,
            marked_at: "2026-04-28T07:00:00Z",
            payload: null,
            created_at: "2026-04-28T07:00:00Z",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        ),
    });
    const out = await createOutcomeMark("p-1", {
      track_kind: "accepted_live",
      horizon: "1h",
      price_at_mark: "100",
      marked_at: "2026-04-28T07:00:00Z",
    });
    expect(out.track_kind).toBe("accepted_live");
    expect(calls[0].method).toBe("POST");
    expect(calls[0].body).toContain('"track_kind":"accepted_live"');
  });
});
```

- [ ] **Step 2: Run red**

```
cd frontend/trading-decision && npm test -- --run api.decisions
```

- [ ] **Step 3: Add types** — append to `frontend/trading-decision/src/api/types.ts`:

```ts
export interface OutcomeCreateRequest {
  track_kind: TrackKind;
  horizon: OutcomeHorizon;
  price_at_mark: DecimalString;
  counterfactual_id?: number | null;
  pnl_pct?: DecimalString | null;
  pnl_amount?: DecimalString | null;
  marked_at: IsoDateTime;
  payload?: Record<string, unknown> | null;
}

export interface SessionAnalyticsCell {
  track_kind: TrackKind;
  horizon: OutcomeHorizon;
  outcome_count: number;
  proposal_count: number;
  mean_pnl_pct: DecimalString | null;
  sum_pnl_amount: DecimalString | null;
  latest_marked_at: IsoDateTime | null;
}

export interface SessionAnalyticsResponse {
  session_uuid: Uuid;
  generated_at: IsoDateTime;
  tracks: TrackKind[];
  horizons: OutcomeHorizon[];
  cells: SessionAnalyticsCell[];
}
```

- [ ] **Step 4: Add API functions** — append to `frontend/trading-decision/src/api/decisions.ts`:

```ts
import type {
  OutcomeCreateRequest,
  OutcomeDetail,
  SessionAnalyticsResponse,
} from "./types";

export async function getSessionAnalytics(
  sessionUuid: string,
): Promise<SessionAnalyticsResponse> {
  return apiFetch<SessionAnalyticsResponse>(
    `/decisions/${encodeURIComponent(sessionUuid)}/analytics`,
  );
}

export async function createOutcomeMark(
  proposalUuid: string,
  body: OutcomeCreateRequest,
): Promise<OutcomeDetail> {
  return apiFetch<OutcomeDetail>(
    `/proposals/${encodeURIComponent(proposalUuid)}/outcomes`,
    { method: "POST", body: JSON.stringify(body) },
  );
}
```

(Update the `import type { … }` block at the top of the file to include `OutcomeCreateRequest`, `OutcomeDetail`, and `SessionAnalyticsResponse`.)

- [ ] **Step 5: Run green**

```
cd frontend/trading-decision && npm test -- --run api.decisions && npm run typecheck
```

- [ ] **Step 6: Commit**

```
git add frontend/trading-decision/src/api/types.ts frontend/trading-decision/src/api/decisions.ts frontend/trading-decision/src/__tests__/api.decisions.test.ts
git commit -m "feat(rob-12): analytics + outcome create api client"
```

---

### Task F2: Test fixtures for outcomes and analytics

**Files:**
- Modify: `frontend/trading-decision/src/test/fixtures.ts`

- [ ] **Step 1: Append to fixtures file** (before the trailing newline):

```ts
import type {
  OutcomeDetail,
  SessionAnalyticsCell,
  SessionAnalyticsResponse,
} from "../api/types";

export function makeOutcome(
  overrides: Partial<OutcomeDetail> = {},
): OutcomeDetail {
  return {
    id: 100,
    counterfactual_id: null,
    track_kind: "accepted_live",
    horizon: "1h",
    price_at_mark: "118000000",
    pnl_pct: "1.2500",
    pnl_amount: "1500.0000",
    marked_at: now,
    payload: null,
    created_at: now,
    ...overrides,
  };
}

export function makeAnalyticsCell(
  overrides: Partial<SessionAnalyticsCell> = {},
): SessionAnalyticsCell {
  return {
    track_kind: "accepted_live",
    horizon: "1h",
    outcome_count: 2,
    proposal_count: 1,
    mean_pnl_pct: "1.2500",
    sum_pnl_amount: "3000.0000",
    latest_marked_at: now,
    ...overrides,
  };
}

export function makeAnalyticsResponse(
  overrides: Partial<SessionAnalyticsResponse> = {},
): SessionAnalyticsResponse {
  return {
    session_uuid: "session-1",
    generated_at: now,
    tracks: [
      "accepted_live", "accepted_paper", "rejected_counterfactual",
      "analyst_alternative", "user_alternative",
    ],
    horizons: ["1h", "4h", "1d", "3d", "7d", "final"],
    cells: [makeAnalyticsCell()],
    ...overrides,
  };
}
```

- [ ] **Step 2: Typecheck**

```
cd frontend/trading-decision && npm run typecheck
```

- [ ] **Step 3: Commit**

```
git add frontend/trading-decision/src/test/fixtures.ts
git commit -m "test(rob-12): outcome and analytics fixtures"
```

---

### Task F3: `OutcomesPanel` component

**Files:**
- Create: `frontend/trading-decision/src/components/OutcomesPanel.tsx`
- Create: `frontend/trading-decision/src/components/OutcomesPanel.module.css`
- Create: `frontend/trading-decision/src/__tests__/OutcomesPanel.test.tsx`

The panel takes `OutcomeDetail[]` and renders a small grouped table: rows are `track_kind`, columns are the 6 horizons, cells contain pnl_pct (and tooltip with pnl_amount + price_at_mark + marked_at).

- [ ] **Step 1: Write failing test**

```tsx
// OutcomesPanel.test.tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import OutcomesPanel from "../components/OutcomesPanel";
import { makeOutcome } from "../test/fixtures";

describe("OutcomesPanel", () => {
  it("shows an empty state when no outcomes are recorded", () => {
    render(<OutcomesPanel outcomes={[]} />);
    expect(screen.getByText(/no outcome marks/i)).toBeInTheDocument();
  });

  it("renders pnl_pct in the cell for the matching track and horizon", () => {
    const outcomes = [
      makeOutcome({
        track_kind: "accepted_live",
        horizon: "1h",
        pnl_pct: "2.5000",
      }),
      makeOutcome({
        id: 101,
        track_kind: "rejected_counterfactual",
        counterfactual_id: 11,
        horizon: "1d",
        pnl_pct: "-0.7500",
      }),
    ];
    render(<OutcomesPanel outcomes={outcomes} />);
    // table semantics
    expect(screen.getByRole("table", { name: /outcome marks/i })).toBeInTheDocument();
    // cell content
    expect(screen.getByText("2.5%")).toBeInTheDocument();
    expect(screen.getByText("-0.75%")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run red**

```
cd frontend/trading-decision && npm test -- --run OutcomesPanel
```

- [ ] **Step 3: Implement component**

```tsx
// frontend/trading-decision/src/components/OutcomesPanel.tsx
import type { OutcomeDetail, OutcomeHorizon, TrackKind } from "../api/types";
import { formatDateTime } from "../format/datetime";
import { formatDecimal } from "../format/decimal";
import styles from "./OutcomesPanel.module.css";

const TRACKS: TrackKind[] = [
  "accepted_live",
  "accepted_paper",
  "rejected_counterfactual",
  "analyst_alternative",
  "user_alternative",
];
const HORIZONS: OutcomeHorizon[] = ["1h", "4h", "1d", "3d", "7d", "final"];

interface OutcomesPanelProps {
  outcomes: OutcomeDetail[];
}

export default function OutcomesPanel({ outcomes }: OutcomesPanelProps) {
  if (outcomes.length === 0) {
    return <p className={styles.empty}>No outcome marks yet.</p>;
  }

  // group by (track_kind, horizon) → first matching outcome
  const cell = (track: TrackKind, horizon: OutcomeHorizon) =>
    outcomes.find((o) => o.track_kind === track && o.horizon === horizon);

  return (
    <table className={styles.table} aria-label="Outcome marks">
      <thead>
        <tr>
          <th scope="col">Track</th>
          {HORIZONS.map((h) => (
            <th key={h} scope="col">{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {TRACKS.map((track) => (
          <tr key={track}>
            <th scope="row" className={styles.trackCell}>{track}</th>
            {HORIZONS.map((h) => {
              const o = cell(track, h);
              if (!o) {
                return <td key={h} className={styles.empty}>—</td>;
              }
              return (
                <td key={h} className={styles.cell}>
                  <span title={tooltip(o)}>{formatPct(o.pnl_pct)}</span>
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function formatPct(pct: string | null | undefined): string {
  if (pct === null || pct === undefined) return "—";
  const n = Number(pct);
  if (!Number.isFinite(n)) return pct;
  return `${formatDecimal(pct, "en-US", { maximumFractionDigits: 2 })}%`;
}

function tooltip(o: OutcomeDetail): string {
  return [
    `price_at_mark: ${formatDecimal(o.price_at_mark)}`,
    o.pnl_amount ? `pnl_amount: ${formatDecimal(o.pnl_amount)}` : null,
    `marked_at: ${formatDateTime(o.marked_at)}`,
  ].filter(Boolean).join(" · ");
}
```

```css
/* frontend/trading-decision/src/components/OutcomesPanel.module.css */
.table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
.table th, .table td { border: 1px solid var(--border, #ddd); padding: 0.4rem 0.6rem; text-align: center; }
.trackCell { text-align: left; font-weight: 600; }
.cell { font-variant-numeric: tabular-nums; }
.empty { color: var(--muted, #888); }
```

- [ ] **Step 4: Run green**

```
cd frontend/trading-decision && npm test -- --run OutcomesPanel && npm run typecheck
```

- [ ] **Step 5: Commit**

```
git add frontend/trading-decision/src/components/OutcomesPanel.tsx frontend/trading-decision/src/components/OutcomesPanel.module.css frontend/trading-decision/src/__tests__/OutcomesPanel.test.tsx
git commit -m "feat(rob-12): outcome marks panel"
```

---

### Task F4: `OutcomeMarkForm` component

**Files:**
- Create: `frontend/trading-decision/src/components/OutcomeMarkForm.tsx`
- Create: `frontend/trading-decision/src/components/OutcomeMarkForm.module.css`
- Create: `frontend/trading-decision/src/__tests__/OutcomeMarkForm.test.tsx`

Form fields: `track_kind` (select with all 5 tracks), `horizon` (select with 6 horizons), `price_at_mark` (text — kept as string), optional `pnl_pct`, optional `pnl_amount`, optional `marked_at` (default = now in ISO), and conditionally `counterfactual_id` (select populated from `proposal.counterfactuals` when track ≠ `accepted_live`).

Validation rules (client-side, mirror server invariants):
- `accepted_live` ⇒ `counterfactual_id` MUST be empty.
- Any other track ⇒ `counterfactual_id` MUST be selected; reject submit with inline error.
- `price_at_mark` required and must parse to a finite number ≥ 0.

- [ ] **Step 1: Write failing test**

```tsx
// OutcomeMarkForm.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import OutcomeMarkForm from "../components/OutcomeMarkForm";
import { makeCounterfactual } from "../test/fixtures";

describe("OutcomeMarkForm", () => {
  it("submits an accepted_live mark with no counterfactual_id", async () => {
    const onSubmit = vi.fn().mockResolvedValue({ ok: true });
    render(<OutcomeMarkForm counterfactuals={[]} onSubmit={onSubmit} />);

    await userEvent.selectOptions(screen.getByLabelText(/track/i), "accepted_live");
    await userEvent.selectOptions(screen.getByLabelText(/horizon/i), "1h");
    await userEvent.type(screen.getByLabelText(/price at mark/i), "100");
    await userEvent.click(screen.getByRole("button", { name: /record mark/i }));

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        track_kind: "accepted_live",
        horizon: "1h",
        price_at_mark: "100",
      }),
    );
    expect(onSubmit.mock.calls[0][0].counterfactual_id).toBeUndefined();
  });

  it("blocks submit when non-accepted-live track has no counterfactual_id selected", async () => {
    const onSubmit = vi.fn();
    render(<OutcomeMarkForm counterfactuals={[]} onSubmit={onSubmit} />);

    await userEvent.selectOptions(screen.getByLabelText(/track/i), "rejected_counterfactual");
    await userEvent.selectOptions(screen.getByLabelText(/horizon/i), "1h");
    await userEvent.type(screen.getByLabelText(/price at mark/i), "100");
    await userEvent.click(screen.getByRole("button", { name: /record mark/i }));

    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText(/counterfactual is required/i)).toBeInTheDocument();
  });

  it("offers counterfactual options when one is provided", async () => {
    const cf = makeCounterfactual({ id: 11, track_kind: "rejected_counterfactual" });
    render(<OutcomeMarkForm counterfactuals={[cf]} onSubmit={vi.fn()} />);
    await userEvent.selectOptions(screen.getByLabelText(/track/i), "rejected_counterfactual");
    expect(screen.getByLabelText(/counterfactual/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run red**

```
cd frontend/trading-decision && npm test -- --run OutcomeMarkForm
```

- [ ] **Step 3: Implement component**

```tsx
// frontend/trading-decision/src/components/OutcomeMarkForm.tsx
import { FormEvent, useState } from "react";
import type {
  CounterfactualDetail,
  OutcomeCreateRequest,
  OutcomeHorizon,
  TrackKind,
} from "../api/types";
import styles from "./OutcomeMarkForm.module.css";

const TRACKS: TrackKind[] = [
  "accepted_live",
  "accepted_paper",
  "rejected_counterfactual",
  "analyst_alternative",
  "user_alternative",
];
const HORIZONS: OutcomeHorizon[] = ["1h", "4h", "1d", "3d", "7d", "final"];

interface OutcomeMarkFormProps {
  counterfactuals: CounterfactualDetail[];
  onSubmit: (
    body: OutcomeCreateRequest,
  ) => Promise<{ ok: boolean; detail?: string }>;
}

export default function OutcomeMarkForm({
  counterfactuals,
  onSubmit,
}: OutcomeMarkFormProps) {
  const [trackKind, setTrackKind] = useState<TrackKind>("accepted_live");
  const [horizon, setHorizon] = useState<OutcomeHorizon>("1h");
  const [price, setPrice] = useState("");
  const [pnlPct, setPnlPct] = useState("");
  const [pnlAmount, setPnlAmount] = useState("");
  const [counterfactualId, setCounterfactualId] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);

    if (!price || !Number.isFinite(Number(price)) || Number(price) < 0) {
      setError("price_at_mark must be a non-negative number");
      return;
    }
    if (trackKind === "accepted_live" && counterfactualId) {
      setError("accepted_live must not have a counterfactual selected");
      return;
    }
    if (trackKind !== "accepted_live" && !counterfactualId) {
      setError("counterfactual is required for this track");
      return;
    }

    const body: OutcomeCreateRequest = {
      track_kind: trackKind,
      horizon,
      price_at_mark: price,
      marked_at: new Date().toISOString(),
    };
    if (counterfactualId) body.counterfactual_id = Number(counterfactualId);
    if (pnlPct) body.pnl_pct = pnlPct;
    if (pnlAmount) body.pnl_amount = pnlAmount;

    setSubmitting(true);
    const res = await onSubmit(body);
    setSubmitting(false);
    if (!res.ok) {
      setError(res.detail ?? "Could not record outcome mark.");
      return;
    }
    // Reset numeric inputs but keep track/horizon for fast multi-mark entry.
    setPrice("");
    setPnlPct("");
    setPnlAmount("");
  }

  return (
    <form className={styles.form} onSubmit={handleSubmit} aria-label="Record outcome mark">
      <label>
        Track
        <select value={trackKind} onChange={(e) => {
          const v = e.target.value as TrackKind;
          setTrackKind(v);
          if (v === "accepted_live") setCounterfactualId("");
        }}>
          {TRACKS.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      </label>

      <label>
        Horizon
        <select value={horizon} onChange={(e) => setHorizon(e.target.value as OutcomeHorizon)}>
          {HORIZONS.map((h) => <option key={h} value={h}>{h}</option>)}
        </select>
      </label>

      {trackKind !== "accepted_live" ? (
        <label>
          Counterfactual
          <select value={counterfactualId} onChange={(e) => setCounterfactualId(e.target.value)}>
            <option value="">— select —</option>
            {counterfactuals
              .filter((c) => c.track_kind === trackKind)
              .map((c) => (
                <option key={c.id} value={c.id}>
                  #{c.id} · baseline {c.baseline_price}
                </option>
              ))}
          </select>
        </label>
      ) : null}

      <label>
        Price at mark
        <input value={price} onChange={(e) => setPrice(e.target.value)} placeholder="e.g. 118000000" />
      </label>

      <label>
        PnL %
        <input value={pnlPct} onChange={(e) => setPnlPct(e.target.value)} placeholder="optional" />
      </label>

      <label>
        PnL amount
        <input value={pnlAmount} onChange={(e) => setPnlAmount(e.target.value)} placeholder="optional" />
      </label>

      {error ? <p role="alert" className={styles.error}>{error}</p> : null}

      <button type="submit" disabled={submitting}>
        {submitting ? "Saving…" : "Record mark"}
      </button>
    </form>
  );
}
```

```css
/* OutcomeMarkForm.module.css */
.form { display: grid; grid-template-columns: repeat(2, minmax(140px, 1fr)); gap: 0.5rem 1rem; }
.form label { display: flex; flex-direction: column; gap: 0.25rem; font-size: 0.85rem; }
.form button { grid-column: 1 / -1; padding: 0.5rem; }
.error { grid-column: 1 / -1; color: var(--danger, #b00020); margin: 0; }
```

- [ ] **Step 4: Run green**

```
cd frontend/trading-decision && npm test -- --run OutcomeMarkForm && npm run typecheck
```

- [ ] **Step 5: Commit**

```
git add frontend/trading-decision/src/components/OutcomeMarkForm.tsx frontend/trading-decision/src/components/OutcomeMarkForm.module.css frontend/trading-decision/src/__tests__/OutcomeMarkForm.test.tsx
git commit -m "feat(rob-12): outcome mark creation form"
```

---

### Task F5: Wire OutcomesPanel + OutcomeMarkForm into ProposalRow

**Files:**
- Modify: `frontend/trading-decision/src/components/ProposalRow.tsx`
- Modify: `frontend/trading-decision/src/__tests__/ProposalRow.test.tsx`
- Modify: `frontend/trading-decision/src/hooks/useDecisionSession.ts` (extend with `recordOutcome`)

The `useDecisionSession` hook already exposes `respond` and `refetch`. Add a sibling `recordOutcome(proposalUuid, body)` that calls `createOutcomeMark` and then triggers `refetch()` on success. Pattern matches `respond` exactly.

`SessionDetailPage` already passes the session via `<ProposalRow onRespond={…} proposal={p} />`. Extend the prop interface to also accept `onRecordOutcome` and render `<OutcomesPanel>` + a collapsible `<details>` containing `<OutcomeMarkForm>` below the existing controls.

- [ ] **Step 1: Extend the hook test**

Add a unit test (or extend existing) verifying `recordOutcome` calls `createOutcomeMark` and refetches on success. Keep style consistent with how `respond` is tested in `useDecisionSession` if a test exists; otherwise add a minimal one.

- [ ] **Step 2: Extend the hook**

In `frontend/trading-decision/src/hooks/useDecisionSession.ts`:

```ts
import { createOutcomeMark, getSession, respondToProposal } from "../api/decisions";
import type { OutcomeCreateRequest } from "../api/types";
// inside the returned object, alongside `respond`:
async function recordOutcome(proposalUuid: string, body: OutcomeCreateRequest) {
  try {
    await createOutcomeMark(proposalUuid, body);
    refetch();
    return { ok: true };
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      redirectToLogin();
      return { ok: false, status: 401, detail: error.detail };
    }
    if (error instanceof ApiError) {
      return { ok: false, status: error.status, detail: error.detail };
    }
    return { ok: false, detail: "Something went wrong. Try again." };
  }
}

return { ...state, refetch, respond, recordOutcome };
```

Update the hook’s return-type interface (`respond` already there — add `recordOutcome` mirror).

- [ ] **Step 3: Wire into `ProposalRow.tsx`**

Add an `onRecordOutcome` prop. Render `<OutcomesPanel outcomes={proposal.outcomes} />` and a `<details><summary>Record outcome mark</summary><OutcomeMarkForm counterfactuals={proposal.counterfactuals} onSubmit={(body) => onRecordOutcome(proposal.proposal_uuid, body)} /></details>` block below the existing response controls.

- [ ] **Step 4: Wire into `SessionDetailPage.tsx`**

Pass `onRecordOutcome={session.recordOutcome}` alongside the existing `onRespond={session.respond}` prop.

- [ ] **Step 5: Update `ProposalRow.test.tsx`**

Add at least one test asserting that the panel renders the `outcomes` prop, and that submitting the form calls the `onRecordOutcome` prop with the expected body.

- [ ] **Step 6: Run all frontend tests**

```
cd frontend/trading-decision && npm test && npm run typecheck
```

- [ ] **Step 7: Commit**

```
git add frontend/trading-decision/src/hooks/useDecisionSession.ts \
        frontend/trading-decision/src/components/ProposalRow.tsx \
        frontend/trading-decision/src/__tests__/ProposalRow.test.tsx \
        frontend/trading-decision/src/pages/SessionDetailPage.tsx
git commit -m "feat(rob-12): render outcome marks and form per proposal"
```

---

### Task F6: `useSessionAnalytics` hook

**Files:**
- Create: `frontend/trading-decision/src/hooks/useSessionAnalytics.ts`

- [ ] **Step 1: Implement** (mirrors `useDecisionSession` but read-only):

```ts
import { useEffect, useState } from "react";
import { ApiError } from "../api/client";
import { getSessionAnalytics } from "../api/decisions";
import type { SessionAnalyticsResponse } from "../api/types";

interface AnalyticsState {
  status: "idle" | "loading" | "success" | "error" | "not_found";
  data: SessionAnalyticsResponse | null;
  error: string | null;
}

export function useSessionAnalytics(sessionUuid: string): AnalyticsState {
  const [state, setState] = useState<AnalyticsState>({
    status: "idle", data: null, error: null,
  });
  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading", data: null, error: null });
    getSessionAnalytics(sessionUuid)
      .then((data) => {
        if (!controller.signal.aborted) {
          setState({ status: "success", data, error: null });
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) {
          setState({ status: "not_found", data: null, error: error.detail });
          return;
        }
        setState({
          status: "error",
          data: null,
          error: error instanceof ApiError ? error.detail : "Could not load analytics.",
        });
      });
    return () => controller.abort();
  }, [sessionUuid]);
  return state;
}
```

- [ ] **Step 2: Typecheck**

```
cd frontend/trading-decision && npm run typecheck
```

- [ ] **Step 3: Commit**

```
git add frontend/trading-decision/src/hooks/useSessionAnalytics.ts
git commit -m "feat(rob-12): useSessionAnalytics hook"
```

---

### Task F7: `AnalyticsMatrix` component

**Files:**
- Create: `frontend/trading-decision/src/components/AnalyticsMatrix.tsx`
- Create: `frontend/trading-decision/src/components/AnalyticsMatrix.module.css`
- Create: `frontend/trading-decision/src/__tests__/AnalyticsMatrix.test.tsx`

Same row/column shape as `OutcomesPanel`: tracks × horizons. Each cell shows `mean_pnl_pct` (formatted as %) plus a small subtitle `n=<outcome_count>` (or "—" when no cell exists for that intersection).

- [ ] **Step 1: Write failing test**

```tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import AnalyticsMatrix from "../components/AnalyticsMatrix";
import { makeAnalyticsResponse, makeAnalyticsCell } from "../test/fixtures";

describe("AnalyticsMatrix", () => {
  it("shows an empty state when no cells exist", () => {
    render(
      <AnalyticsMatrix data={makeAnalyticsResponse({ cells: [] })} />,
    );
    expect(screen.getByText(/no outcomes yet/i)).toBeInTheDocument();
  });

  it("renders one cell for each (track, horizon) row from the response", () => {
    const data = makeAnalyticsResponse({
      cells: [
        makeAnalyticsCell({ track_kind: "accepted_live", horizon: "1h",
                            mean_pnl_pct: "1.5", outcome_count: 3 }),
        makeAnalyticsCell({ track_kind: "rejected_counterfactual", horizon: "1d",
                            mean_pnl_pct: "-0.5", outcome_count: 1 }),
      ],
    });
    render(<AnalyticsMatrix data={data} />);
    expect(screen.getByRole("table", { name: /analytics/i })).toBeInTheDocument();
    expect(screen.getByText("1.5%")).toBeInTheDocument();
    expect(screen.getByText("-0.5%")).toBeInTheDocument();
    expect(screen.getAllByText(/n=/i).length).toBeGreaterThanOrEqual(2);
  });
});
```

- [ ] **Step 2: Run red**

```
cd frontend/trading-decision && npm test -- --run AnalyticsMatrix
```

- [ ] **Step 3: Implement**

```tsx
// AnalyticsMatrix.tsx
import type {
  OutcomeHorizon,
  SessionAnalyticsResponse,
  TrackKind,
} from "../api/types";
import { formatDecimal } from "../format/decimal";
import styles from "./AnalyticsMatrix.module.css";

interface AnalyticsMatrixProps {
  data: SessionAnalyticsResponse;
}

export default function AnalyticsMatrix({ data }: AnalyticsMatrixProps) {
  if (data.cells.length === 0) {
    return <p className={styles.empty}>No outcomes yet for this session.</p>;
  }

  const lookup = new Map<string, (typeof data.cells)[number]>();
  for (const c of data.cells) lookup.set(`${c.track_kind}|${c.horizon}`, c);
  const cell = (track: TrackKind, h: OutcomeHorizon) =>
    lookup.get(`${track}|${h}`);

  return (
    <table className={styles.table} aria-label="Outcome analytics">
      <thead>
        <tr>
          <th scope="col">Track</th>
          {data.horizons.map((h) => (
            <th key={h} scope="col">{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {data.tracks.map((track) => (
          <tr key={track}>
            <th scope="row" className={styles.trackCell}>{track}</th>
            {data.horizons.map((h) => {
              const c = cell(track, h);
              if (!c) return <td key={h} className={styles.empty}>—</td>;
              return (
                <td key={h} className={styles.cell}>
                  <strong>{formatPct(c.mean_pnl_pct)}</strong>
                  <span className={styles.meta}>n={c.outcome_count}</span>
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function formatPct(p: string | null): string {
  if (p === null) return "—";
  return `${formatDecimal(p, "en-US", { maximumFractionDigits: 2 })}%`;
}
```

```css
.table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
.table th, .table td { border: 1px solid var(--border, #ddd); padding: 0.5rem 0.6rem; text-align: center; }
.trackCell { text-align: left; font-weight: 600; }
.cell { font-variant-numeric: tabular-nums; display: flex; flex-direction: column; gap: 0.15rem; }
.meta { color: var(--muted, #888); font-size: 0.75rem; }
.empty { color: var(--muted, #888); }
```

- [ ] **Step 4: Run green**

```
cd frontend/trading-decision && npm test -- --run AnalyticsMatrix && npm run typecheck
```

- [ ] **Step 5: Commit**

```
git add frontend/trading-decision/src/components/AnalyticsMatrix.tsx frontend/trading-decision/src/components/AnalyticsMatrix.module.css frontend/trading-decision/src/__tests__/AnalyticsMatrix.test.tsx
git commit -m "feat(rob-12): analytics matrix component"
```

---

### Task F8: Mount analytics on `SessionDetailPage`

**Files:**
- Modify: `frontend/trading-decision/src/pages/SessionDetailPage.tsx`
- Modify: `frontend/trading-decision/src/__tests__/SessionDetailPage.test.tsx`

- [ ] **Step 1: Add an `<AnalyticsMatrix>` block** below the `MarketBriefPanel`, gated on the analytics hook’s status (`loading` → spinner; `not_found` → silent skip; `error` → small error notice; `success` → `<AnalyticsMatrix data=…/>`).

```tsx
import { useSessionAnalytics } from "../hooks/useSessionAnalytics";
import AnalyticsMatrix from "../components/AnalyticsMatrix";
// inside SessionDetailPage, after `const data = session.data;`:
const analytics = useSessionAnalytics(sessionUuid);
// jsx:
{analytics.status === "success" && analytics.data ? (
  <section aria-label="Analytics">
    <h2>Outcome analytics</h2>
    <AnalyticsMatrix data={analytics.data} />
  </section>
) : null}
```

- [ ] **Step 2: Update the page test**

In `SessionDetailPage.test.tsx`, register an additional mock route for the analytics endpoint (`/trading/api/decisions/<uuid>/analytics`) and assert the matrix renders.

- [ ] **Step 3: Run all tests**

```
cd frontend/trading-decision && npm test && npm run typecheck && npm run build
```

The `npm run build` step is critical because ROB-11 wired this dist into the deploy.

- [ ] **Step 4: Commit**

```
git add frontend/trading-decision/src/pages/SessionDetailPage.tsx frontend/trading-decision/src/__tests__/SessionDetailPage.test.tsx
git commit -m "feat(rob-12): mount analytics matrix on session detail page"
```

---

## 6. Validation Commands (run before opening PR)

Run from repo root unless otherwise noted.

| Concern | Command | Expected |
|---|---|---|
| Backend lint + format | `make lint && make format` | passes |
| Trading decision router tests | `uv run pytest tests/test_trading_decisions_router.py -v` | all PASS, including the two new analytics tests |
| Trading decision service tests | `uv run pytest tests/models/test_trading_decision_service.py -v` | all PASS, including the two new aggregation tests |
| Router safety (no broker imports) | `uv run pytest tests/test_trading_decisions_router_safety.py -v` | PASS — must remain green; if a new import sneaks in, fix it |
| SPA shell tests | `uv run pytest tests/test_trading_decisions_spa_router.py tests/test_trading_decisions_spa_router_safety.py -v` | PASS |
| Frontend typecheck | `cd frontend/trading-decision && npm run typecheck` | no errors |
| Frontend tests | `cd frontend/trading-decision && npm test` | all PASS |
| Frontend build (dist must exist for ROB-11 deploy) | `cd frontend/trading-decision && npm run build` | builds to `dist/`, no warnings about missing chunks |
| Quick manual smoke (optional) | `make dev`, then visit `http://localhost:8000/trading/decisions/sessions/<uuid>` with logged-in cookie | analytics matrix renders; record a mark; matrix updates after refetch |

---

## 7. Risks / Gotchas

1. **Decimal serialization across the wire.** Pydantic v2 emits `Decimal` as JSON strings only when explicitly configured. Existing code in this repo treats decimal-like fields as strings on the SPA (`DecimalString`). Confirm the analytics response also yields strings — if numerical floats appear in the response, add `model_config = ConfigDict(json_encoders={Decimal: str})` to the response schemas, or convert in the route before returning. The serialization test (Task B2 step 1) will catch a regression.
2. **`ROUND_HALF_EVEN` from `func.avg`.** PostgreSQL `AVG(numeric)` returns `numeric` already; SQLAlchemy maps to `Decimal`. Should be correct, but the test in Task B1 asserts the rounded value to confirm.
3. **`postgresql_nulls_not_distinct` (PG ≥ 15).** The unique index on outcomes already enforces this; do **not** rely on application-side de-dup for `accepted_live`.
4. **Form auto-fills `marked_at`.** Server validates `marked_at` as `datetime`. Sending `new Date().toISOString()` is fine; if the server uses a stricter format, swap to `…toISOString().replace("Z", "+00:00")`.
5. **Cross-user 404 vs 403.** Existing code returns 404 for ownership mismatch — match that exactly in the analytics route. The router test asserts 404.
6. **Safety test must stay green.** Do not import anything from `app.services.kis*`, `app.services.upbit*`, `app.services.brokers`, `app.services.order_service`, `app.services.execution_event`, `app.services.fill_notification`, or `app.tasks` from `app.routers.trading_decisions`.
7. **Outcome marks panel scaling.** A proposal can have up to ~30 outcomes (5 tracks × 6 horizons); rendering as a static table is fine. No virtualization needed.
8. **No new SPA dependencies.** `package.json` must remain unchanged. If you find yourself wanting Recharts or React Query — stop and re-read this plan.
9. **No new SPA route.** `routes.tsx` must remain unchanged. Analytics is inlined into `SessionDetailPage`.
10. **Auto mode caveat.** This implementation must not call any live broker/KIS/Upbit code, must not enable automatic live trading, must not introduce secret handling, and must not implement Hermes profile routing.

---

## 8. Spec Coverage Self-Review

| Roadmap Prompt 5 line | Covered by |
|---|---|
| “Outcome read/write API if not already complete.” | Outcome write at `POST /api/proposals/{uuid}/outcomes` already done (ROB-2). Outcome read already nested in `GET /api/decisions/{uuid}`. New: `GET /api/decisions/{uuid}/analytics` for aggregates. |
| “UI sections for outcome marks.” | Task F3 (`OutcomesPanel`) + Task F5 wiring + Task F4 (`OutcomeMarkForm`). |
| “Analytics comparing accepted_live / accepted_paper / rejected_counterfactual / analyst_alternative / user_alternative.” | Task B1–B3 (server aggregation across all 5 tracks) + Task F7 (`AnalyticsMatrix`) + Task F8 (mount). All five track values are echoed verbatim in the response and rendered as rows. |
| “Horizon views: 1h / 4h / 1d / 3d / 7d / final.” | Same — all six horizons are columns in both `OutcomesPanel` and `AnalyticsMatrix`, and the response schema declares them in order. |
| “Future note: profile routing not in this PR.” | Out-of-scope section explicitly defers it. |

No placeholder steps remain; every code-changing step contains the actual code.

---

## 9. Codex --yolo Implementer Handoff Prompt

> Paste this exactly into the next session that runs after the planner. The implementer has the same worktree (`/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-12-trading-decision-outcome-analytics-ui`) and the same branch (`feature/ROB-12-trading-decision-outcome-analytics-ui`).

```text
You are the Codex --yolo implementer for ROB-12 — Trading Decision outcome analytics UI.

Repo / worktree:  /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-12-trading-decision-outcome-analytics-ui
Branch:           feature/ROB-12-trading-decision-outcome-analytics-ui
Plan to follow:   docs/plans/ROB-12-trading-decision-outcome-analytics-ui-plan.md

Hard rules (do not violate):
1. Do NOT enable automatic live trading. Do NOT call live broker/KIS/Upbit order execution from UI or backend. Do NOT import any module under app.services.kis*, app.services.upbit*, app.services.brokers, app.services.order_service, app.services.fill_notification, app.services.execution_event, app.services.kis_websocket*, app.services.redis_token_manager, or app.tasks from app.routers.trading_decisions or any code it pulls in. tests/test_trading_decisions_router_safety.py enforces this.
2. Do NOT introduce new secret handling.
3. Do NOT implement Hermes profile routing in this PR.
4. Do NOT modify the DB schema or add migrations. Reuse the existing TradingDecisionOutcome model.
5. Do NOT add new SPA dependencies. The frontend/trading-decision/package.json must stay unchanged.
6. Do NOT add new SPA routes. Inline everything into SessionDetailPage.
7. Keep the PR additive. Reuse the existing POST /trading/api/proposals/{uuid}/outcomes endpoint and the OutcomeDetail/OutcomeCreateRequest schemas as-is.

Execution method:
- Work tasks in order: B1 → B2 → B3 → F1 → F2 → F3 → F4 → F5 → F6 → F7 → F8.
- Each task is TDD: red test → minimal implementation → green test → commit. Do not skip steps.
- Each step in the plan contains exact code; copy it verbatim and adjust only if a referenced symbol does not exist (e.g., the test harness import for AsyncTestSession must match what test_record_1h_and_1d_outcome_marks already uses around tests/models/test_trading_decision_service.py:440-495).
- Use the commit messages provided in the plan ("feat(rob-12): …"). Sign-off as Co-Authored-By: Paperclip <noreply@paperclip.ing> per CLAUDE.md.

After all tasks:
- Run the full validation matrix in section 6 of the plan. Every command must succeed.
- Push the branch and open a PR against main with title "feat(rob-12): trading decision outcome analytics UI" and a body that links to https://linear.app/mgh3326/issue/ROB-12.

If any step fails:
- Do not bypass with --no-verify. Diagnose and fix root cause.
- If a planned code snippet conflicts with the actual file (e.g., line numbers shifted), prefer the existing file’s structure and adapt the snippet — but keep the public contract (function signatures, endpoint path, response shape) exactly as the plan specifies.

Stop when the PR is opened. Do not start ROB-13 work.
```

---

## 10. Post-implementation checklist

- [ ] `uv run pytest tests/test_trading_decisions_router.py tests/test_trading_decisions_router_safety.py tests/test_trading_decisions_spa_router.py tests/test_trading_decisions_spa_router_safety.py tests/models/test_trading_decision_service.py -v` — all green.
- [ ] `cd frontend/trading-decision && npm run typecheck && npm test && npm run build` — all green; `dist/index.html` exists.
- [ ] `git diff main -- app/routers/trading_decisions.py` shows only the new analytics route + imports.
- [ ] No diff to any file under `app/services/kis*`, `app/services/upbit*`, `app/tasks/`, `alembic/versions/`, or `frontend/trading-decision/package.json`.
- [ ] PR description references Linear ROB-12 and lists the new endpoint, new components, and the unchanged write API.
